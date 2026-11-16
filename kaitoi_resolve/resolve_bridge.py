"""Everything that talks to DaVinci Resolve.

Responsibilities, in the order the plugin uses them:

1. connect to the running Resolve instance (inside Resolve *or* from a shell);
2. find what the editor is pointing at: the In/Out range, the selected clip,
   or the clip under the playhead;
3. export that range as a small video, a still or a sound file, and put every
   project setting the export touched back afterwards;
4. import generated files back and lay them on Kaitoi tracks, leaving the
   existing cut untouched.

Nothing here knows about Kaitoi; nothing in :mod:`api` knows about Resolve.
"""

from __future__ import annotations

import glob
import importlib.machinery
import importlib.util
import os
import sys
import time
from typing import Any, Callable

from . import config

# Where Resolve keeps its scripting library, inside its own program folder.
# Used only when the plugin runs from an external shell: inside Resolve the
# host hands the same module to the script as ``bmd``.
_LIB_PATHS = {
    "darwin": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    "win32": r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
    "linux": "/opt/resolve/libs/Fusion/fusionscript.so",
}

RENDER_DONE = ("Complete", "Failed", "Cancelled", "Canceled")


class ResolveError(RuntimeError):
    pass


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _is_scripting_module(module: Any) -> bool:
    return module is not None and callable(getattr(module, "scriptapp", None)) and callable(getattr(module, "UIDispatcher", None))


