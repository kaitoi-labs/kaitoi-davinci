"""Does a result match what it was made from?

Models return what they return: often 720p, often 24 or 30 frames a second,
often a fixed five seconds. None of that is an error, and the result is still
placed, but an editor needs to know before it reaches a client. These checks
turn the mismatches into plain sentences.

Pure functions over the numbers :func:`resolve_bridge.media_info` reads, so
they run without Resolve.
"""

from __future__ import annotations

from typing import Any

RATE_TOLERANCE = 0.01  # 23.976 and 24 are different rates; 24.0 and 24.00 are not
LENGTH_TOLERANCE_FRAMES = 1.5


def _rate(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def result_warnings(
    info: dict[str, Any],
    *,
    timeline_fps: float,
    timeline_size: tuple[int, int],
    source_frames: int = 0,
    sent_kind: str | None = None,
) -> list[str]:
    """Plain-language notes on how a result differs from its source.

    ``info`` describes the imported result. ``source_frames`` is the length of
    what was sent, in timeline frames (0 when nothing was sent). ``sent_kind``
    is the media kind that was sent; lengths are only compared when a clip or
    sound went out and a clip or sound came back.
    """
    notes: list[str] = []
    kind = info.get("kind")
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    timeline_width, timeline_height = timeline_size

    if kind == "video":
        fps = float(info.get("fps") or 0.0)
        if fps > 0 and abs(fps - timeline_fps) > RATE_TOLERANCE:
            notes.append(
                f"Frame rate: the result is {_rate(fps)} fps on a {_rate(timeline_fps)} fps timeline, "
                "so Resolve will retime it."
            )

    if kind in ("video", "image") and width > 0 and height > 0 and timeline_width > 0 and timeline_height > 0:
        if width < timeline_width and height < timeline_height:
            notes.append(
                f"Size: the result is {width}x{height} on a {timeline_width}x{timeline_height} timeline, "
                "so it will be scaled up."
            )
        if abs(width / height - timeline_width / timeline_height) > 0.01:
            notes.append(f"Shape: the result is {width}x{height}, a different shape from the timeline.")

    if kind in ("video", "audio") and sent_kind in ("video", "audio") and source_frames > 0 and timeline_fps > 0:
        seconds = float(info.get("seconds") or 0.0)
        source_seconds = source_frames / timeline_fps
        if seconds > 0 and abs(seconds - source_seconds) * timeline_fps > LENGTH_TOLERANCE_FRAMES:
            notes.append(f"Length: the result runs {seconds:.2f} s and the source runs {source_seconds:.2f} s.")

    return notes
