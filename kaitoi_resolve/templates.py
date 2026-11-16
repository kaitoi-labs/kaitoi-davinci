"""Kaitoi tools as native Resolve effects and generators.

Resolve cannot dock a script's window, and it does not draw controls a script
adds to a clip after the fact. What it does draw, natively, in the Inspector,
are the controls of a Fusion *template*. So every tool gets a template of its
own, written from the tool's contract: its inputs become real Inspector
controls (text boxes, sliders, dropdowns, checkboxes, file pickers), followed
by Generate, Cancel and a status line.

Two templates per tool:

- an **effect**, for tools that take a clip or a frame. A script can apply it
  to a clip (``TimelineItem.AddEffect``), and it appears in the Effects
  Library under Fusion Effects, in the Kaitoi folder, for dragging onto clips;
- a **generator**, for every tool. It appears under Generators, in the Kaitoi
  folder. Laid on a track like an adjustment clip, it runs the tool on
  whatever plays beneath it, or creates something new in its place.

The buttons carry a few lines of Lua that note which button was pressed on the
composition and start the Kaitoi script, which then reads the controls back
(see :mod:`inspector`). Nothing here talks to Resolve: it writes files.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from typing import Any, Callable

try:  # macOS and Linux
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from . import config, tools

FOLDER = "Kaitoi"  # the folder the templates sit in, in the Effects Library
CORE = "KaitoiCore"  # the tool inside the template that owns the controls
SPEC = "KaitoiSpec"  # hidden control: the tool and how to read each control
STATUS = "KaitoiStatus"
ACTION_KEY = "Kaitoi.Action"  # composition data a button sets before launching
CANCEL_KEY = "Kaitoi.Cancel"
INSTANCE_KEY = "Kaitoi.Instance"
LAUNCHER = "Kaitoi.py"


def templates_root() -> str:
    """Resolve's per-user Fusion templates folder."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Templates")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", home), "Blackmagic Design/DaVinci Resolve/Support/Fusion/Templates")
    return os.path.join(home, ".local/share/DaVinciResolve/Fusion/Templates")


def folder(kind: str) -> str:
    """Where Kaitoi's ``kind`` ("Effects" or "Generators") templates go."""
    return os.path.join(templates_root(), "Edit", kind, FOLDER)


def template_name(tool: tools.Tool) -> str:
    """The name the tool carries in the Effects Library and on the clip."""
    name = re.sub(r'[\\/:*?"<>|]+', " ", tool.name).strip()
    return re.sub(r"\s+", " ", name)[:60] or "Kaitoi"


def control_id(field_name: str) -> str:
    return "K_" + (re.sub(r"[^0-9A-Za-z_]", "_", field_name) or "value")


def has_effect(tool: tools.Tool) -> bool:
    """Can this tool run on a clip? It needs a clip or a frame to take."""
    return any(field.media in ("video", "image") for field in tool.media_inputs)


# ── Lua ─────────────────────────────────────────────────────────────────────

_BUTTON_LUA = r'''
local ok, err = pcall(function()
	comp:SetData("__KEY__", "__VALUE__")
	if "__LAUNCH__" ~= "yes" then return end
	local home = os.getenv("HOME") or os.getenv("USERPROFILE") or ""
	local candidates = {
		home .. "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/__LAUNCHER__",
		(os.getenv("APPDATA") or "") .. "/Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Edit/__LAUNCHER__",
		(os.getenv("PROGRAMDATA") or "") .. "/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/__LAUNCHER__",
		home .. "/.local/share/DaVinciResolve/Fusion/Scripts/Edit/__LAUNCHER__",
		"/opt/resolve/Fusion/Scripts/Edit/__LAUNCHER__",
	}
	for _, path in ipairs(candidates) do
		if bmd.fileexists(path) then
			comp:RunScript(path)
			return
		end
	end
	print("[Kaitoi] The Kaitoi script is not installed. Run install.sh from the plugin folder.")
end)
if not ok then print("[Kaitoi] " .. tostring(err)) end
'''


def _lua_string(code: str) -> str:
    escaped = code.strip().replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def _button_code(action: str) -> str:
    """What a button runs: mark the action on the composition, start the script."""
    key, launch = (CANCEL_KEY, "no") if action == "cancel" else (ACTION_KEY, "yes")
    code = (
        _BUTTON_LUA.replace("__KEY__", key)
        .replace("__VALUE__", action)
        .replace("__LAUNCH__", launch)
        .replace("__LAUNCHER__", LAUNCHER)
    )
    return _lua_string(code)