def _scripting_module(handed: Any = None):
    """Resolve's scripting module, for ``scriptapp`` and ``UIDispatcher``.

    Never by way of Blackmagic's ``DaVinciResolveScript.py``. That loader
    sits in Resolve's support folder (Developer/Scripting/Modules), which on
    macOS and Windows every account of the computer can write to, so
    importing it runs whatever any of them put there, as the person using
    Kaitoi. All it does is load the library named below, which is in
    Resolve's own program folder: no easier to tamper with than Resolve.
    """
    host = sys.modules.get("__main__")
    # Inside Resolve: what the host handed the script, or loaded for itself.
    for module in (handed, getattr(host, "bmd", None), sys.modules.get("fusionscript")):
        if _is_scripting_module(module):
            return module
    lib_path = os.environ.get("RESOLVE_SCRIPT_LIB") or _LIB_PATHS[_platform_key()]
    try:
        loader = importlib.machinery.ExtensionFileLoader("fusionscript", lib_path)
        spec = importlib.util.spec_from_loader("fusionscript", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    except (ImportError, OSError, AttributeError) as exc:
        raise ResolveError(
            "Could not load the DaVinci Resolve scripting module. Is Resolve installed, "
            "and is scripting enabled in Preferences > System > General?"
        ) from exc
    sys.modules["fusionscript"] = module  # a library is loaded once in a process
    return module


class Session:
    """A live handle on Resolve: app, project manager, project, timeline."""

    def __init__(self, resolve: Any, dvr: Any) -> None:
        self.resolve = resolve
        self.dvr = dvr

    # ── objects ─────────────────────────────────────────────────────────────

    @property
    def project(self):
        project = self.resolve.GetProjectManager().GetCurrentProject()
        if project is None:
            raise ResolveError("No project is open in Resolve.")
        return project

    @property
    def timeline(self):
        timeline = self.project.GetCurrentTimeline()
        if timeline is None:
            raise ResolveError("No timeline is open. Open one on the Edit page and try again.")
        return timeline

    @property
    def media_pool(self):
        return self.project.GetMediaPool()

    def ui_manager(self):
        """Fusion's UIManager + a dispatcher, used to build the plugin window."""
        fusion = self.resolve.Fusion()
        if fusion is None:
            raise ResolveError("Fusion UI is unavailable; cannot build the panel.")
        ui = fusion.UIManager
        return ui, self.dvr.UIDispatcher(ui)


def connect(resolve: Any = None, module: Any = None) -> Session:
    """Attach to the running Resolve, from inside it or from an external shell.

    ``resolve`` and ``module`` are the handle and the scripting module (its
    ``bmd``) that Resolve injects into a menu script's globals; pass them
    through when available. Otherwise (external shell, Console) the scripting
    module is loaded from Resolve's program folder and asked for the running
    instance.
    """
    dvr = _scripting_module(module)
    host = sys.modules.get("__main__")
    if resolve is None:
        resolve = getattr(host, "resolve", None)
    if resolve is None:
        # Started from a control on a Fusion effect: the host hands over
        # Fusion's own module, which reaches the same application.
        fusion_module = getattr(host, "bmd", None)
        try:
            resolve = fusion_module.scriptapp("Resolve") if fusion_module is not None else None
        except Exception:  # noqa: BLE001
            resolve = None
    if resolve is None:
        resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise ResolveError("DaVinci Resolve is not running, or scripting access is disabled.")
    return Session(resolve, dvr)


# ── Timecode / frames ───────────────────────────────────────────────────────


def timecode_to_frame(timecode: str, fps: float) -> int:
    """Absolute frame number for a Resolve timecode string.

    A ``;`` separator marks drop-frame timecode, where two frame numbers are
    skipped each minute except every tenth minute — so the arithmetic differs
    from non-drop and must be handled, not approximated.
    """
    if not timecode:
        raise ValueError("empty timecode")
    drop = ";" in timecode
    parts = timecode.replace(";", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"unparseable timecode: {timecode}")
    hours, minutes, seconds, frames = (int(p) for p in parts)
    nominal = int(round(fps))
    total = ((hours * 3600) + (minutes * 60) + seconds) * nominal + frames
    if drop and nominal in (30, 60):
        dropped_per_minute = 2 if nominal == 30 else 4
        elapsed_minutes = hours * 60 + minutes
        total -= dropped_per_minute * (elapsed_minutes - elapsed_minutes // 10)
    return total


def frame_to_timecode(frame: int, fps: float) -> str:
    nominal = max(1, int(round(fps)))
    frames = int(frame) % nominal
    total_seconds = int(frame) // nominal
    return "%02d:%02d:%02d:%02d" % (
        total_seconds // 3600,
        (total_seconds // 60) % 60,
        total_seconds % 60,
        frames,
    )


def timeline_fps(timeline) -> float:
    try:
        return float(timeline.GetSetting("timelineFrameRate"))
    except (TypeError, ValueError):
        return 24.0


def timeline_resolution(timeline) -> tuple[int, int]:
    try:
        width = int(timeline.GetSetting("timelineResolutionWidth"))
        height = int(timeline.GetSetting("timelineResolutionHeight"))
        if width > 0 and height > 0:
            return width, height
    except (TypeError, ValueError):
        pass
    return 1920, 1080


# ── Source selection ────────────────────────────────────────────────────────

MEDIA_KINDS = ("video", "image", "audio")


def _playhead_frame(timeline, fps: float) -> int | None:
    try:
        timecode = timeline.GetCurrentTimecode()
        return timecode_to_frame(timecode, fps) if timecode else None
    except Exception:  # noqa: BLE001 - some pages raise instead of answering
        return None


def _track_items(timeline, kind: str, index: int) -> list:
    return list(timeline.GetItemListInTrack(kind, index) or [])


def _item_track(timeline, item) -> tuple[str, int] | tuple[None, None]:
    """``(track type, track index)`` of a timeline item."""
    try:
        answer = item.GetTrackTypeAndIndex()
        if answer and answer[0] in ("video", "audio"):
            return str(answer[0]), int(answer[1])
    except Exception:  # noqa: BLE001 - older Resolve versions lack the call
        pass
    target_id = item.GetUniqueId()
    for kind in ("video", "audio"):
        for index in range(1, int(timeline.GetTrackCount(kind)) + 1):
            for candidate in _track_items(timeline, kind, index):
                if candidate.GetUniqueId() == target_id:
                    return kind, index
    return None, None


def _item_at_frame(timeline, frame: int) -> tuple[Any, int] | tuple[None, None]:
    """Topmost video item covering ``frame`` — the one the viewer is showing."""
    for index in range(int(timeline.GetTrackCount("video")), 0, -1):
        if timeline.GetIsTrackEnabled("video", index) is False:
            continue
        for item in _track_items(timeline, "video", index):
            if item.GetStart() <= frame < item.GetEnd():
                return item, index
    return None, None


def _audio_item_at_frame(timeline, frame: int) -> tuple[Any, int] | tuple[None, None]:
    """First audio item covering ``frame``, scanning A1 upwards."""
    for index in range(1, int(timeline.GetTrackCount("audio")) + 1):
        if timeline.GetIsTrackEnabled("audio", index) is False:
            continue
        for item in _track_items(timeline, "audio", index):
            if item.GetStart() <= frame < item.GetEnd():
                return item, index
    return None, None


def _range_has(timeline, kind: str, start: int, end: int) -> bool:
    """Does any enabled ``kind`` track hold an item overlapping ``[start, end)``?"""
    for index in range(1, int(timeline.GetTrackCount(kind)) + 1):
        if timeline.GetIsTrackEnabled(kind, index) is False:
            continue
        for item in _track_items(timeline, kind, index):
            if item.GetStart() < end and item.GetEnd() > start:
                return True
    return False


def read_marks(timeline) -> tuple[int, int] | None:
    """The timeline's In/Out range as absolute record frames, end exclusive.

    Resolve reports marks as offsets from the timeline's first frame, with an
    inclusive Out. Returns None unless both In and Out are set.
    """
    try:
        marks = timeline.GetMarkInOut() or {}
    except Exception:  # noqa: BLE001 - older Resolve versions lack the call
        return None
    for kind in ("video", "audio"):
        span = marks.get(kind) or {}
        if "in" in span and "out" in span:
            base = int(timeline.GetStartFrame())
            mark_in, mark_out = int(span["in"]), int(span["out"])
            if mark_out >= mark_in:
                return base + mark_in, base + mark_out + 1
    return None


def _selected_items(timeline) -> list:
    try:
        return list(timeline.GetSelectedClips() or [])
    except Exception:  # noqa: BLE001 - added in Resolve 21.0.4
        return []


def _media_path(item) -> str | None:
    try:
        media_item = item.GetMediaPoolItem()
        return media_item.GetClipProperty("File Path") if media_item else None
    except Exception:  # noqa: BLE001 - titles and generators have no media
        return None


def _source(
    session: Session,
    *,
    origin: str,
    name: str,
    start: int,
    end: int,
    playhead: int | None,
    item: Any = None,
    track_type: str | None = None,
    track_index: int | None = None,
    solo_audio_tracks: tuple[int, ...] = (),
    has_video: bool | None = None,
    has_audio: bool | None = None,
) -> dict[str, Any]:
    timeline = session.timeline
    fps = timeline_fps(timeline)
    if has_video is None:
        has_video = end > start and _range_has(timeline, "video", start, end)
    if has_audio is None:
        has_audio = end > start and _range_has(timeline, "audio", start, end)
    # The still that represents this source: the playhead when it sits inside
    # the range (park on the frame you mean), otherwise the first frame.
    still_frame = playhead if playhead is not None and start <= playhead < max(end, start + 1) else start
    return {
        "origin": origin,  # "marks" | "selection" | "playhead" | "carrier" | "empty"
        "name": name,
        "start": int(start),
        "end": int(end),
        "duration": max(0, int(end) - int(start)),
        "playhead": playhead,
        "still_frame": int(still_frame),
        "fps": fps,
        "timeline_name": timeline.GetName(),
        "item": item,
        "track_type": track_type,
        "track_index": track_index,
        "file_path": _media_path(item) if item is not None else None,
        "has_video": bool(has_video),
        "has_audio": bool(has_audio),
        "solo_audio_tracks": tuple(solo_audio_tracks),
    }


SOURCE_MODES = ("auto", "marks", "selection", "playhead")


def current_source(session: Session, mode: str = "auto") -> dict[str, Any]:
    """What the editor is pointing at, as a time range on the timeline.

    ``auto`` tries, in order: the In/Out range when both marks are set, the
    selected clip, the clip under the playhead (topmost enabled video track,
    which is what the viewer shows, so a result placed on a Kaitoi track is
    what gets picked next), then an audio clip under the playhead. With none
    of those the source is empty: there is nothing to send, only a place to
    put something new.

    A source made of audio clips only reports ``has_video`` False and names the
    tracks to solo, so an export carries those clips and not the whole mix.
    """
    timeline = session.timeline
    fps = timeline_fps(timeline)
    playhead = _playhead_frame(timeline, fps)

    if mode in ("auto", "marks"):
        marks = read_marks(timeline)
        if marks:
            return _source(
                session, origin="marks", name="In/Out range", start=marks[0], end=marks[1], playhead=playhead
            )
        if mode == "marks":
            raise ResolveError("No In/Out range is set on the timeline. Mark one with I and O.")

    if mode in ("auto", "selection"):
        selected = [item for item in _selected_items(timeline) if item.GetStart() is not None]
        if selected:
            located = [(item,) + _item_track(timeline, item) for item in selected]
            videos = [entry for entry in located if entry[1] == "video"]
            audios = [entry for entry in located if entry[1] == "audio"]
            start = min(int(entry[0].GetStart()) for entry in located)
            end = max(int(entry[0].GetEnd()) for entry in located)
            if videos:
                lead = max(videos, key=lambda entry: entry[2] or 0)
                name = lead[0].GetName() if len(videos) == 1 else f"{len(videos)} selected clips"
                return _source(
                    session,
                    origin="selection",
                    name=str(name),
                    start=start,
                    end=end,
                    playhead=playhead,
                    item=lead[0] if len(videos) == 1 else None,
                    track_type="video",
                    track_index=lead[2],
                    has_video=True,
                )
            if audios:
                lead = audios[0]
                name = lead[0].GetName() if len(audios) == 1 else f"{len(audios)} selected audio clips"
                return _source(
                    session,
                    origin="selection",
                    name=str(name),
                    start=start,
                    end=end,
                    playhead=playhead,
                    item=lead[0] if len(audios) == 1 else None,
                    track_type="audio",
                    track_index=lead[2],
                    solo_audio_tracks=tuple(sorted({int(entry[2]) for entry in audios if entry[2]})),
                    has_video=False,
                    has_audio=True,
                )
        if mode == "selection":
            raise ResolveError("No clip is selected in the timeline.")

    if playhead is not None:
        item, track_index = _item_at_frame(timeline, playhead)
        if item is None:
            try:  # on some pages only this call knows the current item
                item = timeline.GetCurrentVideoItem()
            except Exception:  # noqa: BLE001
                item = None
            if item is not None and not (item.GetStart() <= playhead < item.GetEnd()):
                item = None
            if item is not None:
                track_index = _item_track(timeline, item)[1]
        if item is not None:
            return _source(
                session,
                origin="playhead",
                name=str(item.GetName()),
                start=int(item.GetStart()),
                end=int(item.GetEnd()),
                playhead=playhead,
                item=item,
                track_type="video",
                track_index=track_index or 1,
                has_video=True,
            )
        item, track_index = _audio_item_at_frame(timeline, playhead)
        if item is not None:
            return _source(
                session,
                origin="playhead",
                name=str(item.GetName()),
                start=int(item.GetStart()),
                end=int(item.GetEnd()),
                playhead=playhead,
                item=item,
                track_type="audio",
                track_index=track_index,
                solo_audio_tracks=(int(track_index),),
                has_video=False,
                has_audio=True,
            )

    if mode == "playhead":
        raise ResolveError("No clip under the playhead. Park the playhead over a clip, then press Refresh.")
    anchor = playhead if playhead is not None else int(timeline.GetStartFrame())
    return _source(
        session, origin="empty", name="Nothing selected", start=anchor, end=anchor, playhead=playhead,
        has_video=False, has_audio=False,
    )


def source_for_item(session: Session, item: Any, *, generator: bool = False) -> dict[str, Any]:
    """The source a Kaitoi clip stands for.

    An effect sits on a real clip: that clip is the source. A generator lies
    on a track like an adjustment clip: the source is its range, and what
    there is to send is whatever plays on the tracks beneath it.
    """
    timeline = session.timeline
    fps = timeline_fps(timeline)
    playhead = _playhead_frame(timeline, fps)
    kind, index = _item_track(timeline, item)
    start, end = int(item.GetStart()), int(item.GetEnd())
    has_video = True
    if generator:
        has_video = any(
            other.GetStart() < end and other.GetEnd() > start
            for below in range(1, int(index or 1))
            if timeline.GetIsTrackEnabled("video", below) is not False
            for other in _track_items(timeline, "video", below)
        )
    return _source(
        session,
        origin="carrier",
        name=str(item.GetName()),
        start=start,
        end=end,
        playhead=playhead,
        item=None if generator else item,
        track_type=kind or "video",
        track_index=index,
        has_video=has_video,
    )


def find_effect(session: Session, name: str, folder: str = "") -> str | None:
    """The id Resolve gave a Fusion effect template, or None if it has not
    registered it. Resolve picks new effect templates up on its own schedule,
    so a template written a moment ago may not be there yet."""
    try:
        effects = session.resolve.GetEffectsList() or []
    except Exception:  # noqa: BLE001 - added in Resolve 21
        return None
    wanted = name.strip().lower()
    best = None
    for effect in effects:
        if str(effect.get("category")) != "fusion" or str(effect.get("name") or "").strip().lower() != wanted:
            continue
        effect_id = str(effect.get("id") or "")
        if folder and f"/{folder}/" in effect_id:
            return effect_id
        best = best or effect_id
    return best


def registered_effects(session: Session, folder: str) -> set | None:
    """The names (lower case) of the effects Resolve has loaded from ``folder``
    of its Fusion effects. Resolve builds this list when it starts.

    None when this Resolve cannot list its effects (the call arrived in
    version 21): "nothing is loaded" and "cannot tell" are different answers.
    """
    try:
        effects = session.resolve.GetEffectsList()
    except Exception:  # noqa: BLE001 - added in Resolve 21
        return None
    if effects is None:
        return None
    marker = f"/{folder}/"
    return {
        str(effect.get("name") or "").strip().lower()
        for effect in effects
        if str(effect.get("category")) == "fusion" and marker in str(effect.get("id") or "")
    }


def add_effect(item: Any, effect_id: str) -> Any:
    """Apply a Fusion effect to a clip and return its composition."""
    try:
        effect = item.AddEffect("fusion", effect_id)
    except Exception as exc:  # noqa: BLE001
        raise ResolveError(f"Resolve could not add the effect: {exc}") from exc
    comp = effect.GetFusionComp() if effect else None
    if comp is None:
        raise ResolveError("Resolve would not add the effect to this clip.")
    return comp


def insert_is_safe(session: Session, frame: int) -> bool:
    """Would inserting a clip at ``frame`` leave the cut alone?

    Resolve's insert pushes everything from that frame onwards, on every
    track. It is only harmless when nothing is there to push.
    """
    timeline = session.timeline
    for kind in ("video", "audio"):
        for index in range(1, int(timeline.GetTrackCount(kind)) + 1):
            if any(item.GetEnd() > frame for item in _track_items(timeline, kind, index)):
                return False
    return True


def insert_generator(session: Session, name: str) -> Any:
    """Insert a Fusion generator template at the playhead; returns its composition.

    Only call this where :func:`insert_is_safe` says so.
    """
    item = session.timeline.InsertFusionGeneratorIntoTimeline(name)
    if not item or item.GetStart() is None:
        raise ResolveError(f"Resolve does not know a generator called '{name}' yet.")
    comp = item.GetFusionCompByIndex(1)
    if comp is None:
        raise ResolveError("The generator was added but has no composition.")
    return comp


def source_supplies(source: dict[str, Any]) -> set[str]:
    """Which media kinds this source can be exported as."""
    kinds: set[str] = set()
    if source.get("has_video"):
        kinds.update(("video", "image"))
    if source.get("has_audio"):
        kinds.add("audio")
    return kinds


def describe_source(source: dict[str, Any], *, max_name: int = 40) -> str:
    fps = source["fps"]
    if source["origin"] == "empty":
        return f"Nothing selected  ·  new media lands at {frame_to_timecode(source['start'], fps)}"
    name = str(source["name"])
    if len(name) > max_name:  # long generated names have no break points; keep the row narrow
        name = name[: max_name - 1] + "…"
    where = {"marks": "In/Out", "selection": "Selected", "playhead": "Playhead"}.get(source["origin"], "")
    track = ""
    if source.get("track_index") and source.get("track_type"):
        track = f"  ·  {'V' if source['track_type'] == 'video' else 'A'}{source['track_index']}"
    seconds = source["duration"] / max(fps, 1.0)
    return (
        f"{where}: {name}{track}  ·  "
        f"{frame_to_timecode(source['start'], fps)} → {frame_to_timecode(source['end'], fps)}  "
        f"({source['duration']} frames, {seconds:.1f}s @ {fps:g}fps)"
    )


# ── Export (timeline → file) ────────────────────────────────────────────────

KEEP_PRESET_PREFIX = "Kaitoi restore "
AUDIO_ONLY_PRESET = "Audio Only"


def _scaled_size(timeline, max_height: int) -> tuple[int, int]:
    width, height = timeline_resolution(timeline)
    if max_height <= 0 or height <= max_height:
        return width, height
    scale = max_height / float(height)
    # Even dimensions: H.264 chroma subsampling rejects odd sizes.
    return max(2, int(round(width * scale)) // 2 * 2), max(2, int(max_height) // 2 * 2)


def _select_render_format(project, video_format: str, codec: str) -> bool:
    """Select a Deliver format/codec, coping with Resolve's cold Deliver state.

    Until the Deliver page has been touched once in a session,
    ``GetCurrentRenderFormatAndCodec`` reports ``unknown`` and the first
    ``SetCurrentRenderFormatAndCodec`` call is refused even for a valid pair.
    Querying the codec list (or loading a preset) initialises that state, after
    which the same call succeeds.
    """
    if project.SetCurrentRenderFormatAndCodec(video_format, codec):
        return True
    codecs = project.GetRenderCodecs(video_format) or {}
    if project.SetCurrentRenderFormatAndCodec(video_format, codec):
        return True
    if codecs and codec not in codecs.values():
        valid = ", ".join(sorted(codecs.values()))
        raise ResolveError(f"Codec '{codec}' is not available for {video_format}. Valid: {valid}")
    project.LoadRenderPreset("H.264 Master")
    return bool(project.SetCurrentRenderFormatAndCodec(video_format, codec))


# Quality label -> H.264 bit rate in kb/s at 1080p; scaled by output height.
_QUALITY_KBPS = {"Least": 2000, "Low": 4000, "Medium": 8000, "High": 16000, "Best": 32000}


def _apply_render_settings(project, settings: dict[str, Any], height: int) -> bool:
    """``SetRenderSettings`` with a tolerant ``VideoQuality``.

    The scripting docs allow quality as a label ("Medium") or a bit rate, but
    Resolve 21 refuses the label for H.264 in MP4 and rejects the whole dict.
    Retry with an equivalent bit rate, then without the key (Resolve's own
    automatic quality) so an unexpected codec still exports.
    """
    if project.SetRenderSettings(settings):
        return True
    quality = settings.get("VideoQuality")
    if isinstance(quality, str):
        kbps = _QUALITY_KBPS.get(quality, _QUALITY_KBPS["Medium"])
        kbps = max(500, int(kbps * min(1.0, height / 1080.0)))
        if project.SetRenderSettings({**settings, "VideoQuality": kbps}):
            return True
    if "VideoQuality" in settings:
        remaining = {k: v for k, v in settings.items() if k != "VideoQuality"}
        return bool(project.SetRenderSettings(remaining))
    return False


class ExportJob:
    """An in-flight Deliver render of one timeline range.

    Created by :func:`start_export`, advanced by :func:`poll_export`, closed by
    :func:`finish_export` (or :func:`abort_export`). Split this way so a UI
    timer can drive it: **every Resolve call must happen on the thread that
    owns the script**, because calls from a worker thread block while the UI
    thread sits in ``UIDispatcher.RunLoop()`` (and deadlock when the script
    runs inside Resolve). Network work belongs on a worker; this does not.

    It also remembers everything the export changes in the project (page,
    Deliver settings, In/Out marks, muted tracks) so closing it puts each one
    back the way the editor left it.
    """

    def __init__(self, *, kind: str, out_dir: str, name: str, extension: str, width: int, height: int) -> None:
        self.kind = kind
        self.out_dir = out_dir
        self.name = name
        self.extension = extension
        self.width = width
        self.height = height
        self.start_frame = 0
        self.end_frame = 0
        self.job_id: str | None = None
        self.previous_page: str | None = None
        self.previous_format: dict[str, Any] = {}
        self.previous_mode: int | None = None
        self.keep_preset: str | None = None
        self.previous_marks: dict[str, Any] | None = None
        self.previous_timecode: str | None = None
        self.muted_tracks: list[int] = []
        self.started_at = time.monotonic()
        self.last_status: dict[str, Any] = {}
        self.closed = False


def _drop_stale_keep_presets(project) -> None:
    """Remove restore presets a crashed export left behind. They are ours."""
    try:
        for name in project.GetRenderPresetList() or []:
            if isinstance(name, str) and name.startswith(KEEP_PRESET_PREFIX):
                project.DeleteRenderPreset(name)
    except Exception:  # noqa: BLE001 - housekeeping must never block an export
        pass


def _read_raw_marks(timeline) -> dict[str, Any] | None:
    try:
        marks = timeline.GetMarkInOut()
        return dict(marks) if isinstance(marks, dict) else None
    except Exception:  # noqa: BLE001 - older Resolve versions lack the call
        return None


def _restore_marks(timeline, raw: dict[str, Any] | None) -> None:
    """Put the editor's In/Out marks back: a render overwrites them.

    Resolve only sets a complete In/Out pair, and on a timeline whose picture
    and sound marks are linked it refuses to set one side alone. So a pair is
    restored for both sides at once, split marks are tried side by side, and a
    lone In or Out (which the API cannot set) is cleared instead of being
    left as the export's range.
    """
    if raw is None:
        return

    def pair(kind: str) -> tuple[int, int] | None:
        span = raw.get(kind) or {}
        return (int(span["in"]), int(span["out"])) if "in" in span and "out" in span else None

    try:
        timeline.ClearMarkInOut("all")
        video, audio = pair("video"), pair("audio")
        if video and (audio is None or audio == video):
            timeline.SetMarkInOut(video[0], video[1], "all")
        elif audio and video is None:
            timeline.SetMarkInOut(audio[0], audio[1], "all")
        elif video and audio:
            if not (timeline.SetMarkInOut(video[0], video[1], "video") and timeline.SetMarkInOut(audio[0], audio[1], "audio")):
                timeline.SetMarkInOut(video[0], video[1], "all")
                config.log("separate picture and sound In/Out marks could not be restored; both now use the picture range", "WARN")
        elif any((raw.get(kind) or {}) for kind in ("video", "audio")):
            config.log("a lone In or Out mark cannot be restored by script; the marks were cleared", "WARN")
    except Exception as exc:  # noqa: BLE001
        config.log(f"could not restore the In/Out marks: {exc}", "WARN")


def start_export(
    session: Session,
    source: dict[str, Any],
    *,
    kind: str = "video",
    out_dir: str | None = None,
    max_height: int = 720,
    video_format: str = "mp4",
    video_codec: str = "H264",
    quality: str = "Medium",
    include_audio: bool = True,
) -> ExportJob:
    """Queue and start a render of the source's timeline range; returns quickly.

    ``kind="video"`` renders the range as one clip, with its sound when
    ``include_audio`` is set. ``kind="image"`` renders the source's still frame
    as a PNG. ``kind="audio"`` renders the range's sound as a WAV; when the
    source names tracks to solo, every other audio track is muted for the
    render. The range comes from the timeline, so trims, grade and everything
    above the clip are baked in. On any failure the project is restored before
    raising.
    """
    if kind not in MEDIA_KINDS:
        raise ResolveError(f"Cannot export a '{kind}' from the timeline.")
    if source["duration"] <= 0:
        raise ResolveError("There is nothing on the timeline to send. Select a clip or mark a range.")
    project = session.project
    timeline = session.timeline
    out_dir = out_dir or config.work_dir()
    os.makedirs(out_dir, exist_ok=True)

    width, height = _scaled_size(timeline, max_height)
    if kind == "image":
        mark_in = mark_out = int(source.get("still_frame", source["start"]))
    else:
        mark_in, mark_out = int(source["start"]), int(source["end"]) - 1
    job = ExportJob(
        kind=kind,
        out_dir=out_dir,
        name=f"kaitoi_src_{time.strftime('%Y%m%d_%H%M%S')}_{mark_in}",
        extension={"image": "png", "audio": "wav"}.get(kind, video_format),
        width=width,
        height=height,
    )
    job.start_frame, job.end_frame = mark_in, mark_out + 1
    settings: dict[str, Any] = {
        "SelectAllFrames": False,
        "MarkIn": mark_in,
        "MarkOut": mark_out,
        "TargetDir": out_dir,
        "CustomName": job.name,
        "UniqueFilenameStyle": 1,  # suffix, so a re-run never silently overwrites
    }

    # Everything below changes project state the editor did not ask to have
    # changed. Record it first, restore it in _restore_after_export.
    job.previous_marks = _read_raw_marks(timeline)
    job.previous_page = session.resolve.GetCurrentPage()
    try:
        job.previous_timecode = timeline.GetCurrentTimecode() or None
    except Exception:  # noqa: BLE001
        job.previous_timecode = None
    solo = set(source.get("solo_audio_tracks") or ()) if kind == "audio" else set()
    if solo:
        # Tracks only switch off from an editing page: on the Deliver page the
        # call is accepted and ignored, and the render carries the whole mix.
        if job.previous_page == "deliver":
            session.resolve.OpenPage("edit")
        try:
            for index in range(1, int(timeline.GetTrackCount("audio")) + 1):
                if index in solo or timeline.GetIsTrackEnabled("audio", index) is False:
                    continue
                if not timeline.SetTrackEnable("audio", index, False) or timeline.GetIsTrackEnabled("audio", index):
                    raise ResolveError(
                        f"Could not mute A{index} to send this clip's sound on its own. Nothing was sent."
                    )
                job.muted_tracks.append(index)
        except Exception:
            _restore_after_export(session, job)
            raise
    # Render settings only take reliably from the Deliver page: queued from
    # another page Resolve has refused the job and, once, gone down with it.
    session.resolve.OpenPage("deliver")
    job.previous_format = project.GetCurrentRenderFormatAndCodec() or {}
    job.previous_mode = project.GetCurrentRenderMode()
    _drop_stale_keep_presets(project)
    keep = f"{KEEP_PRESET_PREFIX}{time.strftime('%H%M%S')}"
    try:
        # A saved preset is the only way to get every Deliver setting back,
        # including the "export video" switch an audio render turns off.
        if project.SaveAsNewRenderPreset(keep):
            job.keep_preset = keep
    except Exception:  # noqa: BLE001 - fall back to restoring format and mode
        job.keep_preset = None

    try:
        if kind == "audio":
            if not project.LoadRenderPreset(AUDIO_ONLY_PRESET):
                project.SetRenderSettings({"ExportVideo": False, "ExportAudio": True})
            settings.update(
                {"ExportVideo": False, "ExportAudio": True, "AudioFormat": "wav", "AudioBitDepth": 16, "AudioSampleRate": 48000}
            )
            project.SetCurrentRenderMode(1)
            if not project.SetRenderSettings(settings):
                raise ResolveError("Resolve rejected the audio render settings for this range.")
        else:
            if kind == "image":
                ok = _select_render_format(project, "png", "RGB8")
            else:
                ok = _select_render_format(project, video_format, video_codec)
            if not ok:
                raise ResolveError(
                    f"Resolve rejected render format/codec "
                    f"({'png/RGB8' if kind == 'image' else f'{video_format}/{video_codec}'})."
                )
            project.SetCurrentRenderMode(1)  # 1 = single clip (one file for the range)
            # On its own first: after an audio-only render Resolve keeps "export
            # video" off and ignores the switch when it arrives with a frame size.
            project.SetRenderSettings({"ExportVideo": True})
            settings.update(
                {
                    "ExportVideo": True,
                    "ExportAudio": bool(include_audio) and kind == "video",
                    "FormatWidth": width,
                    "FormatHeight": height,
                }
            )
            if kind == "video":
                settings["VideoQuality"] = quality
            if not _apply_render_settings(project, settings, height):
                raise ResolveError("Resolve rejected the render settings for this range.")
        job.job_id = project.AddRenderJob()
        if not job.job_id:
            raise ResolveError("Could not queue a render job for the range.")
        if not project.StartRendering([job.job_id], isInteractiveMode=False):
            raise ResolveError("Resolve refused to start the export render.")
    except Exception:
        _restore_after_export(session, job)
        raise
    return job


def poll_export(session: Session, job: ExportJob) -> tuple[str, int]:
    """``(state, percent)`` of the render; state is in RENDER_DONE when over."""
    status = session.project.GetRenderJobStatus(job.job_id) or {}
    job.last_status = status
    return str(status.get("JobStatus", "Rendering")), int(status.get("CompletionPercentage", 0) or 0)


def finish_export(session: Session, job: ExportJob) -> str:
    """Restore the project and return the rendered file's path.

    Call once the state from :func:`poll_export` is terminal. Raises if the
    render did not complete.
    """
    state = str(job.last_status.get("JobStatus", "Rendering"))
    written = _job_output_path(session, job) if state == "Complete" else None
    try:
        if state != "Complete":
            raise ResolveError(f"Export {state.lower()}: {job.last_status.get('Error') or 'no detail'}")
    finally:
        _restore_after_export(session, job)
    if written and os.path.isfile(written):
        return written
    return _find_rendered_file(job.out_dir, job.name, job.extension)


def abort_export(session: Session, job: ExportJob) -> None:
    """Stop a running render and restore the project.

    Resolve keeps rendering for a moment after it is told to stop, and ignores
    page changes until it has, so the restore waits for it.
    """
    project = session.project
    try:
        project.StopRendering()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                if not project.IsRenderingInProgress():
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.1)
    finally:
        _restore_after_export(session, job)


def _job_output_path(session: Session, job: ExportJob) -> str | None:
    """The file Resolve says this job wrote, read before the job is deleted."""
    try:
        for entry in session.project.GetRenderJobList() or []:
            if entry.get("JobId") == job.job_id and entry.get("OutputFilename"):
                return os.path.join(str(entry.get("TargetDir") or job.out_dir), str(entry["OutputFilename"]))
    except Exception:  # noqa: BLE001 - the folder scan below still finds it
        pass
    return None


def _restore_after_export(session: Session, job: ExportJob) -> None:
    """Undo what the export changed. Each step is independent of the others."""
    if job.closed:
        return
    job.closed = True
    project = session.project
    timeline = session.timeline

    def attempt(label: str, action) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - keep restoring the rest
            config.log(f"restore after export: {label} failed: {exc}", "WARN")

    if job.job_id:
        attempt("delete render job", lambda: project.DeleteRenderJob(job.job_id))
    restored = False
    if job.keep_preset:
        try:
            restored = bool(project.LoadRenderPreset(job.keep_preset))
        except Exception as exc:  # noqa: BLE001
            config.log(f"restore after export: load preset failed: {exc}", "WARN")
        attempt("delete restore preset", lambda: project.DeleteRenderPreset(job.keep_preset))
    if not restored:
        previous = job.previous_format
        if previous.get("format") and previous["format"] != "unknown":
            attempt(
                "render format",
                lambda: project.SetCurrentRenderFormatAndCodec(previous["format"], previous.get("codec", "")),
            )
        attempt("export video switch", lambda: project.SetRenderSettings({"ExportVideo": True}))
    if job.previous_mode is not None:
        attempt("render mode", lambda: project.SetCurrentRenderMode(job.previous_mode))
    _restore_marks(timeline, job.previous_marks)
    # Tracks only switch back on from an editing page, so leave Deliver first.
    current = None
    try:
        current = session.resolve.GetCurrentPage()
    except Exception:  # noqa: BLE001
        pass
    editing_page = job.previous_page if job.previous_page and job.previous_page != "deliver" else "edit"
    if current == "deliver" and (job.muted_tracks or job.previous_page != "deliver"):
        attempt("page", lambda: session.resolve.OpenPage(editing_page))
    for index in job.muted_tracks:
        attempt(f"unmute A{index}", lambda index=index: timeline.SetTrackEnable("audio", index, True))
    if job.previous_page == "deliver" and job.muted_tracks:
        attempt("page", lambda: session.resolve.OpenPage("deliver"))
    if job.previous_timecode:
        attempt("playhead", lambda: _restore_playhead(timeline, job.previous_timecode))


def _restore_playhead(timeline, timecode: str) -> None:
    """Put the playhead back where the editor left it.

    A render moves the playhead, and a stopped render moves it once more, to
    where it began, a moment *after* Resolve reports itself idle. Setting it
    once can therefore be undone, so it is held until it has stayed put.
    """
    steady = 0
    for _ in range(14):
        if timeline.GetCurrentTimecode() == timecode:
            steady += 1
            if steady >= 3:
                return
        else:
            steady = 0
            timeline.SetCurrentTimecode(timecode)
        time.sleep(0.15)
    config.log(f"could not hold the playhead at {timecode}", "WARN")


def render_source(
    session: Session,
    source: dict[str, Any],
    *,
    kind: str = "video",
    out_dir: str | None = None,
    max_height: int = 720,
    video_format: str = "mp4",
    video_codec: str = "H264",
    quality: str = "Medium",
    include_audio: bool = True,
    timeout: float = 600.0,
    on_status: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Blocking convenience around start/poll/finish, for scripts and tests.

    Do not call this from a worker thread while a UI loop is running; use the
    three-step form from the UI timer instead.
    """
    job = start_export(
        session,
        source,
        kind=kind,
        out_dir=out_dir,
        max_height=max_height,
        video_format=video_format,
        video_codec=video_codec,
        quality=quality,
        include_audio=include_audio,
    )
    if on_status:
        on_status(f"Exporting {kind}...")
    deadline = time.monotonic() + timeout
    while True:
        state, percent = poll_export(session, job)
        if state in RENDER_DONE:
            break
        if on_status:
            on_status(f"Exporting {kind}... {percent}%")
        if should_stop is not None and should_stop():
            abort_export(session, job)
            raise ResolveError("Export cancelled.")
        if time.monotonic() > deadline:
            abort_export(session, job)
            raise ResolveError(f"Export timed out after {timeout:.0f}s.")
        time.sleep(0.5)
    return finish_export(session, job)


def _find_rendered_file(out_dir: str, name: str, extension: str) -> str:
    """Locate the file Resolve just wrote.

    Resolve appends its own suffix (frame numbers for stills, a uniqueness
    counter for clips), so the exact filename is not knowable in advance —
    match on the prefix and take the newest.
    """
    matches = sorted(
        glob.glob(os.path.join(out_dir, f"{name}*")),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    matches = [m for m in matches if os.path.isfile(m)]
    if not matches:
        raise ResolveError(
            f"Render reported success but no file matching '{name}*.{extension}' "
            f"appeared in {out_dir}."
        )
    return matches[0]


_SOURCE_FILE_TYPES = {
    "video": (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mxf"),
    "audio": (".wav", ".mp3", ".m4a", ".aac", ".aif", ".aiff", ".flac", ".ogg"),
    "image": (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".exr"),
}


def source_file(source: dict[str, Any], kind: str = "video") -> str:
    """Use the source clip's original media file, skipping the render entirely.

    Fast, and lossless relative to the source, but it ignores trims, grades and
    everything else the timeline adds — so it is only offered as an explicit
    choice, and refuses when the file is missing or is the wrong kind of media.
    """
    path = source.get("file_path")
    if not path or not os.path.isfile(path):
        raise ResolveError("This source has no single readable media file; export the range instead.")
    if not path.lower().endswith(_SOURCE_FILE_TYPES.get(kind, ())):
        raise ResolveError(f"The source file is not {kind} media; export the range instead.")
    return path


# ── Import (file → timeline) ────────────────────────────────────────────────


BIN_NAME = "Kaitoi"
TRACK_PREFIX = "Kaitoi"


def _find_bin(session: Session):
    """The media pool's ``Kaitoi`` bin, or None when there is none yet."""
    root = session.media_pool.GetRootFolder()
    for folder in root.GetSubFolderList() or []:
        if folder.GetName() == BIN_NAME:
            return folder
    return None


def _same_folder(left, right) -> bool:
    try:
        return left is not None and right is not None and left.GetUniqueId() == right.GetUniqueId()
    except Exception:  # noqa: BLE001
        return False


def import_media(session: Session, path: str):
    """Import ``path`` and file it in the Kaitoi bin. Returns its MediaPoolItem.

    The clip is imported into whatever bin the editor has open and then moved,
    so the Media Pool never changes the bin it is showing. Switching it to the
    Kaitoi bin and straight back, as this used to do, crashed Resolve 21.2
    twice on 2026-11-15 (``std::out_of_range`` in its Media Pool view) once
    that bin held a few dozen clips.
    """
    media_pool = session.media_pool
    items = [item for item in (media_pool.ImportMedia([path]) or []) if item is not None]
    if not items:
        raise ResolveError(f"Resolve could not import {os.path.basename(path)}.")
    item = items[0]
    try:
        target = _find_bin(session) or media_pool.AddSubFolder(media_pool.GetRootFolder(), BIN_NAME)
        if target is not None and not _same_folder(target, media_pool.GetCurrentFolder()):
            media_pool.MoveClips([item], target)
    except Exception as exc:  # noqa: BLE001 - the clip is imported; where it is filed is secondary
        config.log(f"could not file {os.path.basename(path)} in the {BIN_NAME} bin: {exc}", "WARN")
    return item


def _to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _to_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def media_info(media_item, timeline_rate: float = 24.0) -> dict[str, Any]:
    """What an imported clip is: kind, size, rate, length, sound.

    Read from Resolve's clip properties, so it describes the file as Resolve
    will play it. ``seconds`` is the running time; ``frames`` counts the clip's
    own frames (0 for sound, 1 for a still).
    """
    try:
        props = media_item.GetClipProperty() or {}
    except Exception:  # noqa: BLE001
        props = {}
    kind_text = str(props.get("Type") or "").lower()
    if "still" in kind_text:
        kind = "image"
    elif "video" in kind_text:
        kind = "video"
    elif "audio" in kind_text:
        kind = "audio"
    else:
        kind = "video" if str(props.get("Resolution") or "").strip() else "audio"
    fps = _to_float(props.get("FPS"), timeline_rate) or timeline_rate
    frames = _to_int(props.get("Frames"))
    width = height = 0
    resolution = str(props.get("Resolution") or "").lower().replace(" ", "")
    if "x" in resolution:
        left, _, right = resolution.partition("x")
        width, height = _to_int(left), _to_int(right)
    seconds = 0.0
    if kind == "video" and frames > 0:
        seconds = frames / fps
    elif kind == "audio":
        try:
            seconds = timecode_to_frame(str(props.get("Duration") or ""), fps) / max(1.0, float(round(fps)))
        except ValueError:
            seconds = 0.0
    return {
        "kind": kind,
        "fps": fps,
        "frames": frames,
        "width": width,
        "height": height,
        "seconds": seconds,
        "audio_channels": _to_int(props.get("Audio Ch")),
        "name": str(props.get("File Name") or props.get("Clip Name") or ""),
    }


def _free_kaitoi_track(session: Session, kind: str, start: int, end: int, skip: set[int]) -> int | None:
    """An existing Kaitoi track of ``kind`` with nothing in ``[start, end)``, if any."""
    timeline = session.timeline
    for index in range(1, int(timeline.GetTrackCount(kind)) + 1):
        if index in skip:
            continue
        name = timeline.GetTrackName(kind, index) or ""
        if not name.startswith(TRACK_PREFIX):
            continue
        busy = any(item.GetStart() < end and item.GetEnd() > start for item in _track_items(timeline, kind, index))
        if not busy:
            return index
    return None


def _new_kaitoi_track(session: Session, kind: str) -> int:
    timeline = session.timeline
    added = timeline.AddTrack("audio", "stereo") if kind == "audio" else timeline.AddTrack("video")
    if not added:
        raise ResolveError(f"Could not add {'an audio' if kind == 'audio' else 'a video'} track for the result.")
    index = int(timeline.GetTrackCount(kind))
    timeline.SetTrackName(kind, index, f"{TRACK_PREFIX} {index}")
    return index


def _place_part(
    session: Session,
    media_item,
    *,
    kind: str,
    record_frame: int,
    span: int,
    source_frames: int | None,
) -> dict[str, Any]:
    """Lay the picture or the sound of ``media_item`` at ``record_frame``.

    Resolve does not refuse a placement onto an occupied range: it answers with
    an empty item or moves the clip to the end of the track. So the landing
    spot is checked afterwards, and a clip that landed elsewhere is removed and
    placed again on a fresh track.
    """
    media_pool = session.media_pool
    timeline = session.timeline
    tried: set[int] = set()
    for attempt in range(2):
        track_index = None if attempt else _free_kaitoi_track(session, kind, record_frame, record_frame + max(1, span), tried)
        if track_index is None:
            track_index = _new_kaitoi_track(session, kind)
        tried.add(track_index)
        info: dict[str, Any] = {
            "mediaPoolItem": media_item,
            "trackIndex": track_index,
            "recordFrame": record_frame,
            "mediaType": 2 if kind == "audio" else 1,
        }
        if source_frames:
            # endFrame is exclusive here (a 120-frame clip placed with endFrame
            # 119 lands as 119 frames), unlike TimelineItem.GetEnd.
            info["startFrame"] = 0
            info["endFrame"] = source_frames
        appended = [item for item in (media_pool.AppendToTimeline([info]) or []) if item is not None]
        placed = appended[0] if appended else None
        start = placed.GetStart() if placed is not None else None
        if start is not None and int(start) == int(record_frame):
            return {"track_index": track_index, "start": int(start), "end": int(placed.GetEnd())}
        if placed is not None and start is not None:
            timeline.DeleteClips([placed], False)
    label = "A" if kind == "audio" else "V"
    raise ResolveError(f"Resolve would not place the result on a {label} track at frame {record_frame}.")


def place_result(
    session: Session,
    media_item,
    source: dict[str, Any],
    *,
    placement: str = "new_track",
    record_frame: int | None = None,
    with_audio: bool = True,
) -> dict[str, Any]:
    """Put a generated file into the timeline.

    ``new_track`` (default) lays it on Kaitoi tracks at ``record_frame`` (the
    start of the source unless given): picture on a video track above, sound on
    an audio track below. Nothing existing is touched, and switching those
    tracks off restores the previous cut. ``append`` drops it at the end of the
    timeline; ``pool`` leaves it in the media pool only.
    """
    timeline = session.timeline
    rate = timeline_fps(timeline)
    info = media_info(media_item, rate)
    result: dict[str, Any] = {"placement": placement, "kind": info["kind"], "video_track": None, "audio_track": None}
    if placement == "pool":
        return result
    if placement == "append":
        appended = [item for item in (session.media_pool.AppendToTimeline([media_item]) or []) if item is not None]
        if not appended or appended[0].GetStart() is None:
            raise ResolveError("Resolve refused to append the result to the timeline.")
        result.update(start=int(appended[0].GetStart()), end=int(appended[0].GetEnd()))
        return result

    frame = int(source["start"] if record_frame is None else record_frame)
    span = max(1, int(round(info["seconds"] * rate))) if info["seconds"] > 0 else max(1, int(source.get("duration") or 0))
    if info["kind"] == "audio":
        placed = _place_part(session, media_item, kind="audio", record_frame=frame, span=span, source_frames=None)
        result.update(audio_track=placed["track_index"], start=placed["start"], end=placed["end"])
        return result
    # A still takes Resolve's own standard still length; a clip is laid in whole.
    source_frames = info["frames"] if info["kind"] == "video" and info["frames"] > 1 else None
    placed = _place_part(session, media_item, kind="video", record_frame=frame, span=span, source_frames=source_frames)
    result.update(video_track=placed["track_index"], start=placed["start"], end=placed["end"])
    if with_audio and info["kind"] == "video" and info["audio_channels"] > 0:
        try:
            sound = _place_part(
                session, media_item, kind="audio", record_frame=frame, span=span, source_frames=source_frames
            )
            result["audio_track"] = sound["track_index"]
        except ResolveError as exc:
            result["audio_error"] = str(exc)
    return result


def describe_placement(placed: dict[str, Any]) -> str:
    if placed.get("placement") == "pool":
        return "media pool"
    if placed.get("placement") == "append":
        return "end of timeline"
    parts = []
    if placed.get("video_track"):
        parts.append(f"V{placed['video_track']}")
    if placed.get("audio_track"):
        parts.append(f"A{placed['audio_track']}")
    return " + ".join(parts) if parts else "timeline"
