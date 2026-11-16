"""Signing in through the browser: the listener on this computer, and what it lets in."""

from __future__ import annotations

from . import HOME  # noqa: F401 - first: outside the tests package, where home is not redirected, this file does not load

import contextlib
import io
import re
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

from kaitoi_resolve import signin


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get(url: str):
    """One request as a browser would send it, without following where it is sent on to."""
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=5) as answer:
            return answer.status, answer.read().decode("utf-8"), dict(answer.headers)
    except urllib.error.HTTPError as answer:
        return answer.code, answer.read().decode("utf-8"), dict(answer.headers)


class Base(unittest.TestCase):
    def start(self, base_url="https://api.example.test", opens=True, offered=True, **more):
        """A sign-in on a server that says ``offered`` when asked whether it signs this plugin in."""
        self.opened = []
        self.asked = []

        def open_browser(url):
            self.opened.append(url)
            if isinstance(opens, Exception):
                raise opens
            return opens

        def server_signs_in(base):
            self.asked.append(base)
            if isinstance(offered, Exception):
                raise offered
            return offered

        flow = signin.SignIn(base_url, open_browser=open_browser, server_signs_in=server_signs_in, **more)
        self.addCleanup(flow.cancel)
        self.started = flow.start()
        until = time.time() + 3  # the server is asked and the browser opened off the caller's thread
        while not self.opened and flow.poll()["state"] == signin.WAITING and time.time() < until:
            time.sleep(0.01)
        return flow

    def parts(self, flow):
        url = urllib.parse.urlsplit(flow.url)
        asked = urllib.parse.parse_qs(url.query)
        return url, asked, asked["callback"][0]

    def back(self, flow, client="resolve", **sent):
        """The browser coming back from Kaitoi with these in the address, named as this plugin's answer unless told otherwise."""
        _, _, callback = self.parts(flow)
        if client is not None:
            sent = {**sent, "client": client}
        return get(callback + "?" + urllib.parse.urlencode(sent))

    def wait(self, flow, state, seconds=3.0):
        until = time.time() + seconds
        while flow.poll()["state"] != state and time.time() < until:
            time.sleep(0.02)
        return flow.poll()


class TheAddressTheBrowserIsSentTo(Base):
    def test_it_is_kaitois_sign_in_page_told_where_to_come_back_and_for_which_app(self):
        flow = self.start("https://api.example.test/")
        url, asked, callback = self.parts(flow)
        self.assertTrue(self.started)
        self.assertEqual(self.opened, [flow.url])
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "api.example.test", "/auth/cli"))
        self.assertEqual(sorted(asked), ["callback", "client", "state"])
        self.assertEqual(asked["client"], ["resolve"])
        # As Kaitoi's sign-in page requires them, or it refuses the sign-in outright.
        self.assertRegex(asked["state"][0], r"^[A-Za-z0-9_-]{32,128}$")
        back = urllib.parse.urlsplit(callback)
        self.assertEqual((back.scheme, back.hostname, back.path, back.query, back.fragment), ("http", "127.0.0.1", "/callback", "", ""))
        self.assertEqual(callback, f"http://127.0.0.1:{back.port}/callback", "in the one spelling Kaitoi accepts")

    def test_it_says_what_to_call_this_computers_key(self):
        flow = self.start(device="Jos-MacBook-Pro 7f3a")
        _url, asked, _callback = self.parts(flow)
        self.assertEqual(sorted(asked), ["callback", "client", "device", "state"])
        self.assertEqual(asked["device"], ["Jos-MacBook-Pro 7f3a"])

    def test_a_name_kaitoi_would_refuse_is_left_out_not_sent(self):
        """Kaitoi refuses the whole sign-in over a name it does not take: better a key with no computer in its name."""
        for bad in ("", "<b>", " lead", "trail ", "x" * 49, "tab\tname", "a&b", "émile", "-dash", None):
            asked = urllib.parse.parse_qs(urllib.parse.urlsplit(signin.sign_in_url("https://api.example.test", 5000, "s", bad)).query)
            self.assertEqual(sorted(asked), ["callback", "client", "state"], bad)

    def test_each_sign_in_has_a_secret_of_its_own(self):
        self.assertNotEqual(self.start().secret, self.start().secret)

    def test_the_listener_is_reachable_from_this_computer_only(self):
        flow = self.start()
        self.assertEqual(flow._listener.server_address[0], "127.0.0.1")


