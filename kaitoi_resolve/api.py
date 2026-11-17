"""Tiny urllib-based Kaitoi REST client.

DaVinci Resolve runs scripts with a plain system Python interpreter and no
guaranteed third-party packages, so this module is stdlib only. It covers
exactly the endpoints the plugin needs: file upload, published workflows
(templates with an endpoint: list, contract, run, poll), inline runs of a
single node (create, poll, cancel, events for progress), the node catalog and
each node's schema, the credit balance, download URL, download bytes.

Errors raise :class:`KaitoiAPIError` carrying the server-provided code and
message so the UI can show something actionable.
"""

from __future__ import annotations

import http.client
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

USER_AGENT = "kaitoi-resolve-plugin/0.2"


class KaitoiAPIError(RuntimeError):
    def __init__(self, status: int, code: str | None, message: str, details: Any = None) -> None:
        super().__init__(f"[{status}] {code or ''} {message}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class Config:
    """Connection settings for one API session."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 120.0) -> None:
        if not api_key:
            raise KaitoiAPIError(0, "NO_API_KEY", "Set an API key in the Kaitoi > Settings tab.")
        self.api_key = api_key
        self.base_url = (base_url or "https://api.studio.kaitoi.io").rstrip("/")
        self.timeout = float(timeout)


def _request(
    config: Config,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    json_body: Any = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    url = config.base_url + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    send_headers = {
        "Authorization": f"Bearer {config.api_key}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    payload = body
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        send_headers["Content-Type"] = "application/json"
    if headers:
        send_headers.update(headers)
    req = urllib.request.Request(url, data=payload, method=method, headers=send_headers)
    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - non-JSON error body
            parsed = None
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            raise KaitoiAPIError(
                exc.code, err.get("code"), err.get("message") or exc.reason, err.get("details")
            ) from exc
        raise KaitoiAPIError(exc.code, None, exc.reason or "request failed") from exc
    except urllib.error.URLError as exc:
        raise KaitoiAPIError(0, "NETWORK", str(exc.reason)) from exc


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": "idem_" + secrets.token_hex(16)}


# ── Files ───────────────────────────────────────────────────────────────────


SMALL_UPLOAD_LIMIT = 25 * 1024 * 1024


def upload_file(
    config: Config,
    path: str,
    *,
    external_id: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Upload ``path`` and return its file record (``fileId`` …).

    Uses the direct-to-storage session flow (``POST /files/uploads`` → signed
    ``PUT`` → ``complete``), which has no per-request size cap and handles
    multipart sessions for long clips. Falls back to the legacy small-file
    route only if the session endpoint is unavailable and the file fits it.
    """
    try:
        return upload_file_session(config, path, external_id=external_id, on_status=on_status)
    except KaitoiAPIError as exc:
        if exc.status in (404, 405, 501) and os.path.getsize(path) <= SMALL_UPLOAD_LIMIT:
            return upload_file_small(config, path, external_id=external_id)
        raise


def _put_bytes(url: str, data: bytes, headers: dict[str, str], *, timeout: float) -> dict[str, str]:
    """PUT raw bytes to a signed storage URL; returns the response headers."""
    req = urllib.request.Request(url, data=data, method="PUT", headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raise KaitoiAPIError(exc.code, "STORAGE_PUT", f"storage rejected the upload: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise KaitoiAPIError(0, "NETWORK", str(exc.reason)) from exc


def upload_file_session(
    config: Config,
    path: str,
    *,
    external_id: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Direct-to-storage upload: create session, PUT bytes, complete."""
    filename = os.path.basename(path)
    size = os.path.getsize(path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body: dict[str, Any] = {"filename": filename, "contentType": content_type, "sizeBytes": size}
    if external_id:
        body["externalId"] = external_id
    _, raw, _ = _request(config, "POST", "/api/v1/files/uploads", json_body=body, headers=_idem())
    session = json.loads(raw.decode("utf-8"))
    upload_id = session.get("uploadId")
    if not upload_id:
        raise KaitoiAPIError(500, "BAD_RESPONSE", "upload session response missing uploadId")

    put_timeout = max(config.timeout, 60.0 + size / (256 * 1024))  # ≥ 2 Mbit/s worth of patience
    complete: dict[str, Any] = {"actualSizeBytes": size}

    if session.get("storageMethod") == "multipart" and session.get("parts"):
        parts = sorted(session["parts"], key=lambda p: int(p["partNumber"]))
        part_size = int(session.get("partSizeBytes") or -(-size // len(parts)))
        etags = []
        with open(path, "rb") as fh:
            for index, part in enumerate(parts):
                chunk = fh.read(part_size)
                if on_status:
                    on_status(f"Uploading part {index + 1}/{len(parts)}...")
                headers = _put_bytes(part["url"], chunk, dict(part.get("headers") or {}), timeout=put_timeout)
                etag = headers.get("etag")
                if not etag:
                    raise KaitoiAPIError(0, "STORAGE_PUT", f"storage returned no ETag for part {part['partNumber']}")
                etags.append({"partNumber": int(part["partNumber"]), "etag": etag})
        complete["multipartParts"] = etags
        complete["actualSha256"] = _sha256(path)
    else:
        upload = session.get("upload") or {}
        if not upload.get("url"):
            raise KaitoiAPIError(500, "BAD_RESPONSE", "upload session carried no signed URL")
        with open(path, "rb") as fh:
            data = fh.read()
        _put_bytes(upload["url"], data, dict(upload.get("headers") or {}), timeout=put_timeout)

    _, raw, _ = _request(
        config, "POST", f"/api/v1/files/uploads/{urllib.parse.quote(upload_id, safe='')}/complete", json_body=complete
    )
    payload = json.loads(raw.decode("utf-8"))
    if not payload.get("fileId"):
        raise KaitoiAPIError(500, "BAD_RESPONSE", "upload completion missing fileId")
    return payload


def _sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_file_small(config: Config, path: str, *, external_id: str | None = None) -> dict[str, Any]:
    """Legacy ``POST /api/v1/files`` (multipart/form-data), capped at 25 MB."""
    size = os.path.getsize(path)
    if size > SMALL_UPLOAD_LIMIT:
        raise KaitoiAPIError(
            0,
            "FILE_TOO_LARGE",
            f"{os.path.basename(path)} is {size / 1024 / 1024:.1f} MB; the small-upload cap is 25 MB. "
            "Lower the export resolution or shorten the clip.",
        )
    filename = os.path.basename(path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        data = fh.read()
    boundary = "----kaitoi-" + secrets.token_hex(8)
    buf = io.BytesIO()
    if external_id:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(b'Content-Disposition: form-data; name="externalId"\r\n\r\n')
        buf.write(external_id.encode())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    buf.write(f"Content-Type: {content_type}\r\n\r\n".encode())
    buf.write(data)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", **_idem()}
    status, raw, _ = _request(config, "POST", "/api/v1/files", body=buf.getvalue(), headers=headers)
    if status >= 300:
        raise KaitoiAPIError(status, None, "upload failed")
    payload = json.loads(raw.decode("utf-8"))
    if not payload.get("fileId"):
        raise KaitoiAPIError(500, "BAD_RESPONSE", "upload response missing fileId")
    return payload


def get_download_url(config: Config, file_id: str, *, expires_in_seconds: int = 900) -> str:
    _, raw, _ = _request(
        config,
        "GET",
        f"/api/v1/files/{file_id}/download-url",
        query={"expiresInSeconds": expires_in_seconds},
    )
    payload = json.loads(raw.decode("utf-8"))
    url = payload.get("downloadUrl") or payload.get("url")
    if not url:
        raise KaitoiAPIError(500, "BAD_RESPONSE", "download URL missing in response")
    return url


NAME_LOOKUP_TRIES = 3  # how often a download is started when the host's name cannot be looked up


def download_to(path: str, url: str, *, timeout: float = 300.0, config: Config | None = None, max_bytes: int | None = None) -> str:
    """Stream ``url`` into ``path`` (see :func:`_download_once`).

    Looking up the host's name can fail for a moment when several downloads
    start at once, as thumbnails do. Nothing was sent then, so it is simply
    tried again, twice at most; anything else that goes wrong is not.
    """
    tried = 0
    while True:
        try:
            return _download_once(path, url, timeout=timeout, config=config, max_bytes=max_bytes)
        except KaitoiAPIError as exc:
            tried += 1
            lookup_failed = isinstance(exc.__cause__, urllib.error.URLError) and isinstance(exc.__cause__.reason, socket.gaierror)
            if not lookup_failed or tried >= NAME_LOOKUP_TRIES:
                raise
            time.sleep(0.25 * tried)


def _download_once(path: str, url: str, *, timeout: float = 300.0, config: Config | None = None, max_bytes: int | None = None) -> str:
    """Stream ``url`` into ``path``. A half-written file is never left behind.

    ``max_bytes`` gives up (code ``TOO_LARGE``) as soon as the file is known
    to be bigger, without fetching the rest.

    ``url`` may be a signed storage URL, or an API path / same-origin API URL,
    in which case ``config`` supplies the bearer key. The key is never sent to
    another host.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        url = web_address(url)
    except ValueError as exc:  # a link no request can be made from, as the request itself would say below
        raise KaitoiAPIError(0, "NETWORK", f"the download broke off: {exc or type(exc).__name__}") from exc
    if url.startswith("/"):
        if config is None:
            raise KaitoiAPIError(0, "BAD_URL", "a relative download URL needs API settings")
        url = config.base_url + url
    if config is not None and _same_origin(url, config.base_url):
        headers["Authorization"] = f"Bearer {config.api_key}"
    partial = path + ".part"
    try:
        req = urllib.request.Request(url, method="GET", headers=headers)  # raises on a link that is not a web address
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(partial, "wb") as fh:
            declared = resp.headers.get("Content-Length")
            if max_bytes is not None and declared and declared.isdigit() and int(declared) > max_bytes:
                raise KaitoiAPIError(0, "TOO_LARGE", "the file is larger than expected")
            received = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if max_bytes is not None and received > max_bytes:
                    raise KaitoiAPIError(0, "TOO_LARGE", "the file is larger than expected")
                fh.write(chunk)
        os.replace(partial, path)
    except urllib.error.HTTPError as exc:
        # A server that is asked too much says when to come back; whoever fetches many files heeds it.
        later = exc.headers.get("Retry-After") if exc.headers is not None else None
        exc.close()  # the refusal's own connection: many may be refused in a row
        raise KaitoiAPIError(exc.code, "DOWNLOAD", f"download failed: {exc.reason}", {"retryAfterSeconds": later} if later else None) from exc
    except urllib.error.URLError as exc:
        raise KaitoiAPIError(0, "NETWORK", str(exc.reason)) from exc
    except (http.client.HTTPException, ValueError) as exc:  # a reply cut short, one that is not HTTP, or no web address
        raise KaitoiAPIError(0, "NETWORK", f"the download broke off: {exc or type(exc).__name__}") from exc
    finally:
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                pass
    return path


_UNSENDABLE = re.compile(r"[^\x21-\x7e]")  # a space, a control character, or a letter outside ASCII


def web_address(url: str) -> str:
    """A link as it can be sent: a space, or any other character a request
    cannot carry, written as its escape. Kaitoi hands out a link to a node's
    thumbnail with the node's name in it as it is, and a name can have a
    space. Every other character is left exactly as it came, so a signed link
    stays the link that was signed."""
    host = urllib.parse.urlsplit(url).netloc
    start = url.index(host) + len(host) if host else 0  # the host is not a place for escapes: a name in another script has its own spelling
    return url[:start] + _UNSENDABLE.sub(lambda found: urllib.parse.quote(found.group()), url[start:])


def _same_origin(url: str, base_url: str) -> bool:
    left, right = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base_url)
    return (left.scheme, left.hostname, left.port) == (right.scheme, right.hostname, right.port)


# ── Runs ────────────────────────────────────────────────────────────────────


def create_run(
    config: Config,
    graph: dict[str, Any],
    target_node_ids: list[str],
    *,
    input_overrides: dict[str, dict[str, Any]] | None = None,
    external_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"graph": graph, "targetNodeIds": target_node_ids}
    if input_overrides:
        body["inputOverrides"] = input_overrides
    if external_id:
        body["externalId"] = external_id
    _, raw, _ = _request(config, "POST", "/api/v1/runs", json_body=body, headers=_idem())
    return json.loads(raw.decode("utf-8"))


def get_run(config: Config, run_id: str) -> dict[str, Any]:
    _, raw, _ = _request(config, "GET", f"/api/v1/runs/{run_id}")
    return json.loads(raw.decode("utf-8"))


PROGRESS_PAGES = 5  # event pages read at one poll: a run far ahead is caught up with over a few
SAID_LETTERS = 100  # of what a node says about its progress, the status line shows this much


def _one_line(text: Any) -> str:
    """What a node said, as one short plain line: its first line, with no
    angle brackets (the window's labels would read them as markup)."""
    for line in str(text or "").splitlines():
        plain = " ".join(line.replace("<", " ").replace(">", " ").split())
        if plain:
            return plain if len(plain) <= SAID_LETTERS else plain[: SAID_LETTERS - 1].rstrip() + "…"
    return ""


def run_progress(
    config: Config, run_id: str, node_id: str = "", seen: dict[str, Any] | None = None, should_stop: Callable[[], bool] | None = None
) -> dict[str, Any]:
    """How far a run has got, read from its events: ``{"fraction", "message", "cursor"}``.

    ``fraction`` is 0 to 1, or None while nothing has been reported;
    ``message`` is the last thing the node itself said, or "". ``node_id``
    names the node that matters in a run of several: the loaders in front of
    it are done at once, and their progress is not the run's.

    ``seen`` is what the call before returned for the same run. Reading goes
    on from the page that one stopped at, so a long run is not read again from
    its first event at every poll: one request as a rule, and never more than
    ``PROGRESS_PAGES``.
    """
    state: dict[str, Any] = {"fraction": None, "message": "", "cursor": None}
    if isinstance(seen, dict):
        state.update({key: seen.get(key, state[key]) for key in state})
    for _ in range(PROGRESS_PAGES):
        if should_stop is not None and should_stop():
            break
        _, raw, _ = _request(config, "GET", f"/api/v1/runs/{run_id}/events", query={"limit": 200, "cursor": state["cursor"]})
        page = json.loads(raw.decode("utf-8"))
        events = page.get("data") if isinstance(page, dict) else None
        if not isinstance(events, list):
            break
        for event in events:
            if not isinstance(event, dict) or (node_id and event.get("nodeId") != node_id):
                continue
            reported = event.get("progress")
            if event.get("type") == "node.progress" and isinstance(reported, (int, float)) and not isinstance(reported, bool):
                state["fraction"] = max(0.0, min(1.0, float(reported)))
            elif event.get("type") == "node.log" and _one_line(event.get("message")):
                state["message"] = _one_line(event.get("message"))
        following = page.get("nextCursor")
        if not page.get("hasMore") or not following:
            break  # the last page so far: read again from here next time, for what has been added to it
        state["cursor"] = following
    return state


def cancel_run(config: Config, run_id: str) -> dict[str, Any]:
    _, raw, _ = _request(config, "POST", f"/api/v1/runs/{run_id}/cancel")
    return json.loads(raw.decode("utf-8"))


TERMINAL_STATES = ("succeeded", "failed", "canceled", "cancelled")


def wait_for_run(
    config: Config,
    run_id: str,
    *,
    timeout: float,
    poll_interval: float,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    return wait_for(
        lambda: get_run(config, run_id),
        label=f"run {run_id}",
        timeout=timeout,
        poll_interval=poll_interval,
        on_progress=on_progress,
        should_stop=should_stop,
    )


def run_error_detail(run: dict[str, Any]) -> str:
    run_id = run.get("id") or run.get("taskId")
    err = run.get("error") or {}
    if not isinstance(err, dict):
        return f"{err} | run={run_id}"
    parts = [err.get("message") or "run did not succeed"]
    if err.get("nodeId"):
        parts.append(f"node={err['nodeId']}")
    if err.get("code"):
        parts.append(f"code={err['code']}")
    parts.append(f"run={run_id}")
    return " | ".join(parts)


def node_output_files(run: dict[str, Any], target_node_id: str) -> list[dict[str, Any]]:
    """Every file ``target_node_id`` produced in an inline run, by output pin.

    Output keys come in two shapes: a bare pin name (``"outputVideo"``) for a
    single-target run, or ``"<nodeId>:<pin>"`` for multi-target runs.
    """
    outputs = run.get("outputs") or {}
    files: list[dict[str, Any]] = []
    if not isinstance(outputs, dict):
        return files
    for key in sorted(outputs):
        value = outputs[key]
        if not (isinstance(value, dict) and value.get("type") == "file" and value.get("fileId")):
            continue
        node_id, _, pin = key.rpartition(":")
        if node_id and node_id != target_node_id:
            continue
        files.append(
            {
                "field": pin,
                "fileId": str(value["fileId"]),
                "downloadUrl": value.get("downloadUrl"),
                "filename": value.get("filename"),
                "contentType": value.get("contentType"),
            }
        )
    return files


def find_output_file_id(run: dict[str, Any], target_node_id: str) -> str | None:
    """Return the first file output produced by ``target_node_id``.

    Output keys come in two shapes: a bare pin name (``"outputVideo"``) for a
    single-target run, or ``"<nodeId>:<pin>"`` for multi-target runs. When a
    node exposes several file pins the lowest-sorting populated one wins, so
    the result is deterministic.
    """
    outputs = run.get("outputs") or {}
    if not isinstance(outputs, dict):
        return None

    def is_file(value: Any) -> bool:
        return isinstance(value, dict) and value.get("type") == "file" and bool(value.get("fileId"))

    matches = [
        (key, value["fileId"])
        for key, value in outputs.items()
        if is_file(value) and (key.startswith(f"{target_node_id}:") or ":" not in key)
    ]
    if matches:
        matches.sort()
        return matches[0][1]
    for value in outputs.values():
        if is_file(value):
            return value["fileId"]
    return None


# ── Published workflows (templates with an endpoint) ───────────────────────


def _get_json(config: Config, path: str, query: dict[str, Any] | None = None) -> Any:
    _, raw, _ = _request(config, "GET", path, query=query)
    return json.loads(raw.decode("utf-8"))


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def list_templates(config: Config, *, has_endpoint: bool | None = True, page_size: int = 100) -> list[dict[str, Any]]:
    """Every template the key's owner has, following the cursor to the end."""
    templates: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(200):  # a bound, not an expectation: 200 pages of 100
        query: dict[str, Any] = {"limit": page_size, "cursor": cursor}
        if has_endpoint is not None:
            query["hasEndpoint"] = "true" if has_endpoint else "false"
        page = _get_json(config, "/api/v1/templates", query)
        templates.extend(page.get("data") or [])
        cursor = page.get("nextCursor")
        if not page.get("hasMore") or not cursor:
            break
    return templates


def get_endpoint_docs(config: Config, template_id: str, endpoint_id: str) -> dict[str, Any]:
    """An endpoint's contract: its inputs, its outputs and where to run it."""
    return _get_json(config, f"/api/v1/templates/{_quote(template_id)}/endpoints/{_quote(endpoint_id)}/docs")


def endpoint_runs_path(template_id: str, endpoint_id: str) -> str:
    return f"/api/v1/templates/{_quote(template_id)}/endpoints/{_quote(endpoint_id)}/runs"


def run_endpoint(
    config: Config,
    template_id: str,
    endpoint_id: str,
    inputs: dict[str, Any],
    *,
    external_id: str | None = None,
) -> dict[str, Any]:
    """Submit an owner run of a published workflow. May spend credits."""
    body: dict[str, Any] = {"inputs": inputs}
    if external_id:
        body["externalId"] = external_id
    _, raw, _ = _request(config, "POST", endpoint_runs_path(template_id, endpoint_id), json_body=body, headers=_idem())
    return json.loads(raw.decode("utf-8"))


def get_endpoint_run(config: Config, template_id: str, endpoint_id: str, task_id: str) -> dict[str, Any]:
    return _get_json(config, f"{endpoint_runs_path(template_id, endpoint_id)}/{_quote(task_id)}")


def file_input(file_id: str) -> dict[str, str]:
    """How an uploaded file is passed as a workflow input."""
    return {"type": "file", "fileId": file_id}


def output_files(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The files a finished workflow run returned, in the order it lists them.

    Each entry: ``field`` (the output's API name), ``fileId``, and when the
    server gave them ``downloadUrl``, ``filename`` and ``contentType``.
    """
    outputs = run.get("outputs") or {}
    files: list[dict[str, Any]] = []
    if not isinstance(outputs, dict):
        return files
    for field, value in outputs.items():
        if isinstance(value, dict) and value.get("type") == "file" and value.get("fileId"):
            files.append(
                {
                    "field": str(field),
                    "fileId": str(value["fileId"]),
                    "downloadUrl": value.get("downloadUrl"),
                    "filename": value.get("filename"),
                    "contentType": value.get("contentType"),
                }
            )
    return files


def output_texts(run: dict[str, Any]) -> dict[str, str]:
    """The plain values a finished workflow run returned (text, numbers)."""
    outputs = run.get("outputs") or {}
    texts: dict[str, str] = {}
    if not isinstance(outputs, dict):
        return texts
    for field, value in outputs.items():
        if isinstance(value, dict):
            if value.get("type") == "file":
                continue
            value = value.get("value")
        if value is None or isinstance(value, (dict, list)):
            continue
        texts[str(field)] = str(value)
    return texts


def wait_for(
    fetch: Callable[[], dict[str, Any]],
    *,
    label: str,
    timeout: float,
    poll_interval: float,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Poll ``fetch`` until the run it returns is in a terminal state."""
    deadline = time.monotonic() + timeout
    while True:
        if should_stop is not None and should_stop():
            raise KaitoiAPIError(0, "ABORTED", "stopped by user")
        run = fetch()
        if on_progress is not None:
            on_progress(run)
        if run.get("status") in TERMINAL_STATES:
            return run
        if time.monotonic() > deadline:
            raise KaitoiAPIError(0, "TIMEOUT", f"{label} did not finish in {timeout:.0f}s")
        # Sleep in slices so a cancel is noticed within ~0.5 s, not a full poll.
        wake = time.monotonic() + poll_interval
        while time.monotonic() < wake:
            if should_stop is not None and should_stop():
                raise KaitoiAPIError(0, "ABORTED", "stopped by user")
            time.sleep(min(0.5, max(0.0, wake - time.monotonic())))


# ── Node types ──────────────────────────────────────────────────────────────


NODE_LIST_FIELDS = "type,title,category,description,tags,source,thumbnailUrl,icon"
NODE_LIST_PIN_FIELDS = "inputs,outputs"  # a server from before these were listable refuses them
NODE_LIST_KIND_FIELDS = "session,agent"  # and so does one from before it said which nodes are sessions and agents


def list_node_types(
    config: Config,
    *,
    page_size: int = 200,
    should_stop: Callable[[], bool] | None = None,
    on_page: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    """Every node the account can use: built-in, community and its own.

    The list comes a page at a time, and a full catalog is several pages:
    ``on_page`` is handed each page's nodes as it arrives, so that a caller
    can show them without waiting for the last.

    Each comes with its ``inputs`` and ``outputs`` when the server can list
    them, and with whether it is a session or an agent node when the server
    says. An older server refuses fields it does not know; the list is then
    asked for with less: first without the two marks, then without the pins
    as well, and :func:`get_node_type` fetches one node's pins when it is
    picked.
    """
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    asks = [f"{NODE_LIST_FIELDS},{NODE_LIST_KIND_FIELDS},{NODE_LIST_PIN_FIELDS}", f"{NODE_LIST_FIELDS},{NODE_LIST_PIN_FIELDS}", NODE_LIST_FIELDS]
    for _ in range(100):  # a bound, not an expectation: 100 pages of 200
        if should_stop is not None and should_stop():
            raise KaitoiAPIError(0, "ABORTED", "stopped by user")
        while True:
            try:
                page = _get_json(config, "/api/v1/node-types", {"limit": page_size, "cursor": cursor, "fields": asks[0]})
                break
            except KaitoiAPIError as exc:
                if exc.code != "INVALID_FIELDS_PROJECTION" or len(asks) == 1:
                    raise
                asks.pop(0)  # what this server refuses is not asked for again, on this page or the next
        arrived = page.get("data") or []
        nodes.extend(arrived)
        if on_page is not None:
            on_page(arrived)
        cursor = page.get("nextCursor")
        if not page.get("hasMore") or not cursor:
            break
    return nodes


def get_node_type(config: Config, node_type: str) -> dict[str, Any]:
    """Public schema for a node type (input/output pins, types, defaults)."""
    encoded = urllib.parse.quote(node_type, safe="")
    _, raw, _ = _request(config, "GET", f"/api/v1/node-types/{encoded}")
    return json.loads(raw.decode("utf-8"))


def get_node_initial_state(config: Config, node_type: str) -> dict[str, Any]:
    """What a node fills in when it starts: the choices and starting values of
    its ``dynamic`` inputs (a list of voices fetched from a provider, say).

    The server runs the node's own start-up step for this, so it can take a
    few seconds. A server from before this existed answers 404.
    """
    encoded = urllib.parse.quote(node_type, safe="")
    _, raw, _ = _request(config, "GET", f"/api/v1/node-type-initial-state/{encoded}")
    return json.loads(raw.decode("utf-8"))


def account_credits(config: Config) -> dict[str, Any]:
    _, raw, _ = _request(config, "GET", "/api/v1/account/credits")
    return json.loads(raw.decode("utf-8"))


def balance_text(credits: Any) -> str:
    """The account's balance in credits, as Kaitoi Studio counts them:
    ``"6,275 credits"``. "" when the answer carries none.

    ``balanceCents`` is the number of whole credits (a credit is one US cent),
    so it is shown as it is. Divided by a hundred it is dollars, not credits.
    """
    whole = credits.get("balanceCents") if isinstance(credits, dict) else None
    if not isinstance(whole, (int, float)) or isinstance(whole, bool):
        return ""
    whole = int(whole)
    return f"{whole:,} credit" + ("" if whole == 1 else "s")


# Kaitoi pin type -> run-graph typed-pin discriminator.
_PIN_TYPE_TO_TYPED = {
    "string": "string",
    "text": "string",
    "int": "number",
    "integer": "number",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "json": "json",
    "object": "json",
    "array": "json",
    "list": "json",
}
# Pins fed by a connection or an override — never inline these as literals.
NON_LITERAL_PIN_TYPES = {"image", "video", "audio", "file", "node", "embedding", "model"}


def autofill_node_inputs(node_detail: dict[str, Any], *, skip_pins: set[str]) -> dict[str, Any]:
    """Build typed-pin literals for every declared pin not fed by a connection.

    Inline-graph nodes do **not** inherit catalog defaults at execution time:
    the executor reads every declared input by key, so a missing pin surfaces
    as a bare ``KeyError`` ('seed', 'duration', ...). Emitting a typed pin for
    each unwired input — a ``null`` pin when there is no default — keeps the
    key present without inventing a value.
    """
    inputs: dict[str, Any] = {}
    for pin in node_detail.get("inputs") or []:
        name = pin.get("name")
        if not name or name in skip_pins:
            continue
        pin_type = (pin.get("type") or "").lower()
        default = pin.get("default")
        if default is None or pin_type in NON_LITERAL_PIN_TYPES:
            inputs[name] = {"type": "null", "value": None}
            continue
        inputs[name] = {"type": _PIN_TYPE_TO_TYPED.get(pin_type, "json"), "value": default}
    return inputs


# Preferred prompt pin names, in order. Falls back to the first free-text pin.
_PROMPT_PIN_NAMES = ("prompt", "inputPrompt", "editPrompt", "textPrompt", "text", "description")
# String pins that configure the model rather than describe the shot.
_NON_PROMPT_STRING_PINS = {
    "resolution",
    "duration",
    "aspectRatio",
    "aspect_ratio",
    "quality",
    "model",
    "mode",
    "style",
    "editStrength",
    "negativePrompt",
    "negative_prompt",
}


def find_input_pin(node_detail: dict[str, Any], pin_type: str) -> str | None:
    """First input pin of ``pin_type`` ("video" / "image"), in declaration order."""
    for pin in node_detail.get("inputs") or []:
        if (pin.get("type") or "").lower() == pin_type.lower():
            return pin.get("name")
    return None


def find_prompt_pin(node_detail: dict[str, Any]) -> str | None:
    """The pin that takes the user's prompt.

    Model nodes carry several string pins (``resolution``, ``duration``…), so
    a name-preference pass runs first; only then does it fall back to the first
    string pin that is neither a known knob nor pre-filled with a default.
    """
    pins = node_detail.get("inputs") or []
    by_name = {p.get("name"): p for p in pins}
    for candidate in _PROMPT_PIN_NAMES:
        if candidate in by_name:
            return candidate
    for pin in pins:
        name = pin.get("name") or ""
        if (pin.get("type") or "").lower() not in ("string", "text"):
            continue
        if name in _NON_PROMPT_STRING_PINS:
            continue
        return name
    return None


def find_output_pin(node_detail: dict[str, Any], pin_type: str, *, default: str | None = None) -> str | None:
    """First output pin of ``pin_type``, ignoring the internal ``self`` pin."""
    for pin in node_detail.get("outputs") or []:
        name = pin.get("name")
        if name == "self":
            continue
        if (pin.get("type") or "").lower() == pin_type.lower():
            return name
    return default
