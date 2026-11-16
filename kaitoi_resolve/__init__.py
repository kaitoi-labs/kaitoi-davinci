"""Kaitoi for DaVinci Resolve Studio.

Run a Kaitoi workflow or a single Kaitoi node on what the timeline plays:
the range is exported, sent to Kaitoi, and the result is laid on a track
above the original. A tool can be run from the Kaitoi window, or from the
Inspector, where it sits on a clip as an effect of its own.

Modules:
    api             Kaitoi's public API, standard library only
    credentials     ~/.kaitoi/credentials.json, the key store Kaitoi's plugins share
    signin          signing in through the browser
    config          ~/.kaitoi_resolve/config.json (settings) and the log
    history         ~/.kaitoi_resolve/history.json (finished runs)
    tools           workflows and nodes as tools: inputs, outputs, filters
    graphs          the small graph built around a single node
    templates       each tool as a Resolve effect and generator
    inspector       running a tool from the Inspector
    resolve_bridge  everything that talks to Resolve
    pipeline        one run: export, upload, run, download, place
    checks          how a result differs from its source
    busy            one run at a time, across scripts
    ui              the window, built with Fusion's UIManager
"""

__version__ = "0.1.0"
