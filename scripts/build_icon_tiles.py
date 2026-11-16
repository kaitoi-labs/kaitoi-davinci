#!/usr/bin/env python3
"""Draw the small tiles the Kaitoi window shows for a node that has no thumbnail.

Kaitoi names an icon for every node (the Lucide set Studio uses). Resolve's
UI kit cannot draw SVG, so each icon is drawn here once, as a PNG tile, and
the tiles ship with the plugin in ``kaitoi_resolve/assets/icons``.

    python3 scripts/build_icon_tiles.py <lucide icons dir> [icon-name ...]

``<lucide icons dir>`` is ``node_modules/lucide-svelte/dist/icons`` of any
project that has Lucide installed. With no names, every icon listed in
``scripts/icon_names.json`` is drawn (the names Kaitoi's nodes use). Runs on
macOS: the SVG is drawn by QuickLook (``qlmanage``), and Pillow builds the
tile. Lucide is ISC licensed; its notice is written beside the tiles.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "kaitoi_resolve", "assets", "icons")
NAMES_FILE = os.path.join(HERE, "icon_names.json")
TILE = (192, 108)  # the list draws 64x36 points; three times that for sharp screens and the larger card
GLYPH = 60
RASTER = 480  # the size the icon is first drawn at, before it is scaled down
# Names Kaitoi nodes use that Lucide has since renamed.
ALIASES = {
    "alert-triangle": "triangle-alert",
    "bar-chart-3": "chart-column",
    "columns": "columns-2",
    "edit": "square-pen",
    "home": "house",
    "play-circle": "circle-play",
    "sliders": "sliders-horizontal",
    "wand-2": "wand-sparkles",
}


LUCIDE_NOTICE = """The icons in this folder are drawn from Lucide (https://lucide.dev).

ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT). All other copyright (c) for Lucide are held by Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
"""


def svg_of(icons_dir: str, name: str) -> str | None:
    path = os.path.join(icons_dir, f"{ALIASES.get(name, name)}.svelte")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return None
    found = re.search(r"const iconNode = (\[.*?\]);\n", source, re.S)
    if not found:
        return None
    shapes = []
    for tag, attrs in json.loads(found.group(1)):
        if not re.fullmatch(r"[a-z]+", tag):
            return None
        pairs = " ".join(f'{key}="{str(value).replace(chr(34), "")}"' for key, value in attrs.items() if re.fullmatch(r"[a-zA-Z0-9-]+", key))
        shapes.append(f"<{tag} {pairs}/>")
    # Black on a white square: QuickLook draws on white, and the darkness of
    # each pixel then becomes how much of the icon is there.
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{RASTER}" height="{RASTER}" viewBox="0 0 24 24" fill="none" '
        'stroke="#000000" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="-1" y="-1" width="26" height="26" fill="#ffffff" stroke="none"/>' + "".join(shapes) + "</svg>"
    )


def tile(glyph: Image.Image) -> Image.Image:
    """A dark tile, lit from the top left like the plugin's other stand-in pictures, with the icon in its middle."""
    width, height = TILE
    base = Image.new("RGB", TILE)
    draw = ImageDraw.Draw(base)
    for x in range(width):
        t = x / (width - 1)
        draw.line([(x, 0), (x, height)], fill=(int(58 - 30 * t), int(58 - 30 * t), int(66 - 34 * t)))
    glyph = glyph.resize((GLYPH, GLYPH), Image.Resampling.LANCZOS)
    alpha = glyph.getchannel("A").point(lambda value: int(value * 0.88))
    base.paste(Image.new("RGB", glyph.size, (236, 236, 240)), ((width - GLYPH) // 2, (height - GLYPH) // 2), alpha)
    return base


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    icons_dir = sys.argv[1]
    names = sys.argv[2:]
    if not names:
        with open(NAMES_FILE, "r", encoding="utf-8") as fh:
            names = json.load(fh)
    os.makedirs(OUT, exist_ok=True)
    drawn, missing = 0, []
    with tempfile.TemporaryDirectory() as work:
        for name in names:
            if not re.fullmatch(r"[a-z0-9-]+", name):
                missing.append(name)
                continue
            svg = svg_of(icons_dir, name)
            if svg is None:
                missing.append(name)
                continue
            source, raster = os.path.join(work, "icon.svg"), os.path.join(work, "icon.svg.png")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write(svg)
            subprocess.run(["qlmanage", "-t", "-s", str(RASTER), "-o", work, source], capture_output=True)
            if not os.path.isfile(raster):
                missing.append(name)
                continue
            with Image.open(raster) as drawn_icon:
                ink = ImageOps.invert(drawn_icon.convert("L"))
            os.remove(raster)
            if ink.getbbox() is None:  # nothing was drawn
                missing.append(name)
                continue
            glyph = Image.new("RGBA", ink.size, (255, 255, 255, 0))
            glyph.putalpha(ink)
            tile(glyph).save(os.path.join(OUT, f"{name}.png"), optimize=True)
            drawn += 1
    with open(os.path.join(OUT, "LICENSE-lucide.txt"), "w", encoding="utf-8") as fh:
        fh.write(LUCIDE_NOTICE)
    print(f"drew {drawn} tiles into {os.path.normpath(OUT)}; no icon for: {', '.join(missing) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
