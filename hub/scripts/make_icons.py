"""Render favicon.ico and the PNG icons from the SVG marks (needs `pip install cairosvg pillow`).

web/static/favicon.svg and logo.svg are the source of truth; logo-animated.svg is logo.svg plus CSS motion and is
used as-is. Re-run after changing the marks:  python scripts/make_icons.py
"""
import io
import shutil
from pathlib import Path

import cairosvg
from PIL import Image

STATIC = Path(__file__).parent.parent / "src" / "acm_hub" / "web" / "static"


def render(svg: str, size: int) -> Image.Image:
    png = cairosvg.svg2png(url=str(STATIC / svg), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


if __name__ == "__main__":
    # The favicon uses the bolder favicon.svg at every size; the larger icons use the detailed logo.
    sizes = [16, 32, 48]
    render("favicon.svg", 48).save(STATIC / "favicon.ico", sizes=[(s, s) for s in sizes],
                                   append_images=[render("favicon.svg", s) for s in sizes[:2]])
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        render("logo.svg", size).save(STATIC / name, optimize=True)
    # README/docs copy of the detailed mark (tests/test_branding.py checks the two stay identical)
    shutil.copyfile(STATIC / "logo.svg", STATIC.parents[4] / "docs" / "assets" / "logo.svg")
