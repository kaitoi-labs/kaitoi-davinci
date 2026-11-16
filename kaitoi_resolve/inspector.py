"""Running a Kaitoi tool from the Inspector.

A Kaitoi effect or generator carries its tool's inputs as native Inspector
controls, with Generate and Cancel buttons (see :mod:`templates`). A button
notes its action on the clip's composition and starts the Kaitoi script; the
script lands here.

This module reads the controls back, works out what the clip stands for on
the timeline, runs the same :class:`pipeline.Job` the window uses, and writes
progress into the clip's Status box. There is no window: the Inspector is the
interface. Resolve runs each button's script on its own, alongside anything
else that is running, so a run that takes minutes does not hold Resolve up.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from . import busy, config, pipeline, resolve_bridge, templates, tools

RUNNING_KEY = "Kaitoi.Running"
TICK_SECONDS = 0.1


def pending_action(comp: Any) -> str:
    """The action a button left on this composition, cleared as it is read."""
    if comp is None:
        return ""
    try:
        action = str(comp.GetData(templates.ACTION_KEY) or "")
        if action:
            comp.SetData(templates.ACTION_KEY, "")
        return action
    except Exception:  # noqa: BLE001 - not a composition a Kaitoi button belongs to
        return ""


def _core(comp: Any) -> Any:
    core = comp.FindTool(templates.CORE)
    if core is None:
        raise resolve_bridge.ResolveError("This clip is not a Kaitoi effect.")
    return core


def read_spec(comp: Any) -> dict[str, Any]:
    raw = _core(comp).GetInput(templates.SPEC)
    try:
        spec = json.loads(str(raw or ""))
    except ValueError:
        spec = None
    if not isinstance(spec, dict) or "tool" not in spec:
        raise resolve_bridge.ResolveError("This Kaitoi clip carries no workflow. Add it again from the Kaitoi window.")
    return spec


def read_values(comp: Any, spec: dict[str, Any], tool: tools.Tool) -> tuple[dict[str, Any], dict[str, str]]:
    """The Inspector's current values: ``(values by field, files by field)``.

    Raises :class:`tools.ToolInputError` naming the control that cannot be sent.
    """
    core = _core(comp)
    fields = {field.name: field for field in tool.inputs}
    values: dict[str, Any] = {}
    files: dict[str, str] = {}
    for control in spec.get("controls") or []:
        field = fields.get(control.get("field"))
        if field is None:
            continue
        raw = core.GetInput(control["id"])
        how = control.get("read")
        if how == "file":
            path = os.path.expanduser(str(raw or "").strip())
            if path:
                if not os.path.isfile(path):
                    raise tools.ToolInputError(f"{field.label}: no file at {path}")
                files[field.name] = path
        elif how == "check":
            values[field.name] = bool(raw)
        elif how == "number":
            if raw is None:
                continue
            values[field.name] = int(round(float(raw))) if control.get("integer") else float(raw)
        elif how == "combo":
            options = control.get("options") or []
            index = int(float(raw or 0)) - int(control.get("offset") or 0)
            if 0 <= index < len(options):
                values[field.name] = options[index]
        else:
            values[field.name] = tools.parse_value(field, raw)
    return values, files


def _instance_id(comp: Any) -> str:
    """This clip's own id, kept on its composition so the clip can be found."""
    instance = str(comp.GetData(templates.INSTANCE_KEY) or "")
    if not instance:
        instance = uuid.uuid4().hex
        comp.SetData(templates.INSTANCE_KEY, instance)
    return instance


def _carries(item: Any, instance: str) -> str:
    """Does ``item`` hold the composition ``instance``? Returns how: "generator",
    "effect" or ""."""
    try:
        for index in range(1, int(item.GetFusionCompCount() or 0) + 1):
            comp = item.GetFusionCompByIndex(index)
            if comp is not None and str(comp.GetData(templates.INSTANCE_KEY) or "") == instance:
                return "generator"
    except Exception:  # noqa: BLE001
        pass
    try:
        for effect in item.GetEffects("fusion") or []:
            comp = effect.GetFusionComp()
            if comp is not None and str(comp.GetData(templates.INSTANCE_KEY) or "") == instance:
                return "effect"
    except Exception:  # noqa: BLE001 - older Resolve versions have no effects call
        pass
    return ""


