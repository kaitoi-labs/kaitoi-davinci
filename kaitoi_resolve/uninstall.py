"""Removing Kaitoi from this computer.

The macOS installer puts ``Uninstall Kaitoi.command`` beside this package, in
a folder only an administrator can change. The Uninstall button in the window
runs that file; a person can also double-click it. So there is one uninstaller
and two ways to start it, and the button exists only where the file does.

On Windows the installer registers its own uninstaller with the system, and an
install from the plugin's folder is removed by ``install.sh --uninstall``:
neither has this file, and neither is offered the button.
"""

from __future__ import annotations

import os
import subprocess
import sys

NAME = "Uninstall Kaitoi.command"
LEFT_PREFIX = "Left in place: "

# How the uninstaller ends.
REMOVED = 0  # all of it is gone
NOTHING = 1  # nothing was touched: the password was not given
LEFT = 3  # Kaitoi is gone, and something of it is still there: kept on purpose, or not deletable


def script() -> str | None:
    """The uninstaller that came with this copy of the plugin, or None."""
    if sys.platform != "darwin":
        return None
    beside = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), NAME)
    return beside if os.path.isfile(beside) and os.access(beside, os.X_OK) else None


def run(path: str, *, settings: bool) -> tuple[int, list[str]]:
    """Run the uninstaller without questions. Blocks while macOS asks for the
    password, so call it from a thread.

    Returns how it ended and the paths it left in place. ``settings`` also
    removes the plugin's settings and history.
    """
    command = [path, "--yes"] + (["--settings"] if settings else [])
    # Not from the folder being deleted, and with nothing to read: it must not
    # wait for an answer. Its paths are read as UTF-8 whatever Resolve's own
    # Python takes text to be: a home folder with an accent in its name must
    # not turn a finished uninstall into an error.
    done = subprocess.run(command, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace")
    left = [line[len(LEFT_PREFIX):] for line in done.stdout.splitlines() if line.startswith(LEFT_PREFIX)]
    return done.returncode, left
