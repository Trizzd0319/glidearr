"""generate_playlist_logos.py — render the Glidearr-branded playlist posters.

One-off operator tool (NOT imported by the engine): produces the SQUARE (1000x1000) PNG poster
art the playlist write-back path uploads to Plex, one per managed playlist family. The engine
only ever READS the committed PNGs under ``support/assets/playlists/`` — regenerate them here
(e.g. to tweak the palette) and re-commit the output.

SQUARE on purpose: Plex renders a PLAYLIST poster in a 1:1 tile and center-crops anything taller
(a 2:3 movie-style poster loses its top + bottom, clipping the title). So everything — mark, glyph,
title, descriptor — is laid out inside the square.

The look mirrors the Glidearr brand: a deep-navy field, a low-opacity "glide" swoosh, the
glide-mark roundel + wordmark up top, a single accent glyph, then the list title + descriptor.
Each family keeps the same system with its own accent colour.

Requires Pillow (already a project dependency). Run from the repo root:
    python -m scripts.support.tools.generate_playlist_logos
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 1000
NAVY = (17, 27, 46)             # #111B2E — base field
NAVY_DEEP = (11, 19, 34)        # #0B1322 — roundel fill
INK = (241, 246, 252)           # #F1F6FC — title
MUTE = (174, 191, 214)          # #AEBFD6 — (reserved) muted text
SWOOSH_WHITE = (232, 238, 247)  # #E8EEF7 — glide trail in the mark

OUT_DIR = Path(__file__).resolve().parents[1] / "assets" / "playlists"

# Each managed playlist family: file slug, title (≤2 lines), descriptor, accent, glyph.
# The suffix→slug mapping here MUST match writeback._BRAND_ASSETS.
PLAYLISTS = [
    {"slug": "up_next", "title": ["Up", "Next"], "desc": "what to watch next",
     "accent": (45, 212, 191), "glyph": "chevrons"},
    {"slug": "the_long_glide", "title": ["The Long", "Glide"], "desc": "in-progress sagas",
     "accent": (129, 140, 248), "glyph": "glide"},
    {"slug": "touch_and_go", "title": ["Touch", "& Go"], "desc": "low-commitment picks",
     "accent": (251, 191, 36), "glyph": "tap"},
    {"slug": "fresh_arrivals", "title": ["Fresh", "Arrivals"], "desc": "just landed",
     "accent": (52, 211, 153), "glyph": "sparkle"},
    {"slug": "anniversary_picks", "title": ["Anniversary", "Picks"], "desc": "this week in history",
     "accent": (251, 113, 133), "glyph": "medal"},
    {"slug": "on_this_week", "title": ["On This", "Week"], "desc": "anniversary shows",
     "accent": (56, 189, 248), "glyph": "calendar"},
]


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    """A TrueType face at ``size``. DejaVu ships with Pillow; Arial is the Windows fallback."""
    names = (["DejaVuSans-Bold.ttf", "arialbd.ttf"] if bold
             else ["DejaVuSans.ttf", "arial.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _blend(base, accent, a: float):
    """Flat blend of ``accent`` over solid ``base`` at alpha ``a`` (cheaper than compositing)."""
    return tuple(round(base[i] * (1 - a) + accent[i] * a) for i in range(3))


def _quad(p0, p1, p2, n: int = 64):
    """Sampled quadratic Bézier ``p0→p2`` with control ``p1`` (for swooshes / arcs)."""
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0]
        y = mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _thick_curve(d, pts, width, fill):
    """A smooth thick stroke: stamp overlapping discs along ``pts``. Avoids the faceted
    wedges a wide polyline shows at its segment joins."""
    r = width / 2
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _sparkle(d, cx, cy, r, fill):
    inner = r * 0.34
    d.polygon([(cx, cy - r), (cx + inner, cy - inner), (cx + r, cy), (cx + inner, cy + inner),
               (cx, cy + r), (cx - inner, cy + inner), (cx - r, cy), (cx - inner, cy - inner)],
              fill=fill)


def _star5(d, cx, cy, r, fill):
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=fill)


def _draw_glyph(d, kind, accent):
    """One accent glyph per family, centred in the upper-middle of the square (~cy 415)."""
    if kind == "chevrons":
        d.line([(405, 330), (510, 420), (405, 510)], fill=accent, width=32, joint="curve")
        d.line([(495, 330), (600, 420), (495, 510)], fill=accent, width=32, joint="curve")
    elif kind == "glide":
        d.line(_quad((355, 505), (500, 300), (655, 405)), fill=accent, width=28, joint="curve")
        d.line([(655, 405), (607, 392)], fill=accent, width=26, joint="curve")
        d.line([(655, 405), (636, 448)], fill=accent, width=26, joint="curve")
    elif kind == "tap":
        d.ellipse([383, 373, 467, 457], outline=accent, width=24)
        d.ellipse([413, 403, 437, 427], fill=accent)
        d.line([(520, 388), (660, 388)], fill=accent, width=20)
        d.line([(520, 418), (622, 418)], fill=accent, width=20)
        d.line([(520, 448), (650, 448)], fill=accent, width=20)
    elif kind == "sparkle":
        _sparkle(d, 488, 408, 112, accent)
        _sparkle(d, 612, 528, 52, _blend(NAVY, accent, 0.85))
    elif kind == "medal":
        d.ellipse([414, 314, 586, 486], outline=accent, width=22)
        _star5(d, 500, 400, 64, accent)
        d.line([(460, 478), (434, 566), (498, 512)], fill=accent, width=20, joint="curve")
        d.line([(540, 478), (566, 566), (502, 512)], fill=accent, width=20, joint="curve")
    elif kind == "calendar":
        x0, y0, x1, y1 = 398, 328, 602, 498
        d.rounded_rectangle([x0, y0, x1, y1], radius=16, outline=accent, width=20)
        d.line([(x0, y0 + 50), (x1, y0 + 50)], fill=accent, width=20)
        d.line([(442, 310), (442, 350)], fill=accent, width=18)
        d.line([(558, 310), (558, 350)], fill=accent, width=18)
        d.rounded_rectangle([x0 + 28, y0 + 74, x0 + 28 + 150, y0 + 74 + 32], radius=8, fill=accent)
        for cx in (442, 500, 558):
            d.ellipse([cx - 9, y0 + 132 - 9, cx + 9, y0 + 132 + 9], fill=accent)


def _draw_mark(d, accent):
    """The Glidearr glide-mark roundel + wordmark, top-left."""
    cx, cy, r = 110, 112, 50
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=NAVY_DEEP, outline=accent, width=6)
    d.line(_quad((cx - 28, cy + 20), (cx - 4, cy - 2), (cx + 26, cy - 22)),
           fill=SWOOSH_WHITE, width=11, joint="curve")
    d.line([(cx + 26, cy - 22), (cx + 7, cy - 20)], fill=accent, width=11, joint="curve")
    d.line([(cx + 26, cy - 22), (cx + 22, cy + 2)], fill=accent, width=11, joint="curve")
    f = _font(True, 46)
    x = 184
    glide = "Glide"
    d.text((x, cy - 30), glide, font=f, fill=INK)
    d.text((x + d.textlength(glide, font=f), cy - 30), "arr", font=f, fill=accent)


def render(spec) -> Image.Image:
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    accent = spec["accent"]

    # Low-opacity glide swoosh sweeping the field (flat-blended; stamped smooth).
    _thick_curve(d, _quad((10, 560), (500, 300), (990, 600), n=240), 120,
                 _blend(NAVY, accent, 0.11))

    _draw_mark(d, accent)
    _draw_glyph(d, spec["glyph"], accent)

    lines = spec["title"]
    n = len(lines)
    tf = _font(True, 96)
    ty = 612 if n == 2 else 664
    for i, line in enumerate(lines):
        d.text((70, ty + i * 104), line, font=tf, fill=INK)
    rule_y = ty + n * 104 + 16
    d.rectangle([70, rule_y, 156, rule_y + 9], fill=accent)
    d.text((70, rule_y + 28), spec["desc"], font=_font(False, 36), fill=accent)
    return img


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for spec in PLAYLISTS:
        path = OUT_DIR / f"{spec['slug']}.png"
        render(spec).save(path, "PNG", optimize=True)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