def _text(value: Any) -> str:
    """A Lua string literal for arbitrary text."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n").replace("\t", " ")
    return f'"{text}"'


def _number(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else repr(number)


# ── Controls ────────────────────────────────────────────────────────────────


def _control(control: str, label: str, data_type: str, extra: list[str]) -> list[str]:
    return [
        f"LINKS_Name = {_text(label)},",
        f'LINKID_DataType = "{data_type}",',
        f'INPID_InputControl = "{control}",',
        "INP_External = false,",
        "INP_Passive = true,",
        'ICS_ControlPage = "Controls",',
        *extra,
    ]


def _field_control(field: tools.Field, source: str = "workflow") -> tuple[list[str], dict[str, Any]]:
    """One input as an Inspector control, and how its value is read back.

    ``source`` ("workflow" or "node") names the entry that sends nothing.
    """
    kind = field.widget
    read: dict[str, Any] = {"id": control_id(field.name), "field": field.name}
    if kind == "media":
        read["read"] = "file"
        return _control("FileControl", field.label, "Text", ["FC_ClipBrowse = false,"]), read
    if kind == "choice":
        # The control lists each option by its label and hands back a position;
        # the option's own value (a word, a number) is what gets sent.
        options = [value for value, _ in field.choices]
        labels = [label for _, label in field.choices]
        # It starts on the entry that sends nothing, which names the value the
        # tool will then use. Left alone, a later change of that value in
        # Studio is what runs, not the value this template was drawn with.
        shown = [tools.leave_label(source, tools.default_label(field))] + labels
        read.update(read="combo", options=options, offset=1)
        extra = ["INP_Integer = true,", "INP_Default = 0,", 'CC_LabelPosition = "Horizontal",']
        extra += [f"{{ CCS_AddString = {_text(option)} }}," for option in shown]
        return _control("ComboControl", field.label, "Number", extra), read
    if kind == "bool":
        if isinstance(field.default, bool):
            read["read"] = "check"
            return _control("CheckboxControl", field.label, "Number", ["INP_Integer = true,", f"INP_Default = {1 if field.default else 0},"]), read
        read.update(read="combo", options=[True, False], offset=1)
        extra = ["INP_Integer = true,", "INP_Default = 0,", 'CC_LabelPosition = "Horizontal",']
        extra += [f"{{ CCS_AddString = {_text(choice)} }}," for choice in tools.bool_choices(source)]
        return _control("ComboControl", field.label, "Number", extra), read
    if kind == "number" and isinstance(field.default, (int, float)) and not isinstance(field.default, bool):
        read.update(read="number", integer=field.data_type == "int")
        low = field.minimum if isinstance(field.minimum, (int, float)) else None
        high = field.maximum if isinstance(field.maximum, (int, float)) else None
        extra = [f"INP_Default = {_number(field.default)},"]
        if field.data_type == "int":
            extra.append("INP_Integer = true,")
        if low is not None and high is not None and high > low:
            extra += [f"INP_MinScale = {_number(low)},", f"INP_MaxScale = {_number(high)},"]
            extra += [f"INP_MinAllowed = {_number(low)},", f"INP_MaxAllowed = {_number(high)},"]
            return _control("SliderControl", field.label, "Number", extra), read
        return _control("ScrewControl", field.label, "Number", extra), read
    # Free text. Numbers and colours with no known default are typed, so an
    # empty box can mean "leave it to the workflow".
    read["read"] = "text"
    extra = [f"TEC_Lines = {4 if field.is_prompt else 1},", "TEC_Wrap = true,"]
    if isinstance(field.default, str) and field.default:
        extra.append(f"INPS_DefaultText = {_text(field.default)},")
    return _control("TextEditControl", field.label, "Text", extra), read


def tool_to_dict(tool: tools.Tool) -> dict[str, Any]:
    """A tool as plain data, so a template can carry its own definition."""

    def field(item: tools.Field) -> dict[str, Any]:
        return {
            "name": item.name,
            "label": item.label,
            "data_type": item.data_type,
            "default": item.default,
            "options": list(item.options),
            "option_labels": list(item.option_labels),
            "minimum": item.minimum,
            "maximum": item.maximum,
            "step": item.step,
            "dynamic": item.dynamic,
        }

    return {
        "id": tool.id,
        "source": tool.source,
        "name": tool.name,
        "description": tool.description,
        "category": tool.category,
        "tags": list(tool.tags),
        "inputs": [field(item) for item in tool.inputs],
        "outputs": [{"name": o.name, "label": o.label, "data_type": o.data_type} for o in tool.outputs],
        "template_id": tool.template_id,
        "endpoint_id": tool.endpoint_id,
        "node_type": tool.node_type,
        "origin": tool.origin,
    }


def tool_from_dict(data: dict[str, Any]) -> tools.Tool:
    return tools.Tool(
        id=str(data.get("id") or ""),
        source=str(data.get("source") or "workflow"),
        name=str(data.get("name") or "Kaitoi"),
        description=str(data.get("description") or ""),
        category=str(data.get("category") or ""),
        tags=tuple(data.get("tags") or ()),
        inputs=tuple(
            tools.Field(
                name=str(item["name"]),
                label=str(item.get("label") or item["name"]),
                data_type=str(item.get("data_type") or ""),
                default=item.get("default"),
                options=tuple(item.get("options") or ()),
                option_labels=tuple(item.get("option_labels") or ()),
                minimum=item.get("minimum"),
                maximum=item.get("maximum"),
                step=item.get("step"),
                dynamic=bool(item.get("dynamic")),
            )
            for item in data.get("inputs") or []
        ),
        outputs=tuple(
            tools.Output(name=str(o["name"]), label=str(o.get("label") or o["name"]), data_type=str(o.get("data_type") or ""))
            for o in data.get("outputs") or []
        ),
        template_id=str(data.get("template_id") or ""),
        endpoint_id=str(data.get("endpoint_id") or ""),
        node_type=str(data.get("node_type") or ""),
        origin=str(data.get("origin") or ""),
    )


def _controls(tool: tools.Tool, kind: str) -> tuple[list[tuple[str, list[str]]], dict[str, Any]]:
    """The ordered controls of a template, and the spec stored beside them.

    The media input the timeline fills gets no control: for an effect that is
    the first clip or frame input (the clip it sits on), for a generator the
    first media input of any kind (what plays beneath it).
    """
    if kind == "Effects":
        fed = next((f for f in tool.media_inputs if f.media in ("video", "image")), None)
    else:
        fed = tool.media_inputs[0] if tool.media_inputs else None
    prompt = next((f for f in tool.param_inputs if f.is_prompt), None)
    ordered = ([prompt] if prompt else []) + [f for f in tool.inputs if f is not fed and f is not prompt]

    controls: list[tuple[str, list[str]]] = []
    reads: list[dict[str, Any]] = []
    for field in ordered:
        lines, read = _field_control(field, tool.source)
        controls.append((read["id"], lines))
        reads.append(read)
    controls.append(("KaitoiGenerate", _control("ButtonControl", "Generate", "Number", [f"BTNCS_Execute = {_button_code('generate')},"])))
    controls.append(("KaitoiCancel", _control("ButtonControl", "Cancel", "Number", [f"BTNCS_Execute = {_button_code('cancel')},"])))
    controls.append(
        (STATUS, _control("TextEditControl", "Status", "Text", ["TEC_Lines = 3,", "TEC_Wrap = true,", "TEC_ReadOnly = true,", 'INPS_DefaultText = "Ready.",']))
    )
    spec = {"version": 1, "kind": kind, "tool": tool_to_dict(tool), "controls": reads}
    return controls, spec


def _render(tool: tools.Tool, kind: str) -> str:
    """The ``.setting`` text of the tool's effect or generator template."""
    controls, spec = _controls(tool, kind)
    macro = "Kaitoi_" + (re.sub(r"[^0-9A-Za-z]", "", tool.name) or "Tool")[:40]
    tab = "\t"

    user_controls = []
    for name, lines in controls:
        body = "\n".join(f"{tab * 7}{line}" for line in lines)
        user_controls.append(f"{tab * 6}{name} = {{\n{body}\n{tab * 6}}},")
    hidden = _control(
        "TextEditControl", "Kaitoi", "Text", ["TEC_Lines = 1,", "TEC_ReadOnly = true,", "IC_Visible = false,", f"INPS_DefaultText = {_text(json.dumps(spec, separators=(',', ':')))},"]
    )
    hidden_body = "\n".join(f"{tab * 7}{line}" for line in hidden)
    user_controls.append(f"{tab * 6}{SPEC} = {{\n{hidden_body}\n{tab * 6}}},")

    published = []
    if kind == "Effects":
        published.append(f'{tab * 4}MainInput1 = InstanceInput {{\n{tab * 5}SourceOp = "{CORE}",\n{tab * 5}Source = "Input",\n{tab * 4}}},')
    for index, (name, _) in enumerate(controls, 1):
        published.append(
            f'{tab * 4}Input{index} = InstanceInput {{\n{tab * 5}SourceOp = "{CORE}",\n{tab * 5}Source = "{name}",\n{tab * 5}Page = "Controls",\n{tab * 4}}},'
        )

    if kind == "Effects":
        core = f"{CORE} = Transform {{\n{tab * 5}CtrlWZoom = false,\n{tab * 5}Inputs = {{\n{tab * 5}}},"
    else:
        core = (
            f"{CORE} = Background {{\n{tab * 5}CtrlWZoom = false,\n{tab * 5}Inputs = {{\n"
            f"{tab * 6}GlobalOut = Input {{ Value = 119, }},\n{tab * 6}Width = Input {{ Value = 1920, }},\n"
            f"{tab * 6}Height = Input {{ Value = 1080, }},\n{tab * 6}UseFrameFormatSettings = Input {{ Value = 1, }},\n"
            f'{tab * 6}["Gamut.SLogVersion"] = Input {{ Value = FuID {{ "SLog2" }}, }},\n'
            f"{tab * 6}TopLeftAlpha = Input {{ Value = 0, }},\n{tab * 5}}},"
        )
    return (
        "{\n"
        f"{tab}Tools = ordered() {{\n"
        f"{tab * 2}{macro} = MacroOperator {{\n"
        f"{tab * 3}CtrlWZoom = false,\n"
        f"{tab * 3}Inputs = ordered() {{\n" + "\n".join(published) + f"\n{tab * 3}}},\n"
        f"{tab * 3}Outputs = {{\n{tab * 4}MainOutput1 = InstanceOutput {{\n{tab * 5}SourceOp = \"{CORE}\",\n{tab * 5}Source = \"Output\",\n{tab * 4}}},\n{tab * 3}}},\n"
        f"{tab * 3}ViewInfo = GroupInfo {{ Pos = {{ 0, 0 }} }},\n"
        f"{tab * 3}Tools = ordered() {{\n"
        f"{tab * 4}{core}\n"
        f"{tab * 5}ViewInfo = OperatorInfo {{ Pos = {{ 0, 0 }} }},\n"
        f"{tab * 5}UserControls = ordered() {{\n" + "\n".join(user_controls) + f"\n{tab * 5}}},\n"
        f"{tab * 4}}},\n"
        f"{tab * 3}}},\n"
        f"{tab * 2}}},\n"
        f"{tab}}},\n"
        f'{tab}ActiveTool = "{macro}",\n'
        "}\n"
    )