def find_carrier(session: resolve_bridge.Session, comp: Any) -> tuple[Any, str]:
    """The timeline clip this composition belongs to, and how it carries it.

    The clip whose button was pressed is almost always selected or under the
    playhead, so those are looked at first; the whole timeline only after.
    """
    timeline = session.timeline
    instance = _instance_id(comp)
    seen: set[str] = set()

    def check(item: Any) -> tuple[Any, str] | None:
        try:
            key = str(item.GetUniqueId())
        except Exception:  # noqa: BLE001
            return None
        if key in seen:
            return None
        seen.add(key)
        how = _carries(item, instance)
        return (item, how) if how else None

    for item in resolve_bridge._selected_items(timeline):
        found = check(item)
        if found:
            return found
    playhead = resolve_bridge._playhead_frame(timeline, resolve_bridge.timeline_fps(timeline))
    tracks = range(int(timeline.GetTrackCount("video")), 0, -1)
    if playhead is not None:
        for index in tracks:
            for item in resolve_bridge._track_items(timeline, "video", index):
                if item.GetStart() <= playhead < item.GetEnd():
                    found = check(item)
                    if found:
                        return found
    for index in tracks:
        for item in resolve_bridge._track_items(timeline, "video", index):
            found = check(item)
            if found:
                return found
    raise resolve_bridge.ResolveError("Could not find this Kaitoi clip on the current timeline.")


def composition_of(item: Any, how: str, instance: str) -> Any:
    """The Kaitoi composition ``item`` carries, fetched afresh."""
    if how == "generator":
        for index in range(1, int(item.GetFusionCompCount() or 0) + 1):
            comp = item.GetFusionCompByIndex(index)
            if comp is not None and str(comp.GetData(templates.INSTANCE_KEY) or "") == instance:
                return comp
    else:
        for effect in item.GetEffects("fusion") or []:
            comp = effect.GetFusionComp()
            if comp is not None and str(comp.GetData(templates.INSTANCE_KEY) or "") == instance:
                return comp
    return None


class _Clip:
    """A steady handle on a Kaitoi clip's composition.

    Resolve drops a composition's script handle when it leaves the page the
    clip is on, which an export does. So the composition is looked up again
    through its timeline clip whenever the old handle has gone dead.
    """

    def __init__(self, comp: Any) -> None:
        self.comp = comp
        self.item: Any = None
        self.how = ""
        self.instance = ""

    def bind(self, item: Any, how: str, instance: str) -> None:
        self.item, self.how, self.instance = item, how, instance

    def _fresh(self) -> Any:
        if self.item is not None:
            try:
                comp = composition_of(self.item, self.how, self.instance)
                if comp is not None:
                    self.comp = comp
            except Exception:  # noqa: BLE001 - the clip was deleted
                pass
        return self.comp

    def get_data(self, key: str) -> str:
        for comp in (self.comp, None):
            try:
                target = comp if comp is not None else self._fresh()
                return str(target.GetData(key) or "")
            except Exception:  # noqa: BLE001 - dead handle; try once more, afresh
                continue
        return ""

    def set_data(self, key: str, value: str) -> None:
        for comp in (self.comp, None):
            try:
                target = comp if comp is not None else self._fresh()
                target.SetData(key, value)
                return
            except Exception:  # noqa: BLE001
                continue

    def set_status(self, text: str) -> None:
        for comp in (self.comp, None):
            try:
                target = comp if comp is not None else self._fresh()
                target.FindTool(templates.CORE).SetInput(templates.STATUS, text)
                return
            except Exception:  # noqa: BLE001
                continue


STATUS_SECONDS = 0.5  # how often progress within one step is written to the clip


def progress_line(snapshot: dict[str, Any]) -> str:
    """One line for the clip's Status box: which step of how many, and what it is doing."""
    stage = snapshot.get("stage")
    step = stage + 1 if isinstance(stage, int) and stage >= 0 else 1
    return f"{step}/{len(pipeline.STAGES)}  {snapshot.get('status') or ''}".rstrip()


