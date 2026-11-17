"""The Kaitoi window, built with Fusion's UIManager.

A narrow palette, one column, sized to sit beside the Inspector. Four tabs:

- **Workflows** and **Nodes**: search, filters, and the list of what the
  account can run, grouped by category, with a card describing the chosen
  one. The two share one browsing view.
- **Run**, reached from that card and not a tab of its own: what the tool
  will take from the timeline, a form drawn from its inputs, Generate, and
  progress.
- **History** and **Account**.

Resolve gives scripts a floating window only; nothing a script opens can be
docked. So this window is styled to match Resolve's own panels (its greys, its
field and list treatment, its red tab underline), stays on top, and remembers
where it was left.

The window is pumped by our own loop (``disp.StepLoop()`` + a short sleep)
rather than ``disp.RunLoop()``: in Resolve 21 a ``ui.Timer`` never fires
inside ``RunLoop`` for a Python script, so nothing could update the panel or
notice Cancel. Each pass of the loop dispatches pending UI events, advances
the job (export polling and the final import happen here, on the thread that
owns Resolve), picks up what the background loader fetched, repaints, and
sleeps briefly so the network threads get the GIL.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from typing import Any

from . import api, busy, config, credentials, history, pipeline, resolve_bridge, signin, templates, tools, uninstall

WINDOW_ID = "KaitoiResolveWin"
DEFAULT_GEOMETRY = [120, 110, 440, 780]
# Loop cadence. Idle: dispatch events every 50 ms. While a network thread
# runs: sleep longer, since ``time.sleep`` releases the GIL and that is when
# the worker thread actually gets to execute Python.
IDLE_SLEEP_SECONDS = 0.05
WORKER_YIELD_SECONDS = 0.1
FOLLOW_SECONDS = 1.0  # how often the window looks at what the editor selected
SPINNER = "◐◓◑◒"  # renders clearly in Resolve's UI font; braille glyphs come out as specks
MAX_ROWS = 8  # form rows built up front; UIManager cannot add widgets later
FORM_PROMPT, FORM_PLAIN = "P", "N"  # the form with the large text box, and the one without
PICKED = "#picked"  # in a form's remembered values: the position chosen in each picker (no input can have this name)
LABEL_WIDTH = 104
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
GENERIC_ICONS = {"video": "film", "image": "image", "audio": "audio-lines", "text": "file-text"}
ICON_NAME = re.compile(r"[a-z0-9-]{1,60}")  # an icon name is used as a file name: nothing else is let through

VIEW_BROWSE, VIEW_RUN, VIEW_HISTORY, VIEW_ACCOUNT = 0, 1, 2, 3
VIEW_UNINSTALL = 4  # asked for from the Account tab; once the uninstaller has started, no tab leads away
# What stands under the message on that view: the question's buttons, nothing while macOS asks for the password, then the way out.
STAGE_ASK, STAGE_WAIT, STAGE_GONE = 0, 1, 2
WORKFLOWS, NODES = "workflows", "nodes"
# (button, title, view, catalog): the two catalogs share one browsing view.
NAV = [
    ("NavWorkflows", "Workflows", VIEW_BROWSE, WORKFLOWS),
    ("NavNodes", "Nodes", VIEW_BROWSE, NODES),
    ("NavHistory", "History", VIEW_HISTORY, ""),
    ("NavAccount", "Account", VIEW_ACCOUNT, ""),
]
LIST_CAP = 100  # rows drawn at once; a list item costs several calls into Resolve
# Installing many nodes asks the server for each one's inputs. It allows 120
# requests a minute from one address, shared with everything else the window
# does, so the installer keeps to about 80.
BULK_PAUSE_SECONDS = 0.75
STATE_PAUSE_SECONDS = 0.6  # a node must stay picked this long before its lists are asked for
SEARCH_PAUSE_SECONDS = 0.25  # the list waits for typing to stop before it redraws
ORIGINS = [("builtin", "OriginBuiltin", "Built-in"), ("community", "OriginCommunity", "Community"), ("user", "OriginUser", "User")]
BAR_HEIGHT = 4  # the progress bar under every view while a run is on
# The width of the card that is not its name's, measured on screen: the
# window's two margins, the picture (128), the Favorite button (76) and the
# gaps between them.
CARD_TAKEN = 244
SIGN_IN, SIGN_IN_WAITING = "Sign in with Kaitoi", "Cancel sign-in"
SIGN_IN_ASKING = "Starting the sign-in with Kaitoi..."
SIGN_IN_OPENED = "Your browser has opened Kaitoi. Sign in there, then come back here."
SIGN_IN_BUSY = "Something is still running with the key in use. Sign in again when it has finished."
SIGN_IN_ENDED = "This computer's sign-in has ended or was replaced. Press Sign in with Kaitoi again."
SIGN_IN_FIRST = "A sign-in is waiting in your browser. Finish it or cancel it first."
SIGN_IN_WHAT = "a sign-in"  # what the other Kaitoi scripts are told Kaitoi is busy with meanwhile
FAVORITE_OFF, FAVORITE_ON = "☆ Favorite", "★ Favorite"  # the same width, so the button does not move
FAVORITE_MARK = "★ "  # before the name of a favorite in the list

SOURCE_LABELS = [
    ("auto", "Auto"),
    ("playhead", "Clip under playhead"),
    ("selection", "Selected clip"),
    ("marks", "In/Out range"),
]
PLACEMENT_LABELS = [
    ("new_track", "A Kaitoi track, at the source"),
    ("append", "The end of the timeline"),
    ("pool", "The media pool only"),
]
QUALITIES = ["Least", "Low", "Medium", "High", "Best"]
SENDS = {
    "video": "Sends the range as a small video{sound}.",
    "image": "Sends the frame under the playhead.",
    "audio": "Sends the sound of the range.",
}
NEEDS = {"video": "a clip", "image": "a frame", "audio": "sound"}

# ── Look: Resolve's own palette ─────────────────────────────────────────────

C_TEXT = "#dedede"
C_DIM = "#8b8b93"
C_FIELD = "#1b1b1f"
C_LINE = "#3a3a42"
C_SELECT = "#3d3d48"
C_ACCENT = "#e64b3d"  # the red under Resolve's active tab

STYLE_FIELD = (
    f"QLineEdit, QTextEdit {{ background: {C_FIELD}; color: {C_TEXT}; border: 1px solid {C_LINE};"
    " border-radius: 4px; padding: 5px 7px; selection-background-color: #5b5b68; }"
    " QLineEdit:focus, QTextEdit:focus { border: 1px solid #70707c; }"
)
STYLE_LIST = (
    f"QTreeWidget {{ background: {C_FIELD}; color: {C_TEXT}; border: 1px solid #17171a; border-radius: 4px; outline: 0; }}"
    " QTreeWidget::item { padding: 3px 2px; border: 0; }"
    f" QTreeWidget::item:selected {{ background: {C_SELECT}; color: #ffffff; }}"
    " QTreeWidget::item:hover:!selected { background: #26262c; }"
    f" QHeaderView::section {{ background: #232328; color: {C_DIM}; border: 0; padding: 4px 6px; }}"
)
STYLE_PRIMARY = (
    "QPushButton { background: #50505a; color: #ffffff; border: 1px solid #62626e; border-radius: 5px;"
    " padding: 7px 14px; font-weight: 600; }"
    " QPushButton:hover { background: #5d5d69; }"
    " QPushButton:pressed { background: #44444d; }"
    f" QPushButton:disabled {{ background: #2e2e34; color: #6a6a72; border: 1px solid {C_LINE}; }}"
)
STYLE_QUIET = (
    f"QPushButton {{ background: transparent; color: {C_DIM}; border: 1px solid {C_LINE}; border-radius: 5px; padding: 5px 12px; }}"
    f" QPushButton:hover {{ color: {C_TEXT}; border: 1px solid #5a5a66; }}"
    " QPushButton:disabled { color: #55555c; border: 1px solid #303036; }"
)
STYLE_SECOND = (
    f"QPushButton {{ background: #34343b; color: {C_TEXT}; border: 1px solid #4a4a55; border-radius: 5px; padding: 5px 10px; }}"
    " QPushButton:hover { background: #3e3e46; border: 1px solid #5f5f6b; }"
    " QPushButton:pressed { background: #2c2c32; }"
    " QPushButton:disabled { background: transparent; color: #5c5c64; border: 1px solid #303036; }"
)
STYLE_SMALL = (
    f"QPushButton {{ background: #303037; color: #b4b4bc; border: 1px solid #44444e; border-radius: 4px; padding: 2px 9px; font-size: 11px; }}"
    f" QPushButton:hover {{ color: {C_TEXT}; border: 1px solid #5a5a66; }}"
    " QPushButton:disabled { background: transparent; color: #55555c; border: 1px solid #303036; }"
)
STYLE_SLIDER = (
    "QSlider::groove:horizontal { height: 4px; background: #17171a; border: 1px solid #3a3a42; border-radius: 3px; }"
    " QSlider::sub-page:horizontal { background: #6f6f7c; border-radius: 3px; }"
    " QSlider::handle:horizontal { background: #d0d0d6; width: 12px; margin: -5px 0; border-radius: 6px; }"
    " QSlider::handle:horizontal:hover { background: #ffffff; }"
)
STYLE_LINK = (
    f"QPushButton {{ background: transparent; color: {C_DIM}; border: 0; padding: 2px 0; text-align: left; }}"
    f" QPushButton:hover {{ color: {C_TEXT}; }}"
)
STYLE_TITLE = "color: #f2f2f2; font-size: 15px; font-weight: 600;"
STYLE_DIM = f"color: {C_DIM}; font-size: 11px;"
STYLE_BODY = f"color: {C_TEXT};"
STYLE_NOTE = "color: #d6b36a; font-size: 11px;"
STATE_TEXT = {templates.READY: "In Resolve", templates.RESTART: "Restart", templates.NOT_INSTALLED: ""}
STATE_COLOR = {
    templates.READY: {"R": 0.42, "G": 0.72, "B": 0.48, "A": 1.0},
    templates.RESTART: {"R": 0.84, "G": 0.70, "B": 0.42, "A": 1.0},
}
STATE_LINE = {
    templates.READY: "<span style='color:#6bb87a'>● In Resolve.</span> {use}",
    templates.RESTART: "<span style='color:#d6b36a'>● Installed.</span> Restart Resolve to use it on clips.",
    templates.NOT_INSTALLED: "<span style='color:#8b8b93'>○ Not in Resolve yet.</span> Use installs it.",
}
STYLE_BANNER = "background: #3a3222; color: #e6c98a; border: 1px solid #5a4c2c; border-radius: 5px; padding: 6px 9px; font-size: 11px;"
STYLE_CARD = "background: #232329; border: 1px solid #303038; border-radius: 6px;"


TIP_LETTERS = 80  # a tooltip is a hint, read at a glance: longer text belongs on the card


def short_tip(text: str) -> str:
    """The first sentence of a description, cut to a line: what a tooltip has room for."""
    words = " ".join(str(text or "").split())
    first = re.split(r"(?<=[.!?])\s", words, maxsplit=1)[0]
    if len(first) <= TIP_LETTERS:
        return first
    return first[: TIP_LETTERS - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


# The share of a run each step usually is: Export, Upload, Prepare, Run,
# Download, Place. The run itself is most of the wait, so it is most of the bar.
STAGE_SHARES = (0.10, 0.10, 0.05, 0.60, 0.10, 0.05)


def overall_progress(stage: int, fraction: float | None) -> float:
    """How much of a whole run is behind it, 0 to 1: the steps done, plus as
    much of the step it is on as Kaitoi says is done."""
    if stage < 0:
        return 0.0
    if stage >= len(STAGE_SHARES):
        return 1.0
    inside = max(0.0, min(1.0, float(fraction))) if fraction is not None else 0.0
    return max(0.0, min(1.0, sum(STAGE_SHARES[:stage]) + STAGE_SHARES[stage] * inside))


def bar_style(done: float) -> str:
    """A thin bar filled from the left as far as ``done`` (0 to 1). This UI
    kit has no progress bar of its own: a label painted in two colors is one."""
    at = max(0.0, min(1.0, done))
    if at <= 0.0:
        return f"background: {C_LINE}; border-radius: 2px;"
    if at >= 1.0:
        return f"background: {C_ACCENT}; border-radius: 2px;"
    return (f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {C_ACCENT}, stop:{at:.3f} {C_ACCENT}, "
            f"stop:{min(1.0, at + 0.001):.3f} {C_LINE}, stop:1 {C_LINE}); border-radius: 2px;")


UNINSTALL_ASK = (
    "Uninstall Kaitoi?\n\n"
    "This removes Kaitoi from your DaVinci Resolve, and its code from this Mac, so it stops working for every account. "
    "macOS asks for your password.\n\n"
    "Your results and your Kaitoi sign-in stay."
)
UNINSTALL_WHAT = "the uninstaller"  # what the other Kaitoi scripts are told Kaitoi is busy with meanwhile
UNINSTALLING = "Removing Kaitoi. macOS asks for your password."
UNINSTALLED = "Kaitoi was removed from this Mac.\nRestart DaVinci Resolve to clear it from the menus."


def _nav_style(active: bool) -> str:
    color, line = ("#ffffff", C_ACCENT) if active else (C_DIM, "transparent")
    return (
        f"QPushButton {{ background: transparent; color: {color}; border: 0; border-bottom: 2px solid {line};"
        " border-radius: 0; padding: 7px 4px 6px 4px; font-size: 13px; }"
        " QPushButton:hover { color: #ffffff; }"
    )


def _open_path(path: str) -> None:
    """Hand a file to the OS viewer/player."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:  # noqa: BLE001
        config.log(f"could not open {path}: {exc}", "WARN")


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _icon_tile(name: str) -> str:
    """The bundled tile for a node's icon, or "" when there is none (a name
    the set does not have, or a provider's logo, which is not bundled)."""
    if not name or not ICON_NAME.fullmatch(name):
        return ""
    path = os.path.join(ASSETS, "icons", f"{name}.png")
    return path if os.path.isfile(path) else ""


