"""One Kaitoi run at a time, across every Kaitoi script.

Resolve lets several scripts run at once: the Kaitoi window, and one script per
Generate pressed in the Inspector. On 2026-11-15 Resolve 21.2 crashed
(``std::out_of_range: vector``) when one script added a result to the timeline
while another was reading that timeline. So a run announces itself in a small
file, and while it stands every other Kaitoi script keeps its hands off
Resolve: a second run is refused, and the window stops watching the timeline.

A file is used because the scripts share nothing else: each is started by
Resolve on its own.
"""

from __future__ import annotations

import json
import os
import time

from . import config

STALE_SECONDS = 90  # a live run renews its marker every few seconds; an old one was left by a crash


def _path() -> str:
    config.app_dir()
    return os.path.join(config.APP_DIR, "busy.json")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def holder() -> dict | None:
    """The run in progress, or None. A marker left by a dead process is cleared."""
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = int(data.get("pid") or 0)
    fresh = time.time() - float(data.get("since") or 0) < STALE_SECONDS
    if pid and fresh and _alive(pid):
        return data
    try:
        os.remove(_path())
    except OSError:
        pass
    return None


def acquire(owner: str, what: str) -> bool:
    """Claim Resolve for a run. False when another run holds it.

    ``owner`` tells the scripts apart: every script Resolve starts shares one
    process, so the process id alone cannot.
    """
    current = holder()
    if current is not None and current.get("owner") != owner:
        return False
    marker = {"pid": os.getpid(), "owner": owner, "what": what, "since": time.time()}
    partial = _path() + f".{owner}.part"
    try:
        with open(partial, "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
        os.replace(partial, _path())
    except OSError as exc:
        config.log(f"could not mark Kaitoi as busy: {exc}", "WARN")
        return True  # never block a run because a marker could not be written
    return True


def renew(owner: str) -> None:
    """Say the run is still going. Call every few seconds while it runs."""
    current = holder()
    if current is not None and current.get("owner") == owner and time.time() - float(current.get("since") or 0) > 5:
        acquire(owner, str(current.get("what") or ""))


def release(owner: str) -> None:
    current = holder()
    if current is not None and current.get("owner") == owner:
        try:
            os.remove(_path())
        except OSError:
            pass


def held_by_other(owner: str) -> str:
    """What another script is running right now, or an empty string."""
    current = holder()
    if current is None or current.get("owner") == owner:
        return ""
    return str(current.get("what") or "another run")