def run(session: resolve_bridge.Session, comp: Any) -> None:
    """Run the clip's tool with the Inspector's values. Blocks until it ends."""
    clip = _Clip(comp)
    last = {"text": None}

    def status(text: str) -> None:
        if text != last["text"]:
            last["text"] = text
            clip.set_status(text)

    if str(comp.GetData(RUNNING_KEY) or ""):
        status("Already running. Press Cancel to stop it.")
        return
    owner = uuid.uuid4().hex
    other = busy.held_by_other(owner)
    if other or not busy.acquire(owner, "a run"):
        status(f"Kaitoi is busy with {other or 'another run'}. Press Generate again when it has finished.")
        return
    comp.SetData(RUNNING_KEY, "1")
    comp.SetData(templates.CANCEL_KEY, "")
    try:
        settings = config.load()
        if not str(settings.get("api_key", "")).strip():
            status("Not signed in. Open Workspace > Scripts > Edit > Kaitoi, then Account.")
            return
        spec = read_spec(comp)
        tool = templates.tool_from_dict(spec["tool"])
        values, files = read_values(comp, spec, tool)
        busy.acquire(owner, tool.name)
        item, how = find_carrier(session, comp)
        clip.bind(item, how, _instance_id(comp))
        source = resolve_bridge.source_for_item(session, item, generator=how == "generator")
        supplies = resolve_bridge.source_supplies(source)
        if how == "effect":
            fed = next((f for f in tool.media_inputs if f.media in ("video", "image")), None)
        else:
            fed = tool.media_inputs[0] if tool.media_inputs else None
        primary = fed if fed is not None and fed.media in supplies else None
        if fed is not None and primary is None:
            need = {"video": "a clip", "image": "a frame", "audio": "sound"}[fed.media]
            status(f"This needs {need}, and there is none under this clip.")
            return
        job = pipeline.Job(
            session,
            source,
            tool,
            values=values,
            files=files,
            primary=primary,
            placement=str(settings.get("last_placement") or "new_track"),
            settings=settings,
        )
        job.start()
        shown_at = 0.0
        while job.running:
            job.step()
            if clip.get_data(templates.CANCEL_KEY) and not job._stop.is_set():
                job.cancel()
            snapshot = job.poll()
            # Every change of step shows at once; progress within a step
            # (the queue, a percentage) a couple of times a second.
            line = progress_line(snapshot)
            now = time.monotonic()
            if line != last["text"] and (line.split("  ", 1)[0] != str(last["text"] or "").split("  ", 1)[0] or now - shown_at >= STATUS_SECONDS):
                status(line)
                shown_at = now
            busy.renew(owner)
            time.sleep(TICK_SECONDS)
        snapshot = job.poll()
        if snapshot["error"]:
            first = snapshot["error"].splitlines()[0]
            if "\n" in snapshot["error"]:
                config.log(f"job error detail:\n{snapshot['error']}", "ERROR")
            status(first)
            return
        # Importing and placing take a moment and happen right here, with no
        # loop to report them: say so first, or the clip looks finished and idle.
        status(f"{len(pipeline.STAGES)}/{len(pipeline.STAGES)}  {pipeline.STAGES[-1]}: adding the result to the timeline...")
        placed = job.finish_on_main()
        landed = [entry for entry in placed if "placed" in entry]
        lines = [f"Done in {int(snapshot['elapsed'])}s."]
        if landed:
            lines[0] += " Placed " + ", ".join(f"{entry['label']} on {entry['where']}" for entry in landed) + "."
        lines += [f"{label}: {value}" for label, value in job.texts.items()]
        lines += [f"Not placed: {entry['label']}: {entry['error']}" for entry in placed if "error" in entry]
        lines += job.warnings
        status("\n".join(lines))
    except (resolve_bridge.ResolveError, tools.ToolInputError) as exc:
        status(str(exc))
    except Exception as exc:  # noqa: BLE001 - never leave the clip saying "running"
        config.log(f"inspector run crashed: {exc!r}", "ERROR")
        status(f"Something went wrong: {exc}. Details are in {config.log_path()}")
    finally:
        clip.set_data(RUNNING_KEY, "")
        busy.release(owner)


def handle(session: resolve_bridge.Session, comp: Any) -> bool:
    """Deal with a button press on a Kaitoi clip. False when there was none,
    or when the button asked for the Kaitoi window."""
    action = pending_action(comp)
    if action == "generate":
        config.log("generate pressed in the Inspector")
        run(session, comp)
        return True
    return False
