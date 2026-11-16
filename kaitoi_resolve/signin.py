"""Signing in to Kaitoi in the browser, with no key to copy and paste.

The plugin starts a listener on this computer, on an address only this
computer can reach, and opens Kaitoi's sign-in page with that address and a
secret made for this one sign-in. The person signs in there the way they
always do (password, Google, GitHub). Kaitoi then sends the browser back to
the listener with a key made for this plugin, and the listener takes it only
if the secret comes back with it.

The key is the plugin's own: signing in here does not sign the Kaitoi CLI out,
or this plugin on another computer. It is named for this computer, and signing
in here again replaces the key this computer had before. It stops working by
itself after thirty days, and is listed with the account's other keys in
Kaitoi Studio, where it can be revoked sooner.

That holds only on a server that knows plugins can sign in. An older one takes
the same address for the CLI's own sign-in: it would replace the CLI's login
and answer with the CLI's keys. So the server is asked first whether it signs
this plugin in, and the browser is opened only when it says yes; and an answer
that does not name this plugin, or that carries the CLI's relay key, is
refused, whatever else it holds.

Nothing here touches Resolve, the window or the settings: :class:`SignIn` is
started, polled and cancelled by whoever owns those.
"""

from __future__ import annotations

import hmac
import html
import http.server
import json
import platform
import re
import secrets
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Callable

CLIENT = "resolve"  # the name Kaitoi knows this plugin by on its sign-in pages
TIMEOUT_SECONDS = 600  # Kaitoi keeps a sign-in open for ten minutes, so the listener waits as long
WAITING, DONE, FAILED, CANCELLED = "waiting", "done", "failed", "cancelled"

ASK_SECONDS = 10.0  # how long the server is given to say whether it signs plugins in
ASK_BYTES = 64 * 1024  # and how much it may say: the settings are a few hundred letters
ASK_LIMIT_SECONDS = 30.0  # past this with no browser opened, the sign-in is over, whatever the server is still sending
NOT_OFFERED = "This Kaitoi server does not sign plugins in yet. Use an API key from Kaitoi Studio for now."
NOT_REACHED = "Could not reach Kaitoi to start the sign-in. Check the server address and your connection."
NOT_OURS = "Kaitoi answered as it does for the Kaitoi CLI, not for this plugin. Nothing was saved. Use an API key for now."

