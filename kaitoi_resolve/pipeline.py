"""The end-to-end job: timeline → Kaitoi → timeline.

Steps, in order:

1. export what the tool takes from the timeline (a clip, a frame or its
   sound), or nothing when the tool creates from scratch;
2. upload it, with any extra files the editor picked;
3. run the tool: a published workflow through its endpoint, or a single node
   as an inline graph built from the node's live schema;
4. wait for the run;
5. download every file it returned and lay each one on the timeline.

Threading rule: **Resolve is only ever called from the UI thread**; a Resolve
call from another thread blocks while the UI thread is in ``RunLoop()`` and
deadlocks when the script runs inside Resolve. So step 1 (a Deliver render) is
started on the UI thread and advanced from the panel's loop via
:meth:`Job.step`; steps 2-4 and the downloads are pure network and run on a
worker thread; the import returns to the UI thread through
:meth:`Job.finish_on_main`.
"""

from __future__ import annotations

import os
import re
import threading
import time
import traceback
from typing import Any

from . import api, checks, config, graphs, history, resolve_bridge, tools


# Displayed in the panel; ``Job.stage`` indexes into this list.
STAGES = (
    "Export from timeline",
    "Upload",
    "Prepare",
    "Run",
    "Download result",
    "Place on timeline",
)
# One-word forms for the progress strip, so all six fit on a single line.
STAGES_SHORT = ("Export", "Upload", "Prepare", "Run", "Download", "Place")

PROGRESS_TIMEOUT_SECONDS = 10.0  # the longest one read of a run's progress is waited for
PROGRESS_REST_POLLS = 3  # after a read that failed, this many polls pass before the next

NO_CANCEL_NOTE = (
    "Stopped waiting. Kaitoi cannot cancel a workflow run yet, so this one may still finish and use credits."
)


def _safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("_")[:60] or "result"


