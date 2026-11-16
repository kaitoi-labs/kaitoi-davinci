#!/usr/bin/env python3
"""Kaitoi — generate from the timeline (DaVinci Resolve Studio launcher).

Resolve lists this file under Workspace > Scripts > Edit. It locates the
``kaitoi_resolve`` package next to it (following the symlink ``install.sh``
creates), then opens the panel.

Runs from a terminal too, against the live Resolve instance:

    python3 "Kaitoi.py"            # open the panel
    python3 "Kaitoi.py" --check    # print what the timeline selection is and exit
"""

import os
import sys

LAUNCHER = "Kaitoi.py"
PACKAGE = "kaitoi_resolve"


def _script_dirs():
    """Directories that may hold this launcher (or a symlink to it).

    Resolve ``exec``s menu scripts without ``__file__``, so the launcher cannot
    simply look next to itself: it checks the Scripts/Edit folders Resolve
    scans, following the symlink ``install.sh`` leaves there.
    """
    dirs = []
    if "__file__" in globals():
        dirs.append(os.path.dirname(os.path.realpath(__file__)))
    if sys.argv and sys.argv[0]:
        dirs.append(os.path.dirname(os.path.realpath(sys.argv[0])))
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        # The user's own folder only. Resolve also scans one that every user
        # of the Mac shares, and every account can write to that one.
        dirs += [os.path.join(home, "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit")]
    elif sys.platform.startswith("win"):
        dirs += [
            os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Edit"),
            os.path.join(os.environ.get("PROGRAMDATA", ""), "Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit"),
        ]
    else:
        dirs += [
            os.path.join(home, ".local/share/DaVinciResolve/Fusion/Scripts/Edit"),
            "/opt/resolve/Fusion/Scripts/Edit",
        ]
    return dirs


def _package_homes():
    """Where an installer puts the package: outside Resolve's Scripts folders
    (Resolve would list every module of it in its Scripts menu), and in a
    place only an administrator can write to."""
    if sys.platform == "darwin":
        return ["/Library/Application Support/Kaitoi/Resolve"]
    if sys.platform.startswith("win"):
        return [os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Kaitoi", "Resolve")]
    return ["/opt/kaitoi/resolve"]


def _shared_root():
    if sys.platform == "darwin":
        return "/Library/Application Support/Blackmagic Design"
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("PROGRAMDATA", ""), "Blackmagic Design")
    return None


def _shared(directory):
    """Is this inside Resolve's folder for every user of the computer? On
    macOS and Windows every account can write there, so a package found there
    could have been put there by any of them."""
    shared = _shared_root()
    if not shared:
        return False
    inside = os.path.normcase(os.path.join(shared, ""))
    if os.path.normcase(os.path.join(directory, "")).startswith(inside):
        return True
    # The same folder under another spelling: in other letter case, by the
    # second name a Mac's system disk has for it, or through a link into it.
    path = os.path.realpath(directory)
    while True:
        try:
            if os.path.samefile(path, shared):
                return True
        except OSError:
            pass  # one of the two does not exist: not the same folder
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def _find_package_dir():
    # An installed package wins over a copy left in a Scripts folder by an
    # earlier manual install.
    for directory in _package_homes():
        if os.path.isdir(os.path.join(directory, PACKAGE)):
            return directory
    for directory in _script_dirs():
        if _shared(directory):
            continue  # any user of the computer can write there: code is never taken from it
        if os.path.isdir(os.path.join(directory, PACKAGE)):
            return directory
        launcher = os.path.join(directory, LAUNCHER)
        if os.path.lexists(launcher):
            real = os.path.dirname(os.path.realpath(launcher))
            if not _shared(real) and os.path.isdir(os.path.join(real, PACKAGE)):
                return real
    raise ImportError(
        f"Cannot find the '{PACKAGE}' package. Run install.sh from the plugin folder "
        f"(looked in: {', '.join(d for d in _package_homes() + _script_dirs() if d)})"
    )


_HERE = _find_package_dir()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Resolve reuses one Python interpreter for every script run, so a previously
# imported copy of the package would otherwise mask edits made on disk.
for _name in [m for m in list(sys.modules) if m == "kaitoi_resolve" or m.startswith("kaitoi_resolve.")]:
    del sys.modules[_name]

from kaitoi_resolve import config, inspector, resolve_bridge, ui  # noqa: E402


def main(argv):
    try:
        # Inside Resolve the host injects `resolve` into this script's globals.
        session = resolve_bridge.connect(resolve=globals().get("resolve"), module=globals().get("bmd"))
    except resolve_bridge.ResolveError as exc:
        print(f"[Kaitoi] {exc}")
        return 1

    if "--check" in argv:
        source = resolve_bridge.current_source(session)
        print(resolve_bridge.describe_source(source))
        print(f"can send: {', '.join(sorted(resolve_bridge.source_supplies(source))) or 'nothing'}")
        print(f"source file: {source.get('file_path')}")
        return 0

    # A button on a Kaitoi clip in the Inspector started this script: do what
    # the button asked. Generate runs here, with no window.
    if inspector.handle(session, globals().get("comp")):
        return 0

    config.log(f"window opened (Resolve {session.resolve.GetVersionString()})")
    ui.launch(session)
    return 0


if "resolve" in globals() or "fusion" in globals() or "bmd" in globals():
    # Launched inside Resolve, from Workspace > Scripts or from a button on the
    # Kaitoi effect in the Inspector: no argv, no exit code.
    main([])
elif __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