def effect_setting(tool: tools.Tool) -> str:
    return _render(tool, "Effects")


def generator_setting(tool: tools.Tool) -> str:
    return _render(tool, "Generators")


# ── Installing into Resolve's template folders ──────────────────────────────
#
# "Installing" a tool writes its template files (plain text, no node code)
# where Resolve looks for them. What is installed is recorded in a manifest,
# so the window can show it and so nothing Kaitoi did not write is ever
# touched. Resolve loads new generators at once but new *effects* only when it
# starts, which is why an installed tool can still be waiting for a restart.

READY, RESTART, NOT_INSTALLED = "ready", "restart", "none"
_lock = threading.Lock()


def _manifest_path() -> str:
    config.app_dir()
    return os.path.join(config.APP_DIR, "templates.json")


def _read_manifest() -> dict[str, Any]:
    try:
        with open(_manifest_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    tools_map = data.get("tools")
    return {"tools": tools_map if isinstance(tools_map, dict) else {}, "files": list(data.get("files") or [])}


@contextlib.contextmanager
def _locked():
    """One writer at a time, across every Kaitoi script and process.

    Several can be running: the window, one script per Generate pressed, a
    second window. Each loads this module afresh, so a lock held in memory
    is not shared between them; a lock on a file is.
    """
    with _lock:
        handle = None
        try:
            config.app_dir()
            handle = open(os.path.join(config.APP_DIR, "templates.lock"), "a+")
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                handle.seek(0)
                for _ in range(60):  # each try waits up to ten seconds
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        continue
        except OSError as exc:  # never refuse to install because a lock file could not be made
            config.log(f"could not lock the template record: {exc}", "WARN")
        try:
            yield
        finally:
            if handle is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    else:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                handle.close()


def _write_whole(path: str, text: str) -> None:
    """Write ``path`` in one step: readers see the old file or the new one,
    and two writers never share a half-written file."""
    descriptor, partial = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + ".", suffix=".part")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(partial, path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


def _write_manifest(manifest: dict[str, Any]) -> None:
    try:
        _write_whole(_manifest_path(), json.dumps(manifest))
    except OSError as exc:
        config.log(f"could not record the installed templates: {exc}", "WARN")


def _write_if_changed(path: str, text: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            if fh.read() == text:
                return False
    except OSError:
        pass
    _write_whole(path, text)
    return True


def _is_ours(path: str) -> bool:
    """Does the template at ``path`` carry Kaitoi's hidden control? A file
    without it was put there by someone else."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return SPEC in fh.read()
    except OSError:
        return False


def _remove_files(paths) -> None:
    for path in paths:
        # Only ever a file inside a Kaitoi folder: nothing else is ours.
        if os.path.basename(os.path.dirname(path)) == FOLDER and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _strays(manifest: dict[str, Any]) -> list[str]:
    """Kaitoi templates on disk that the manifest does not list: written by an
    earlier version of this plugin, before it kept a record.

    Told apart by the hidden control every Kaitoi template carries. A file
    without it belongs to someone else and is never returned.
    """
    listed = {path for entry in manifest["tools"].values() for path in entry.get("files") or []} | set(manifest["files"])
    found: list[str] = []
    for kind in ("Generators", "Effects"):
        directory = folder(kind)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            if not name.endswith(".setting") or path in listed:
                continue
            if _is_ours(path):
                found.append(path)
                icon = path[: -len(".setting")] + ".png"
                if icon not in listed and os.path.isfile(icon):
                    found.append(icon)
    return found


def installed() -> dict[str, dict[str, Any]]:
    """What Kaitoi has installed in Resolve: ``{tool id: {name, kinds, files}}``."""
    with _locked():
        return dict(_read_manifest()["tools"])


def _unique_name(tool: tools.Tool, taken: dict[str, dict[str, Any]]) -> str:
    """The tool's name in the Effects Library. Two tools with one title (an
    account's own nodes often share them) get a number, so neither overwrites
    the other."""
    mine = taken.get(tool.id)
    if mine and mine.get("name"):
        return str(mine["name"])
    base = template_name(tool)
    used = {str(entry.get("name") or "").lower() for key, entry in taken.items() if key != tool.id}

    def someone_elses(name: str) -> bool:
        # A template of that name that someone else put in the Kaitoi folder is
        # theirs: the tool takes the next name instead of writing over it.
        for kind in ("Generators", "Effects"):
            path = os.path.join(folder(kind), f"{name}.setting")
            if os.path.isfile(path) and not _is_ours(path):
                return True
        return False

    name, count = base, 1
    while name.lower() in used or someone_elses(name):
        count += 1
        name = f"{base} {count}"
    return name


def install(tool: tools.Tool, *, icon: str | None = None) -> dict[str, Any]:
    """Write the tool's templates where Resolve looks. Returns its manifest entry.

    ``icon`` is a PNG shown beside the template in the Effects Library.
    """
    with _locked():
        manifest = _read_manifest()
        name = _unique_name(tool, manifest["tools"])
        kinds = ["Generators"] + (["Effects"] if has_effect(tool) else [])
        files: list[str] = []
        for kind in kinds:
            directory = folder(kind)
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"{name}.setting")
            _write_if_changed(path, _render(tool, kind))
            files.append(path)
            if icon and icon.lower().endswith(".png") and os.path.isfile(icon):
                try:
                    shutil.copyfile(icon, os.path.join(directory, f"{name}.png"))
                    files.append(os.path.join(directory, f"{name}.png"))
                except OSError as exc:
                    config.log(f"could not copy the icon for {name}: {exc}", "WARN")
        previous = manifest["tools"].get(tool.id) or {}
        _remove_files(set(previous.get("files") or []) - set(files))  # it no longer takes a clip, say
        entry = {"name": name, "kinds": kinds, "files": files}
        manifest["tools"][tool.id] = entry
        manifest["files"] = [path for path in manifest["files"] if path not in files]
        _write_manifest(manifest)
        return entry


def uninstall(tool_id: str) -> bool:
    """Remove one tool's templates from Resolve. False when it was not installed."""
    with _locked():
        manifest = _read_manifest()
        entry = manifest["tools"].pop(tool_id, None)
        if entry is None:
            return False
        _remove_files(entry.get("files") or [])
        _write_manifest(manifest)
        return True


def uninstall_all() -> int:
    """Remove everything Kaitoi installed in Resolve. Returns how many tools.

    That includes Kaitoi templates an earlier version left without a record.
    """
    with _locked():
        manifest = _read_manifest()
        strays = _strays(manifest)
        unlisted = [path for path in list(manifest["files"]) + strays if path.endswith(".setting") and os.path.isfile(path)]
        count = len(manifest["tools"]) + len({os.path.basename(path) for path in unlisted})
        for entry in manifest["tools"].values():
            _remove_files(entry.get("files") or [])
        _remove_files(manifest["files"])  # files recorded by an earlier version
        _remove_files(strays)
        _write_manifest({"tools": {}, "files": []})
        return count


WORKFLOW_PREFIX = "workflow:"  # how a workflow's id starts; a node's starts otherwise


def sync(all_tools, *, icons: dict[str, str] | None = None, prefix: str = WORKFLOW_PREFIX, keep=()) -> int:
    """Install every given tool, and remove the installed ones with ``prefix``
    that are no longer among them (a workflow that was unpublished). Tools of
    another kind, installed one by one, are left alone.

    ``keep`` names tools to leave installed although they are not given: a
    workflow that is still published but could not be read this time.

    Returns how many tools were installed.
    """
    icons = icons or {}
    kept = set(keep)
    count = 0
    for tool in all_tools:
        kept.add(tool.id)
        try:
            install(tool, icon=icons.get(tool.id))
            count += 1
        except Exception as exc:  # noqa: BLE001 - one tool that cannot be written must not stop the rest
            config.log(f"could not install the template for '{tool.name}': {exc}", "WARN")
    for tool_id in [key for key in installed() if key.startswith(prefix) and key not in kept]:
        uninstall(tool_id)
    with _locked():
        manifest = _read_manifest()
        if manifest["files"]:
            _remove_files(manifest["files"])  # an earlier version tracked bare files
            manifest["files"] = []
            _write_manifest(manifest)
    return count


def state(tool_id: str, registered: set | None, catalog: dict[str, dict[str, Any]] | None = None) -> str:
    """Where a tool stands in Resolve.

    ``registered`` holds the names (lower case) of the Kaitoi effects Resolve
    has loaded, or None when this Resolve cannot say (before version 21).
    READY: usable from the Inspector now. RESTART: installed, but it takes a
    clip and Resolve has not loaded its effect yet. NOT_INSTALLED.

    When Resolve cannot say, an installed tool counts as READY: telling
    someone to restart for ever would be worse than not telling them once.
    """
    entry = (catalog if catalog is not None else installed()).get(tool_id)
    if not entry:
        return NOT_INSTALLED
    if registered is not None and "Effects" in (entry.get("kinds") or []) and str(entry.get("name") or "").lower() not in registered:
        return RESTART
    return READY


def install_many(
    cfg: Any,
    wanted,
    progress: dict[str, Any],
    stop: threading.Event,
    *,
    icon_for: Callable[[tools.Tool], str | None] = lambda tool: None,
    pause: float = 0.75,
) -> None:
    """Install each of ``wanted``, fetching the pins of any node that has not
    loaded them. Meant for a background thread: it touches files and the
    network only, and reports through ``progress``.

    ``progress`` gains ``done``, ``failed``, ``loaded`` (the tools installed,
    with their pins), ``waiting`` (seconds left of a pause the server asked
    for) and, at the end, ``finished``. ``pause`` spaces out the lookups: the
    server allows 120 requests a minute from one address, shared with
    everything else the window does.
    """
    progress.update(done=0, failed=0, loaded=[], waiting=0, finished=False)

    def waiting(seconds: int) -> None:
        progress["waiting"] = seconds

    try:
        for tool in wanted:
            if stop.is_set():
                break
            asked = not tool.ready
            try:
                loaded = tools.load_node_patiently(cfg, tool, should_stop=stop.is_set, on_wait=waiting) if asked else tool
                progress["waiting"] = 0
                if loaded is None:  # stopped during a pause
                    break
                install(loaded, icon=icon_for(loaded))
                progress["loaded"].append(loaded)
            except Exception as exc:  # noqa: BLE001 - one bad node must not stop the rest
                progress["waiting"] = 0
                progress["failed"] += 1
                config.log(f"could not install '{tool.name}': {exc}", "WARN")
            progress["done"] += 1
            if asked and stop.wait(pause):
                break
    finally:
        progress["finished"] = True