class TheAnswer(Base):
    def test_a_key_that_comes_back_with_the_secret_is_taken(self):
        flow = self.start()
        status, _, headers = self.back(flow, state=flow.secret, api_token="key-123", user_login="jo", user_email="jo@example.test", user_id="u1")
        self.assertEqual((status, headers.get("Location")), (303, "/done"), "on to an address without the key in it")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        seen = flow.poll()
        self.assertEqual(seen["state"], signin.DONE)
        self.assertEqual(seen["result"], {"api_key": "key-123", "user": "jo", "email": "jo@example.test"})
        _, _, callback = self.parts(flow)
        status, page, headers = get(callback.replace("/callback", "/done"))
        self.assertEqual(status, 200)
        self.assertIn("Signed in", page)
        self.assertIn("go back to DaVinci Resolve", page)
        self.assertNotIn("key-123", page)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))

    def test_with_no_name_the_email_stands_for_who_signed_in(self):
        flow = self.start()
        self.back(flow, state=flow.secret, api_token="key-123", user_email="jo@example.test")
        self.assertEqual(flow.poll()["result"]["user"], "jo@example.test")

    def test_an_answer_without_the_secret_is_turned_away_and_the_sign_in_goes_on_waiting(self):
        flow = self.start()
        for sent in ({"api_token": "stolen"}, {"state": "wrong" * 9, "api_token": "stolen"}, {"state": flow.secret[:-1], "api_token": "stolen"},
                     {"state": "", "api_token": "stolen"}, {"state": flow.secret + "x", "api_token": "stolen"}, {"error": "access_denied"}):
            status, page, _ = self.back(flow, **sent)
            self.assertEqual(status, 400, sent)
            self.assertNotIn("stolen", page)
            self.assertEqual(flow.poll()["state"], signin.WAITING, "a stranger's request does not end the person's own sign-in")
        _, _, callback = self.parts(flow)
        status, _, _ = get(f"{callback}?state={flow.secret}&state={flow.secret}&api_token=twice&client=resolve")
        self.assertEqual(status, 400, "the secret given twice is not the secret")
        self.assertEqual(flow.poll()["state"], signin.WAITING)
        self.back(flow, state=flow.secret, api_token="key-123", user_login="jo")
        self.assertEqual(flow.poll()["result"]["api_key"], "key-123")

    def test_the_first_answer_is_the_answer(self):
        flow = self.start()
        self.back(flow, state=flow.secret, api_token="first", user_login="jo")
        self.back(flow, state=flow.secret, api_token="second", user_login="someone else")
        self.back(flow, state=flow.secret, error="access_denied")
        self.assertEqual((flow.poll()["state"], flow.poll()["result"]["api_key"], flow.poll()["result"]["user"]), (signin.DONE, "first", "jo"))

    def test_kaitoi_saying_no_is_said_in_plain_words(self):
        # As Kaitoi sends each of them to a plugin: with the plugin's name in the answer.
        for error, said in (("access_denied", "does not have access"), ("server_error", "could not finish"), ("something_new", "did not finish")):
            flow = self.start()
            status, _, _ = self.back(flow, state=flow.secret, error=error, error_description="<b>markup</b>")
            self.assertEqual(status, 303)
            seen = flow.poll()
            self.assertEqual(seen["state"], signin.FAILED)
            self.assertIn(said, seen["message"])
            self.assertNotIn("<b>", seen["message"], "what the server wrote is not passed on")
            self.assertEqual(seen["result"], {})
            _, _, callback = self.parts(flow)
            _, page, _ = get(callback.replace("/callback", "/done"))
            self.assertIn("Not signed in", page)

    def test_an_answer_with_the_secret_and_no_key_is_a_failure(self):
        flow = self.start()
        self.back(flow, state=flow.secret, user_login="jo")
        self.assertEqual(flow.poll()["state"], signin.FAILED)
        self.assertIn("no key", flow.poll()["message"])

    def test_nothing_else_is_served(self):
        flow = self.start()
        _, _, callback = self.parts(flow)
        root = callback.replace("/callback", "")
        for path in ("/", "/callback/extra", "/favicon.ico", "/../etc/passwd", "/CALLBACK"):
            self.assertEqual(get(root + path)[0], 404, path)
        request = urllib.request.Request(callback, data=b"state=" + flow.secret.encode() + b"&api_token=posted", method="POST")
        try:
            status = urllib.request.urlopen(request, timeout=5).status
        except urllib.error.HTTPError as answer:
            status = answer.code
        self.assertEqual(status, 405)
        self.assertEqual(flow.poll()["state"], signin.WAITING)

    def test_the_key_is_never_written_to_a_log(self):
        seen = io.StringIO()
        with contextlib.redirect_stderr(seen), contextlib.redirect_stdout(seen):
            flow = self.start()
            self.back(flow, state=flow.secret, api_token="key-123-secret", user_login="jo")
            self.back(flow, state="wrong", api_token="key-456-secret")
            time.sleep(0.1)
        self.assertEqual(seen.getvalue(), "")