def fit_columns(first: int, second: int, window_width: int) -> tuple:
    """The first two list columns, cut back when they would leave the third no
    room for its heading; never made wider than asked. The numbers are the
    window's margins, the list's scrollbar and that heading."""
    room = window_width - 40 - 18 - 84
    if first + second <= room:
        return first, second
    second = min(second, max(80, room // 3))
    return min(first, max(120, room - second)), second


def _placeholder(tool: tools.Tool) -> str:
    """A stand-in picture for a tool with no thumbnail: a node's own icon when
    the plugin has it, else a plain icon for what the node makes. A workflow
    gets a tile coloured by what it makes.

    A node whose pins are not loaded yet is judged by its category, which for
    model nodes names the result last ("Image to Video").
    """
    tile = _icon_tile(tool.icon)
    if tile:
        return tile
    made = tool.media_outputs[0].media if tool.media_outputs else "text"
    if not tool.media_outputs and tool.source == "node":
        words = tool.category.lower()
        spots = {
            "video": words.rfind("video"),
            "image": words.rfind("image"),
            "audio": max(words.rfind(word) for word in ("audio", "speech", "music", "voice", "sound")),
        }
        best = max(spots, key=lambda kind: spots[kind])
        made = best if spots[best] >= 0 else "text"
    if tool.source == "node":
        # Its own icon is one the plugin does not carry (a provider's logo, an
        # icon from another set): a plain icon for what it makes, so every
        # node in the list has one.
        generic = _icon_tile(GENERIC_ICONS.get(made, ""))
        if generic:
            return generic
    return os.path.join(ASSETS, f"placeholder_{made}.png")


class Loader:
    """Fetches the workflow list, the balance and thumbnails off the UI thread.

    The UI thread starts it and later collects what it produced with
    :meth:`take`; the two only meet under the lock.
    """

    def __init__(self, cfg: api.Config, known: dict | None) -> None:
        self.cfg = cfg
        self.known = known
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._tools: list | None = None
        self._snapshot: dict | None = None
        self._error: str | None = None
        self._balance: str | None = None
        self._thumbnails: list = []
        self._nodes: list | None = None
        self.done = False
        self._thread = threading.Thread(target=self._run, name="kaitoi-loader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        try:
            try:
                found, snapshot = tools.load_workflow_tools(self.cfg, self.known, should_stop=self._stop.is_set)
                with self._lock:
                    self._tools, self._snapshot = found, snapshot
            except api.KaitoiAPIError as exc:
                if exc.code != "ABORTED":
                    with self._lock:
                        self._error = str(exc)
                found = []
            try:
                balance = api.balance_text(api.account_credits(self.cfg))
                if balance:
                    with self._lock:
                        self._balance = balance
            except api.KaitoiAPIError:
                pass  # the key may not carry account_credits:read; the list still works
            for tool in found:
                if self._stop.is_set():
                    break
                path = tools.fetch_thumbnail(self.cfg, tool)
                if path:
                    with self._lock:
                        self._thumbnails.append((tool.id, path))
            if not self._stop.is_set():
                tools.drop_replaced_thumbnails(found)
            nodes = []
            try:
                nodes = tools.load_node_catalog(self.cfg, should_stop=self._stop.is_set)
                with self._lock:
                    self._nodes = nodes
            except api.KaitoiAPIError as exc:
                if exc.code != "ABORTED":
                    config.log(f"could not load the node catalog: {exc}", "WARN")
            # Few nodes have a thumbnail. Kaitoi's own and the community's come
            # first: an account's own nodes are listed only when asked for.
            pictured = sorted((node for node in nodes if node.thumbnail_url), key=lambda node: node.origin == "user")
            for node in pictured:
                if self._stop.is_set() or tools.renditions_unavailable():
                    break
                path = tools.fetch_thumbnail(self.cfg, node)
                if path:
                    with self._lock:
                        self._thumbnails.append((node.id, path))
        except Exception as exc:  # noqa: BLE001 - a loader thread must never die silently
            config.log(f"workflow loader crashed: {exc}", "ERROR")
            with self._lock:
                self._error = f"Could not load workflows: {exc}"
        finally:
            self.done = True

    def take(self) -> dict[str, Any]:
        """Everything new since the last call. UI thread only."""
        with self._lock:
            out = {
                "tools": self._tools,
                "snapshot": self._snapshot,
                "error": self._error,
                "balance": self._balance,
                "thumbnails": self._thumbnails,
                "nodes": self._nodes,
            }
            self._tools = self._snapshot = self._error = self._balance = self._nodes = None
            self._thumbnails = []
        return out


class Panel:
    def __init__(self, session: resolve_bridge.Session) -> None:
        self.session = session
        self.settings = config.load()
        self.source: dict[str, Any] | None = None
        self.job: pipeline.Job | None = None
        self.loader: Loader | None = None
        self.last_result_path: str | None = None

        self.workflow_tools = tools.tools_from_snapshot(tools.read_snapshot(self._base_url(), self._account()))
        self.node_tools = tools.read_nodes(self._base_url(), self._account())
        self.catalog = NODES if str(self.settings.get("last_tool", "")).startswith("node:") else WORKFLOWS
        self._node_fetch: dict[str, Any] = {}  # node id -> {"thread", "tool", "error"}
        self._node_lock = threading.Lock()
        self._installed: dict[str, dict[str, Any]] = templates.installed()  # what Kaitoi has put in Resolve
        # What Resolve has loaded; None when this Resolve cannot say.
        self._registered: set | None = resolve_bridge.registered_effects(session, templates.FOLDER)
        self._bulk: dict[str, Any] | None = None  # an "install all" in progress
        self._uninstaller = uninstall.script()  # None where this copy of the plugin came without one
        self._uninstall: dict[str, Any] | None = None  # the uninstaller at work: {"done", "code", "left"}
        self._removed = False  # Kaitoi was uninstalled from this window: it only waits to be closed
        self._shown: dict[tuple[str, str], Any] = {}  # (widget, property) -> the value last put there
        self._list_signature: Any = None
        self.rebuilds = 0  # how many times the list was really redrawn
        self._search_due = 0.0
        self._source_identity: Any = None
        self.tool: tools.Tool | None = None
        self.balance = ""
        self.load_error = ""
        self.view = VIEW_BROWSE
        self._tool_items: dict[str, Any] = {}
        self._rows: list[tuple[tools.Field, str]] = []  # (field, widget kind) per visible row
        self._prompt_field: tools.Field | None = None
        self._overflow: list[tools.Field] = []
        self._form_tool_id = ""
        self._show_keys: list[str] = ["task:"]
        self._chosen: dict[str, str] = {}  # the tool last picked on each tab
        self._history_signature: tuple | None = None  # the history rows on screen
        self._tiles: dict[str, str] = {}  # stored thumbnail -> its 16:9 picture, "" while there is none
        # Nodes that fill in their own lists (voices fetched from a provider):
        # the lists are asked for when such a node is picked, once a session.
        self._states: dict[str, Any] = {}  # tool id -> what its node filled in
        self._state_fetch: dict[str, Any] = {}  # tool id -> {"state", "error"} while and after it is asked for
        self._state_tried: set = set()
        self._state_due: tuple | None = None  # (tool id, when): a pick waits a moment before it asks
        self._state_retry: set = set()  # asked, and the server was busy: ask again when the user next picks it
        self._state_account = 0  # goes up when the key or the server changes: answers asked for before are dropped
        self._form_tool: tools.Tool | None = None  # the tool as it was when the form's boxes were last filled
        self._late_lists: tuple | None = None  # (tool id, the tool before): lists that came while its form was off screen
        self._held_picks: dict = {}  # field name -> (entry, position) remembered from a picker whose list is not loaded
        self._unmatched: list = []  # (field label, text) typed into a box that became a picker without that entry
        self._form = FORM_PLAIN  # which of the two forms the Run view is showing
        widths = self.settings.get("list_columns")
        self._columns: list = list(widths) if isinstance(widths, list) and len(widths) == 3 else [204, 116, 64]
        self._columns_set = False
        self._columns_drawn: tuple | None = None  # the widths last put on the list, cut to fit the window
        self._columns_width = 0  # the window width they were fitted to
        self._columns_checked = 0.0
        self._history_rows: list[dict] = []
        self._updating = False
        self._follow_signature: Any = None
        self._follow_at = 0.0
        self._listed_supplies: frozenset | None = None
        self._owner = uuid.uuid4().hex  # this window, to the one-run-at-a-time rule

        self.ui, self.disp = session.ui_manager()
        self.spinner_index = 0
        self.closed = False
        geometry = self.settings.get("panel_geometry")
        if not (isinstance(geometry, list) and len(geometry) == 4 and all(isinstance(v, (int, float)) for v in geometry)):
            geometry = list(DEFAULT_GEOMETRY)
        self._asked_geometry = [int(v) for v in geometry]
        window: dict[str, Any] = {"ID": WINDOW_ID, "WindowTitle": "Kaitoi", "Geometry": self._asked_geometry}
        if self.settings.get("keep_on_top", True):
            window["WindowFlags"] = {"Window": True, "WindowStaysOnTopHint": True}
        self.win = self.disp.AddWindow(window, self._layout())
        self.items = self.win.GetItems()
        self._shown_geometry: list[int] | None = None
        self._bind()
        self._load_settings_into_ui()
        self._refresh_history()
        self.refresh_source(quiet=True)
        self._rebuild_show_filter()
        self._rebuild_list()
        self._select_tool(str(self.settings.get("last_tool", "")))
        self._refresh_installed(redraw=False)
        self._set_view(VIEW_BROWSE)
        self._render_progress(None)
        self.reload_workflows(quiet=True)

    def _base_url(self) -> str:
        return str(self.settings.get("base_url") or credentials.DEFAULT_BASE_URL).rstrip("/")

    def _account(self) -> str:
        """The mark of the account in use (see :func:`tools.account_mark`)."""
        return tools.account_mark(self.settings.get("api_key"))

    # ── layout ──────────────────────────────────────────────────────────────

    def _layout(self):
        ui = self.ui
        nav = [
            ui.Button({"ID": button_id, "Text": title, "Flat": True, "Weight": 0.0, "StyleSheet": _nav_style(False)})
            for button_id, title, _, _ in NAV
        ]
        return ui.VGroup(
            {"Spacing": 8},
            [
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 16},
                    [*nav, ui.HGap(0, 1.0), ui.Label({"ID": "RestartNote", "Text": "", "Weight": 0.0, "StyleSheet": STYLE_NOTE})],
                ),
                ui.Stack(
                    {"ID": "Views", "Weight": 1.0},
                    [self._browse_view(), self._run_view(), self._history_view(), self._account_view(), self._uninstall_view()],
                ),
                # Under every view, so a run can be followed from the list as well.
                ui.Label({"ID": "Progress", "Text": "", "Weight": 0.0, "Visible": False, "MinimumSize": [0, 0], "MaximumSize": [16777215, 0], "StyleSheet": bar_style(0)}),
                ui.Label({"ID": "Status", "Text": "", "Weight": 0.0, "WordWrap": True, "StyleSheet": STYLE_DIM}),
            ],
        )

    def _browse_view(self):
        ui = self.ui
        return ui.VGroup(
            {"Spacing": 8},
            [
                ui.LineEdit(
                    {
                        "ID": "Search",
                        "PlaceholderText": "Search workflows",
                        "ClearButtonEnabled": True,
                        "Weight": 0.0,
                        "StyleSheet": STYLE_FIELD,
                    }
                ),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 8},
                    [
                        ui.Label({"Text": "Show", "Weight": 0.0, "MinimumSize": [34, 0], "StyleSheet": STYLE_DIM}),
                        ui.ComboBox({"ID": "Show", "Weight": 1.0}),
                    ],
                ),
                # A hidden row keeps its space in this UI kit, so the two catalogs
                # share one row with a page each instead of showing and hiding rows.
                ui.Stack(
                    {"ID": "FilterRow", "Weight": 0.0},
                    [
                        ui.HGroup(
                            {"Spacing": 12},
                            [ui.CheckBox({"ID": "FitsOnly", "Text": "Only what fits the timeline selection", "Weight": 0.0}), ui.HGap(0, 1.0)],
                        ),
                        ui.HGroup(
                            {"Spacing": 12},
                            [
                                *[ui.CheckBox({"ID": box_id, "Text": label, "Weight": 0.0}) for _, box_id, label in ORIGINS],
                                ui.CheckBox({"ID": "InstalledOnly", "Text": "Installed only", "Weight": 0.0}),
                                ui.HGap(0, 1.0),
                            ],
                        ),
                    ],
                ),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 12},
                    [
                        ui.CheckBox({"ID": "MediaOnly", "Text": "Works in Resolve", "Weight": 0.0,
                                     "ToolTip": "Results Resolve can place, from inputs it can supply."}),
                        ui.CheckBox({"ID": "FavoritesOnly", "Text": "Favorites only", "Weight": 0.0,
                                     "ToolTip": "Only the ones you marked as Favorite."}),
                        ui.HGap(0, 1.0),
                    ],
                ),
                ui.Tree(
                    {
                        "ID": "Tools",
                        "Weight": 1.0,
                        "HeaderHidden": False,  # the header is what lets a column be dragged wider
                        "RootIsDecorated": False,
                        "Indentation": 0,
                        "IconSize": [64, 36],
                        "UniformRowHeights": False,
                        "StyleSheet": STYLE_LIST,
                    }
                ),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 10},
                    [
                        ui.Button(
                            {
                                "ID": "Thumb",
                                "Text": "",
                                "Flat": True,
                                "IconSize": [128, 72],
                                "MinimumSize": [128, 72],
                                "MaximumSize": [128, 72],
                                "Weight": 0.0,
                                "StyleSheet": "QPushButton { border: 0; padding: 0; background: #17171a; border-radius: 4px; }",
                            }
                        ),
                        ui.VGroup(
                            {"Weight": 1.0, "Spacing": 2},
                            [
                                # From the top: where more room is kept than the text takes, it is left under it, not around it.
                                ui.Label({"ID": "DetailName", "Text": "", "Weight": 0.0, "WordWrap": True, "Alignment": {"AlignTop": True, "AlignLeft": True}, "StyleSheet": STYLE_TITLE}),
                                ui.Label({"ID": "DetailTask", "Text": "", "Weight": 0.0, "WordWrap": True, "Alignment": {"AlignTop": True, "AlignLeft": True}, "StyleSheet": STYLE_DIM}),
                                ui.VGap(0, 1.0),
                            ],
                        ),
                        ui.VGroup(
                            {"Weight": 0.0},
                            [ui.Button({"ID": "Favorite", "Text": FAVORITE_OFF, "Weight": 0.0, "Enabled": False, "StyleSheet": STYLE_SMALL}), ui.VGap(0, 1.0)],
                        ),
                    ],
                ),
                ui.Label(
                    {
                        "ID": "DetailText",
                        "Text": "Pick a workflow to see what it does.",
                        "Weight": 0.0,
                        "WordWrap": True,
                        "MinimumSize": [0, 52],
                        "Alignment": {"AlignTop": True, "AlignLeft": True},
                        "StyleSheet": STYLE_BODY,
                    }
                ),
                ui.Label({"ID": "DetailState", "Text": "", "Weight": 0.0, "WordWrap": True, "StyleSheet": "font-size: 11px;"}),
                ui.Button({"ID": "Use", "Text": "Use this workflow", "Weight": 0.0, "Enabled": False, "StyleSheet": STYLE_PRIMARY}),
                # A workflow is always in Resolve, so its card has one secondary
                # action; a node has two. A hidden button keeps its space in this
                # UI kit, hence a page for each.
                ui.Stack(
                    {"ID": "CardActions", "Weight": 0.0},
                    [
                        ui.HGroup(
                            {"Spacing": 8},
                            [ui.Button({"ID": "RunHereWide", "Text": "Run from this window", "Weight": 1.0, "Enabled": False, "StyleSheet": STYLE_SECOND})],
                        ),
                        ui.HGroup(
                            {"Spacing": 8},
                            [
                                ui.Button({"ID": "RunHere", "Text": "Run from this window", "Weight": 1.0, "Enabled": False, "StyleSheet": STYLE_SECOND}),
                                ui.Button({"ID": "Install", "Text": "Install in Resolve", "Weight": 1.0, "Enabled": False, "StyleSheet": STYLE_SECOND}),
                            ],
                        ),
                    ],
                ),
                ui.VGap(2),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 8},
                    [
                        ui.Label({"ID": "ListInfo", "Text": "", "Weight": 1.0, "StyleSheet": STYLE_DIM}),
                        ui.Button({"ID": "InstallAll", "Text": "All installed", "Weight": 0.0, "Enabled": False, "StyleSheet": STYLE_SMALL}),
                        ui.Button({"ID": "Reload", "Text": "Reload", "Weight": 0.0, "StyleSheet": STYLE_SMALL}),
                    ],
                ),
            ],
        )

    def _form_rows(self, form: str) -> list:
        """The rows of one form. ``form`` prefixes every widget's id."""
        ui = self.ui
        rows = []
        for index in range(MAX_ROWS):
            rows.append(
                ui.HGroup(
                    {"ID": f"{form}Row{index}", "Weight": 0.0, "Visible": False, "Spacing": 8},
                    [
                        ui.Label(
                            {
                                "ID": f"{form}RowLabel{index}",
                                "Text": "",
                                "Weight": 0.0,
                                "MinimumSize": [LABEL_WIDTH, 0],
                                "MaximumSize": [LABEL_WIDTH, 16777215],
                                "Alignment": {"AlignRight": True, "AlignVCenter": True},
                                "StyleSheet": f"color: {C_DIM};",
                            }
                        ),
                        # One editor per kind of value. A hidden widget in a row
                        # still takes its share of the width, so they are stacked.
                        ui.Stack(
                            {"ID": f"{form}RowStack{index}", "Weight": 1.0},
                            [
                                ui.LineEdit({"ID": f"{form}RowEdit{index}", "StyleSheet": STYLE_FIELD}),
                                ui.ComboBox({"ID": f"{form}RowCombo{index}"}),
                                ui.HGroup(
                                    {"Spacing": 6},
                                    [
                                        ui.LineEdit({"ID": f"{form}RowPath{index}", "Weight": 1.0, "StyleSheet": STYLE_FIELD}),
                                        ui.Button({"ID": f"{form}RowBrowse{index}", "Text": "Choose...", "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                                    ],
                                ),
                                ui.HGroup(
                                    {"Spacing": 8},
                                    [
                                        # Shares of the row, not fixed widths: a fixed width beside
                                        # a slider pushed the box past the edge of the window.
                                        ui.Slider({"ID": f"{form}RowSlider{index}", "Weight": 4.0, "Orientation": "Horizontal", "StyleSheet": STYLE_SLIDER}),
                                        ui.LineEdit({"ID": f"{form}RowValue{index}", "Weight": 1.0, "StyleSheet": STYLE_FIELD}),
                                    ],
                                ),
                            ],
                        ),
                    ],
                )
            )
        return rows

    def _run_view(self):
        ui = self.ui
        # Two forms, one with the large text box and one without, each with its
        # own rows. A hidden widget keeps its space in this UI kit, so hiding
        # the text box left a blank band in the middle of the form. In either
        # form the rows not in use are the last ones, where the space they
        # keep is only the empty bottom of the window.
        forms = ui.Stack(
            {"ID": "Forms", "Weight": 1.0},
            [
                ui.VGroup(
                    {"Spacing": 6},
                    [
                        ui.Label({"ID": "PromptLabel", "Text": "Prompt", "Weight": 0.0, "StyleSheet": f"color: {C_DIM};"}),
                        ui.TextEdit({"ID": "Prompt", "Weight": 0.0, "MinimumSize": [0, 84], "MaximumSize": [16777215, 84], "StyleSheet": STYLE_FIELD}),
                        *self._form_rows(FORM_PROMPT),
                        ui.Label({"ID": f"{FORM_PROMPT}FormNote", "Text": "", "Weight": 0.0, "WordWrap": True, "StyleSheet": STYLE_DIM}),
                        ui.VGap(0, 1.0),
                    ],
                ),
                ui.VGroup(
                    {"Spacing": 6},
                    [
                        *self._form_rows(FORM_PLAIN),
                        ui.Label({"ID": f"{FORM_PLAIN}FormNote", "Text": "", "Weight": 0.0, "WordWrap": True, "StyleSheet": STYLE_DIM}),
                        ui.VGap(0, 1.0),
                    ],
                ),
            ],
        )

        return ui.VGroup(
            {"Spacing": 6},
            [
                ui.Button({"ID": "Back", "Text": "‹  All workflows", "Flat": True, "Weight": 0.0, "MinimumSize": [150, 0], "StyleSheet": STYLE_LINK}),
                ui.Label({"ID": "RunTitle", "Text": "", "Weight": 0.0, "WordWrap": True, "StyleSheet": STYLE_TITLE}),
                ui.Label({"ID": "RunSub", "Text": "", "Weight": 0.0, "WordWrap": True, "StyleSheet": STYLE_DIM}),
                ui.VGroup(
                    {"Weight": 0.0, "Spacing": 6, "StyleSheet": ""},
                    [
                        ui.HGroup(
                            {"Weight": 0.0, "Spacing": 8},
                            [
                                ui.Label(
                                    {
                                        "Text": "Source",
                                        "Weight": 0.0,
                                        "MinimumSize": [LABEL_WIDTH, 0],
                                        "MaximumSize": [LABEL_WIDTH, 16777215],
                                        "Alignment": {"AlignRight": True, "AlignVCenter": True},
                                        "StyleSheet": f"color: {C_DIM};",
                                    }
                                ),
                                ui.ComboBox({"ID": "SourceMode", "Weight": 1.0}),
                                ui.Button({"ID": "RefreshSource", "Text": "Refresh", "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                            ],
                        ),
                        ui.Label(
                            {
                                "ID": "SourceInfo",
                                "Text": "",
                                "Weight": 0.0,
                                "WordWrap": True,
                                "MinimumSize": [0, 50],
                                "Alignment": {"AlignVCenter": True, "AlignLeft": True},
                                "StyleSheet": f"{STYLE_CARD} color: {C_TEXT}; padding: 6px 10px;",
                            }
                        ),
                    ],
                ),
                ui.CheckBox(
                    {"ID": "UseSource", "Text": "Send the original media file, not a render of the range", "Checked": False, "Weight": 0.0}
                ),
                forms,
                ui.Label({"ID": "Notes", "Text": "", "Weight": 0.0, "WordWrap": True, "Visible": False, "StyleSheet": STYLE_NOTE}),
                ui.Label(
                    {"ID": "Steps", "Text": "", "Weight": 0.0, "MinimumSize": [0, 20], "Alignment": {"AlignHCenter": True}, "StyleSheet": STYLE_DIM}
                ),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 8},
                    [
                        ui.Button({"ID": "Generate", "Text": "Generate", "Weight": 1.0, "Enabled": False, "StyleSheet": STYLE_PRIMARY}),
                        ui.Button({"ID": "Cancel", "Text": "Cancel", "Enabled": False, "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                        ui.Button({"ID": "OpenResult", "Text": "Open result", "Enabled": False, "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                    ],
                ),
            ],
        )

    def _history_view(self):
        ui = self.ui
        return ui.VGroup(
            {"Spacing": 8},
            [
                ui.Tree({"ID": "History", "Weight": 1.0, "RootIsDecorated": False, "Indentation": 0, "StyleSheet": STYLE_LIST}),
                ui.Label({"ID": "HistoryNotes", "Text": "", "Weight": 0.0, "WordWrap": True, "StyleSheet": STYLE_NOTE}),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 8},
                    [
                        ui.Button({"ID": "HistoryImport", "Text": "Place again", "Weight": 1.0, "StyleSheet": STYLE_PRIMARY}),
                        ui.Button({"ID": "HistoryOpen", "Text": "Open file", "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                        ui.Button({"ID": "HistoryClear", "Text": "Clear", "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                    ],
                ),
            ],
        )

    def _account_view(self):
        ui = self.ui

        def row(label, widget):
            return ui.HGroup(
                {"Weight": 0.0, "Spacing": 8},
                [
                    ui.Label(
                        {
                            "Text": label,
                            "Weight": 0.0,
                            "MinimumSize": [LABEL_WIDTH, 0],
                            "MaximumSize": [LABEL_WIDTH, 16777215],
                            "Alignment": {"AlignRight": True, "AlignVCenter": True},
                            "StyleSheet": f"color: {C_DIM};",
                        }
                    ),
                    widget,
                ],
            )

        def heading(text):
            return ui.Label({"Text": text, "Weight": 0.0, "StyleSheet": "color: #f2f2f2; font-weight: 600; padding-top: 6px;"})

        return ui.VGroup(
            {"Spacing": 6},
            [
                heading("Account"),
                ui.Label({"ID": "AccountInfo", "Text": "", "Weight": 0.0, "WordWrap": True, "StyleSheet": STYLE_DIM}),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 8},
                    [
                        # As wide as its longer label, so that it does not grow when the label changes.
                        ui.Button({"ID": "SignIn", "Text": SIGN_IN, "Weight": 1.0, "StyleSheet": STYLE_PRIMARY}),
                        ui.Button({"ID": "SignOut", "Text": "Sign out", "Weight": 0.0, "Enabled": False, "StyleSheet": STYLE_QUIET}),
                    ],
                ),
                ui.Label({"Text": "Or use an API key from Kaitoi Studio:", "Weight": 0.0, "StyleSheet": STYLE_DIM}),
                row("API key", ui.LineEdit({"ID": "ApiKey", "EchoMode": "Password", "Weight": 1.0, "StyleSheet": STYLE_FIELD})),
                row("Server", ui.LineEdit({"ID": "BaseUrl", "Weight": 1.0, "StyleSheet": STYLE_FIELD})),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 8},
                    [
                        ui.HGap(LABEL_WIDTH + 8, 0.0),
                        ui.Button({"ID": "TestConnection", "Text": "Test connection", "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                        ui.Button({"ID": "OpenStudio", "Text": "Open Studio", "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                        ui.HGap(0, 1.0),
                    ],
                ),
                heading("What gets sent"),
                row("Max height", ui.LineEdit({"ID": "MaxHeight", "Weight": 1.0, "StyleSheet": STYLE_FIELD})),
                row("Quality", ui.ComboBox({"ID": "Quality", "Weight": 1.0})),
                ui.CheckBox({"ID": "IncludeAudio", "Text": "Send clips with their sound", "Weight": 0.0}),
                heading("Results"),
                row("Place on", ui.ComboBox({"ID": "Placement", "Weight": 1.0})),
                row("Work folder", ui.LineEdit({"ID": "WorkDir", "Weight": 1.0, "StyleSheet": STYLE_FIELD})),
                row("Give up after (s)", ui.LineEdit({"ID": "RunTimeout", "Weight": 1.0, "StyleSheet": STYLE_FIELD})),
                ui.CheckBox({"ID": "PlaceAudio", "Text": "Put a result clip's sound on its own audio track", "Weight": 0.0}),
                heading("In Resolve"),
                ui.Label(
                    {
                        "ID": "InstalledInfo",
                        "Text": "",
                        "Weight": 0.0,
                        "MinimumSize": [0, 18],
                        "ToolTip": "Small settings files. No node code is stored here.",
                        "StyleSheet": STYLE_DIM,
                    }
                ),
                ui.HGroup(
                    {"Weight": 0.0, "Spacing": 8},
                    [
                        ui.Button({"ID": "RemoveAll", "Text": "Remove everything Kaitoi installed", "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                        *([ui.Button({"ID": "Uninstall", "Text": "Uninstall Kaitoi", "Weight": 0.0, "StyleSheet": STYLE_QUIET})] if self._uninstaller else []),
                        ui.HGap(0, 1.0),
                    ],
                ),
                heading("Window"),
                ui.CheckBox({"ID": "FollowTimeline", "Text": "Follow what is selected on the timeline", "Weight": 0.0}),
                ui.CheckBox({"ID": "KeepOnTop", "Text": "Stay on top (from the next time it opens)", "Weight": 0.0}),
                ui.VGap(0, 1.0),
                ui.Button({"ID": "SaveSettings", "Text": "Save", "Weight": 0.0, "StyleSheet": STYLE_PRIMARY}),
            ],
        )

    def _uninstall_view(self):
        """Uninstalling, on a view of its own: the question, then one message
        while macOS asks for the password, then the way out.

        A view and not rows of the Account tab, because in this UI kit a
        hidden row keeps its space: rows that come and go would leave a gap
        in the tab, and make the window taller for everyone. What changes
        under the message is one page of a stack for the same reason.
        """
        ui = self.ui
        return ui.VGroup(
            {"Spacing": 12},
            [
                ui.VGap(0, 1.0),
                ui.Label({"ID": "UninstallText", "Text": "", "Weight": 0.0, "WordWrap": True, "Alignment": {"AlignTop": True, "AlignLeft": True}, "StyleSheet": STYLE_BODY}),
                ui.Stack(
                    {"ID": "UninstallStage", "Weight": 0.0},
                    [
                        ui.VGroup(
                            {"Spacing": 10},
                            [
                                ui.CheckBox({"ID": "UninstallSettings", "Text": "Also remove my settings and history", "Weight": 0.0}),
                                ui.HGroup(
                                    {"Weight": 0.0, "Spacing": 8},
                                    [
                                        ui.Button({"ID": "UninstallGo", "Text": "Uninstall", "Weight": 0.0, "StyleSheet": STYLE_PRIMARY}),
                                        ui.Button({"ID": "UninstallCancel", "Text": "Cancel", "Weight": 0.0, "StyleSheet": STYLE_QUIET}),
                                        ui.HGap(0, 1.0),
                                    ],
                                ),
                            ],
                        ),
                        ui.Label({"Text": "", "Weight": 0.0}),
                        ui.VGroup(
                            [
                                ui.HGroup(
                                    {"Weight": 0.0},
                                    [ui.Button({"ID": "UninstallClose", "Text": "Close", "Weight": 0.0, "StyleSheet": STYLE_PRIMARY}), ui.HGap(0, 1.0)],
                                ),
                                ui.VGap(0, 1.0),
                            ]
                        ),
                    ],
                ),
                ui.VGap(0, 2.0),
            ],
        )

    def _show_account_info(self) -> None:
        """The line at the top of the Account tab, with the room its lines
        take: a home folder's path makes it two, and a wrapping label left to
        itself is one line high."""
        text = self._account_info()
        height = self._text_height(text)
        self._put("AccountInfo", "Text", text)
        self._put("SignOut", "Enabled", self._signed_in())
        if self._shown.get(("AccountInfo", "MinimumSize")) != [0, height]:
            self._put("AccountInfo", "MinimumSize", [0, height])
            self._relayout()

    def _account_info(self) -> str:
        where = credentials.source()
        balance = f"  {self.balance}." if self.balance else ""
        if where == "not set":
            return "Not signed in. Sign in with your Kaitoi account, or paste an API key from Kaitoi Studio."
        # Signed in, as long as the key in use is still the one the sign-in
        # gave: one pasted in since, or set in the environment, is not it.
        if self._signed_in():
            return f"Signed in as {self.settings.get('signed_in_as')}." + balance
        return f"Using the key from {where}." + balance

    def _signed_in(self) -> bool:
        """Is the key in use the one a sign-in here gave, on the server that
        gave it? A key pasted in since, one set in the environment, or the
        same key pointed at another server, is not a sign-in."""
        who = str(self.settings.get("signed_in_as") or "")
        end = str(self.settings.get("signed_in_key_end") or "")
        return bool(
            who
            and end
            and str(self.settings.get("api_key") or "").endswith(end)
            and str(self.settings.get("signed_in_server") or "").rstrip("/") == self._base_url()
            and not credentials.env_api_key()
        )

    # ── signing in ──────────────────────────────────────────────────────────

    def _sign_in_pressed(self) -> None:
        """Start a sign-in in the browser, or call off the one that is waiting."""
        flow = getattr(self, "_signin", None)
        if flow is not None:
            flow.cancel()
            return
        if credentials.env_api_key():
            # That key is used whatever is saved here: a sign-in would say
            # "signed in" and change nothing.
            self.status("A Kaitoi key is set in this computer's environment and is used instead. Remove it to sign in here.")
            return
        # A new sign-in ends the key this computer has, on Kaitoi, the moment
        # it finishes in the browser: a run that is using that key would go on
        # and be paid for, and its result never fetched. So no sign-in while
        # something runs, and nothing is started while a sign-in waits: this
        # window refuses (see _on_generate, _install_all), and the other
        # Kaitoi scripts, one per Generate in the Inspector, see the same
        # marker a run sets.
        if self.job is not None or self._bulk is not None or busy.held_by_other(self._owner) or not busy.acquire(self._owner, SIGN_IN_WHAT):
            self.status(SIGN_IN_BUSY)
            return
        flow = signin.SignIn(self._base_url(), device=self._computer())
        self._signin = flow
        self._put("SignIn", "Text", SIGN_IN_WAITING)
        self._signin_said = False
        if flow.start():
            # The server is asked first whether it signs plugins in; the
            # browser is opened only on a yes, and only then is it said.
            self.status(SIGN_IN_ASKING)

    def _computer(self) -> str:
        """What this computer's sign-in key is called. Its last four letters
        are made once and kept with the settings: they are what lets a new
        sign-in here replace the key this computer had, and no other
        computer's, also where two computers have the same name."""
        mark = str(self.settings.get("computer_mark") or "")
        if not re.fullmatch(r"[0-9a-f]{4}", mark):
            mark = secrets.token_hex(2)
            try:
                self.settings = config.update(computer_mark=mark, last_values=self.settings.get("last_values") or {})
            except Exception as exc:  # noqa: BLE001 - not kept: the next sign-in here leaves this one's key to end by itself
                config.log(f"could not keep this computer's mark: {exc}", "WARN")
        return signin.device_name(mark)

    def _collect_sign_in(self) -> bool:
        """Take the answer of a sign-in that is waiting. True while it still is."""
        flow = getattr(self, "_signin", None)
        if flow is None:
            return False
        seen = flow.poll()
        if seen["state"] == signin.WAITING:
            busy.renew(self._owner)
            if seen.get("opened") and not getattr(self, "_signin_said", False):
                self._signin_said = True
                self.status(SIGN_IN_OPENED)
            return True
        self._signin = None
        busy.release(self._owner)
        self._put("SignIn", "Text", SIGN_IN)
        if seen["state"] != signin.DONE:
            self.status(seen["message"] or "Not signed in.")
            return False
        key = seen["result"]["api_key"]
        before = (str(self.settings.get("api_key", "")), self._base_url())
        self.settings = config.update(
            api_key=key,
            base_url=flow.base_url,
            # Who the key belongs to, and the end of the key it was said of:
            # a key pasted in later is not this person's sign-in.
            signed_in_as=seen["result"].get("user") or "your Kaitoi account",
            signed_in_key_end=key[-6:],
            signed_in_server=flow.base_url,
            last_values=self.settings.get("last_values") or {},
        )
        self.items["ApiKey"].Text = str(self.settings.get("api_key", ""))
        self.items["BaseUrl"].Text = str(self.settings.get("base_url", ""))
        self.status(f"Signed in as {self.settings.get('signed_in_as')}.")
        self._account_switched(before)
        return False

    def _sign_out(self) -> None:
        """Forget the key on this computer. The key itself is ended in Kaitoi Studio, or by itself after thirty days."""
        if not self._signed_in():
            # A key pasted in is the person's own to keep or clear in the box
            # below (Studio shows a key only once), and one from the
            # environment is not this window's at all.
            self.status("Nobody is signed in here. To stop using a pasted key, clear the API key box and save.")
            return
        before = (str(self.settings.get("api_key", "")), self._base_url())
        self.settings = config.update(
            api_key="", signed_in_as="", signed_in_key_end="", signed_in_server="", last_values=self.settings.get("last_values") or {}
        )
        self.items["ApiKey"].Text = ""
        self.status("Signed out on this computer." + self._remove_workflow_effects())
        self._account_switched(before)

    def _remove_workflow_effects(self) -> str:
        """Take the account's workflows out of Resolve's Effects Library: they
        are that account's, and cannot run with nobody signed in. Nodes the
        person installed one by one stay; so does a workflow already on a
        clip. Returns what to say of it, "" when there was none."""
        try:
            theirs = {tool_id: entry for tool_id, entry in templates.installed().items() if tool_id.startswith(templates.WORKFLOW_PREFIX)}
            if not theirs:
                return ""
            templates.sync([], prefix=templates.WORKFLOW_PREFIX)
            listed = templates.installed()
            # Still there: one the record still lists, or one whose file is
            # still in Resolve's folder whatever the record says.
            left = [
                tool_id
                for tool_id, entry in theirs.items()
                if tool_id in listed or any(os.path.isfile(path) for path in (entry or {}).get("files") or [])
            ]
            self._refresh_installed(redraw=False)
        except Exception as exc:  # noqa: BLE001 - signing out is not held up by a file that will not go
            config.log(f"could not remove the workflows from the Effects Library: {exc}", "WARN")
            return " Your workflows could not be removed from Resolve's Effects Library."
        gone = len(theirs) - len(left)
        if left:
            config.log(f"{len(left)} workflow(s) could not be removed from the Effects Library", "WARN")
            return f" {gone} of your {len(theirs)} workflows were removed from Resolve's Effects Library."
        what = "Your workflow was" if gone == 1 else f"Your {gone} workflows were"
        return f" {what} removed from Resolve's Effects Library."

    # ── wiring ──────────────────────────────────────────────────────────────

    def _bind(self) -> None:
        events = self.win.On
        getattr(events, WINDOW_ID).Close = self._on_close
        for button_id, _, view, catalog in NAV:
            getattr(events, button_id).Clicked = lambda ev, view=view, catalog=catalog: self._go(view, catalog)
        events.Show.CurrentIndexChanged = lambda ev: self._on_filter_changed()
        for _, box_id, _ in ORIGINS:
            getattr(events, box_id).Clicked = lambda ev: self._on_origin_changed()
        events.Search.TextChanged = lambda ev: self._on_search_typed()
        events.Tools.ItemClicked = lambda ev: self._on_tool_picked()
        events.Tools.CurrentItemChanged = lambda ev: self._on_tool_picked()
        events.Tools.ItemDoubleClicked = lambda ev: (self._on_tool_picked(), self._use())
        events.Use.Clicked = lambda ev: self._use()
        events.RunHere.Clicked = lambda ev: self._open_run()
        events.RunHereWide.Clicked = lambda ev: self._open_run()
        events.Install.Clicked = lambda ev: self._toggle_install()
        events.InstallAll.Clicked = lambda ev: self._install_all()
        events.InstalledOnly.Clicked = lambda ev: self._on_filter_changed()
        events.FitsOnly.Clicked = lambda ev: self._on_filter_changed()
        events.MediaOnly.Clicked = lambda ev: self._on_kept_changed()
        events.FavoritesOnly.Clicked = lambda ev: self._on_kept_changed()
        events.Favorite.Clicked = lambda ev: self._toggle_favorite()
        events.RemoveAll.Clicked = lambda ev: self._remove_all()
        if self._uninstaller:
            events.Uninstall.Clicked = lambda ev: self._uninstall_pressed()
        events.UninstallGo.Clicked = lambda ev: self._uninstall_confirmed()
        events.UninstallCancel.Clicked = lambda ev: self._uninstall_cancelled()
        events.UninstallClose.Clicked = lambda ev: self._on_close()
        events.Back.Clicked = lambda ev: self._set_view(VIEW_BROWSE)
        events.Reload.Clicked = lambda ev: self.reload_workflows()
        events.SourceMode.CurrentIndexChanged = lambda ev: self._on_source_mode()
        events.RefreshSource.Clicked = lambda ev: self.refresh_source()
        events.Generate.Clicked = self._on_generate
        events.Cancel.Clicked = self._on_cancel
        events.OpenResult.Clicked = lambda ev: self._open_last_result()
        events.History.ItemClicked = lambda ev: self._on_history_picked()
        events.HistoryOpen.Clicked = lambda ev: self._history_action("open")
        events.HistoryImport.Clicked = lambda ev: self._history_action("import")
        events.HistoryClear.Clicked = lambda ev: (history.clear(), self._refresh_history())
        events.SaveSettings.Clicked = lambda ev: self._save_settings()
        events.TestConnection.Clicked = lambda ev: self._test_connection()
        events.SignIn.Clicked = lambda ev: self._sign_in_pressed()
        events.SignOut.Clicked = lambda ev: self._sign_out()
        events.OpenStudio.Clicked = lambda ev: webbrowser.open(
            str(self.settings.get("web_url") or credentials.DEFAULT_WEB_URL)
        )
        for form in (FORM_PROMPT, FORM_PLAIN):
            for index in range(MAX_ROWS):
                getattr(events, f"{form}RowBrowse{index}").Clicked = lambda ev, index=index: self._browse(index)
                slider = getattr(events, f"{form}RowSlider{index}")
                slider.SliderMoved = lambda ev, form=form, index=index: self._on_slider(form, index)
                slider.ValueChanged = lambda ev, form=form, index=index: self._on_slider(form, index)
                getattr(events, f"{form}RowValue{index}").TextChanged = lambda ev, form=form, index=index: self._on_slider_text(form, index)

    def _go(self, view: int, catalog: str) -> None:
        """A tab was pressed: switch catalog if it names one, then the view."""
        if catalog and catalog != self.catalog:
            self._chosen[self.catalog] = self.tool.id if self.tool else ""
            self.catalog = catalog
            wanted = self._chosen.get(catalog, "")
            self.tool = next((tool for tool in self.listing if tool.id == wanted), None)
            self.items["Search"].Text = ""
            self._rebuild_show_filter()
            self._rebuild_list()
            self._show_detail()
            if self.tool is not None and not self.tool.ready:
                self._fetch_node(self.tool)
        self._set_view(view)

    def _set_view(self, view: int) -> None:
        """Switch what the window shows, and mark the matching tab."""
        self.view = view
        self.items["Views"].CurrentIndex = view
        section = VIEW_BROWSE if view == VIEW_RUN else view
        for button_id, _, target, catalog in NAV:
            active = target == section and (not catalog or catalog == self.catalog)
            self.items[button_id].StyleSheet = _nav_style(active)
        if view == VIEW_HISTORY:
            self._refresh_history()
        if view == VIEW_ACCOUNT:
            self._show_account_info()

    def _load_settings_into_ui(self) -> None:
        items = self.items
        self._updating = True
        try:
            for _, label in SOURCE_LABELS:
                items["SourceMode"].AddItem(label)
            for _, label in PLACEMENT_LABELS:
                items["Placement"].AddItem(label)
            for quality in QUALITIES:
                items["Quality"].AddItem(quality)
            items["SourceMode"].CurrentIndex = self._index_of(SOURCE_LABELS, self.settings.get("last_source_mode", "auto"))
            items["Placement"].CurrentIndex = self._index_of(
                PLACEMENT_LABELS, self.settings.get("last_placement", "new_track")
            )
            wanted = str(self.settings.get("export_quality", "Medium"))
            items["Quality"].CurrentIndex = QUALITIES.index(wanted) if wanted in QUALITIES else 2
        finally:
            self._updating = False
        items["ApiKey"].Text = str(self.settings.get("api_key", ""))
        items["BaseUrl"].Text = str(self.settings.get("base_url", ""))
        items["WorkDir"].Text = str(self.settings.get("work_dir", ""))
        items["MaxHeight"].Text = str(self.settings.get("export_max_height", 720))
        items["RunTimeout"].Text = str(self.settings.get("run_timeout_seconds", 900))
        items["IncludeAudio"].Checked = bool(self.settings.get("export_include_audio", True))
        items["PlaceAudio"].Checked = bool(self.settings.get("place_result_audio", True))
        items["FollowTimeline"].Checked = bool(self.settings.get("follow_timeline", True))
        items["FitsOnly"].Checked = bool(self.settings.get("fits_only", True))
        items["MediaOnly"].Checked = bool(self.settings.get("media_only", True))
        items["FavoritesOnly"].Checked = bool(self.settings.get("favorites_only", False))
        wanted_origins = self.settings.get("node_origins")
        wanted_origins = wanted_origins if isinstance(wanted_origins, dict) else {}
        for origin, box_id, _ in ORIGINS:
            items[box_id].Checked = bool(wanted_origins.get(origin, origin != "user"))
        items["KeepOnTop"].Checked = bool(self.settings.get("keep_on_top", True))

    @staticmethod
    def _index_of(pairs: list, key: Any) -> int:
        for index, (candidate, _) in enumerate(pairs):
            if candidate == key:
                return index
        return 0

    @property
    def source_mode(self) -> str:
        return SOURCE_LABELS[int(self.items["SourceMode"].CurrentIndex)][0]

    @property
    def placement(self) -> str:
        return PLACEMENT_LABELS[int(self.items["Placement"].CurrentIndex)][0]

    @property
    def show_key(self) -> str:
        index = int(self.items["Show"].CurrentIndex)
        return self._show_keys[index] if 0 <= index < len(self._show_keys) else ""

    @property
    def all_tools(self) -> list:
        return list(self.workflow_tools) + list(self.node_tools)

    @property
    def listing(self) -> list:
        """The catalog the browsing view is showing."""
        return self.node_tools if self.catalog == NODES else self.workflow_tools

    @property
    def favorites(self) -> set:
        """The ids of the nodes and workflows the user marked, kept with the plugin's settings on this computer."""
        kept = self.settings.get("favorites")
        return {str(tool_id) for tool_id in kept} if isinstance(kept, list) else set()

    def _kept(self) -> dict:
        """The two filters both tabs share, as :func:`tools.filter_tools` takes them."""
        return {
            "placeable": bool(self.items["MediaOnly"].Checked),
            "only": self.favorites if self.items["FavoritesOnly"].Checked else None,
        }

    @property
    def node_origins(self) -> set:
        """Where listed nodes may come from: the switches that are on."""
        return {origin for origin, box_id, _ in ORIGINS if self.items[box_id].Checked}

    @property
    def supplies(self) -> set:
        return resolve_bridge.source_supplies(self.source) if self.source else set()

    # ── source ──────────────────────────────────────────────────────────────

    def _text_height(self, text: str, *, taken: int = 40, letter: float = 5.9, line: int = 15) -> int:
        """Room for a wrapped message, in points. A wrapping label in this UI
        kit is laid out one line high whatever it holds, so the lines a
        message will take are worked out from the window's width.

        ``taken`` is the width that is not the label's (the window's margins,
        and whatever stands beside it); ``letter`` and ``line`` are the width
        of an average letter and the height of a line in the label's font.
        The defaults are for small text across the whole window."""
        if not text:
            return 0
        geometry = self._geometry() if getattr(self, "win", None) is not None else None
        per_line = max(8, int(((geometry[2] if geometry else DEFAULT_GEOMETRY[2]) - taken) / letter))
        lines = 0
        for part in text.split("\n"):
            # Never fewer lines than its letters alone would fill: wrapped
            # at spaces, a paragraph whose lines all fill exactly counts one
            # line short of what wider letters than the average would take.
            lines += max(self._wrapped(part, per_line), -(-len(part) // per_line))
        return line * lines + 2

    @staticmethod
    def _wrapped(part: str, per_line: int) -> int:
        """The lines one paragraph takes when wrapped at spaces, as a label
        does: a long word (a file name) that does not fit the rest of a line
        starts the next one."""
        lines, used = 1, 0
        for word in part.split(" "):
            if used and used + 1 + len(word) > per_line:
                lines += 1
                used = 0
            used += len(word) + (1 if used else 0)
            if used > per_line:  # longer than a whole line
                lines += (used - 1) // per_line
                used = (used - 1) % per_line + 1
        return lines

    def _fit_card(self) -> None:
        """Give the name and the line under it on the card the room their
        lines take: beside the picture and the Favorite button there is little
        width, and a long name runs on to a second line."""
        changed = False
        for item_id, letter, line in (("DetailName", 8.0, 20), ("DetailTask", 5.9, 15)):
            height = self._text_height(str(self._shown.get((item_id, "Text"), "")), taken=CARD_TAKEN, letter=letter, line=line)
            if self._shown.get((item_id, "MinimumSize")) != [0, height]:
                self._put(item_id, "MinimumSize", [0, height])
                changed = True
        if changed:
            self._relayout()

    def status(self, text: str) -> None:
        height = self._text_height(text)
        grew = height != getattr(self, "_status_height", 0)
        self._status_height = height
        self._put("Status", "Text", text)
        if grew:
            self._put("Status", "MinimumSize", [0, height])
            self._relayout()

    def notes(self, lines: list[str]) -> None:
        text = "\n".join(lines)
        self.items["Notes"].Text = text
        # With the room its lines take: left to itself the label is one line
        # high, and a longer note shows as a strip through the middle of it.
        if self._reveal("Notes", bool(lines), self._text_height(text)):
            self._relayout()

    def refresh_source(self, *, quiet: bool = False) -> None:
        """Read what the editor is pointing at and redraw what depends on it."""
        if busy.held_by_other(self._owner):
            return  # another Kaitoi script is running; two scripts in Resolve at once can crash it
        try:
            self.source = resolve_bridge.current_source(self.session, self.source_mode)
            self._put("SourceInfo", "Text", self._source_card(self.source))
            if not quiet:
                self.status("")
        except resolve_bridge.ResolveError as exc:
            self.source = None
            self._put("SourceInfo", "Text", str(exc))
            if not quiet:
                self.status(str(exc))
        self._follow_signature = self._timeline_signature()
        # During playback the playhead moves every tick but the source rarely
        # changes; nothing below needs redoing unless it did.
        source = self.source or {}
        identity = (source.get("origin"), source.get("start"), source.get("end"), source.get("track_type"), source.get("track_index"), frozenset(self.supplies))
        if identity == self._source_identity and self._listed_supplies is not None:
            return
        self._source_identity = identity
        if self._listed_supplies is not None:
            if frozenset(self.supplies) != self._listed_supplies and self.catalog == WORKFLOWS and self.items["FitsOnly"].Checked:
                self._rebuild_list()
            self._show_detail()
            if self.view == VIEW_RUN:
                self._show_run()

    @staticmethod
    def _source_card(source: dict[str, Any]) -> str:
        """The source in two short lines: what it is, then where and how long."""
        fps = source["fps"]
        start = resolve_bridge.frame_to_timecode(source["start"], fps)
        if source["origin"] == "empty":
            return f"Nothing selected<br><span style='color:{C_DIM}'>New media lands at {start}</span>"
        kind = {"marks": "In/Out range", "selection": "Selected", "playhead": "Under the playhead"}.get(source["origin"], "")
        name = str(source["name"])
        name = name if len(name) <= 34 else name[:33] + "…"
        track = ""
        if source.get("track_index") and source.get("track_type"):
            track = f" on {'V' if source['track_type'] == 'video' else 'A'}{source['track_index']}"
        title = kind if source["origin"] == "marks" else f"{kind}: {_escape(name)}{track}"
        end = resolve_bridge.frame_to_timecode(source["end"], fps)
        seconds = source["duration"] / max(fps, 1.0)
        return f"{title}<br><span style='color:{C_DIM}'>{start} → {end}  ·  {seconds:.1f} s  ·  {fps:g} fps</span>"

    def _on_source_mode(self) -> None:
        if self._updating:
            return
        self.refresh_source()

    def _timeline_signature(self) -> Any:
        """A cheap fingerprint of what the editor is pointing at."""
        try:
            timeline = self.session.timeline
            selected = resolve_bridge._selected_items(timeline)
            return (
                timeline.GetName(),
                timeline.GetCurrentTimecode(),
                str(resolve_bridge._read_raw_marks(timeline)),
                tuple(item.GetUniqueId() for item in selected),
            )
        except Exception:  # noqa: BLE001 - no project or timeline open right now
            return None

    def _follow_timeline(self) -> None:
        """Keep the source current without a click, at most once a second."""
        if not self.settings.get("follow_timeline", True) or self.job is not None:
            return
        if busy.held_by_other(self._owner):
            return
        now = time.monotonic()
        if now - self._follow_at < FOLLOW_SECONDS or self.view not in (VIEW_BROWSE, VIEW_RUN):
            return
        self._follow_at = now
        if self._timeline_signature() != self._follow_signature:
            self.refresh_source(quiet=True)

    # ── the workflow list ───────────────────────────────────────────────────

    def reload_workflows(self, *, quiet: bool = False) -> None:
        """Fetch the account's workflows again, in the background."""
        if self.loader is not None and self.loader.running:
            return
        key = str(self.settings.get("api_key", "")).strip()
        if not key:
            self.load_error = "Sign in under Account to see your workflows."
            self._update_list_info(None)
            return
        try:
            cfg = api.Config(key, self._base_url(), timeout=float(self.settings.get("request_timeout_seconds", 120)))
        except api.KaitoiAPIError as exc:
            self.load_error = str(exc)
            return
        self.load_error = ""
        self.loader = Loader(cfg, tools.read_snapshot(cfg.base_url, tools.account_mark(cfg.api_key)))
        self.loader.start()
        if not quiet:
            self.status("Loading your workflows...")

    def _collect_loader(self) -> bool:
        """Apply whatever the loader has ready. Returns True while it runs."""
        loader = self.loader
        if loader is None:
            return False
        fresh = loader.take()
        if fresh["error"]:
            self.load_error = fresh["error"]
            # Kaitoi refusing the key a sign-in here gave: it ran out, was
            # revoked in Studio, or a later sign-in from this computer was
            # finished in the browser after this window stopped waiting for it.
            refused = "[401]" in fresh["error"] and self._signed_in()
            self.status(SIGN_IN_ENDED if refused else f"Could not load workflows: {fresh['error']}")
            self._update_list_info(None)
        if fresh["balance"]:
            self.balance = fresh["balance"]
            self._update_list_info(None)
            self._show_account_info()
        if fresh["tools"] is not None:
            self.workflow_tools = fresh["tools"]
            if fresh["snapshot"]:
                tools.write_snapshot(fresh["snapshot"])
            # Every workflow also lives in Resolve's Effects Library, with its own controls.
            unread = {f"workflow:{endpoint_id}" for endpoint_id in (fresh["snapshot"] or {}).get("skipped") or []}
            templates.sync(self.workflow_tools, icons={tool.id: _placeholder(tool) for tool in self.workflow_tools}, keep=unread)
            self._refresh_installed(redraw=False)
            if self.catalog == WORKFLOWS:
                keep = self.tool.id if self.tool else self._remembered_tool()
                self._rebuild_show_filter()
                self._rebuild_list()
                self._select_tool(keep)
            if self.job is None and self._shown.get(("Status", "Text")) == "Loading your workflows...":
                self.status("")
        if fresh["nodes"] is not None:
            # Keep the pins of nodes already opened; the catalog only has summaries.
            opened = {tool.id: tool for tool in self.node_tools if tool.ready}
            self.node_tools = [node if node.ready else opened.get(node.id, node) for node in fresh["nodes"]]
            # A refreshed catalog knows nothing of the lists nodes filled in; put them back.
            self.node_tools = [tools.apply_initial_state(node, self._states[node.id]) if node.id in self._states else node for node in self.node_tools]
            if self.tool is not None and self.tool.source == "node":
                self.tool = next((node for node in self.node_tools if node.id == self.tool.id), self.tool)
            # Pins are stored only when the server listed them (see write_nodes).
            tools.write_nodes(self._base_url(), self._account(), self.node_tools, with_pins=any(node.ready for node in fresh["nodes"]))
            if self.catalog == NODES:
                keep = self.tool.id if self.tool else self._remembered_tool()
                self._rebuild_show_filter()
                self._rebuild_list()
                self._select_tool(keep)
        by_id = {tool.id: tool for tool in self.all_tools} if fresh["thumbnails"] else {}
        for tool_id, path in fresh["thumbnails"]:
            item = self._tool_items.get(tool_id)
            tool = by_id.get(tool_id)
            if tool is not None:
                self._tiles.pop(tools.thumbnail_path(tool), None)  # fetched, or fetched again: look at what is there now
            if item is not None and tool is not None:
                self._set_item_icon(item, self._picture(tool))  # a 16:9 picture or its cut; the icon again if the cut is gone
                self._list_signature = None  # its picture may have changed; the next real rebuild recomputes
            if self.tool is not None and self.tool.id == tool_id:
                self._show_thumbnail(self.tool)
        if loader.done and not loader.running:
            self.loader = None
            return False
        return True

    def _rebuild_show_filter(self) -> None:
        """Fill the filters for the catalog on show.

        Workflows: every task, then one entry per task present. Nodes: every
        category, then one entry per category the origin switches allow. The
        row of switches under it shows the page for the catalog.
        """
        items = self.items
        combo = items["Show"]
        nodes = self.catalog == NODES
        if nodes:
            # Only the categories of the nodes the switches allow: an account's
            # own nodes bring dozens of one-off categories with them.
            allowed = self.node_origins
            listed = tools.filter_tools(self.node_tools, origins=allowed, **self._kept())
            keys = ["cat:"] + [f"cat:{name}" for name in tools.categories(listed)]
            labels = ["All categories"] + [key[4:] for key in keys[1:]]
            current = self.show_key if self.show_key.startswith("cat:") else "cat:"
        else:
            keys = ["task:"]
            labels = ["Every task"]
            for key, label in tools.tasks(tools.filter_tools(self.workflow_tools, **self._kept())):
                keys.append(f"task:{key}")
                labels.append(label)
            current = self.show_key if self.show_key.startswith("task:") else "task:"
        self._updating = True
        try:
            combo.Clear()
            for label in labels:
                combo.AddItem(label)
            self._show_keys = keys
            combo.CurrentIndex = keys.index(current) if current in keys else 0
            counts = {origin: sum(1 for node in self.node_tools if node.origin == origin) for origin, _, _ in ORIGINS}
            for origin, box_id, label in ORIGINS:
                self._put(box_id, "ToolTip", f"{counts[origin]} {label.lower()} nodes")
            self._put("FilterRow", "CurrentIndex", 1 if nodes else 0)
            items["Search"].PlaceholderText = "Search nodes" if nodes else "Search workflows"
        finally:
            self._updating = False
        self._relayout()

    def _on_filter_changed(self) -> None:
        if self._updating:
            return
        self._rebuild_list()

    def _on_kept_changed(self) -> None:
        """One of the two filters both tabs share was switched."""
        if self._updating:
            return
        self.settings = config.update(
            media_only=bool(self.items["MediaOnly"].Checked),
            favorites_only=bool(self.items["FavoritesOnly"].Checked),
            last_values=self.settings.get("last_values") or {},  # not yet saved: an update reads the rest back from the file
        )
        self._rebuild_show_filter()  # the categories and tasks on offer follow
        self._rebuild_list()

    def _toggle_favorite(self) -> None:
        tool = self.tool
        if tool is None:
            return
        marked = self.favorites
        marked.symmetric_difference_update({tool.id})
        # Saved at once: a favorite is something the user did, not a state of the window.
        self.settings = config.update(favorites=sorted(marked), last_values=self.settings.get("last_values") or {})
        self._show_favorite()
        if self.items["FavoritesOnly"].Checked:
            self._rebuild_show_filter()
        self._rebuild_list()

    def _show_favorite(self) -> None:
        tool = self.tool
        self._put("Favorite", "Text", FAVORITE_ON if tool is not None and tool.id in self.favorites else FAVORITE_OFF)
        self._put("Favorite", "Enabled", tool is not None)
        self._put("Favorite", "ToolTip", "" if tool is None else ("Remove from favorites." if tool.id in self.favorites else "Add to favorites."))

    def _row_name(self, tool: tools.Tool, marked: set) -> str:
        return (FAVORITE_MARK if tool.id in marked else "") + tool.name

    def _listed(self) -> list:
        """The tools the list shows: the catalog on show, narrowed by the search box and every filter."""
        key = self.show_key
        query = str(self.items["Search"].Text or "")
        if self.catalog == NODES:
            matched = tools.filter_tools(
                self.node_tools, query=query, category=key[4:] if key.startswith("cat:") else "", origins=self.node_origins, **self._kept()
            )
        else:
            matched = tools.filter_tools(
                self.workflow_tools,
                query=query,
                supplies=self.supplies if self.items["FitsOnly"].Checked else None,
                task=key[5:] if key.startswith("task:") else "",
                **self._kept(),
            )
        if self.catalog == NODES and self.items["InstalledOnly"].Checked:
            matched = [tool for tool in matched if tool.id in self._installed]
        return matched

    def _on_origin_changed(self) -> None:
        if self._updating:
            return
        self.settings["node_origins"] = {origin: bool(self.items[box_id].Checked) for origin, box_id, _ in ORIGINS}
        self._rebuild_show_filter()  # the categories on offer follow the switches
        self._rebuild_list()

    def _on_search_typed(self) -> None:
        """Typing redraws the list once it pauses, not on every letter."""
        if self._updating:
            return
        self._search_due = time.monotonic() + SEARCH_PAUSE_SECONDS

    def _reveal(self, item_id: str, visible: bool, height: int = 0, inside: tuple = ()) -> bool:
        """Show or hide a widget, giving its space back when hidden.

        This UI kit keeps a hidden widget's space, so hiding alone leaves a
        gap; its height is squeezed to nothing as well. A row is a layout, not
        a widget, and only gives its space back when the widgets ``inside`` it
        are squeezed too. ``height`` fixes the height while shown, for text
        that would otherwise be cut off. Returns True when anything changed.
        """
        if self._shown.get((item_id, "revealed")) == (visible, height):
            return False
        self._shown[(item_id, "revealed")] = (visible, height)
        for name in (item_id, *inside):
            item = self.items[name]
            item.Visible = visible
            fixed = height if name == item_id else 0
            item.MaximumSize = [16777215, (fixed or 16777215) if visible else 0]
            if name == item_id:
                item.MinimumSize = [0, fixed if visible else 0]
        return True

    def _put(self, item_id: str, prop: str, value: Any) -> None:
        """Set a widget property only when it changes.

        Resolve repaints a widget every time one of its properties is set,
        even to the value it already has. The window refreshes itself often
        (it follows the timeline), so unconditional sets show up as flicker.
        """
        key = (item_id, prop)
        if self._shown.get(key, self) == value:
            return
        self._shown[key] = value
        setattr(self.items[item_id], prop, value)

    def _set_item_icon(self, item, path: str) -> None:
        try:
            item.Icon[0] = self.ui.Icon({"File": path})
        except Exception as exc:  # noqa: BLE001 - a missing picture must not break the list
            config.log(f"could not show thumbnail {path}: {exc}", "WARN")

    def _picture(self, tool: tools.Tool) -> str:
        """The tool's thumbnail when it has been fetched, else a stand-in.

        Only a 16:9 picture is used, the thumbnail or its cut: in a list any
        other shape comes out a different width and pushes that row's text out
        of line, and the card shows what the row shows.
        """
        if not tool.thumbnail_url:
            return _placeholder(tool)
        stored = tools.thumbnail_path(tool)  # a workflow's is named for its date: a newer one is another file
        known = self._tiles.get(stored)
        if known is None:
            # "" for none yet. Forgotten when the loader brings this tool's
            # picture, which is the only way one arrives or changes.
            known = self._tiles[stored] = tools.thumbnail_tile(tool) or ""
        return known or _placeholder(tool)

    def _rebuild_list(self) -> None:
        """Redraw the list from the tools, the filter and the search box."""
        tree = self.items["Tools"]
        self._listed_supplies = frozenset(self.supplies)
        query = str(self.items["Search"].Text or "")
        matched = self._listed()
        marked = self.favorites
        self._listed_all = matched
        chosen = matched[:LIST_CAP]
        self._matched = len(matched)
        current_id = self.tool.id if self.tool else ""
        grouped = tools.group(chosen)
        signature = tuple(
            (category, tuple((tool.id, self._row_name(tool, marked), tools.task_label(tool), self._picture(tool), self._state(tool)) for tool in members))
            for category, members in grouped
        )
        if signature == self._list_signature:
            self._update_list_info(len(chosen))
            return  # the same rows are already on screen: redrawing them is the flicker
        self._list_signature = signature
        self.rebuilds += 1
        self._updating = True
        try:
            tree.UpdatesEnabled = False
            self._read_columns()
            tree.Clear()
            tree.ColumnCount = 3
            tree.SetHeaderLabels(["Node" if self.catalog == NODES else "Workflow", "Task", "In Resolve"])
            self._tool_items = {}
            for category, members in grouped:
                top = tree.NewItem()
                top.Text[0] = category.upper()
                top.TextColor[0] = {"R": 0.55, "G": 0.55, "B": 0.58, "A": 1.0}
                top.Flags = {"ItemIsEnabled": True}  # a heading: shown, not selectable
                tree.AddTopLevelItem(top)
                for tool in members:
                    row = tree.NewItem()
                    row.Text[0] = self._row_name(tool, marked)
                    row.Text[1] = tools.task_label(tool)
                    row.TextColor[1] = {"R": 0.55, "G": 0.55, "B": 0.58, "A": 1.0}
                    state = self._state(tool)
                    row.Text[2] = STATE_TEXT[state]
                    if state in STATE_COLOR:
                        row.TextColor[2] = STATE_COLOR[state]
                    row.StatusTip[0] = tool.id  # not drawn; how a clicked row finds its tool
                    row.ToolTip[0] = short_tip(tool.description) or tool.name
                    self._set_item_icon(row, self._picture(tool))
                    top.AddChild(row)
                    self._tool_items[tool.id] = row
                top.Expanded = True
            self._draw_columns()
            if current_id in self._tool_items:
                self._tool_items[current_id].Selected = True
        finally:
            tree.UpdatesEnabled = True
            self._updating = False
        self._update_list_info(len(chosen))
        if self.tool is None and chosen and not query.strip():
            first = grouped[0][1][0]  # the first row as the list shows it
            self._select_tool(first.id)

    def _draw_columns(self) -> None:
        """Put the column widths on the list, cut to fit the window.

        The last column takes what is left. What the user asked for is kept as
        it is, for a wider window; only what is drawn is cut back, or the list
        grows a scrollbar along its bottom.
        """
        geometry = self._geometry()
        self._columns_width = geometry[2] if geometry else DEFAULT_GEOMETRY[2]
        drawn = fit_columns(self._columns[0], self._columns[1], self._columns_width)
        tree = self.items["Tools"]
        for column, width in enumerate(drawn):
            tree.ColumnWidth[column] = width
        self._columns_drawn = drawn
        self._columns_set = True

    def _refit_columns(self) -> None:
        """Once a second: a window made narrower or wider gets its columns fitted again."""
        now = time.monotonic()
        if not self._columns_set or now - self._columns_checked < 1.0:
            return
        self._columns_checked = now
        geometry = self._geometry()
        if geometry and geometry[2] != self._columns_width:
            self._read_columns()
            self._draw_columns()
            self._fit_card()  # a name that fitted one line may need two now, or the other way round

    def _read_columns(self) -> None:
        """Take note of the list's column widths when the user has dragged them."""
        if not self._columns_set:
            return
        try:
            tree = self.items["Tools"]
            widths = [int(tree.ColumnWidth[column]) for column in range(2)]
        except Exception:  # noqa: BLE001 - a UI kit that will not say keeps the last known widths
            return
        drawn = self._columns_drawn
        if not drawn:
            return
        for column, (width, was) in enumerate(zip(widths, drawn)):
            # A column still as it was drawn was not dragged: what the user
            # asked for it stands, even where it was drawn narrower to fit.
            if abs(width - was) > 1 and 20 <= width <= 2000:
                self._columns[column] = width

    def _update_list_info(self, shown: int | None) -> None:
        if shown is None:
            shown = len(self._tool_items)
        nodes = self.catalog == NODES
        total = len(self.listing)
        if nodes:  # of the nodes the switches allow, not of every node there is
            allowed = self.node_origins
            total = sum(1 for node in self.node_tools if node.origin in allowed)
        matched = getattr(self, "_matched", shown)
        noun = "nodes" if nodes else "workflows"
        if self._bulk is not None:
            self._put("ListInfo", "Text", self._bulk_line())
            self._update_install_all()
            return
        # What the search and the filters leave, first; then what that is out of.
        counted = f"{matched:,} {noun if matched != 1 else noun[:-1]}"
        if matched > shown:
            parts = [f"{counted}, first {shown} shown"]
        else:
            parts = [f"{counted} of {total:,}" if matched != total else counted]
        if self.balance:
            parts.append(self.balance)
        loading = bool(self.loader and self.loader.running)
        if self.load_error:
            parts.append(self.load_error)
        elif not self.listing and loading:
            parts = [f"Loading {noun}..."]
        elif not self.workflow_tools and not nodes and not loading:
            parts.append("Nothing published yet: in Studio, save a template and give it an API endpoint.")
        elif nodes and not self.node_origins:
            parts = ["Switch on Built-in, Community or User above to list nodes"]
        elif self.items["FavoritesOnly"].Checked and not any(tool.id in self.favorites for tool in self.listing):
            # None on this tab: an empty list with no word of why would look broken.
            parts = [f"No favorite {noun} yet: switch Favorites only off to pick some"]
        self._put("ListInfo", "Text", "  ·  ".join(parts))
        self._put("ListInfo", "ToolTip", "Search or filter to narrow the list down." if matched > shown else "")
        self._update_install_all()

    def _remembered_tool(self) -> str:
        """The tool last used, when it belongs to the tab on show."""
        last = str(self.settings.get("last_tool", ""))
        return last if last.startswith("node:") == (self.catalog == NODES) else ""

    def _select_tool(self, tool_id: str) -> None:
        """Make ``tool_id`` the chosen tool if the tab on show has it, whatever the filter shows."""
        found = next((tool for tool in self.listing if tool.id == tool_id), None)
        if found is None:
            if self.tool is not None and all(tool.id != self.tool.id for tool in self.all_tools):
                self.tool = None  # the chosen workflow was unpublished
                self._show_detail()
                if self.view == VIEW_RUN:
                    self._set_view(VIEW_BROWSE)
            return
        self.tool = found
        self._updating = True
        try:
            for other_id, other in self._tool_items.items():
                other.Selected = other_id == tool_id
        finally:
            self._updating = False
        self._show_detail()
        if not found.ready:
            self._fetch_node(found)
        elif self.view == VIEW_RUN:
            self._show_run()

    def _on_tool_picked(self) -> None:
        if self._updating:
            return
        item = self.items["Tools"].CurrentItem()
        if item is None:
            return
        tool_id = str(item.StatusTip[0] or "")
        if not tool_id:  # a category heading
            return
        found = next((tool for tool in self.all_tools if tool.id == tool_id), None)
        if found is None or (self.tool is not None and found.id == self.tool.id):
            return
        self._retry_state_on_pick(found)
        self.tool = found
        self._show_detail()
        if not found.ready:
            self._fetch_node(found)

    def _fetch_node(self, tool: tools.Tool) -> None:
        """Get a node's inputs and outputs, in the background, the first time it is picked."""
        with self._node_lock:
            if tool.id in self._node_fetch:
                return
            self._node_fetch[tool.id] = {"tool": None, "error": None}
        try:
            cfg = api.Config(
                str(self.settings.get("api_key", "")), self._base_url(), timeout=float(self.settings.get("request_timeout_seconds", 120))
            )
        except api.KaitoiAPIError as exc:
            with self._node_lock:
                self._node_fetch[tool.id]["error"] = str(exc)
            return

        def work() -> None:
            try:
                loaded, error = tools.load_node(cfg, tool), None
            except api.KaitoiAPIError as exc:
                loaded, error = None, str(exc)
            except Exception as exc:  # noqa: BLE001 - never die silently on a thread
                loaded, error = None, f"{exc}"
            with self._node_lock:
                self._node_fetch[tool.id] = {"tool": loaded, "error": error or (None if loaded else "no answer")}

        threading.Thread(target=work, name="kaitoi-node", daemon=True).start()

    def _want_state(self, tool: tools.Tool | None) -> None:
        """A node that fills in its own lists was picked: ask for them shortly,
        if it is still the one picked (so stepping down the list asks for none)."""
        if tool is not None and tool.id not in self._state_tried and tools.needs_initial_state(tool):
            self._state_due = (tool.id, time.monotonic() + STATE_PAUSE_SECONDS)

    def _fetch_state(self, tool: tools.Tool) -> None:
        """Ask Kaitoi, in the background, for the lists a node fills in when it starts."""
        self._state_tried.add(tool.id)
        try:
            cfg = api.Config(
                str(self.settings.get("api_key", "")), self._base_url(), timeout=float(self.settings.get("request_timeout_seconds", 120))
            )
        except api.KaitoiAPIError:
            self._state_retry.add(tool.id)  # no key yet: asked again when the node is next picked
            return
        account = self._state_account
        with self._node_lock:
            self._state_fetch[tool.id] = {"state": None, "error": None, "account": account}

        def work() -> None:
            try:
                state, error = api.get_node_initial_state(cfg, tool.node_type), None
            except api.KaitoiAPIError as exc:
                state, error = None, exc
            except Exception as exc:  # noqa: BLE001 - never die silently on a thread
                state, error = None, api.KaitoiAPIError(0, "FAILED", str(exc))
            with self._node_lock:
                asked = self._state_fetch.get(tool.id)
                if asked is None or asked.get("account") != account:
                    return  # the key or the server changed meanwhile: this answer is nobody's
                self._state_fetch[tool.id] = {"state": state, "error": error or (None if state is not None else api.KaitoiAPIError(0, "FAILED", "no answer")), "account": account}

        threading.Thread(target=work, name="kaitoi-node-state", daemon=True).start()
        if self.view == VIEW_RUN and self.tool is not None and self.tool.id == tool.id:
            self._show_run()  # its boxes now say the list is on its way

    def _collect_states(self) -> bool:
        """Start a list request that is due; apply the lists that have arrived.
        Returns True while any is on its way."""
        due = self._state_due
        if due is not None and time.monotonic() >= due[1]:
            self._state_due = None
            if self.tool is not None and self.tool.id == due[0] and tools.needs_initial_state(self.tool) and due[0] not in self._state_tried:
                self._fetch_state(self.tool)
        with self._node_lock:
            done = {key: value for key, value in self._state_fetch.items() if value["state"] is not None or value["error"] is not None}
            for key in done:
                del self._state_fetch[key]
            waiting = bool(self._state_fetch)
        for tool_id, result in done.items():
            if result.get("account") != self._state_account:
                continue  # asked for with another key or server: not this account's lists
            current = self.tool is not None and self.tool.id == tool_id
            before = next((node for node in self.node_tools if node.id == tool_id), None)
            if result["state"] is None:
                error = result["error"]
                if current and getattr(error, "status", 0) == 422:
                    self.status("This node could not load its lists. Open it in Studio to see why. You can still type a value here.")
                elif getattr(error, "status", 0) not in (404, 405):  # 404: a server from before lists could be asked for
                    config.log(f"could not load the lists of {tool_id}: {error}", "WARN")
                if getattr(error, "status", 0) not in (422, 404, 405):
                    # Busy, over its budget, or the network: not an answer. It
                    # is asked again when the user next picks the node, never
                    # on a timer: a server that is refusing must not be asked
                    # again every second by every open window.
                    self._state_retry.add(tool_id)
            else:
                self._states[tool_id] = result["state"]
                self.node_tools = [tools.apply_initial_state(node, result["state"]) if node.id == tool_id else node for node in self.node_tools]
                if self._form_tool_id == tool_id and not (current and self.view == VIEW_RUN):
                    self._late_lists = (tool_id, before)  # its form is not on screen: refilled when it next is
            if current:
                self.tool = next((node for node in self.node_tools if node.id == tool_id), self.tool)
                self._show_detail()
                if self.view == VIEW_RUN:
                    self._show_run(lists_arrived_over=before if result["state"] is not None else None)
        return waiting

    def _collect_nodes(self) -> bool:
        """Apply node details that have arrived. Returns True while any is still on its way."""
        with self._node_lock:
            done = {key: value for key, value in self._node_fetch.items() if value["tool"] is not None or value["error"]}
            for key in done:
                del self._node_fetch[key]
            waiting = bool(self._node_fetch)
        left_out = False
        for tool_id, result in done.items():
            loaded = result["tool"]
            if loaded is None:
                if self.tool is not None and self.tool.id == tool_id:
                    self._put("DetailText", "Text", f"Could not load this node: {_escape(result['error'])}")
                continue
            self.node_tools = [loaded if node.id == tool_id else node for node in self.node_tools]
            if self.tool is not None and self.tool.id == tool_id:
                self.tool = loaded
                self._show_detail()
                item = self._tool_items.get(tool_id)
                if item is not None:
                    item.Text[1] = tools.task_label(loaded)
            if self.items["MediaOnly"].Checked and not tools.places_in_resolve(loaded) and tool_id in self._tool_items:
                left_out = True
        if left_out:
            # What these make was not known until now (a server that lists
            # nodes without their pins), and it is nothing that works in
            # Resolve: the filter that is on leaves them out from here. Once
            # for all that arrived together, and only for rows on show.
            self._rebuild_show_filter()
            self._rebuild_list()
        return waiting

    # ── the chosen workflow: card and form ──────────────────────────────────

    def _primary(self) -> tools.Field | None:
        return tools.primary_input(self.tool, self.supplies) if self.tool else None

    def _sends_line(self) -> str:
        tool = self.tool
        if tool is None:
            return ""
        primary = self._primary()
        if primary is not None:
            sound = " with its sound" if primary.media == "video" and self.settings.get("export_include_audio", True) else ""
            return SENDS[primary.media].format(sound=sound)
        if not tool.media_inputs:
            return "Takes nothing from the timeline. The result lands at the playhead, or at the In point of a marked range."
        wanted = " or ".join(dict.fromkeys(NEEDS[field.media] for field in tool.media_inputs))
        return f"Needs {wanted}, and nothing selected on the timeline has it. Select a clip, or choose a file."

    def _show_thumbnail(self, tool: tools.Tool | None) -> None:
        button = self.items["Thumb"]
        picture = self._picture(tool) if tool is not None else ""
        try:
            version = (picture, os.path.getmtime(picture)) if picture else picture  # a cut made again keeps its name
        except OSError:
            version = picture
        if self._shown.get(("Thumb", "picture"), None) == version:
            return
        self._shown[("Thumb", "picture")] = version
        if tool is None:
            button.Visible = False
            return
        try:
            button.Icon = self.ui.Icon({"File": picture})
            button.Visible = True
        except Exception as exc:  # noqa: BLE001
            config.log(f"could not show thumbnail: {exc}", "WARN")
            button.Visible = False

    def _show_detail(self) -> None:
        """Fill the card under the list for the chosen tool."""
        tool = self.tool
        self._want_state(tool)
        self._show_thumbnail(tool)
        self._show_favorite()
        if tool is None:
            self._put("DetailName", "Text", "")
            self._put("DetailTask", "Text", "")
            self._put("DetailText", "Text", f"Pick a {'node' if self.catalog == NODES else 'workflow'} to see what it does.")
            self._put("Use", "Text", "Use this node" if self.catalog == NODES else "Use this workflow")
            self._put("Use", "Enabled", False)
            self._put("RunHere", "Enabled", False)
            self._put("RunHereWide", "Enabled", False)
            self._put("DetailState", "Text", "")
            self._put("Install", "Text", "Install in Resolve")
            self._put("Install", "Enabled", False)
            self._put("CardActions", "CurrentIndex", 1 if self.catalog == NODES else 0)
            self._fit_card()
            return
        self._put("DetailName", "Text", tool.name)
        self._put("DetailTask", "Text", f"{tools.task_label(tool)}  ·  {tool.category}")
        self._fit_card()
        body = _escape(tool.description) + "<br>" if tool.description else ""
        hint = self._sends_line() if tool.ready else "Loading its inputs..."
        if tool.needs:
            hint = f"Needs {' and '.join(tool.needs)}, which cannot be supplied from Resolve."
        elif tool.session:
            hint = "A live session node, made for Kaitoi Studio. It may not work from Resolve."
        elif tool.agent:
            hint = "An agent node, made to work with other nodes in Kaitoi Studio. It may not work from Resolve."
        self._put("DetailText", "Text", f"{body}<span style='color:{C_DIM}'>{_escape(hint)}</span>")
        self._put("Use", "Text", "Use this node" if tool.source == "node" else "Use this workflow")
        self._put("Use", "Enabled", tool.ready)
        self._put("RunHere", "Enabled", tool.ready)
        self._put("RunHereWide", "Enabled", tool.ready)
        self._put("CardActions", "CurrentIndex", 0 if tool.source == "workflow" else 1)
        state = self._state(tool)
        use = "Use puts it on the selected clip." if tool.media_inputs else "Use adds it to the timeline at the playhead."
        self._put("DetailState", "Text", STATE_LINE[state].replace("{use}", use))
        if tool.source == "workflow":
            # Every published workflow is kept in Resolve by the window itself,
            # so there is nothing here to install or remove by hand.
            self._put("Install", "Enabled", False)
        else:
            self._put("Install", "Text", "Install in Resolve" if state == templates.NOT_INSTALLED else "Remove from Resolve")
            self._put("Install", "Enabled", tool.ready or state != templates.NOT_INSTALLED)

    # ── what is installed in Resolve ────────────────────────────────────────

    def _state(self, tool: tools.Tool) -> str:
        return templates.state(tool.id, self._registered, self._installed)

    def _refresh_installed(self, *, redraw: bool = True) -> None:
        """Re-read what is installed and redraw whatever shows it."""
        self._installed = templates.installed()
        if not busy.held_by_other(self._owner):  # while another script runs, leave Resolve alone
            self._registered = resolve_bridge.registered_effects(self.session, templates.FOLDER)
        waiting = sum(1 for tool_id in self._installed if templates.state(tool_id, self._registered, self._installed) == templates.RESTART)
        note = ""
        if waiting:
            what = "tool is" if waiting == 1 else "tools are"
            note = f"{waiting} {what} installed. Restart Resolve to use {'it' if waiting == 1 else 'them'} on clips."
        self._put("RestartNote", "Text", f"● {waiting} need a restart" if waiting else "")
        self._put("RestartNote", "ToolTip", note)
        ready = len(self._installed) - waiting
        self._put(
            "InstalledInfo",
            "Text",
            f"{len(self._installed)} in Resolve's Effects Library: {ready} ready, {waiting} waiting for a restart.",
        )
        if redraw:
            self._rebuild_list()
            self._show_detail()

    def _toggle_install(self) -> None:
        """The card's link: install the chosen tool in Resolve, or remove it."""
        tool = self.tool
        if tool is None or tool.source == "workflow":
            return
        try:
            if tool.id in self._installed:
                name = str(self._installed[tool.id].get("name") or tool.name)
                templates.uninstall(tool.id)
                self.status(f"Removed “{name}” from Resolve's Effects Library. Clips that already carry it keep working.")
            else:
                entry = templates.install(tool, icon=_placeholder(tool))
                waits = templates.state(tool.id, self._registered, {tool.id: entry}) == templates.RESTART
                self.status(
                    f"Installed “{entry['name']}”. " + ("Restart Resolve to use it on clips." if waits else "It is ready in the Effects Library, under Kaitoi.")
                )
        except OSError as exc:
            self.status(f"Could not change Resolve's Effects Library: {exc}")
        self._refresh_installed()

    def _update_install_all(self) -> None:
        """Offer to install every node the list is showing, when there are any to install."""
        # Always on show: a button revealed after its row was laid out is drawn
        # clipped in this UI kit. With nothing to install it says so instead.
        was = self._shown.get(("InstallAll", "Text"), "All installed")
        if self._bulk is not None:
            self._put("InstallAll", "Text", "Stop installing")
            self._put("InstallAll", "Enabled", True)
        else:
            missing = [tool for tool in getattr(self, "_listed_all", []) if tool.id not in self._installed] if self.catalog == NODES else []
            self._put("InstallAll", "Text", f"Install all {len(missing)}" if missing else "All installed")
            self._put("InstallAll", "ToolTip", "Install every node in this list in Resolve." if missing else "")
            self._put("InstallAll", "Enabled", bool(missing))
        if len(str(self._shown.get(("InstallAll", "Text"), ""))) != len(str(was)):
            self._relayout()  # the button keeps the width it was laid out with until then, and clips a longer label

    def _install_all(self) -> None:
        """Install every listed node that is not installed yet, in the background."""
        if self._bulk is not None:
            if time.monotonic() - self._bulk["started"] >= 1.0:  # not the second half of a double click
                self._bulk["stop"].set()
            return
        missing = [tool for tool in getattr(self, "_listed_all", []) if tool.id not in self._installed]
        if not missing:
            return
        if getattr(self, "_signin", None) is not None:
            self.status(SIGN_IN_FIRST)  # installing asks Kaitoi about each node, with the key a finished sign-in ends
            return
        try:
            cfg = api.Config(
                str(self.settings.get("api_key", "")), self._base_url(), timeout=float(self.settings.get("request_timeout_seconds", 120))
            )
        except api.KaitoiAPIError as exc:
            self.status(str(exc))
            return
        stop = threading.Event()
        bulk: dict[str, Any] = {"total": len(missing), "stop": stop, "started": time.monotonic()}
        worker = threading.Thread(
            target=templates.install_many,
            args=(cfg, missing, bulk, stop),
            kwargs={"icon_for": _placeholder, "pause": BULK_PAUSE_SECONDS},
            name="kaitoi-install",
            daemon=True,
        )
        self._bulk = bulk
        worker.start()
        self._update_list_info(None)

    def _bulk_line(self) -> str:
        """An install-all's progress, for the line under the list."""
        bulk = self._bulk
        if bulk is None:
            return ""
        done, total = int(bulk.get("done") or 0), bulk["total"]
        if bulk.get("waiting"):
            return f"Installing {done} of {total}, paused {bulk['waiting']} s"
        pace = (time.monotonic() - bulk["started"]) / done if done else 0.0
        left = (total - done) * pace
        about = f", about {int(left // 60) + 1} min left" if left >= 60 else ""
        return f"Installing {done} of {total}{about}"

    def _collect_bulk(self) -> bool:
        """Show an install-all's progress; finish it when it ends. True while it runs."""
        bulk = self._bulk
        if bulk is None:
            return False
        if not bulk.get("finished"):
            self._update_list_info(None)
            return True
        self._bulk = None
        # The install worked from the list as it was drawn; lists a node filled
        # in since then are put back on, or a picker would turn into a plain box.
        loaded = {tool.id: tools.apply_initial_state(tool, self._states[tool.id]) if tool.id in self._states else tool for tool in bulk["loaded"]}
        self.node_tools = [loaded.get(node.id, node) for node in self.node_tools]
        if self.tool is not None and self.tool.id in loaded:
            self.tool = loaded[self.tool.id]
        self._refresh_installed()
        count = len(loaded)
        waits = sum(1 for tool_id in loaded if self._state(loaded[tool_id]) == templates.RESTART)
        parts = [f"Installed {count} in Resolve." if count else "Nothing was installed."]
        if bulk["stop"].is_set():
            parts.append("Stopped early.")
        if bulk["failed"]:
            parts.append(f"{bulk['failed']} could not be installed; see the log.")
        if waits:
            parts.append(f"Restart Resolve to use {'it' if waits == 1 else 'them'} on clips.")
        self.status(" ".join(parts))
        return False

    def _remove_all(self) -> None:
        if self._bulk is not None:
            self.status("An install is still running. Stop it first, then remove.")
            return
        count = templates.uninstall_all()
        self.status(
            f"Removed {count} from Resolve's Effects Library. Clips that already carry one keep working. "
            "Your workflows are added again the next time this window loads them."
        )
        self._refresh_installed()

    # ── uninstalling ────────────────────────────────────────────────────────

    def _uninstall_blocked(self) -> bool:
        """Is something of Kaitoi's still at work? Uninstalled under it, part
        of Kaitoi would be written back, or a run that was paid for would
        never be placed. Says what, so that it is plain what to wait for."""
        if self.job is not None:  # also one whose result has arrived and is about to be placed
            why = "A run is still going. Wait for it or cancel it, then uninstall."
        elif self._bulk is not None:
            why = "An install is still running. Stop it first, then uninstall."
        elif getattr(self, "_signin", None) is not None:
            why = "A sign-in is still waiting. Finish it first, then uninstall."
        elif busy.held_by_other(self._owner):
            why = "A run started from the Inspector is still going. Wait for it, then uninstall."
        elif self.loader is not None and self.loader.running:
            why = "Kaitoi is still loading your workflows. Try again in a moment."
        else:
            return False
        self.status(why)
        return True

    def _show_uninstall(self, text: str, stage: int) -> None:
        """Put a message on the uninstall view, with what belongs under it."""
        self._put("UninstallText", "Text", text)
        self._put("UninstallText", "MinimumSize", [0, self._text_height(text, letter=6.6, line=17)])
        self.items["UninstallStage"].CurrentIndex = stage
        self._relayout()

    def _uninstall_pressed(self) -> None:
        """Ask first. Nothing is removed by this button."""
        if self._uninstall_blocked():
            return
        self.items["UninstallSettings"].Checked = False
        self._show_uninstall(UNINSTALL_ASK, STAGE_ASK)
        self._set_view(VIEW_UNINSTALL)

    def _uninstall_cancelled(self) -> None:
        if self._uninstall is None and not self._removed:
            self._set_view(VIEW_ACCOUNT)

    def _uninstall_confirmed(self) -> None:
        """Run the uninstaller, and show nothing else until it has answered.

        It waits for a password, so it runs on a thread; the window keeps
        answering, and has only this message in it. From here on nothing is
        written into Kaitoi's folders: what the uninstaller deletes must not
        be put back behind it.
        """
        if self._uninstall is not None or self._removed or not self._uninstaller or self.view != VIEW_UNINSTALL:
            return
        # The same marker a run sets: while it stands, a Generate pressed in
        # the Inspector is refused, so no run starts under the uninstaller.
        if self._uninstall_blocked() or not busy.acquire(self._owner, UNINSTALL_WHAT):
            self._set_view(VIEW_ACCOUNT)
            return
        path, settings = self._uninstaller, bool(self.items["UninstallSettings"].Checked)
        state: dict[str, Any] = {"done": threading.Event(), "code": uninstall.NOTHING, "left": []}

        def work() -> None:
            try:
                state["code"], state["left"] = uninstall.run(path, settings=settings)
            except Exception:  # noqa: BLE001 - it could not be started, so nothing was removed
                state["code"] = uninstall.NOTHING
            finally:
                state["done"].set()

        self._uninstall = state
        config.leave_gone(True)
        for button_id, _, _, _ in NAV:
            self.items[button_id].Enabled = False
        self.status("")
        self._show_uninstall(UNINSTALLING, STAGE_WAIT)
        threading.Thread(target=work, name="kaitoi-uninstall", daemon=True).start()

    def _collect_uninstall(self) -> bool:
        """Take the uninstaller's answer when it has one. True while it runs."""
        state = self._uninstall
        if state is None:
            return False
        if not state["done"].is_set():
            busy.renew(self._owner)
            return True
        self._uninstall = None
        busy.release(self._owner)
        # However it ended, an uninstaller that is no longer there did its
        # work: it is in the folder it deletes. Its answer alone could be lost
        # on the way, and the window would then carry on with its code gone.
        removed = state["code"] in (uninstall.REMOVED, uninstall.LEFT) or not os.path.exists(self._uninstaller or "")
        if not removed:
            # The password was not given: everything is as it was, and so is the window.
            config.leave_gone(False)
            for button_id, _, _, _ in NAV:
                self.items[button_id].Enabled = True
            self._set_view(VIEW_ACCOUNT)
            self.status("Nothing was removed.")
            return False
        self._removed = True
        text = UNINSTALLED
        if state["left"]:
            text += "\n\nThese were left in place:\n" + "\n".join(state["left"])
        self._show_uninstall(text, STAGE_GONE)
        return False

    def _use(self) -> None:
        """Put the chosen workflow on the timeline, so the Inspector runs it.

        A clip is selected or under the playhead and the workflow takes a clip
        or a frame: the workflow is added to that clip as an effect. The clip
        is not moved or cut; its Inspector, under Effects, gets the workflow's
        inputs and Generate. Otherwise, where nothing would be pushed aside,
        the workflow's generator is laid at the playhead. Where neither is
        possible the reason is given and the workflow opens in this window.
        """
        tool = self.tool
        if tool is None:
            return
        other = busy.held_by_other(self._owner)
        if other:
            self.status(f"Kaitoi is busy with {other}. Try again when it has finished.")
            return
        self.refresh_source(quiet=True)
        source = self.source
        try:
            name = str(templates.install(tool, icon=_placeholder(tool))["name"])
        except OSError as exc:
            self.status(f"Could not write the workflow into Resolve's Effects Library: {exc}")
            return
        self._refresh_installed()
        why = ""
        try:
            item = source.get("item") if source and source.get("track_type") == "video" else None
            if item is not None and templates.has_effect(tool):
                effect_id = resolve_bridge.find_effect(self.session, name, templates.FOLDER)
                if effect_id:
                    already = any((fx.GetInfo() or {}).get("id") == effect_id for fx in (item.GetEffects("fusion") or []))
                    if not already:
                        comp = resolve_bridge.add_effect(item, effect_id)
                        comp.SetData(templates.INSTANCE_KEY, uuid.uuid4().hex)
                    clip = str(source["name"])
                    clip = clip if len(clip) <= 30 else clip[:29] + "…"
                    self._remember_use(tool)
                    self.status(
                        f"{'Already on' if already else 'Added to'} “{clip}”. In the Inspector, open Effects: "
                        "set it up there and press Generate."
                    )
                    return
                why = "It is installed, but Resolve loads new effects when it starts: restart it once to use this on clips. "
            elif templates.has_effect(tool) and item is None:
                why = "No clip is selected. "
            if source is not None and source["origin"] == "empty" and resolve_bridge.insert_is_safe(self.session, int(source["start"])):
                comp = resolve_bridge.insert_generator(self.session, name)
                comp.SetData(templates.INSTANCE_KEY, uuid.uuid4().hex)
                self._remember_use(tool)
                self.status(f"“{name}” is on the timeline at the playhead. Set it up in the Inspector and press Generate.")
                return
        except resolve_bridge.ResolveError as exc:
            why = f"{exc} "
        self._open_run()
        self.status(
            f"{why}To run it from the Inspector, drag “{name}” from Effects > Generators > Kaitoi onto a track "
            "above the shot. Or run it right here."
        )

    def _remember_use(self, tool: tools.Tool) -> None:
        self.settings = config.update(last_tool=tool.id, fits_only=bool(self.items["FitsOnly"].Checked), last_values=self.settings.get("last_values") or {})

    def _retry_state_on_pick(self, tool: tools.Tool) -> None:
        """The user picked this node (or opened its form) themselves: if its
        lists were asked for and the server was only busy, ask once more."""
        if tool.id in self._state_retry:
            self._state_retry.discard(tool.id)
            self._state_tried.discard(tool.id)

    def _open_run(self) -> None:
        if self.tool is None:
            return
        self._retry_state_on_pick(self.tool)
        self._want_state(self.tool)
        self._show_run()
        self._set_view(VIEW_RUN)

    def _show_run(self, lists_arrived_over: tools.Tool | None = None) -> None:
        """Draw the Run view for the chosen tool: what it takes, and its form.

        ``lists_arrived_over``: the tool as it was a moment ago, when the form
        is drawn again because the node's own lists have just arrived.
        """
        items = self.items
        tool = self.tool
        if tool is None:
            return
        same_tool = self._form_tool_id == tool.id
        kept = self._form_text() if same_tool else None
        late, self._late_lists = self._late_lists, None
        if lists_arrived_over is None and late is not None and late[0] == tool.id:
            lists_arrived_over = late[1]  # they came while the user was back in the list
        if kept is not None and lists_arrived_over is not None:
            kept = tools.empty_boxes_to_refill(kept, lists_arrived_over, tool)
        if not same_tool:
            self._remember_values()
        items["Back"].Text = "‹  All nodes" if tool.source == "node" else "‹  All workflows"
        items["RunTitle"].Text = tool.name
        sub = f"{tools.task_label(tool)}  ·  {self._sends_line()}"
        items["RunSub"].Text = sub
        items["RunSub"].MinimumSize = [0, self._text_height(sub)]

        primary = self._primary()
        prompt = next((field for field in tool.param_inputs if field.is_prompt), None)
        fields = [field for field in tool.media_inputs if field is not primary]
        fields += [field for field in tool.param_inputs if field is not prompt]
        self._layout_form(fields[:MAX_ROWS], prompt, fields[MAX_ROWS:])
        self._form_tool = tool  # what the boxes are filled from: lists that come later, unseen, do not make an empty box an emptied one
        self._form_tool_id = tool.id
        saved = kept if same_tool else (self.settings.get("last_values") or {}).get(tool.id)
        self._fill_form(saved if isinstance(saved, dict) else {})
        if lists_arrived_over is not None and self._unmatched:
            # Typed while the list was on its way, and not one of its entries.
            self.status(f"\"{self._unmatched[0][1]}\" is not in the list for {self._unmatched[0][0]}. Pick one from the list.")
        elif self._held_picks and not same_tool:
            # Nothing on screen shows it, and a run started now would use the tool's own value.
            name, (entry, _) = next(iter(self._held_picks.items()))
            label = next((field.label for field in tool.inputs if field.name == name), name)
            self.status(f"Your last choice for {label} (\"{entry}\") comes back when its list has loaded.")
        can_use_file = primary is not None and primary.media in ("video", "audio") and bool((self.source or {}).get("file_path"))
        items["UseSource"].Enabled = can_use_file
        if not can_use_file:
            items["UseSource"].Checked = False
        items["Generate"].Enabled = self.job is None

    def _row(self, name: str, index: int | None = None):
        """A widget of the form in use, for example ``_row("RowEdit", 2)``."""
        return self.items[f"{self._form}{name}{'' if index is None else index}"]

    def _layout_form(self, fields: list, prompt: tools.Field | None, overflow: list) -> None:
        items = self.items
        self._rows = []
        self._prompt_field = prompt
        self._overflow = list(overflow)
        self._form = FORM_PROMPT if prompt is not None else FORM_PLAIN
        self._updating = True
        source = self.tool.source if self.tool else "workflow"
        try:
            items["Forms"].CurrentIndex = 0 if prompt is not None else 1
            for index in range(MAX_ROWS):
                if index >= len(fields):
                    self._reveal(f"{self._form}Row{index}", False)
                    continue
                field = fields[index]
                kind = field.widget
                scale = tools.slider_scale(field)
                if scale is not None:
                    kind = "slider"
                edit, combo, path = self._row("RowEdit", index), self._row("RowCombo", index), self._row("RowPath", index)
                label = self._row("RowLabel", index)
                label.Text = field.label if len(field.label) <= 15 else field.label[:14].rstrip() + "..."  # the column is that wide
                label.ToolTip = field.label
                self._row("RowStack", index).CurrentIndex = {"choice": 1, "bool": 1, "media": 2, "slider": 3}.get(kind, 0)
                edit.Text = ""
                edit.ToolTip = ""
                path.Text = ""
                combo.Clear()
                leave = "Node's own value" if source == "node" else "Workflow's own value"
                if kind == "choice":
                    combo.AddItem(tools.leave_label(source, tools.default_label(field)))
                    for _, label in field.choices:
                        combo.AddItem(label)
                elif kind == "bool":
                    for choice in tools.bool_choices(source, tools.default_label(field)):
                        combo.AddItem(choice)
                elif kind == "media":
                    path.PlaceholderText = f"{tools.MEDIA_LABELS[field.media]} file (optional)"
                elif kind == "slider":
                    slider, value = self._row("RowSlider", index), self._row("RowValue", index)
                    slider.Minimum = 0
                    slider.Maximum = scale[1]
                    slider.Value = 0
                    value.Text = ""
                    value.PlaceholderText = tools.number_text(field.default) if tools._number(field.default) is not None else "default"
                    value.ToolTip = f"From {tools.number_text(field.minimum)} to {tools.number_text(field.maximum)}. Empty uses its own value."
                elif kind == "color":
                    edit.PlaceholderText = "#rrggbb"
                elif kind == "number" and field.minimum is not None and field.maximum is not None:
                    start = f"{field.default}, " if field.default is not None else ""
                    edit.PlaceholderText = f"{start}from {field.minimum} to {field.maximum}"
                elif field.dynamic:
                    tool_id = self.tool.id if self.tool is not None else ""
                    asking = tool_id in self._state_fetch or (self._state_due or ("",))[0] == tool_id
                    if asking:
                        edit.PlaceholderText = "Loading its list..."
                    elif tool_id in self._state_retry:
                        edit.PlaceholderText = "Type a value (its list did not load)"
                        edit.ToolTip = "Kaitoi was busy. Go back and open the node again."
                    elif tool_id in self._states or tool_id not in self._state_tried:
                        edit.PlaceholderText = leave  # the node had no list for this one: it is a plain box
                    else:
                        edit.PlaceholderText = "Type a value (no list available)"
                        edit.ToolTip = "List not loaded. See its choices in Studio."
                elif field.default not in (None, ""):
                    edit.PlaceholderText = str(field.default)
                else:
                    edit.PlaceholderText = leave
                self._reveal(f"{self._form}Row{index}", True)
                self._rows.append((field, kind))
            if prompt is not None:
                items["PromptLabel"].Text = prompt.label
                items["Prompt"].PlainText = ""
            names = ", ".join(field.label for field in overflow)
            own = "the node's own values" if source == "node" else "the workflow's own values"
            more = "1 more input keeps" if len(overflow) == 1 else f"{len(overflow)} more inputs keep"
            note = f"{more} {own}: {names}." if overflow else ""
            self._row("FormNote").Text = note
            self._row("FormNote").MinimumSize = [0, self._text_height(note)]
        finally:
            self._updating = False
        self._relayout()

    def _on_slider(self, form: str, index: int) -> None:
        """A slider moved: write its number in the box beside it."""
        if self._updating or form != self._form or index >= len(self._rows):
            return
        field, kind = self._rows[index]
        scale = tools.slider_scale(field)
        if kind != "slider" or scale is None:
            return
        position = int(self._row("RowSlider", index).Value)
        typed = str(self._row("RowValue", index).Text or "").strip()
        rests_at = typed or (tools.number_text(field.default) if tools._number(field.default) is not None else "")
        try:
            if position == tools.slider_position(field, float(rests_at.replace(",", ".")), scale):
                return  # where the form put it, or where the box already says: nothing new to write
        except ValueError:
            if not typed and position == 0:
                return
        text = tools.number_text(tools.slider_value(field, position, scale))
        if typed != text:
            self._updating = True
            try:
                self._row("RowValue", index).Text = text
            finally:
                self._updating = False

    def _on_slider_text(self, form: str, index: int) -> None:
        """A number was typed beside a slider: move the slider to it."""
        if self._updating or form != self._form or index >= len(self._rows):
            return
        field, kind = self._rows[index]
        scale = tools.slider_scale(field)
        if kind != "slider" or scale is None:
            return
        try:
            typed = float(str(self._row("RowValue", index).Text or "").strip().replace(",", "."))
        except ValueError:
            return  # empty or half-typed: the slider waits
        self._updating = True
        try:
            self._row("RowSlider", index).Value = tools.slider_position(field, typed, scale)
        finally:
            self._updating = False

    def _relayout(self) -> None:
        try:
            self.win.RecalcLayout()
        except Exception:  # noqa: BLE001 - older UI kits lack it; the layout still works
            pass

    def _fill_form(self, saved: dict) -> None:
        """Put remembered text, or the contract's defaults, into the form."""
        items = self.items
        # What was remembered from a picker is the words on its entry, not a
        # value. While the node's list is not loaded the input is a plain box,
        # and those words typed into it would be sent as they are.
        picked = saved.get(PICKED) if isinstance(saved.get(PICKED), dict) else {}
        # Once the node has answered, a list that is not here is not coming:
        # the input has stopped being a picker, and there is nothing to hold for.
        answered = self._form_tool is not None and self._form_tool.id in self._states
        self._held_picks = {}
        self._unmatched = []
        for index, (field, kind) in enumerate(self._rows):
            text = saved.get(field.name)
            if field.dynamic and kind not in ("choice", "bool") and field.name in picked:
                # Remembered from the picker, and there is no picker yet. A
                # real pick is held, not shown and not dropped: it goes back
                # on the picker when the list arrives, and is remembered
                # meanwhile. The first entry (position 0) was no pick at all.
                if picked[field.name] and text not in (None, "") and not answered:
                    self._held_picks[field.name] = (str(text), picked[field.name])
                text = None
            if kind == "slider":
                # The box holds only what was typed last time. Empty sends
                # nothing, so the tool's own value is used, whatever it is by
                # then; the slider just sits where that value is known to be.
                typed = str(text).strip() if text not in (None, "") else ""
                self._row("RowValue", index).Text = typed
                rests_at = typed or (tools.number_text(field.default) if tools._number(field.default) is not None else "")
                try:
                    self._row("RowSlider", index).Value = tools.slider_position(field, float(rests_at.replace(",", ".")), tools.slider_scale(field))
                except ValueError:
                    pass
                continue
            if kind in ("choice", "bool"):
                combo = self._row("RowCombo", index)
                if kind == "choice":
                    # What was picked last time comes back by its position, so
                    # two options that read alike stay two options. Nothing
                    # picked means the first entry, which sends nothing.
                    labels = [label for _, label in field.choices]
                    position = picked.get(field.name)
                    if not (isinstance(position, int) and 1 <= position <= len(labels) and labels[position - 1] == text):
                        position = labels.index(text) + 1 if text in labels else 0
                    if not position and text and not tools.is_leave_label(str(text)):
                        # Typed into the plain box while the list was on its
                        # way: an option's value, or its name in any case.
                        position = tools.option_position(field, str(text))
                        if not position:
                            self._unmatched.append((field.label, str(text)))
                    combo.CurrentIndex = position
                    continue
                choices = ["", "On", "Off"]
                combo.CurrentIndex = choices.index(text) if text in choices else 0
            elif text is not None:
                self._row("RowPath" if kind == "media" else "RowEdit", index).Text = str(text)
            elif kind == "text" and field.dynamic and not field.options and field.default not in (None, "") and field.name not in self._held_picks:
                # The node's own sample, as Studio and the Inspector start with. Emptied, the
                # box is left out of the request like any other empty box.
                self._row("RowEdit", index).Text = str(field.default)
        if self._prompt_field is not None:
            prompt = self._prompt_field
            sample = str(prompt.default) if prompt.dynamic and isinstance(prompt.default, str) else ""
            text = saved.get(prompt.name)
            if prompt.dynamic and prompt.name in picked:
                # The large box standing in for a picker (an input the node
                # lists itself, named like a prompt): held, as for any box.
                if picked[prompt.name] and text not in (None, "") and not answered:
                    self._held_picks[prompt.name] = (str(text), picked[prompt.name])
                text = None
            # A held choice keeps the box empty: with the sample in it, the sample would be what is remembered.
            items["Prompt"].PlainText = str(text or "") if text is not None else ("" if prompt.name in self._held_picks else sample)

    def _form_text(self, to_send: bool = False) -> dict:
        """What the form holds right now, as text by field name.

        A choice remembered from a picker whose list is not loaded yet is held
        behind its empty box. It is part of what the form holds, so that it is
        remembered and put back on the picker; it is never ``to_send``, being
        the words on an entry and not a value.
        """
        items = self.items
        text: dict[str, Any] = {}
        positions: dict[str, int] = {}
        for index, (field, kind) in enumerate(self._rows):
            if kind in ("choice", "bool"):
                text[field.name] = str(self._row("RowCombo", index).CurrentText or "")
                if kind == "choice":
                    positions[field.name] = int(self._row("RowCombo", index).CurrentIndex)
            else:
                box = {"media": "RowPath", "slider": "RowValue"}.get(kind, "RowEdit")
                text[field.name] = str(self._row(box, index).Text or "").strip()
                held = self._held_picks.get(field.name)
                if held is not None and not text[field.name] and not to_send:
                    text[field.name], positions[field.name] = held
        if self._prompt_field is not None:
            name = self._prompt_field.name
            text[name] = str(items["Prompt"].PlainText or "").strip()
            held = self._held_picks.get(name)
            if held is not None and not text[name] and not to_send:
                text[name], positions[name] = held
        text[PICKED] = positions
        return text

    def _remember_values(self) -> None:
        """Keep what was typed for the tool in the form, so it is there next time."""
        if not self._form_tool_id:
            return
        remembered = dict(self.settings.get("last_values") or {})
        text = self._form_text()
        shown = self._form_tool if self._form_tool is not None and self._form_tool.id == self._form_tool_id else None
        if shown is not None:
            # A box that is empty because the node's sample never arrived is
            # not remembered as emptied: next time the sample may be there.
            text = tools.empty_boxes_to_refill(text, shown, shown)
        remembered[self._form_tool_id] = text
        self.settings["last_values"] = remembered

    def _browse(self, index: int) -> None:
        if index >= len(self._rows):
            return
        field, _ = self._rows[index]
        try:
            fusion = self.session.resolve.Fusion()
            picked = fusion.RequestFile("", "", {"FReqS_Title": f"Choose {field.label}", "FReqB_SeqGather": False})
        except Exception as exc:  # noqa: BLE001 - no file dialog on this build
            config.log(f"file dialog unavailable: {exc}", "WARN")
            self.status("Type or paste the file's path into the box.")
            return
        if picked:
            self._row("RowPath", index).Text = str(picked)

    # ── run ─────────────────────────────────────────────────────────────────

    def _on_generate(self, _event=None) -> None:
        if self.job is not None and self.job.running:
            self.status("A run is already in progress.")
            return
        tool = self.tool
        if tool is None:
            self.status("Choose a workflow first.")
            return
        if not str(self.settings.get("api_key", "")).strip():
            self.status("No API key yet: add one under Account.")
            return
        if getattr(self, "_signin", None) is not None:
            self.status(SIGN_IN_FIRST)  # finishing it ends the key this run would start with
            return
        other = busy.held_by_other(self._owner)
        if other or not busy.acquire(self._owner, tool.name):
            self.status(f"Kaitoi is busy with {other or 'another run'}. Try again when it has finished.")
            return
        started = False
        try:
            started = self._start_job(tool)
        finally:
            if not started:
                busy.release(self._owner)

    def _read_form(self, tool: tools.Tool, primary: tools.Field | None) -> tuple:
        """``(values, files)`` to run ``tool`` with, as the form stands.

        Only what is on screen is sent. A choice held behind an empty box,
        waiting for its picker, is the words on an entry and not a value: it
        is left out, and the tool's own value is used.
        """
        text = self._form_text(to_send=True)
        picked = {field.name: int(self._row("RowCombo", index).CurrentIndex) for index, (field, kind) in enumerate(self._rows) if kind == "choice"}
        values: dict[str, Any] = {}
        files: dict[str, str] = {}
        for field in tool.inputs:
            if field is primary:
                continue
            raw = text.get(field.name)
            if field.media:
                path = os.path.expanduser(str(raw or "").strip())
                if path:
                    if not os.path.isfile(path):
                        raise tools.ToolInputError(f"{field.label}: no file at {path}")
                    files[field.name] = path
                continue
            if field in self._overflow:
                continue
            if field.name in picked:
                # The option at that position; the first entry sends nothing.
                position = picked[field.name] - 1
                values[field.name] = field.options[position] if 0 <= position < len(field.options) else None
                continue
            values[field.name] = tools.parse_value(field, raw)
        return values, files

    def _start_job(self, tool: tools.Tool) -> bool:
        """Read the form and start the run. False when nothing was started."""
        self.refresh_source(quiet=True)
        source = self.source
        if source is None:
            self.status(str(self.items["SourceInfo"].Text))
            return False
        primary = self._primary()
        try:
            values, files = self._read_form(tool, primary)
        except tools.ToolInputError as exc:
            self.status(str(exc))
            return False
        if tool.media_inputs and primary is None and not files:
            self.status(self._sends_line())
            return False

        self._remember_values()
        self.settings = config.update(
            last_tool=tool.id,
            fits_only=bool(self.items["FitsOnly"].Checked),
            last_source_mode=self.source_mode,
            last_placement=self.placement,
            last_values=self.settings.get("last_values") or {},
        )
        self.last_result_path = None
        self.notes([])
        self.items["OpenResult"].Enabled = False
        self.items["Generate"].Enabled = False
        self.items["Cancel"].Enabled = True

        job = pipeline.Job(
            self.session,
            source,
            tool,
            values=values,
            files=files,
            primary=primary,
            placement=self.placement,
            use_source_file=bool(self.items["UseSource"].Checked),
            settings=self.settings,
        )
        job.start()
        self.job = job
        self._render_progress(job.poll())
        return True

    def _on_cancel(self, _event=None) -> None:
        if self.job is not None and self.job.running:
            self.job.cancel()
            self.items["Cancel"].Enabled = False
            self.status("Cancelling...")

    def _show_bar(self, done: float | None) -> None:
        """The bar above the status line: how much of the run is behind it, or nothing when none is on."""
        if done is None:
            changed = self._reveal("Progress", False)
        else:
            self._put("Progress", "StyleSheet", bar_style(round(done, 2)))  # by whole percents: a repaint per tick would flicker
            changed = self._reveal("Progress", True, BAR_HEIGHT)
        if changed:
            self._relayout()

    def _render_progress(self, snapshot: dict[str, Any] | None, *, failed: bool = False) -> None:
        """Draw the stage strip + spinner from a job snapshot (None = idle)."""
        if snapshot is None:
            self.items["Steps"].Text = ""
            self._show_bar(None)
            return
        stage = int(snapshot.get("stage", -1))
        running = bool(snapshot.get("running"))
        self._show_bar(overall_progress(stage, snapshot.get("fraction")) if running else None)
        parts = []
        for index, name in enumerate(pipeline.STAGES_SHORT):
            if index < stage or (index == stage and not running and not failed):
                mark = "✓"
            elif index == stage:
                mark = "✗" if failed else SPINNER[self.spinner_index % len(SPINNER)]
            else:
                mark = "○"
            parts.append(f"{mark} {name}")
        self.items["Steps"].Text = "  ›  ".join(parts)
        if running:
            self.spinner_index += 1
            elapsed = int(snapshot.get("elapsed", 0))
            clock = f"{elapsed // 60}m{elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
            self.status(f"{snapshot['status']}   [{clock}]")

    def _on_tick(self) -> bool:
        """One heartbeat: advance the job, repaint, finish it when it lands.

        Returns True while a network thread is busy (the caller sleeps longer).
        """
        if self._uninstall is not None or self._removed:
            return self._collect_uninstall()  # nothing else moves while Kaitoi is being removed, or once it is gone
        loading = self._collect_loader()
        loading = self._collect_sign_in() or loading
        loading = self._collect_nodes() or loading
        loading = self._collect_states() or loading
        loading = self._collect_bulk() or loading
        if self._search_due and time.monotonic() >= self._search_due:
            self._search_due = 0.0
            self._rebuild_list()
        self._refit_columns()
        job = self.job
        if job is None:
            self._follow_timeline()
            return loading
        job.step()  # drives the export phase; Resolve calls stay on this thread
        snapshot = job.poll()
        if snapshot["running"]:
            busy.renew(self._owner)
            self._render_progress(snapshot)
            return job.phase == "network" or loading

        # The worker is done — exactly one of these branches applies.
        try:
            return self._finish_job(job, snapshot, loading)
        finally:
            busy.release(self._owner)

    def _finish_job(self, job: pipeline.Job, snapshot: dict[str, Any], loading: bool) -> bool:
        self.job = None
        self.items["Generate"].Enabled = self.tool is not None
        self.items["Cancel"].Enabled = False
        if snapshot["error"]:
            self._render_progress(snapshot, failed=True)
            first_line = snapshot["error"].splitlines()[0]
            if "\n" in snapshot["error"]:
                config.log(f"job error detail:\n{snapshot['error']}", "ERROR")
                self.status(f"{first_line}  (full detail in {config.log_path()})")
            else:
                self.status(first_line)
            return loading
        try:
            self._render_progress(job.poll())  # "Place" spinning
            placed = job.finish_on_main()
        except resolve_bridge.ResolveError as exc:
            self._render_progress(job.poll(), failed=True)
            self.status(f"Generated, but not placed: {exc}")
            return loading
        landed = [entry for entry in placed if "placed" in entry]
        failed_entries = [entry for entry in placed if "error" in entry]
        self.last_result_path = placed[0]["path"] if placed else None
        self.items["OpenResult"].Enabled = bool(self.last_result_path)
        lines = [f"{label}: {value}" for label, value in job.texts.items()]
        lines += [f"Not placed: {entry['label']}: {entry['error']}" for entry in failed_entries]
        lines += job.warnings
        self.notes(lines)
        self._render_progress(job.poll(), failed=bool(failed_entries) and not landed)
        where = ", ".join(f"{entry['label']} on {entry['where']}" for entry in landed)
        took = f"Done in {int(snapshot['elapsed'])}s."
        if landed:
            self.status(f"{took} Placed {where}.")
        elif failed_entries:
            self.status(f"{took} The files were downloaded but none could be placed.")
        else:
            self.status(f"{took} The workflow returned no media.")
        self.refresh_source(quiet=True)
        self.reload_workflows(quiet=True)  # refreshes the balance after a run
        return True

    def _open_last_result(self) -> None:
        if self.last_result_path:
            _open_path(self.last_result_path)

    # ── history ─────────────────────────────────────────────────────────────

    def _refresh_history(self) -> None:
        """Redraw the history list, only when its rows changed, and in one go.

        Rows added one by one with drawing on show up as a sweep down the
        list; drawing is held until they are all in.
        """
        tree = self.items["History"]
        rows = history.load()
        self._history_rows = rows
        drawn = []
        for entry in rows:
            outputs = entry.get("outputs") or []
            drawn.append(
                (
                    str(entry.get("tool") or entry.get("model") or ""),
                    ", ".join(str(o.get("where") or "") for o in outputs if o.get("where")) or "-",
                    str(entry.get("at", ""))[5:16],
                    str(entry.get("at", "")),
                    f"From {entry.get('source') or entry.get('clip') or 'nothing'}",
                )
            )
        signature = tuple(drawn)
        if signature == self._history_signature:
            return
        widths = [210, 90]
        if self._history_signature is not None:
            try:  # keep the columns as the user dragged them
                widths = [int(tree.ColumnWidth[column]) for column in range(2)]
            except Exception:  # noqa: BLE001
                pass
        self._history_signature = signature
        try:
            tree.UpdatesEnabled = False
            tree.Clear()
            tree.ColumnCount = 3
            tree.SetHeaderLabels(["Name", "Placed", "When"])
            for name, where, when, stamp, tip in drawn:
                row = tree.NewItem()
                row.Text[0] = name
                row.Text[1] = where
                row.Text[2] = when
                row.StatusTip[0] = stamp
                row.ToolTip[0] = tip
                tree.AddTopLevelItem(row)
            for column, width in enumerate(widths):
                tree.ColumnWidth[column] = width
        finally:
            tree.UpdatesEnabled = True

    def _selected_history(self) -> dict | None:
        tree = self.items["History"]
        selected = tree.SelectedItems()
        if not selected:
            return None
        # SelectedItems() is a 1-based map; the first entry is the current row.
        first = selected[1] if 1 in selected else list(selected.values())[0]
        stamp = str(first.StatusTip[0] or "")
        for entry in self._history_rows:
            if str(entry.get("at", "")) == stamp:
                return entry
        return None

    def _history_paths(self, entry: dict) -> list[str]:
        paths = [str(o.get("path")) for o in (entry.get("outputs") or []) if o.get("path")]
        if not paths and entry.get("path"):
            paths = [str(entry["path"])]
        return paths

    def _on_history_picked(self) -> None:
        entry = self._selected_history()
        if not entry:
            return
        lines = [f"{label}: {value}" for label, value in (entry.get("texts") or {}).items()]
        lines += [str(note) for note in (entry.get("warnings") or [])]
        self.items["HistoryNotes"].Text = "\n".join(lines)

    def _history_action(self, action: str) -> None:
        entry = self._selected_history()
        if not entry:
            self.status("Select a row first.")
            return
        paths = [path for path in self._history_paths(entry) if os.path.isfile(path)]
        if not paths:
            self.status("That result is no longer on disk.")
            return
        if action == "open":
            _open_path(paths[0])
            return
        self.refresh_source(quiet=True)
        if self.source is None:
            self.status(str(self.items["SourceInfo"].Text))
            return
        try:
            placed = []
            for path in paths:
                media_item = resolve_bridge.import_media(self.session, path)
                placed.append(
                    resolve_bridge.place_result(
                        self.session,
                        media_item,
                        self.source,
                        placement=self.placement,
                        with_audio=bool(self.settings.get("place_result_audio", True)),
                    )
                )
            where = ", ".join(resolve_bridge.describe_placement(entry) for entry in placed)
            self.status(f"Placed again on {where}.")
        except resolve_bridge.ResolveError as exc:
            self.status(str(exc))

    # ── account and settings ────────────────────────────────────────────────

    def _save_settings(self) -> None:
        items = self.items
        try:
            max_height = int(float(items["MaxHeight"].Text or 720))
            run_timeout = float(items["RunTimeout"].Text or 900)
        except ValueError:
            self.status("Max height and the time limit must be numbers.")
            return
        before = (str(self.settings.get("api_key", "")), self._base_url())
        self.settings = config.update(
            last_values=self.settings.get("last_values") or {},  # typed into a form and not yet saved
            api_key=str(items["ApiKey"].Text or "").strip(),
            base_url=str(items["BaseUrl"].Text or "").strip() or credentials.DEFAULT_BASE_URL,
            work_dir=str(items["WorkDir"].Text or "").strip(),
            export_max_height=max_height,
            export_quality=QUALITIES[int(items["Quality"].CurrentIndex)],
            run_timeout_seconds=run_timeout,
            export_include_audio=bool(items["IncludeAudio"].Checked),
            place_result_audio=bool(items["PlaceAudio"].Checked),
            last_placement=self.placement,
            follow_timeline=bool(items["FollowTimeline"].Checked),
            keep_on_top=bool(items["KeepOnTop"].Checked),
        )
        self.status("Saved.")
        self._account_switched(before)

    def _account_switched(self, before: tuple) -> None:
        """After anything that may have changed the key or the server
        (``before`` is the pair as it was): start over with what the new
        account has, and show whose it is."""
        if before != (str(self.settings.get("api_key", "")), self._base_url()):
            # Another account or server: what was listed is no longer true,
            # and the lists its nodes filled in (its voices, say) are not ours.
            self._state_account += 1
            self._states.clear()
            self._state_tried.clear()
            self._state_retry.clear()
            self._state_due = None
            with self._node_lock:
                self._state_fetch.clear()
            self._late_lists = None
            if self.loader is not None:
                self.loader.stop()  # still fetching for the account before: its answers are not wanted
                self.loader = None
            # What this account was last seen to have on this server, when it
            # has been here before. Never what the account before it had:
            # with nobody signed in, or somebody new, the lists start empty.
            self.node_tools = tools.read_nodes(self._base_url(), self._account())
            self.workflow_tools = tools.tools_from_snapshot(tools.read_snapshot(self._base_url(), self._account()))
            self.balance = ""
            self._rebuild_show_filter()
            self._rebuild_list()
            self._select_tool(self.tool.id if self.tool else "")
            self.reload_workflows(quiet=True)
        self._show_account_info()
        self._show_detail()

    def _test_connection(self) -> None:
        try:
            cfg = api.Config(
                api_key=str(self.items["ApiKey"].Text or "").strip(),
                base_url=str(self.items["BaseUrl"].Text or "").strip(),
                timeout=30,
            )
            balance = api.balance_text(api.account_credits(cfg))
            self.status("Connected." + (f" Balance: {balance}." if balance else ""))
        except api.KaitoiAPIError as exc:
            self.status(f"Connection failed: {exc}")

    # ── window ──────────────────────────────────────────────────────────────

    def _geometry(self) -> list[int] | None:
        try:
            raw = self.win.Geometry
            return [int(raw[index]) for index in (1, 2, 3, 4)]
        except Exception:  # noqa: BLE001
            return None

    def _save_geometry(self) -> None:
        """Remember where the window was left.

        Resolve reports the position a title bar lower than it was asked for,
        so the offset seen at open time is taken back out; otherwise the window
        would creep down the screen every time it opened.
        """
        now, shown = self._geometry(), self._shown_geometry
        if not now or not shown:
            return
        dx, dy = shown[0] - self._asked_geometry[0], shown[1] - self._asked_geometry[1]
        geometry = [now[0] - dx, now[1] - dy, now[2], now[3]]
        if geometry[2] > 200 and geometry[3] > 200:
            self._remember_values()
            self.settings = config.update(
                panel_geometry=geometry,
                last_values=self.settings.get("last_values") or {},
                node_origins={origin: bool(self.items[box_id].Checked) for origin, box_id, _ in ORIGINS},
                fits_only=bool(self.items["FitsOnly"].Checked),
                media_only=bool(self.items["MediaOnly"].Checked),
                favorites_only=bool(self.items["FavoritesOnly"].Checked),
                list_columns=self._columns,
            )

    def _on_close(self, _event=None) -> None:
        if self._uninstall is not None or self._removed:
            # Kaitoi is being removed, or is gone: nothing is kept for a next time.
            self.closed = True
            return
        waiting = getattr(self, "_signin", None)
        if waiting is not None:
            waiting.cancel()  # nobody is left to take its answer
        if self.job is not None and self.job.running:
            self.job.cancel()
            self.job.step()  # aborts a running export now, while we still own the thread
        if self.loader is not None:
            self.loader.stop()
        if self._bulk is not None:
            self._bulk["stop"].set()
        busy.release(self._owner)
        self._read_columns()
        self._save_geometry()
        self.closed = True

    def step(self) -> bool:
        """One pass of the window's loop. Returns True while a network thread is busy."""
        self.disp.StepLoop()  # dispatch pending clicks/close events
        if self.closed:
            return False
        return self._on_tick()

    def show(self) -> None:
        """Show the window and pump it until closed. Blocks the caller."""
        self.win.Show()
        self._shown_geometry = self._geometry()
        try:
            while not self.closed:
                busy = self.step()
                time.sleep(WORKER_YIELD_SECONDS if busy else IDLE_SLEEP_SECONDS)
        finally:
            self.win.Hide()


def launch(session: resolve_bridge.Session | None = None) -> None:
    """Open the window. Blocks until it is closed."""
    session = session or resolve_bridge.connect()
    try:
        # One window at a time: a second launch (the menu, or a button on the
        # Kaitoi effect) brings the open one forward instead.
        existing = session.ui_manager()[0].FindWindow(WINDOW_ID)
        if existing is not None:
            existing.Show()
            existing.Raise()
            return
    except Exception:  # noqa: BLE001 - no window to find
        pass
    Panel(session).show()
