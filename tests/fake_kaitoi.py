"""A stand-in Kaitoi API for the tests: just enough of /api/v1 to exercise the client.

It keeps what it was sent (``server.seen``) so a test can assert on requests,
and it can be told how many status polls a run needs before it finishes.
"""

from __future__ import annotations

import json
import struct
import zlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PIN = {"name": "Prompt", "fieldName": "prompt", "dataType": "string", "kind": "primitive"}
VIDEO_IN = {"name": "Clip", "fieldName": "video", "dataType": "video", "kind": "file"}
VIDEO_OUT = {"name": "Result", "fieldName": "video", "dataType": "video", "kind": "file"}

NODE_DETAILS = {
    "builtin/video/video_reverse": {
        "type": "builtin/video/video_reverse", "title": "Video Reverse & Speed", "category": "Video Editing", "source": "builtin",
        "description": "Reverse a clip.", "tags": ["rewind"],
        "inputs": [
            {"name": "inputVideo", "type": "video", "label": "Input Video", "default": None, "requirement": "required"},
            {"name": "speedFactor", "type": "float", "label": "Speed Factor", "default": 1.0, "requirement": "defaulted",
             "options": None, "min": 0.1, "max": 10.0, "step": 0.1, "widget": "slider"},
            {"name": "keepAudio", "type": "bool", "label": "Keep Audio", "default": True, "requirement": "defaulted"},
            {"name": "model", "type": "node", "label": "Model", "default": None},
        ],
        "outputs": [{"name": "outputVideo", "type": "video", "label": "Output Video"}, {"name": "self", "type": "node", "label": "Node"}],
    },
    "builtin/loaders/load_video": {
        "type": "builtin/loaders/load_video", "title": "Load Video", "category": "Loaders", "source": "builtin",
        "inputs": [{"name": "inputVideo", "type": "video", "label": "Video", "default": None}],
        "outputs": [{"name": "outputVideo", "type": "video", "label": "Video"}, {"name": "self", "type": "node"}],
    },
    "community/acme/sparkle": {
        "type": "community/acme/sparkle", "title": "Sparkle", "category": "Image to Video", "source": "community",
        "description": "Adds sparkle.", "tags": ["glitter"],
        "inputs": [
            {"name": "startImage", "type": "image", "label": "Start Image", "default": None},
            {"name": "prompt", "type": "string", "label": "Prompt", "default": ""},
            {"name": "style", "type": "string", "label": "Style", "default": "soft",
             "options": [{"value": "soft", "label": "Soft glow"}, {"value": "hard", "label": "hard"}]},
            {"name": "fps", "type": "int", "label": "FPS", "default": 24, "options": [{"value": 24, "label": "24"}, {"value": 30, "label": "30"}]},
        ],
        "outputs": [{"name": "video", "type": "video", "label": "Video"}],
    },
}


def fake_jpeg(width: int, height: int) -> bytes:
    """Enough of a JPEG to say how big it is; nothing in the tests draws it."""
    return b"\xff\xd8\xff\xc0" + struct.pack(">HBHH", 11, 8, height, width) + b"\x01\x11\x00" + b"J" * 900