class EndingWithoutAnAnswer(Base):
    def test_cancelling_stops_the_listener(self):
        flow = self.start()
        _, _, callback = self.parts(flow)
        flow.cancel()
        self.assertEqual(flow.poll()["state"], signin.CANCELLED)
        flow._thread.join(timeout=6)
        self.assertFalse(flow._thread.is_alive(), "nothing is left listening")
        with self.assertRaises(OSError):
            urllib.request.urlopen(f"{callback}?state={flow.secret}&api_token=late", timeout=2)
        self.assertEqual(flow.poll()["state"], signin.CANCELLED)
        self.assertEqual(flow.poll()["result"], {})

    def test_nobody_signing_in_ends_it_after_the_time_kaitoi_keeps_a_sign_in_open(self):
        self.assertEqual(signin.TIMEOUT_SECONDS, 600)
        flow = self.start(timeout=0.2)
        seen = self.wait(flow, signin.FAILED)
        self.assertEqual(seen["state"], signin.FAILED)
        self.assertIn("ten minutes", seen["message"])
        flow._thread.join(timeout=6)
        self.assertFalse(flow._thread.is_alive())

    def test_a_computer_with_no_browser_to_open_says_so(self):
        for opens in (False, RuntimeError("no display")):
            flow = self.start(opens=opens)
            self.wait(flow, signin.FAILED)
            self.assertEqual(flow.poll()["state"], signin.FAILED)
            self.assertIn("Could not open a browser", flow.poll()["message"])
            flow._thread.join(timeout=6)
            self.assertFalse(flow._thread.is_alive())

    def test_polling_never_waits(self):
        flow = self.start()
        began = time.monotonic()
        for _ in range(200):
            flow.poll()
        self.assertLess(time.monotonic() - began, 0.2)

    def test_the_module_touches_no_window_no_settings_and_no_resolve(self):
        with open(signin.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertEqual(re.findall(r"^(?:from|import) (\S+)", source, re.M),
                         ["__future__", "hmac", "html", "http.server", "json", "platform", "re", "secrets", "threading", "time", "unicodedata",
                          "urllib.error", "urllib.parse", "urllib.request", "webbrowser", "typing"])


class WhatThisComputersKeyIsCalled(unittest.TestCase):
    # The same pattern Kaitoi checks the name against, written out again here on purpose.
    TAKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._'()-]{0,46}[A-Za-z0-9.)])?")

    def test_the_computers_own_name_then_the_installs_mark(self):
        for host, named, bare in (
            ("Jos-MacBook-Pro.local", "Jos-MacBook-Pro 7f3a", "Jos-MacBook-Pro"),
            ("edit_bay (3).corp.example", "edit_bay (3) 7f3a", "edit_bay (3)"),
            ("émile\u2019s  Mac Studio.lan", "emiles Mac Studio 7f3a", "emiles Mac Studio"),
            ("-lead.local", "lead 7f3a", "lead"),
            ("trail-", "trail 7f3a", "trail"),
            ("a", "a 7f3a", "a"),
            ("x" * 80, "x" * 43 + " 7f3a", "x" * 48),
            ("y" * 42 + "-zzz", "y" * 42 + " 7f3a", "y" * 42 + "-zzz"),
            ("", "computer 7f3a", ""),
            ("---", "computer 7f3a", ""),
            ("\u65e5\u672c\u8a9e", "computer 7f3a", ""),
            ("<script>alert(1)</script>", "script alert(1) script 7f3a", "script alert(1) script"),
        ):
            self.assertEqual(signin.device_name("7f3a", host), named, host)
            self.assertEqual(signin.device_name("", host), bare, host)
            for name in (named, bare):
                self.assertTrue(name == "" or self.TAKEN.fullmatch(name), name)
                self.assertLessEqual(len(name), 48)

    def test_only_a_mark_of_four_plain_letters_is_added(self):
        for bad in ("7F3A", "7f3", "7f3a0", "7f3 ", "zzzz", "a b", None, 7):
            self.assertEqual(signin.device_name(bad, "studio"), "studio", bad)

    def test_two_computers_of_one_name_get_keys_of_different_names(self):
        self.assertNotEqual(signin.device_name("7f3a", "MacBook-Pro"), signin.device_name("01bc", "MacBook-Pro"))

    def test_it_asks_this_computer_for_its_name_and_survives_one_that_will_not_say(self):
        with mock.patch.object(signin.platform, "node", return_value="Studio-2.local"):
            self.assertEqual(signin.device_name("7f3a"), "Studio-2 7f3a")
        with mock.patch.object(signin.platform, "node", side_effect=OSError("no name")):
            self.assertEqual(signin.device_name("7f3a"), "computer 7f3a")
            self.assertEqual(signin.device_name(), "")


class AServerThatDoesNotKnowPlugins(Base):
    """An older server takes the same address for the CLI's own sign-in: it replaces the CLI's login and answers with the CLI's keys."""

    def test_the_server_is_asked_before_the_browser_is_opened(self):
        flow = self.start()
        self.assertEqual(self.asked, ["https://api.example.test"])
        self.assertEqual(self.opened, [flow.url])

    def test_it_says_the_browser_is_open_only_once_it_is(self):
        import threading

        asked, go = threading.Event(), threading.Event()

        def slow(_base):
            asked.set()
            go.wait(3)
            return True

        opened = []
        flow = signin.SignIn("https://api.example.test", open_browser=opened.append, server_signs_in=slow)
        self.addCleanup(flow.cancel)
        self.assertTrue(flow.start())
        self.assertTrue(asked.wait(3))
        self.assertEqual((flow.poll()["opened"], opened), (False, []), "the server is still being asked")
        go.set()
        until = time.time() + 3
        while not flow.poll()["opened"] and time.time() < until:
            time.sleep(0.01)
        self.assertEqual((flow.poll()["opened"], opened), (True, [flow.url]))

    def test_a_server_that_holds_the_question_open_does_not_hold_the_sign_in(self):
        """However long the server takes to say anything, the sign-in is over after its limit, and says what happened."""
        import threading

        answered = threading.Event()

        def slow(_base):
            time.sleep(1.2)
            answered.set()
            return True

        opened = []
        flow = signin.SignIn("https://api.example.test", open_browser=opened.append, server_signs_in=slow, ask_limit=0.3)
        self.addCleanup(flow.cancel)
        self.assertTrue(flow.start())
        self.assertEqual(flow.poll()["state"], signin.WAITING, "within the limit it waits")
        seen = self.wait(flow, signin.FAILED, seconds=1.0)
        self.assertEqual((seen["state"], seen["message"]), (signin.FAILED, signin.NOT_REACHED), "not: nobody signed in within ten minutes")
        self.assertTrue(answered.wait(3))
        time.sleep(0.1)
        self.assertEqual(opened, [], "a yes that comes after that opens no browser")

    def test_a_yes_after_the_limit_opens_no_browser_even_if_nobody_was_watching(self):
        """The window may not have looked in between: the sign-in notices by itself that its time is up."""
        opened = []

        def slow(_base):
            time.sleep(0.6)
            return True

        flow = signin.SignIn("https://api.example.test", open_browser=opened.append, server_signs_in=slow, ask_limit=0.2)
        self.addCleanup(flow.cancel)
        self.assertTrue(flow.start())
        time.sleep(1.0)  # no poll in all that time
        self.assertEqual(opened, [])
        seen = flow.poll()
        self.assertEqual((seen["state"], seen["message"], seen["opened"]), (signin.FAILED, signin.NOT_REACHED, False))

    def test_a_browser_slow_to_start_is_not_a_server_that_did_not_answer(self):
        """The limit ends when the server has said yes. Ending the sign-in while the browser opens would leave the
        person signing in to a listener that is gone, and Kaitoi ending the key this computer has."""
        opened = []

        def slow_browser(url):
            time.sleep(0.8)
            opened.append(url)
            return True

        flow = signin.SignIn("https://api.example.test", open_browser=slow_browser, server_signs_in=lambda _base: True, ask_limit=0.2)
        self.addCleanup(flow.cancel)
        self.assertTrue(flow.start())
        time.sleep(0.5)
        self.assertEqual((flow.poll()["state"], flow.poll()["opened"], opened), (signin.WAITING, False, []), "past the limit, the browser still starting")
        until = time.time() + 3
        while not flow.poll()["opened"] and time.time() < until:
            time.sleep(0.02)
        self.assertEqual((flow.poll()["state"], flow.poll()["opened"]), (signin.WAITING, True))

    def test_the_limit_is_on_the_question_not_on_the_person(self):
        flow = self.start(ask_limit=0.2)  # the server said yes at once and the browser is open
        time.sleep(0.5)
        self.assertEqual(flow.poll()["state"], signin.WAITING, "the person has their ten minutes")
        self.assertGreaterEqual(signin.TIMEOUT_SECONDS, 20 * signin.ASK_LIMIT_SECONDS)
        self.assertGreater(signin.ASK_LIMIT_SECONDS, signin.ASK_SECONDS)

    def test_a_browser_that_never_opened_is_not_said_to_be_open(self):
        for more in ({"offered": False}, {"offered": OSError("down")}, {"opens": False}, {"opens": RuntimeError("no browser")}):
            flow = self.start(**more)
            self.assertEqual(self.wait(flow, signin.FAILED)["state"], signin.FAILED, more)
            self.assertFalse(flow.poll()["opened"], more)

    def test_on_a_server_that_does_not_sign_plugins_in_the_browser_is_never_opened(self):
        flow = self.start(offered=False)
        seen = self.wait(flow, signin.FAILED)
        self.assertEqual((seen["state"], seen["message"]), (signin.FAILED, signin.NOT_OFFERED))
        self.assertEqual(self.opened, [], "opening it would start the CLI's sign-in there, and sign the CLI out")
        self.assertIn("API key", seen["message"])

    def test_a_server_that_cannot_be_asked_is_not_opened_either(self):
        for wrong in (OSError("refused"), ValueError("not the settings"), RuntimeError("anything else")):
            flow = self.start(offered=wrong)
            seen = self.wait(flow, signin.FAILED)
            self.assertEqual((seen["state"], seen["message"]), (signin.FAILED, signin.NOT_REACHED), repr(wrong))
            self.assertEqual(self.opened, [])

    def test_starting_never_waits_for_the_server(self):
        release = __import__("threading").Event()

        def slow(_base):
            release.wait(5)
            return True

        opened = []
        flow = signin.SignIn("https://api.example.test", open_browser=opened.append, server_signs_in=slow)
        self.addCleanup(flow.cancel)
        began = time.monotonic()
        self.assertTrue(flow.start())
        self.assertLess(time.monotonic() - began, 0.5, "the window's own thread is never held up")
        self.assertEqual((flow.poll()["state"], opened), (signin.WAITING, []))
        flow.cancel()  # called off while the server is still being asked
        release.set()
        flow._thread.join(timeout=6)
        self.assertEqual(opened, [], "no tab is opened for a sign-in nobody is waiting for")

    def test_an_answer_shaped_like_the_clis_is_refused_whatever_it_holds(self):
        for sent in ({"api_token": "cli-key", "token": "relay-key"},  # what an older server sends: the CLI's two keys, no plugin named
                     {"api_token": "cli-key"},
                     {"api_token": "key", "client": "resolve", "token": "relay-key"},
                     {"api_token": "key", "client": "something-else"},
                     {"api_token": "key", "client": ""},
                     {"error": "access_denied"}):  # a no from a server that never heard of plugins; one that has names the plugin
            flow = self.start()
            _, _, callback = self.parts(flow)
            status, _, _ = get(callback + "?" + urllib.parse.urlencode({"state": flow.secret, "user_login": "jo", **sent}))
            self.assertEqual(status, 303, sent)
            seen = flow.poll()
            self.assertEqual((seen["state"], seen["message"], seen["result"]), (signin.FAILED, signin.NOT_OURS, {}), sent)
            _, _, callback = self.parts(flow)
            _, page, _ = get(callback.replace("/callback", "/done"))
            self.assertIn("Not signed in", page)
            self.assertNotIn("cli-key", page)
            self.assertNotIn("relay-key", page)

    def test_a_plan_without_api_access_is_said_in_plain_words(self):
        flow = self.start()
        self.back(flow, state=flow.secret, error="api_access_unavailable")
        self.assertIn("plan does not include API access", flow.poll()["message"])


class AskingTheServer(unittest.TestCase):
    """The question itself, put to a small server on this computer."""

    def serve(self, body, status=200):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            asked = []

            def do_GET(self):
                Handler.asked.append(self.path)
                data = body if isinstance(body, bytes) else __import__("json").dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", Handler

    def test_a_server_that_lists_this_plugin_says_yes(self):
        base, handler = self.serve({"password": True, "github": False, "google": True, "plugins": ["resolve"]})
        self.assertTrue(signin.server_signs_in(base + "/"))
        self.assertEqual(handler.asked, ["/auth/config"])

    def test_one_that_does_not_says_no(self):
        for settings in ({"password": True, "github": False, "google": True},  # a server from before plugins could sign in
                         {"password": True, "plugins": []}, {"password": True, "plugins": ["another-plugin"]},
                         {"plugins": "resolve"}, {"plugins": None}, {"plugins": {"resolve": True}}):
            base, _ = self.serve(settings)
            self.assertFalse(signin.server_signs_in(base), settings)

    def test_an_answer_that_is_not_the_settings_is_an_error_not_a_yes(self):
        for body in (b"<html>a sign-in page</html>", ["resolve"], "resolve"):
            base, _ = self.serve(body)
            with self.assertRaises(ValueError):
                signin.server_signs_in(base)
        base, _ = self.serve({"detail": "Not Found"}, status=404)
        with self.assertRaises(OSError):
            signin.server_signs_in(base)

    def test_an_answer_far_too_long_to_be_the_settings_is_not_read_to_its_end(self):
        base, _ = self.serve({"plugins": ["resolve"], "padding": "x" * (signin.ASK_BYTES - 200)})
        self.assertTrue(signin.server_signs_in(base), "up to the limit it is read")
        base, _ = self.serve({"plugins": ["resolve"], "padding": "x" * (signin.ASK_BYTES + 1)})
        with self.assertRaises(ValueError):
            signin.server_signs_in(base)

    def small_server(self, respond, threaded=False):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                respond(self)

            def log_message(self, *args):
                pass

        server = (http.server.ThreadingHTTPServer if threaded else http.server.HTTPServer)(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def redirecting(self, to, status=302):
        def respond(handler):
            if handler.path != "/auth/config":
                data = b'{"plugins": ["resolve"]}'
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
                return
            handler.send_response(status)
            handler.send_header("Location", to)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        return self.small_server(respond)

    def test_another_servers_yes_is_not_this_servers(self):
        """The browser is opened on the server that was asked, so only its own answer counts."""
        elsewhere, handler = self.serve({"plugins": ["resolve"]})
        self.assertTrue(signin.server_signs_in(elsewhere))
        del handler.asked[:]
        with self.assertRaises(OSError):
            signin.server_signs_in(self.redirecting(elsewhere + "/auth/config"))
        self.assertEqual(handler.asked, [], "the other server was not asked on this one's behalf")
        # The same computer under another name or scheme is another server too.
        for other in (elsewhere.replace("127.0.0.1", "localhost"), elsewhere.replace("http://", "https://")):
            with self.assertRaises(OSError):
                signin.server_signs_in(self.redirecting(other + "/auth/config"))
        self.assertEqual(handler.asked, [])

    def test_what_counts_as_the_same_server(self):
        self.assertNotEqual(signin._origin("http://api.example.test:8443/"), signin._origin("https://api.example.test:8443/"), "same name, same port, another scheme")
        same = signin._origin("https://API.example.test/auth/config")
        self.assertEqual(same, signin._origin("https://api.example.test:443/elsewhere?x=1"))
        self.assertEqual(signin._origin("http://api.example.test"), signin._origin("http://api.example.test:80/"))
        for other in ("http://api.example.test/auth/config", "https://api.example.test:8443/auth/config", "https://www.example.test/auth/config",
                      "https://api.example.test.evil.example/auth/config", "https://localhost/auth/config", "//api.example.test/auth/config", "/auth/config"):
            self.assertNotEqual(same, signin._origin(other), other)

    def test_the_same_port_under_another_name_or_scheme_is_not_asked_again(self):
        """Only the port is the same: the name and the scheme are part of which server it is."""
        asked = []

        def respond(handler):
            asked.append(handler.path)
            port = handler.server.server_address[1]
            if handler.path == "/auth/config":
                handler.send_response(302)
                handler.send_header("Location", self.to.format(port=port))
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            data = b'{"plugins": ["resolve"]}'
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)

        base = self.small_server(respond)
        for self.to in ("http://localhost:{port}/moved", "https://127.0.0.1:{port}/moved"):
            del asked[:]
            with self.assertRaises(OSError):
                signin.server_signs_in(base, timeout=2)
            self.assertEqual(asked, ["/auth/config"], self.to)
        self.to = "http://127.0.0.1:{port}/moved"
        self.assertTrue(signin.server_signs_in(base), "the same server under the same name is followed")

    def test_a_redirect_within_the_same_server_is_followed(self):
        self.assertTrue(signin.server_signs_in(self.redirecting("/v2/auth/config", status=307)))

    def test_an_answer_that_trickles_in_is_given_up_on(self):
        def respond(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "4000")
            handler.end_headers()
            try:
                for _ in range(40):
                    handler.wfile.write(b" ")
                    handler.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass

        began = time.monotonic()
        with self.assertRaises(OSError):
            signin.server_signs_in(self.small_server(respond, threaded=True), timeout=0.5)
        self.assertLess(time.monotonic() - began, 2.0, "not the four seconds the answer would take, let alone for ever")
        with self.assertRaises(OSError):
            signin.server_signs_in("http://127.0.0.1:9", timeout=2)


if __name__ == "__main__":
    unittest.main()