# What Kaitoi says went wrong, in the plugin's words.
_ERRORS = {
    "access_denied": "This account does not have access to Kaitoi yet.",
    "api_access_unavailable": "This account's plan does not include API access, which the plugin needs.",
    "server_error": "Kaitoi could not finish the sign-in. Try again in a moment.",
}

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Kaitoi</title>
<style>body{{font:16px -apple-system,Segoe UI,sans-serif;background:#1b1b1f;color:#dedede;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0}}main{{max-width:26em;padding:2em;text-align:center}}
h1{{font-size:20px;font-weight:600}}p{{color:#8b8b93}}</style></head>
<body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


def _origin(url: str) -> tuple:
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower()))


class _SameServer(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only within the server that was asked: the sign-in
    is opened on that server, so another one's settings say nothing about it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            return None  # the redirect is then the answer, and an error
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def server_signs_in(base_url: str, timeout: float = ASK_SECONDS) -> bool:
    """Does this server sign this plugin in? Asked of its public sign-in
    settings. Raises OSError when it cannot be reached, does not answer in
    time or sends the question elsewhere, ValueError when what it answers is
    not those settings."""
    request = urllib.request.Request(f"{base_url.rstrip('/')}/auth/config", headers={"Accept": "application/json"})
    until = time.monotonic() + timeout
    body = b""
    with urllib.request.build_opener(_SameServer).open(request, timeout=timeout) as answer:
        while True:
            piece = answer.read1(8192)
            if not piece:
                break
            body += piece
            if len(body) > ASK_BYTES:
                raise ValueError("too long to be the sign-in settings")
            if time.monotonic() > until:  # an answer that trickles in is no answer
                raise TimeoutError("the sign-in settings did not arrive in time")
    settings = json.loads(body.decode("utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("not the sign-in settings")
    plugins = settings.get("plugins")
    return isinstance(plugins, list) and CLIENT in plugins


# What Kaitoi takes as a computer's name: plain letters, digits and a few
# signs, at most 48, starting and ending on one that is not a space.
DEVICE_LETTERS = 48
_DEVICE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._'()-]{0,46}[A-Za-z0-9.)])?")
_MARK = re.compile(r"[0-9a-f]{4}")


def device_name(mark: str = "", host: str | None = None) -> str:
    """What this computer's key is called in Kaitoi: the computer's own name
    as far as Kaitoi takes it, then ``mark``, four letters that are this
    install's alone. The mark is what keeps two computers of one name (two
    "MacBook-Pro", every seat of a facility set up from one image) from
    replacing each other's key. "" when there is nothing Kaitoi would take:
    the key then carries no computer, and replaces none."""
    if host is None:
        try:
            host = platform.node()
        except Exception:  # noqa: BLE001 - a computer that will not say its name has none
            host = ""
    name = unicodedata.normalize("NFKD", str(host or "").split(".")[0]).encode("ascii", "ignore").decode("ascii")
    name = " ".join(re.sub(r"[^A-Za-z0-9 _'()-]", " ", name).split())
    name = re.sub(r"^[^A-Za-z0-9]+", "", name)
    mark = mark if isinstance(mark, str) and _MARK.fullmatch(mark) else ""
    if mark:
        name = re.sub(r"[^A-Za-z0-9)]+$", "", name[: DEVICE_LETTERS - len(mark) - 1])
        name = f"{name or 'computer'} {mark}"
    else:
        name = re.sub(r"[^A-Za-z0-9)]+$", "", name[:DEVICE_LETTERS])
    return name if _DEVICE.fullmatch(name) else ""


def sign_in_url(base_url: str, port: int, state: str, device: str = "") -> str:
    """Kaitoi's sign-in page, told where on this computer to send the key,
    and what to call this computer's key."""
    callback = f"http://127.0.0.1:{port}/callback"
    asked = {"callback": callback, "state": state, "client": CLIENT}
    if device and _DEVICE.fullmatch(device):
        asked["device"] = device
    return f"{base_url.rstrip('/')}/auth/cli?{urllib.parse.urlencode(asked)}"


class _Listener(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, flow: "SignIn") -> None:
        super().__init__(("127.0.0.1", 0), _Handler)  # this computer only, on a port the system picks
        self.flow = flow
        self.timeout = 0.25


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Listener

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        pass  # a request's address carries the key: it is never written anywhere

    def _page(self, status: int, title: str, text: str) -> None:
        body = _PAGE.format(title=html.escape(title), text=html.escape(text)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - the base class's name
        flow = self.server.flow
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/done":
            title, text = flow.farewell()
            return self._page(200, title, text)
        if url.path != "/callback":
            return self._page(404, "Nothing here", "This address only finishes a Kaitoi sign-in.")
        sent = urllib.parse.parse_qs(url.query)

        def one(name: str) -> str:
            values = sent.get(name) or [""]
            return values[0] if len(values) == 1 else ""

        # Only the sign-in this plugin started: anything else is turned away,
        # and the sign-in goes on waiting for its own answer.
        if not hmac.compare_digest(one("state").encode("utf-8"), flow.secret.encode("utf-8")):
            return self._page(400, "Not this sign-in", "Start the sign-in again from the Kaitoi window in DaVinci Resolve.")
        # And only as a plugin is answered: the CLI's answer, which an older
        # server gives, holds the CLI's keys, and they are not this plugin's to keep.
        if one("client") != CLIENT or "token" in sent:
            flow.finish(FAILED, NOT_OURS)
        elif one("error"):
            flow.finish(FAILED, _ERRORS.get(one("error"), "The sign-in did not finish. Try again."))
        elif one("api_token"):
            flow.finish(DONE, "", {"api_key": one("api_token"), "user": one("user_login") or one("user_email"), "email": one("user_email")})
        else:
            flow.finish(FAILED, "Kaitoi sent no key back. Try again.")
        # On to an address without the key in it, so the key does not stay in
        # the tab's address bar.
        self.send_response(303)
        self.send_header("Location", "/done")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self._page(405, "Nothing here", "This address only finishes a Kaitoi sign-in.")


class SignIn:
    """One sign-in, from opening the browser to holding the key."""

    def __init__(
        self,
        base_url: str,
        *,
        device: str = "",
        open_browser: Callable[[str], Any] = webbrowser.open,
        timeout: float = TIMEOUT_SECONDS,
        server_signs_in: Callable[[str], bool] = server_signs_in,
        ask_limit: float = ASK_LIMIT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device = device
        self.secret = secrets.token_urlsafe(32)
        self.url = ""
        self._open = open_browser
        self._offered = server_signs_in
        self._timeout = float(timeout)
        self._ask_limit = float(ask_limit)
        self._lock = threading.Lock()
        self._state = WAITING
        self._message = ""
        self._result: dict[str, str] = {}
        self._opened = False
        self._answered = False  # the server said yes: what is left is the browser, and then the person
        self._listener: _Listener | None = None
        self._thread: threading.Thread | None = None
        self._started = 0.0

    def start(self) -> bool:
        """Start listening, and in the background ask the server and open the
        browser. Never waits on the network. False, with the reason in
        :meth:`poll`, when this computer will not let it listen."""
        try:
            self._listener = _Listener(self)
        except OSError as exc:
            self.finish(FAILED, f"Could not start listening for the sign-in on this computer: {exc}")
            return False
        self.url = sign_in_url(self.base_url, self._listener.server_address[1], self.secret, self.device)
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._serve, name="kaitoi-sign-in", daemon=True)
        self._thread.start()
        return True

    def _begin(self) -> None:
        """Ask the server whether it signs this plugin in; only then open the
        browser. On a server that does not, opening it would start the CLI's
        sign-in and replace the CLI's login before anything came back here."""
        try:
            offered = self._offered(self.base_url)
        except (OSError, ValueError):
            return self.finish(FAILED, NOT_REACHED)
        except Exception:  # noqa: BLE001 - whatever it was, the server did not say yes
            return self.finish(FAILED, NOT_REACHED)
        if not offered:
            return self.finish(FAILED, NOT_OFFERED)
        self.poll()  # past its limits the sign-in is over, and this is where that is noticed
        with self._lock:
            # In one step with the check, so that a sign-in ended this very
            # moment opens no browser: cancelled while the server was being
            # asked, or out of time.
            if self._state != WAITING:
                return None
            # From here on the limit on the question no longer applies: a
            # browser that is slow to start is not a server that did not answer.
            self._answered = True
        try:
            opened = self._open(self.url)
        except Exception:  # noqa: BLE001 - no browser to open is one answer, whatever raised it
            opened = False
        if opened is False:
            self.finish(FAILED, "Could not open a browser on this computer.")
        else:
            with self._lock:
                self._opened = True
        return None

    def _serve(self) -> None:
        listener = self._listener
        try:
            self._begin()
            while self.poll()["state"] == WAITING:
                listener.handle_request()  # returns after its short timeout when nothing came
            # The browser is sent on to the closing page: answer that too, briefly.
            until = time.monotonic() + 3.0
            while time.monotonic() < until:
                listener.handle_request()
        finally:
            listener.server_close()

    def finish(self, state: str, message: str = "", result: dict[str, str] | None = None) -> None:
        """Settle the sign-in, once: the first answer is the answer."""
        with self._lock:
            if self._state != WAITING:
                return
            self._state, self._message, self._result = state, message, dict(result or {})

    def farewell(self) -> tuple[str, str]:
        """What the browser tab says when it is over."""
        with self._lock:
            state, message = self._state, self._message
        if state == DONE:
            return "Signed in", "You can close this tab and go back to DaVinci Resolve."
        return "Not signed in", message or "The sign-in did not finish. Start it again from the Kaitoi window."

    def cancel(self) -> None:
        self.finish(CANCELLED, "Sign-in cancelled.")

    def poll(self) -> dict[str, Any]:
        """Where the sign-in stands: ``{"state", "message", "result",
        "opened"}``, the last of them true once the browser has been opened on
        Kaitoi's sign-in page. Safe to call at every tick; it never waits."""
        waited = time.monotonic() - self._started if self._started else 0.0
        with self._lock:
            answered = self._answered
        if not answered and waited > self._ask_limit:
            # The server never got as far as a yes: no browser was opened, so
            # nobody was kept from signing in. (A server can hold the
            # question open far longer than it is given, a letter at a time.)
            self.finish(FAILED, NOT_REACHED)
        elif waited > self._timeout:
            self.finish(FAILED, "Nobody signed in within ten minutes. Start again when you are ready.")
        with self._lock:
            return {"state": self._state, "message": self._message, "result": dict(self._result), "opened": self._opened}