def fake_png(width: int, height: int, channels: int, pixel) -> bytes:
    """A real 8-bit PNG, written here and not by the plugin, with every kind
    of row the format has: ``pixel(x, y)`` gives a tuple of ``channels``."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    def nearest(left, above, corner):
        guess = left + above - corner
        return min((abs(guess - left), 0, left), (abs(guess - above), 1, above), (abs(guess - corner), 2, corner))[2]

    stride, raw, previous = width * channels, bytearray(), bytes(width * channels)
    for y in range(height):
        row = bytes(value for x in range(width) for value in pixel(x, y))
        kind = y % 5
        line = bytearray(stride)
        for at in range(stride):
            left = row[at - channels] if at >= channels else 0
            corner = previous[at - channels] if at >= channels else 0
            guess = (0, left, previous[at], (left + previous[at]) >> 1, nearest(left, previous[at], corner))[kind]
            line[at] = (row[at] - guess) & 255
        raw += bytes([kind]) + line
        previous = row
    colour = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    packed = zlib.compress(bytes(raw))
    half = len(packed) // 2  # two pieces, as a larger file has
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
            + chunk(b"tEXt", b"note\x00skipped") + chunk(b"IDAT", packed[:half]) + chunk(b"IDAT", packed[half:]) + chunk(b"IEND", b""))


class FakeKaitoi(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.seen: list[dict] = []
        self.polls_before_done = 1
        self.run_status = "succeeded"
        self.run_events: list | None = []  # what a run's events answer; None for a server that has no such route
        self.events_page = 200  # events to a page, whatever the caller asks for
        self.events_fault = ""  # "drop": the connection is closed with no answer; "junk": an answer that is not JSON
        self.events_key = ""  # stands for the key the server signs a place in the events with: one signed with another is refused
        self.stamps: dict[str, str] = {}  # endpoint id -> updatedAt, for an endpoint that was edited
        self.template_stamps: dict[str, str] = {}  # template id -> updatedAt, for a template that was saved again
        self.saved_style = "warm"  # the value the template holds for the Style input
        self.docs_fail: set[str] = set()  # endpoint ids whose contract cannot be read right now
        self.describes_inputs = True  # False: a server from before contracts carried choices and saved defaults
        self.renders_thumbnails = True  # False: a server that sends the stored thumbnail whatever is asked
        self.thumbnail_bytes = fake_jpeg(256, 144)  # the small JPEG a current server makes
        self.thumbnail_png = b""  # the same picture as a PNG, when a test needs one
        self.png_status = None  # a status to refuse every request for a PNG with
        self.thumbnails_too_many = 0  # how many thumbnail requests to answer "too many requests, come back in 7 s" first
        self.node_thumbnails = False  # True: every node in the list has a thumbnail, as most of Kaitoi's own do
        self.thumbnail_broken = False  # True: a reply that stops making sense part way
        self.stored_thumbnail = b"\x89PNG\r\n\x1a\n" + b"P" * 4000  # what the author uploaded
        self.initial_state = "ok"  # "ok", "fails" (the node cannot start) or "missing" (a server from before this existed)
        self.lists_pins = True  # False: a server from before the node list could carry inputs and outputs
        self.lists_kinds = True  # False: one from before it said which nodes are session and agent nodes
        self.rate_limited = 0  # how many node lookups to refuse with "too many requests" first
        self.inline_contracts = False  # template list carries inputs/outputs (the newer API)
        self.files: dict[str, bytes] = {"out1.mp4": b"RESULT-BYTES"}
        self.uploads: dict[str, bytes] = {}
        self._polls: dict[str, int] = {}
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> "FakeKaitoi":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def templates(self) -> list[dict]:
        def endpoint(endpoint_id: str, template_id: str, name: str) -> dict:
            stamp = self.stamps.get(endpoint_id, "2026-01-01T00:00:00Z")
            entry = {"endpointId": endpoint_id, "templateId": template_id, "name": name, "isActive": True, "updatedAt": stamp, "inputCount": 2, "outputCount": 1}
            if self.inline_contracts:
                entry.update(inputs=[VIDEO_IN, PIN], outputs=[VIDEO_OUT])
            return entry

        return [
            {"templateId": "t1", "name": "Rain", "description": "Make it rain.", "category": "Weather", "tags": ["storm"], "hasEndpoint": True,
             "updatedAt": self.template_stamps.get("t1", "2026-01-01T00:00:00Z"),
             "thumbnailUrl": f"{self.url}/api/v1/template-thumbnails/t1?token=signed", "endpoints": [endpoint("e1", "t1", "Rain API")]},
            {"templateId": "t2", "name": "Looks", "description": None, "category": None, "tags": [], "hasEndpoint": True, "endpoints": [endpoint("e2", "t2", "Warm"), endpoint("e3", "t2", "Cold")]},
        ]


class _Handler(BaseHTTPRequestHandler):
    server: FakeKaitoi

    def log_message(self, *args) -> None:  # keep test output clean
        pass

    def _send(self, status: int, payload, headers: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body: bytes) -> dict:
        try:
            parsed = json.loads(body.decode()) if body else None
        except ValueError:
            parsed = None
        entry = {"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"), "json": parsed, "raw": body}
        self.server.seen.append(entry)
        return entry

    def _run_state(self, run_id: str, kind: str) -> dict:
        count = self.server._polls.get(run_id, 0) + 1
        self.server._polls[run_id] = count
        done = count >= self.server.polls_before_done
        status = self.server.run_status if done else "running"
        outputs = {}
        if done and status == "succeeded":
            file_output = {"type": "file", "fileId": "out1.mp4", "filename": "out1.mp4", "contentType": "video/mp4", "downloadUrl": f"{self.server.url}/storage/out1.mp4"}
            outputs = {"video": file_output, "note": {"type": "string", "value": "ok"}} if kind == "endpoint" else {"outputVideo": file_output}
        error = {"message": "boom", "code": "EXECUTION_ERROR"} if done and status == "failed" else None
        key = "taskId" if kind == "endpoint" else "id"
        return {key: run_id, "status": status, "outputs": outputs, "error": error, "progress": 100 if done else 40}

    def do_GET(self) -> None:  # noqa: N802
        self._record(b"")
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        path = url.path
        if path == "/api/v1/templates":
            items = self.server.templates()
            cursor = int(query.get("cursor", ["0"])[0])
            limit = int(query.get("limit", ["1"])[0])
            page = items[cursor:cursor + limit]
            more = cursor + limit < len(items)
            return self._send(200, {"data": page, "hasMore": more, "nextCursor": str(cursor + limit) if more else None})
        if path.endswith("/docs") and "/endpoints/" in path and path.split("/")[-2] in self.server.docs_fail:
            return self._send(503, {"error": {"code": "UNAVAILABLE", "message": "try again"}})
        if path.endswith("/docs") and "/endpoints/" in path:
            inputs = [VIDEO_IN, PIN]
            if self.server.describes_inputs:
                style = {"name": "Style", "fieldName": "style", "dataType": "string", "kind": "primitive"}
                details = {"default": None, "options": None, "min": None, "max": None, "step": None, "widget": None}
                inputs = [{**VIDEO_IN, **details}, {**PIN, **details}, {**style, **details, "default": self.server.saved_style, "options": [{"value": "warm", "label": "Warm"}, {"value": "cold", "label": "Cold"}]}]
            return self._send(200, {"inputs": inputs, "outputs": [VIDEO_OUT]})
        if "-thumbnails/" in path:
            if self.headers.get("Authorization"):
                return self._send(400, {"error": {"code": "BEARER_SENT", "message": "the signed link is the permission"}})
            if self.server.thumbnail_broken:
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"zz\r\nnot a chunk\r\n")
                return None
            if self.server.thumbnails_too_many > 0:
                self.server.thumbnails_too_many -= 1
                return self._send(429, {"error": {"code": "RATE_LIMITED", "message": "Too many requests."}}, {"Retry-After": "7"})
            if self.server.png_status and query.get("format") == ["png"]:
                return self._send(self.server.png_status, {"error": {"code": "THUMBNAIL_NOT_RENDERABLE", "message": "no"}})
            if self.server.renders_thumbnails and query.get("format") == ["jpeg"] and query.get("size"):
                return self._send(200, self.server.thumbnail_bytes)
            if self.server.renders_thumbnails and query.get("format") == ["png"] and query.get("size"):
                return self._send(200, self.server.thumbnail_png)
            return self._send(200, self.server.stored_thumbnail)
        if "/endpoints/" in path and "/runs/" in path:
            return self._send(200, self._run_state(path.rsplit("/", 1)[1], "endpoint"))
        if path.startswith("/api/v1/node-type-initial-state/"):
            if self.server.initial_state == "missing":
                return self._send(404, {"error": {"code": "NOT_FOUND", "message": "no such route"}})
            if self.server.initial_state == "fails":
                return self._send(422, {"error": {"code": "NODE_TYPE_INIT_FAILED", "message": "The node could not start."}})
            from urllib.parse import unquote

            voices = [{"value": "v1", "label": "Aria"}, {"value": "v2", "label": "Blade"}]
            state = {"selectedVoice": {"options": voices, "value": "v1"}, "prompt": {"options": [{"value": "x", "label": "X"}], "value": "ignored"}}
            return self._send(200, {"type": unquote(path[len("/api/v1/node-type-initial-state/"):]), "inputs": state})
        if path == "/api/v1/node-types":
            asked = set(query.get("fields", [""])[0].split(","))
            wants_pins = bool(asked & {"inputs", "outputs"})
            if (wants_pins and not self.server.lists_pins) or (asked & {"session", "agent"} and not self.server.lists_kinds):
                error = {"code": "INVALID_FIELDS_PROJECTION", "message": "fields must be a non-empty comma-separated list of supported fields."}
                return self._send(400, {"error": error})
            items = [{k: v for k, v in node.items() if wants_pins or k not in ("inputs", "outputs")} for node in NODE_DETAILS.values()]
            if self.server.node_thumbnails:
                items = [{**node, "thumbnailUrl": f"{self.server.url}/api/v1/node-type-thumbnails/{node['type']}?token=signed"} for node in items]
            cursor = int(query.get("cursor", ["0"])[0])
            page = items[cursor:cursor + 2]
            more = cursor + 2 < len(items)
            return self._send(200, {"data": page, "hasMore": more, "nextCursor": str(cursor + 2) if more else None})
        if path.startswith("/api/v1/node-types/"):
            from urllib.parse import unquote

            if self.server.rate_limited > 0:
                self.server.rate_limited -= 1
                error = {"code": "RATE_LIMITED", "message": "Too many requests. Please retry later.", "details": {"retryAfterSeconds": 7}}
                return self._send(429, {"error": error})
            node = NODE_DETAILS.get(unquote(path[len("/api/v1/node-types/"):]))
            return self._send(200, node) if node else self._send(404, {"error": {"code": "NODE_TYPE_NOT_FOUND", "message": "Node type not found."}})
        if path.startswith("/api/v1/runs/") and path.endswith("/events"):
            if self.server.run_events is None:
                return self._send(404, {"error": {"code": "NOT_FOUND", "message": "no such route"}})
            if self.server.events_fault == "drop":
                self.connection.close()
                return None
            if self.server.events_fault == "junk":
                return self._send(200, b"<html>not what was asked for</html>")
            asked = query.get("cursor", [self.server.events_key + "0"])[0]
            if not asked.startswith(self.server.events_key) or not asked[len(self.server.events_key):].isdigit():
                return self._send(400, {"error": {"code": "INVALID_CURSOR", "message": "The cursor is invalid."}})
            start = int(asked[len(self.server.events_key):])
            size = min(int(query.get("limit", ["50"])[0]), self.server.events_page)
            page = self.server.run_events[start:start + size]
            more = start + size < len(self.server.run_events)
            following = f"{self.server.events_key}{start + size}" if more else None  # as Kaitoi: no cursor after the last page
            return self._send(200, {"data": page, "hasMore": more, "nextCursor": following})
        if path.startswith("/api/v1/runs/"):
            return self._send(200, self._run_state(path.rsplit("/", 1)[1], "inline"))
        if path.endswith("/download-url"):
            return self._send(200, {"downloadUrl": f"{self.server.url}/storage/{path.split('/')[4]}"})
        if path.startswith("/storage/"):
            data = self.server.files.get(path.rsplit("/", 1)[1])
            return self._send(200, data) if data is not None else self._send(404, {})
        if path == "/api/v1/account/credits":
            return self._send(200, {"balanceCents": 643})
        return self._send(404, {"error": {"code": "NOT_FOUND", "message": "no such route"}})

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        entry = self._record(body)
        path = urlsplit(self.path).path
        if path == "/api/v1/files/uploads":
            name = entry["json"]["filename"]
            return self._send(201, {"uploadId": f"up-{name}", "storageMethod": "single", "upload": {"url": f"{self.server.url}/put/{name}", "headers": {}}})
        if path.startswith("/api/v1/files/uploads/") and path.endswith("/complete"):
            name = path.split("/")[5][3:]
            return self._send(200, {"fileId": f"file-{name}", "sizeBytes": entry["json"].get("actualSizeBytes")})
        if "/endpoints/" in path and path.endswith("/runs"):
            return self._send(202, {"taskId": "task1", "status": "queued"})
        if path == "/api/v1/runs":
            return self._send(202, {"id": "run1", "status": "queued"})
        if path.endswith("/cancel"):
            return self._send(200, {"id": path.split("/")[4], "status": "canceled"})
        return self._send(404, {"error": {"code": "NOT_FOUND", "message": "no such route"}})

    def do_PUT(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._record(body)
        self.server.uploads[self.path.rsplit("/", 1)[1]] = body
        self._send(200, {}, {"ETag": '"etag1"'})