class Job:
    """One run of a tool, from the timeline and back.

    Started with :meth:`start`, advanced with :meth:`step` and read with
    :meth:`poll` from the UI loop, and completed on the UI thread with
    :meth:`finish_on_main`.

    ``values`` are the tool's parsed non-media inputs by field name. ``files``
    are local paths for media inputs the editor filled from disk. ``primary``
    is the media input the timeline fills, or None when the tool creates from
    nothing or every media input comes from disk.
    """

    def __init__(
        self,
        session: resolve_bridge.Session,
        source: dict[str, Any],
        tool: tools.Tool,
        *,
        values: dict[str, Any] | None = None,
        files: dict[str, str] | None = None,
        primary: tools.Field | None = None,
        placement: str = "new_track",
        use_source_file: bool = False,
        settings: dict | None = None,
    ) -> None:
        self.session = session
        self.source = source
        self.tool = tool
        self.values = dict(values or {})
        self.files = dict(files or {})
        self.primary = primary
        self.placement = placement
        self.use_source_file = use_source_file
        self.settings = settings or config.load()

        self.status = "idle"
        self.error: str | None = None
        # Files the run returned, downloaded: [{path, field, label}]
        self.results: list[dict[str, Any]] = []
        self.texts: dict[str, str] = {}
        self.placed: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.run_id: str | None = None
        self.started_at = 0.0
        # "export" (UI thread, tick-driven) → "network" (worker) → "done"
        self.phase = "idle"
        self.stage = -1  # index into STAGES; -1 before start
        self.fraction: float | None = None  # how far the stage itself has got, 0 to 1, when Kaitoi says
        self.local_source: str | None = None

        self._export: resolve_bridge.ExportJob | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ── what goes where ─────────────────────────────────────────────────────

    @property
    def sent_kind(self) -> str | None:
        """The media kind taken from the timeline, if any."""
        return self.primary.media if self.primary is not None else None

    @property
    def record_frame(self) -> int:
        """Where results land: the start of what was sent.

        A frame was sent: that frame. A clip or its sound: the first frame of
        the range. Nothing was sent: the start of the In/Out range or of the
        Kaitoi clip the run came from, otherwise the playhead.
        """
        kind = self.sent_kind
        origin = self.source.get("origin")
        if kind == "image" and origin != "carrier":
            return int(self.source.get("still_frame", self.source["start"]))
        if kind is None and origin not in ("marks", "carrier") and self.source.get("playhead") is not None:
            return int(self.source["playhead"])
        return int(self.source["start"])

    # ── lifecycle (UI thread) ───────────────────────────────────────────────

    def start(self) -> None:
        """Kick off the job. Fast: queues the export (or skips it) and returns."""
        if self.phase != "idle":
            raise RuntimeError("job already started")
        self.started_at = time.monotonic()
        kind = self.sent_kind
        if kind is None:
            self._set_stage(1, "nothing to send from the timeline")
            self._start_worker()
            return
        self._set_stage(0, "starting...")
        try:
            if self.use_source_file:
                self.local_source = resolve_bridge.source_file(self.source, kind)
                self._set_status(f"Using source file {os.path.basename(self.local_source)}")
                self._start_worker()
                return
            self._export = resolve_bridge.start_export(
                self.session,
                self.source,
                kind=kind,
                out_dir=config.work_dir(),
                max_height=int(self.settings.get("export_max_height", 720)),
                video_format=str(self.settings.get("export_format", "mp4")),
                video_codec=str(self.settings.get("export_codec", "H264")),
                quality=str(self.settings.get("export_quality", "Medium")),
                include_audio=bool(self.settings.get("export_include_audio", True)),
            )
        except resolve_bridge.ResolveError as exc:
            self._fail(str(exc))
            return
        self.phase = "export"
        what = {"video": "clip", "image": "frame", "audio": "sound"}[kind]
        self._set_status(f"{STAGES[0]}: rendering the {what}...")

    def step(self) -> None:
        """Advance the export. Call from the UI loop; no-op outside that phase."""
        if self.phase != "export" or self._export is None:
            return
        export = self._export
        try:
            if self._stop.is_set():
                resolve_bridge.abort_export(self.session, export)
                self._fail("Cancelled.", level="WARN")
                return
            state, percent = resolve_bridge.poll_export(self.session, export)
            if state not in resolve_bridge.RENDER_DONE:
                timeout = float(self.settings.get("export_timeout_seconds", 600))
                if time.monotonic() - export.started_at > timeout:
                    resolve_bridge.abort_export(self.session, export)
                    self._fail(f"The export timed out after {timeout:.0f}s.")
                    return
                self._set_status(f"{STAGES[0]}: rendering... {percent}%", log=False)
                return
            self.local_source = resolve_bridge.finish_export(self.session, export)
        except resolve_bridge.ResolveError as exc:
            self._fail(str(exc))
            return
        except Exception:  # noqa: BLE001 - never let the UI loop die on an export hiccup
            resolve_bridge._restore_after_export(self.session, export)
            self._fail("Unexpected error during export:\n" + traceback.format_exc())
            return
        self._start_worker()

    def _start_worker(self) -> None:
        self.phase = "network"
        try:
            self._thread = threading.Thread(target=self._run, name="kaitoi-job", daemon=True)
            self._thread.start()
        except Exception as exc:  # noqa: BLE001 - some embedded interpreters cannot spawn threads
            config.log(f"worker thread unavailable ({exc}); running network steps inline", "WARN")
            self._thread = None
            self._run()

    def _fail(self, message: str, level: str = "ERROR") -> None:
        with self._lock:
            self.error = message
        self.phase = "done"
        config.log(f"job failed: {message}", level)

    def cancel(self) -> None:
        """Ask the job to stop. Cheap and non-blocking: safe from the UI thread.

        The worker notices the flag at its next checkpoint (export poll, run
        poll, between steps), so no network call happens on the UI thread.
        """
        self._stop.set()
        self._set_status("Cancelling...")

    @property
    def running(self) -> bool:
        if self.phase == "export":
            return True
        if self.phase == "network":
            return self._thread is not None and self._thread.is_alive()
        return False

    def poll(self) -> dict[str, Any]:
        """Snapshot for the UI: safe to call from the loop every tick."""
        with self._lock:
            return {
                "status": self.status,
                "stage": self.stage,
                "fraction": self.fraction,
                "error": self.error,
                "results": list(self.results),
                "texts": dict(self.texts),
                "running": self.running,
                "elapsed": time.monotonic() - self.started_at if self.started_at else 0.0,
            }

    # ── worker ──────────────────────────────────────────────────────────────

    def _config(self) -> api.Config:
        return api.Config(
            api_key=str(self.settings.get("api_key", "")),
            base_url=str(self.settings.get("base_url", "")),
            timeout=float(self.settings.get("request_timeout_seconds", 120)),
        )

    def _set_status(self, text: str, *, log: bool = True) -> None:
        with self._lock:
            self.status = text
        if log:
            config.log(text)

    def _set_stage(self, stage: int, text: str) -> None:
        with self._lock:
            self.stage = stage
            self.fraction = None  # the stage before's progress is not this one's
        self._set_status(f"{STAGES[stage]}: {text}")

    def _set_fraction(self, fraction: float | None) -> None:
        with self._lock:
            self.fraction = fraction

    def _run(self) -> None:
        """Worker thread: upload, run, download. Never touches Resolve."""
        run_finished = False
        try:
            cfg = self._config()
            external_id = str(self.settings.get("external_id") or "davinci_resolve_plugin")
            self._check_stop()

            # 2. Upload what the timeline gave and what the editor picked.
            file_ids: dict[str, str] = {}
            uploads: list[tuple[str, str]] = []
            if self.primary is not None:
                if not self.local_source:
                    raise api.KaitoiAPIError(0, "NO_SOURCE", "the export produced nothing to send")
                uploads.append((self.primary.name, self.local_source))
            for field_name, path in self.files.items():
                if not os.path.isfile(path):
                    raise api.KaitoiAPIError(0, "NO_FILE", f"file not found: {path}")
                uploads.append((field_name, path))
            for index, (field_name, path) in enumerate(uploads):
                size_mb = os.path.getsize(path) / 1024 / 1024
                count = f" ({index + 1}/{len(uploads)})" if len(uploads) > 1 else ""
                self._set_stage(1, f"sending {os.path.basename(path)} ({size_mb:.1f} MB){count}...")
                upload = api.upload_file(cfg, path, external_id=external_id, on_status=self._set_status)
                file_ids[field_name] = upload["fileId"]
                self._check_stop()

            # 3-4. Run.
            if self.tool.source == "node":
                files = self._run_node(cfg, file_ids, external_id)
            else:
                files = self._run_workflow(cfg, file_ids, external_id)
            run_finished = True

            # 5. Download.
            results: list[dict[str, Any]] = []
            for index, entry in enumerate(files):
                self._check_stop()
                count = f" ({index + 1}/{len(files)})" if len(files) > 1 else ""
                self._set_stage(4, f"fetching {entry['label']}{count}...")
                url = entry.get("downloadUrl") or api.get_download_url(cfg, entry["fileId"])
                # File ids already carry their extension ("<hash>.mp4").
                extension = os.path.splitext(entry["fileId"])[1] or os.path.splitext(str(entry.get("filename") or ""))[1]
                stamp = time.strftime("%Y%m%d_%H%M%S")
                local = os.path.join(
                    config.work_dir(),
                    f"kaitoi_{_safe_name(self.tool.name)}_{_safe_name(entry['field'])}_{stamp}{extension}",
                )
                api.download_to(
                    local, url, timeout=float(self.settings.get("request_timeout_seconds", 120)) * 4, config=cfg
                )
                results.append({"path": local, "field": entry["field"], "label": entry["label"]})
            if not results and not self.texts:
                raise api.KaitoiAPIError(0, "NO_OUTPUT", f"run {self.run_id} returned nothing to place")
            with self._lock:
                self.results = results
            self._set_stage(5, "importing...")
        except graphs.GraphBuildError as exc:
            with self._lock:
                self.error = str(exc)
            config.log(f"job failed: {exc}", "ERROR")
        except api.KaitoiAPIError as exc:
            aborted = exc.code == "ABORTED"
            message = "Cancelled." if aborted else str(exc)
            if aborted and self.run_id and not run_finished:
                message = self._cancel_remote()
            with self._lock:
                self.error = message
            config.log(f"job failed: {exc}", "WARN" if aborted else "ERROR")
        except Exception:  # noqa: BLE001 - a worker thread must never die silently
            detail = traceback.format_exc()
            with self._lock:
                self.error = "Unexpected error:\n" + detail
            config.log(f"job crashed:\n{detail}", "ERROR")
        finally:
            self.phase = "done"

    def _run_workflow(self, cfg: api.Config, file_ids: dict[str, str], external_id: str) -> list[dict[str, Any]]:
        tool = self.tool
        self._set_stage(2, "building the request...")
        inputs = tools.build_inputs(tool, self.values, file_ids)
        config.log(f"workflow '{tool.name}' inputs: {sorted(inputs)}")
        self._check_stop()

        self._set_stage(3, f"submitting to {tool.name}...")
        run = api.run_endpoint(cfg, tool.template_id, tool.endpoint_id, inputs, external_id=external_id)
        task_id = run.get("taskId")
        if not task_id:
            raise api.KaitoiAPIError(0, "BAD_RESPONSE", "run response carried no taskId")
        self.run_id = task_id

        def progress(current: dict[str, Any]) -> None:
            detail = current.get("progressMessage") or current.get("currentNode") or current.get("status")
            percent = current.get("progress")
            known = isinstance(percent, (int, float)) and not isinstance(percent, bool)
            self._set_fraction(max(0.0, min(1.0, percent / 100.0)) if known else None)
            suffix = f" {percent}%" if known and 0 < percent < 100 else ""
            self._set_status(f"{STAGES[3]}: {tool.name}: {detail}{suffix} ({self._elapsed()})", log=False)

        run = api.wait_for(
            lambda: api.get_endpoint_run(cfg, tool.template_id, tool.endpoint_id, task_id),
            label=f"run {task_id}",
            timeout=float(self.settings.get("run_timeout_seconds", 900)),
            poll_interval=float(self.settings.get("poll_interval_seconds", 3.0)),
            on_progress=progress,
            should_stop=self._stop.is_set,
        )
        if run.get("status") != "succeeded":
            raise api.KaitoiAPIError(0, "RUN_FAILED", api.run_error_detail(run))
        config.log(f"run {task_id} succeeded in {self._elapsed()}")
        labels = {output.name: output.label for output in tool.outputs}
        files = api.output_files(run)
        for entry in files:
            entry["label"] = labels.get(entry["field"], entry["field"])
        with self._lock:
            self.texts = {labels.get(name, name): text for name, text in api.output_texts(run).items()}
        return files

    def _run_node(self, cfg: api.Config, file_ids: dict[str, str], external_id: str) -> list[dict[str, Any]]:
        tool = self.tool
        self._set_stage(2, "reading the node...")
        built = graphs.build_node_graph(cfg, tool, self.values, file_ids)
        config.log(f"graph: {graphs.describe(built)}")
        self._check_stop()

        self._set_stage(3, f"submitting to {built['title']}...")
        run = api.create_run(
            cfg,
            built["graph"],
            target_node_ids=[built["target"]],
            input_overrides=built["overrides"],
            external_id=external_id,
        )
        run_id = run.get("id")
        if not run_id:
            raise api.KaitoiAPIError(0, "BAD_RESPONSE", "run response carried no id")
        self.run_id = run_id

        seen: dict[str, Any] = {}
        told = [False]
        rest = [0]  # polls to let pass before the progress is asked for again
        # Asked with little patience: a server slow to say how far a run is
        # must not hold up noticing that it has ended, or a Cancel.
        quick = api.Config(cfg.api_key, cfg.base_url, timeout=min(cfg.timeout, PROGRESS_TIMEOUT_SECONDS))

        def progress(current: dict[str, Any]) -> None:
            if rest[0] > 0:
                rest[0] -= 1
            elif current.get("status") not in api.TERMINAL_STATES:
                try:
                    seen.update(api.run_progress(quick, run_id, built["target"], seen, should_stop=self._stop.is_set))
                except Exception as exc:  # noqa: BLE001 - progress is a courtesy: whatever goes wrong reading it must not fail the run
                    rest[0] = PROGRESS_REST_POLLS
                    if getattr(exc, "status", 0) in (400, 422):
                        seen.pop("cursor", None)  # the place it was read up to is refused: begin again from the first page
                    if not told[0]:
                        told[0] = True
                        config.log(f"could not read the progress of {run_id}: {exc}", "WARN")
            fraction, said = seen.get("fraction"), seen.get("message") or ""  # what was last known, when this read failed
            self._set_fraction(fraction)
            percent = f" {round(fraction * 100)}%" if fraction is not None and 0 < fraction < 1 else ""
            self._set_status(f"{STAGES[3]}: {built['title']}: {said or current.get('status')}{percent} ({self._elapsed()})", log=False)

        run = api.wait_for_run(
            cfg,
            run_id,
            timeout=float(self.settings.get("run_timeout_seconds", 900)),
            poll_interval=float(self.settings.get("poll_interval_seconds", 3.0)),
            on_progress=progress,
            should_stop=self._stop.is_set,
        )
        if run.get("status") != "succeeded":
            raise api.KaitoiAPIError(0, "RUN_FAILED", api.run_error_detail(run))
        config.log(f"run {run_id} succeeded in {self._elapsed()}; credits used: {run.get('creditsUsed')}")
        labels = {output.name: output.label for output in tool.outputs}
        files = api.node_output_files(run, built["target"])
        for entry in files:
            entry["label"] = labels.get(entry["field"], entry["field"])
        return files

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self.started_at)
        return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise api.KaitoiAPIError(0, "ABORTED", "stopped by user")

    def _cancel_remote(self) -> str:
        """Stop the live run if Kaitoi can. Worker thread only. Returns what to tell the editor."""
        run_id = self.run_id
        if not run_id:
            return "Cancelled."
        if self.tool.source != "node":
            config.log(f"stopped waiting for {run_id}; workflow runs cannot be cancelled", "WARN")
            return NO_CANCEL_NOTE
        try:
            api.cancel_run(self._config(), run_id)
            config.log(f"cancelled run {run_id}")
            return "Cancelled."
        except api.KaitoiAPIError as exc:
            config.log(f"remote cancel failed for {run_id}: {exc}", "WARN")
            return f"Stopped waiting, but the run could not be cancelled: {exc.message}"

    # ── UI thread ───────────────────────────────────────────────────────────

    def finish_on_main(self) -> list[dict[str, Any]]:
        """Import the finished results and place them. Call from the UI thread.

        Media pool and timeline edits are done here rather than on the worker
        because they mutate the project the editor is looking at. One result
        failing to place does not stop the others.
        """
        timeline = self.session.timeline
        rate = resolve_bridge.timeline_fps(timeline)
        size = resolve_bridge.timeline_resolution(timeline)
        try:  # laying a clip on the timeline drags the playhead to its end
            parked = timeline.GetCurrentTimecode() or None
        except Exception:  # noqa: BLE001
            parked = None
        placed_all: list[dict[str, Any]] = []
        for result in self.results:
            path = result["path"]
            self._set_stage(5, f"importing {os.path.basename(path)}...")
            entry: dict[str, Any] = {"path": path, "field": result["field"], "label": result["label"]}
            try:
                media_item = resolve_bridge.import_media(self.session, path)
                info = resolve_bridge.media_info(media_item, rate)
                placed = resolve_bridge.place_result(
                    self.session,
                    media_item,
                    self.source,
                    placement=self.placement,
                    record_frame=self.record_frame,
                    with_audio=bool(self.settings.get("place_result_audio", True)),
                )
                entry.update(placed=placed, kind=info["kind"], where=resolve_bridge.describe_placement(placed))
                notes = checks.result_warnings(
                    info,
                    timeline_fps=rate,
                    timeline_size=size,
                    source_frames=int(self.source.get("duration") or 0),
                    sent_kind=self.sent_kind,
                )
                if placed.get("audio_error"):
                    notes.append(f"Sound: the picture was placed but its sound was not ({placed['audio_error']})")
                entry["warnings"] = notes
                prefix = f"{result['label']}: " if len(self.results) > 1 else ""
                self.warnings.extend(prefix + note for note in notes)
            except resolve_bridge.ResolveError as exc:
                entry["error"] = str(exc)
                config.log(f"could not place {path}: {exc}", "ERROR")
            placed_all.append(entry)
        self.placed = placed_all
        if parked and self.placement == "new_track":
            try:
                timeline.SetCurrentTimecode(parked)
            except Exception:  # noqa: BLE001
                pass
        history.add(
            {
                "run_id": self.run_id,
                "tool": self.tool.name,
                "tool_id": self.tool.id,
                "task": tools.task_label(self.tool),
                "values": {k: v for k, v in self.values.items() if isinstance(v, (str, int, float, bool))},
                "source": self.source.get("name"),
                "timeline": self.source.get("timeline_name"),
                "record_frame": self.record_frame,
                "placement": self.placement,
                "outputs": [
                    {
                        "path": e["path"],
                        "label": e["label"],
                        "kind": e.get("kind"),
                        "where": e.get("where"),
                        "error": e.get("error"),
                    }
                    for e in placed_all
                ],
                "texts": dict(self.texts),
                "warnings": list(self.warnings),
                "path": placed_all[0]["path"] if placed_all else None,
            }
        )
        return placed_all
