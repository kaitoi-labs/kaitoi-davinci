"""The window without Resolve: a ``ui.Panel`` over stand-in widgets.

Enough for the code that decides what the form, the list and the columns
hold. Nothing here draws; a widget is a bag of the properties the window sets
and reads back.
"""

from __future__ import annotations

import threading

from kaitoi_resolve import ui


class Widget:
    def __init__(self, name: str = "") -> None:
        object.__setattr__(self, "_values", {"Text": "", "PlainText": "", "CurrentIndex": 0, "Checked": False, "Value": 0, "PlaceholderText": "", "ToolTip": ""})
        object.__setattr__(self, "_entries", [])
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "ColumnWidth", {})
        object.__setattr__(self, "Icon", {})

    def __getattr__(self, name):
        values = object.__getattribute__(self, "_values")
        if name == "CurrentText":
            entries, index = self._entries, values["CurrentIndex"]
            return entries[index] if 0 <= index < len(entries) else ""
        if name in values:
            return values[name]
        return lambda *args, **kwargs: Widget()  # any method of a real widget

    def __setattr__(self, name, value):
        self._values[name] = value

    def Clear(self):
        self._entries.clear()
        self._values["CurrentIndex"] = -1

    def AddItem(self, label):
        self._entries.append(label)
        if self._values["CurrentIndex"] == -1:
            self._values["CurrentIndex"] = 0


class Row:
    """A row of the list: what the window writes on it, by column."""

    def __init__(self) -> None:
        self.Text, self.TextColor, self.StatusTip, self.ToolTip, self.Icon = {}, {}, {}, {}, {}
        self.children: list = []
        self.Selected = self.Expanded = False
        self.Flags: dict = {}

    def AddChild(self, row) -> None:
        self.children.append(row)


class Tree(Widget):
    """The list itself: enough for the window to rebuild it for real."""

    def __init__(self) -> None:
        super().__init__("Tools")
        object.__setattr__(self, "top", [])

    def NewItem(self):
        return Row()

    def AddTopLevelItem(self, row) -> None:
        self.top.append(row)

    def Clear(self) -> None:
        self.top.clear()

    def rows(self) -> list:
        """The text of every row that names a tool, in the order drawn."""
        return [child.Text.get(0, "") for heading in self.top for child in heading.children]


class Widgets(dict):
    def __missing__(self, key):
        self[key] = Widget(key)
        return self[key]


class Icons:
    def Icon(self, spec):
        return spec["File"]


class Window(Widget):
    def __init__(self, width: int) -> None:
        super().__init__("window")
        self.Geometry = {1: 0, 2: 0, 3: width, 4: 700}


class FinishedLoader:
    """A loader that has just brought these thumbnails and has nothing else to say."""

    done, running = True, False

    def __init__(self, thumbnails=()):
        self.thumbnails = list(thumbnails)
        self.stopped = False

    def take(self):
        out, self.thumbnails = self.thumbnails, []
        return {"tools": None, "snapshot": None, "error": None, "balance": None, "nodes": None, "thumbnails": out}

    def stop(self):
        self.stopped = True


def panel(settings: dict | None = None, node_tools=(), workflow_tools=(), width: int | None = None) -> ui.Panel:
    made = object.__new__(ui.Panel)
    made.settings = {"api_key": "k", "base_url": "http://127.0.0.1:9", "last_values": {}, "request_timeout_seconds": 5, **(settings or {})}
    made.items = Widgets()
    made.ui = Icons()
    made.win = Window(width) if width else None
    made.source = None
    made.job = None
    made.loader = None
    made.catalog = ui.NODES
    made.workflow_tools = list(workflow_tools)
    made.node_tools = list(node_tools)
    made._node_fetch = {}
    made._node_lock = threading.Lock()
    made._installed = {}
    made._registered = None
    made._bulk = None
    made._uninstaller = None
    made._uninstall = None
    made._removed = False
    made._shown = {}
    made._list_signature = None
    made.tool = None
    made.view = ui.VIEW_BROWSE
    made._tool_items = {}
    made._rows = []
    made._prompt_field = None
    made._overflow = []
    made._form_tool_id = ""
    made._show_keys = ["cat:"]
    made._tiles = {}
    made._states = {}
    made._state_fetch = {}
    made._state_tried = set()
    made._state_due = None
    made._state_retry = set()
    made._state_account = 0
    made._late_lists = None
    made._form_tool = None
    made._held_picks = {}
    made._unmatched = []
    made._form = ui.FORM_PLAIN
    made._columns = [204, 116, 64]
    made._columns_set = False
    made._columns_drawn = None
    made._columns_width = 0
    made._columns_checked = 0.0
    made._updating = False
    made._owner = "test"
    return made


def boxes(made: ui.Panel) -> dict:
    """What each row of the form shows: ``(kind, text, position or placeholder)`` by field name."""
    shown = {}
    for index, (field, kind) in enumerate(made._rows):
        if kind in ("choice", "bool"):
            combo = made._row("RowCombo", index)
            shown[field.name] = (kind, combo.CurrentText, combo.CurrentIndex)
        else:
            box = made._row({"media": "RowPath", "slider": "RowValue"}.get(kind, "RowEdit"), index)
            shown[field.name] = (kind, box.Text, box.PlaceholderText)
    return shown


def row_of(made: ui.Panel, name: str) -> int:
    return next(index for index, (field, _) in enumerate(made._rows) if field.name == name)
