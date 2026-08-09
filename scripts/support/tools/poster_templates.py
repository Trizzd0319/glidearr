"""poster_templates.py — the design system for the playlist/collection posters.

SINGLE SOURCE OF TRUTH. The 24 ``*.template.svg`` files under
``support/assets/posters/{playlists,collections}/`` are GENERATED from this
module, not hand-authored. Editing an SVG directly will work until the next
rebuild and then be silently reverted, so change the design here.

The reason it is generated rather than drawn: the same twelve families exist on
two canvases, and every one shares a layout, a type scale and a glider system.
Hand-maintaining 24 files in lockstep is how they drift apart, and a poster that
has drifted is invisible until somebody looks at a TV.

WHY TWO CANVASES. Plex renders a PLAYLIST in a 1:1 tile and CENTRE-CROPS
anything taller - a 2:3 poster loses 200px top and bottom, which is the kicker
and the entire why line. A COLLECTION is a library item and sits in the same 2:3
grid as a movie, so a square poster is pillarboxed. Both shapes are correct, for
different targets.

WHERE THE DATE SITS, and why not where it was first put. Measured at real Plex
tile sizes (160 / 220 / 300 px): the kicker row is illegible at all three, the
why line is marginal at 220 and gone at 160, and the TITLE is the only element
that always reads. So a date meant to be NOTICED cannot live in the kicker.

It is right-aligned on the LAST title line's baseline at ~46% of title size.
Inline after a one-line title was measured and rejected: against the 840px inner
measure it fits "Tonight" (590) and "On This Week" (803) but overruns on
"Anniversary Picks" (947), "Fresh Arrivals" (849) and "Because You Watched"
(1074), and un-wrapping those titles to one line would shrink the only element
that stays legible. Right-aligned fits every family with room to spare (tightest
is "You Watched": 308px free for a 215px date) and fills the dead space beside
the shorter second line rather than adding a row.
"""
from __future__ import annotations

from pathlib import Path

#: Bump when the GENERATED OUTPUT changes. ``generate_posters`` stamps this into
#: every template and rebuilds any file carrying a different value, which is what
#: stops a stale template set surviving a design change unnoticed - the exact
#: failure that shipped date-less posters after the date band was added.
TEMPLATE_VERSION = "6"

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "posters"

M = 80                                  # side margin, both kinds

#: kind -> (width, height, kicker_y, title1_y, title2_y, rule_y, why_y, title_px)
GEOM = {
    "playlist":   (1000, 1000, 104, 760, 856, 898, 940, 96),
    "collection": (1000, 1500, 110, 1150, 1250, 1296, 1342, 112),
}

CORM = "'Cormorant Garamond', Garamond, Georgia, serif"
LORA = "'Lora', Georgia, 'Times New Roman', serif"
DEEP = "#0B1322"
INK = "#F1F6FC"

#: slug -> accent + kicker + title lines. Accents carried over from the Pillow
#: generator so the families keep the colours the household already sees.
FAMILIES = {
    "up_next":             dict(accent="#2dd4bf", kicker="01 - UP NEXT",     t=("Up", "Next")),
    "the_long_glide":      dict(accent="#818cf8", kicker="02 - SAGA",        t=("The Long", "Glide")),
    "touch_and_go":        dict(accent="#fbbf24", kicker="03 - STANDALONE",  t=("Touch", "&amp; Go")),
    "fresh_arrivals":      dict(accent="#34d399", kicker="04 - RECENT",      t=("Fresh", "Arrivals")),
    "anniversary_picks":   dict(accent="#fb7185", kicker="05 - ANNIVERSARY", t=("Anniversary", "Picks")),
    "on_this_week":        dict(accent="#38bdf8", kicker="06 - THIS WEEK",   t=("On This", "Week")),
    "hidden_gems":         dict(accent="#e8b768", kicker="07 - DISCOVERY",   t=("Hidden", "Gems")),
    "franchise_run":       dict(accent="#a78bfa", kicker="08 - FRANCHISE",   t=("Franchise", "Run")),
    "tonight":             dict(accent="#f472b6", kicker="09 - TONIGHT",     t=("Tonight", "")),
    "because_you_watched": dict(accent="#22d3ee", kicker="10 - AFFINITY",    t=("Because", "You Watched")),
    "household_picks":     dict(accent="#facc15", kicker="11 - HOUSEHOLD",   t=("Household", "Picks")),
    "kids_safe":           dict(accent="#4ade80", kicker="12 - GATED",       t=("Kids", "Safe")),
    # SHELF variants. Separate slugs, not a reuse of fresh_arrivals /
    # anniversary_picks, because those two are ALSO per-user playlist families -
    # changing their titles to carry a library name would have broken the
    # personal posters that share the slug.
    #
    # The LIBRARY is the headline information here. There are four "This Week In
    # History" shelves, one per section, and in the grid they truncate to
    # "This Week In History - TV Sho..." - identical, distinguishable only by
    # artwork that reshuffles weekly. So the library name gets a title line of
    # its own, in the accent, at title scale where it is actually legible.
    "shelf_just_landed":   dict(accent="#34d399", kicker="SHELF - RECENT",
                                t=("Just Landed", "{{LIBRARY}}")),
    "shelf_this_week":     dict(accent="#fb7185", kicker="SHELF - ANNIVERSARY",
                                t=("This Week In History", "{{LIBRARY}}")),
}

#: Slugs whose title runs at a reduced size. "This Week In History" measures
#: 800px at 96px against an 840px inner measure - it fits, with 40px to spare and
#: no room for a longer library name underneath. At 80px it is 666px, and every
#: section name in this library clears comfortably (widest: TV Shows-Anime, 558).
SMALL_TITLE = {"shelf_this_week": 80, "shelf_just_landed": 80}

#: The why line. ``ranked`` emits slot tspans; ``scalar`` a {{TOKEN}}; ``static``
#: plain text. The slot markup MUST match ``posters._slot_pattern`` character for
#: character, including the single spaces around each separator tspan - that
#: regex is what removes an unused slot together with its leading separator, so a
#: two-item line reads "Alien . Predator" and not "Alien . Predator .".
WHY = {
    "up_next":             ("ranked", "next up in ", "SHOW", 3),
    "the_long_glide":      ("ranked", "continuing ", "FRANCHISE", 3),
    "touch_and_go":        ("ranked", "one-offs in ", "GENRE", 3),
    "hidden_gems":         ("ranked", "owned, unplayed - ", "GENRE", 3),
    "franchise_run":       ("ranked", "all of ", "FRANCHISE", 3),
    "because_you_watched": ("ranked", "because you watch ", "GENRE", 3),
    # Tonight: the after-work slot. COUNT plus what kind of thing it is.
    # TWO genre slots, not three - the line already carries a number and this is
    # the poster somebody glances at while tired.
    #
    # IT NO LONGER SAYS "UNDER AN HOUR". Tonight was briefly runtime-filtered and
    # the copy asserted it; the filter was removed when Tonight became
    # weekday-habit-driven, and the claim was left behind. A live run then shipped
    # a poster reading "7 under an hour" onto a playlist holding 8h12m. Copy that
    # asserts a property the selection no longer enforces is worse than vaguer
    # copy, because it is checkable and it is wrong.
    # "picked for tonight", not "for tonight", so the line survives losing its
    # number. The shared fallback renders with count=NO_VALUE, and a why-line of
    # "for tonight - comedy" opens on a preposition and reads like a truncation.
    # "picked for tonight - comedy" stands on its own; "6 picked for tonight -
    # comedy" is unchanged in meaning when the number IS known.
    "tonight":             ("ranked", "{{COUNT}} picked for tonight - ", "GENRE", 2),
    "fresh_arrivals":      ("scalar", "{{COUNT}} just landed this week"),
    "anniversary_picks":   ("scalar", "{{COUNT}} picks from this week in history"),
    "on_this_week":        ("scalar", "{{COUNT}} shows with an anniversary this week"),
    "kids_safe":           ("scalar", "rated {{CERT}} and below"),
    "household_picks":     ("static", "something for everyone in the house"),
    # Shelf why-lines carry the WEEK, because the date band is unavailable: it
    # right-aligns on the last title line, which here holds the library name, and
    # "TV Shows-Anime" (558) plus a date (215) would overrun the 840px measure.
    "shelf_just_landed":   ("scalar", "{{COUNT}} added · since {{SINCE}}"),
    "shelf_this_week":     ("scalar", "{{COUNT}} · {{WEEK}}"),
}

#: slug -> the live-date token drawn beside the title. Only families whose
#: content is genuinely time-bound get one; the rest answer "what kind of thing
#: is this", which the title already does.
DATE_BAND = {
    "tonight":           "{{DAY}} {{DATE}}",   # TUE 11 AUG - which evening this is for
    "anniversary_picks": "{{WEEK}}",           # 9 - 15 AUG - the week it draws from
    "on_this_week":      "{{WEEK}}",
    "fresh_arrivals":    "SINCE {{SINCE}}",
}

GLIDER = ('<polygon points="90,35 4,6 38,35" fill="{a}"/>'
          '<polygon points="90,35 38,35 4,64" fill="{b}"/>')


def glider(x, y, rot, scale, a, b, op=1.0):
    o = "" if op >= 1 else f' opacity="{op}"'
    return (f'<g{o} transform="translate({x},{y}) rotate({rot}) scale({scale}) '
            f'translate(-45,-35)">{GLIDER.format(a=a, b=b)}</g>')


def formation(slug, accent, W, H):
    """The glider arrangement. Laid out against the SQUARE canvas, then shifted
    down proportionally on the taller collection canvas so it stays centred in
    the art band above the type rather than drifting into it."""
    dy = int((H - 1000) * 0.42)
    if dy:
        return f'<g transform="translate(0,{dy})">{formation(slug, accent, W, 1000)}</g>'
    if slug == "hidden_gems":                       # one lit, cluster faint
        return ('<circle cx="640" cy="400" r="150" fill="%s" opacity="0.06"/>' % accent
                + glider(250, 560, -16, 1.15, "#8fa2bd", "#4b5a75", 0.20)
                + glider(330, 600, -6, 1.15, "#8fa2bd", "#4b5a75", 0.20)
                + glider(640, 400, -18, 2.5, INK, accent))
    if slug == "touch_and_go":                      # single, no sequence
        return glider(520, 430, -18, 2.6, INK, accent)
    if slug == "franchise_run":                     # equal scale on one arc
        return "".join(glider(x, y, -14, 1.75, INK if i == 2 else "#c3cfe0",
                              accent if i == 2 else "#6d7bb0",
                              1.0 if i == 2 else 0.45 + i * 0.18)
                       for i, (x, y) in enumerate([(240, 545), (430, 495), (630, 455)]))
    if slug == "tonight":                           # short hop, low and level
        return (glider(300, 505, -6, 1.25, "#8fa2bd", "#4b5a75", 0.35)
                + glider(560, 470, -6, 2.3, INK, accent))
    if slug == "because_you_watched":               # two trails converging
        return ('<g stroke="#22d3ee" fill="none" stroke-width="1">'
                '<path d="M-40 720 Q 300 640 600 430" opacity="0.14"/>'
                '<path d="M-40 300 Q 320 320 600 430" opacity="0.12"/></g>'
                + glider(250, 640, -28, 1.15, "#8fa2bd", "#4b5a75", 0.30)
                + glider(255, 330, 12, 1.15, "#8fa2bd", "#4b5a75", 0.30)
                + glider(600, 430, -18, 2.5, INK, accent))
    if slug == "household_picks":                   # loose flock, no single lead
        return (glider(250, 560, -14, 1.3, "#c9d1bd", "#7a6a3a", 0.40)
                + glider(400, 640, -4, 1.2, "#d6c9a8", "#7a6a3a", 0.42)
                + glider(430, 470, -22, 1.45, "#e6dcc0", "#8a7a4a", 0.55)
                + glider(640, 545, -12, 2.2, INK, accent))
    if slug == "kids_safe":                         # one glider inside two enclosures
        return ('<g stroke="#4ade80" fill="none">'
                '<path d="M300 330 Q 500 250 700 330 Q 760 520 500 660 Q 240 520 300 330 Z" '
                'stroke-width="1.5" opacity="0.30"/>'
                '<path d="M336 355 Q 500 288 664 355 Q 712 512 500 626 Q 288 512 336 355 Z" '
                'stroke-width="1" opacity="0.15"/></g>'
                + glider(500, 465, -18, 2.1, INK, accent))
    steep = -44 if slug == "fresh_arrivals" else -18
    return (glider(250, 560, steep, 1.2, "#8fa2bd", "#4b5a75", 0.34)
            + glider(400, 490, steep, 1.7, "#c3cfe0", "#6d7bb0", 0.60)
            + glider(600, 405, steep, 2.5, INK, accent))


def why_markup(slug, accent, why_y):
    spec = WHY[slug]
    base = (f'<text x="{M}" y="{why_y}" font-family="{LORA}" font-style="italic" '
            f'font-size="26" fill="#E2E8F6" fill-opacity="0.80">')
    if spec[0] in ("scalar", "static"):
        return base + spec[1] + "</text>"
    _, prefix, token, n = spec
    parts = [prefix, f'<tspan fill="{accent}" fill-opacity="1">{{{{{token}_1}}}}</tspan>']
    for i in range(2, n + 1):
        op = "0.80" if i == 2 else "0.62"
        parts.append(f' <tspan fill-opacity="0.5">\u00b7</tspan> '
                     f'<tspan fill-opacity="{op}">{{{{{token}_{i}}}}}</tspan>')
    return base + "".join(parts) + "</text>"


def date_band(slug, accent, W, y, px):
    """The live date, RIGHT-ALIGNED on the last title line's baseline."""
    tok = DATE_BAND.get(slug)
    if not tok:
        return ""
    return (f'  <text x="{W - M}" y="{y}" text-anchor="end" font-family="{LORA}" '
            f'font-size="{px}" letter-spacing="2.2" fill="{accent}" '
            f'fill-opacity="0.92">{tok}</text>\n')


def build(slug: str, kind: str = "playlist") -> str:
    """The finished tokenised SVG for one family on one canvas."""
    W, H, KICKER_Y, T1_Y, T2_Y, RULE_Y, WHY_Y, TPX = GEOM[kind]
    TPX = SMALL_TITLE.get(slug, TPX)
    f = FAMILIES[slug]
    a = f["accent"]
    t1, t2 = f["t"]
    line2 = (f'  <text x="{M}" y="{T2_Y}" font-family="{CORM}" font-weight="300" '
             f'font-size="{TPX}" fill="{INK}" letter-spacing="-1.4">{t2}</text>\n'
             if t2 else "")
    band = date_band(slug, a, W, (T2_Y if t2 else T1_Y), int(TPX * 0.46))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" data-glidearr-template="{TEMPLATE_VERSION}">
  <title>Glidearr - {t1} {t2}</title>
  <desc>Glidearr {kind} poster, {slug}, {W}x{H}. GENERATED by poster_templates.py - do not hand-edit.</desc>
  <defs>
    <radialGradient id="ground" cx="50%" cy="36%" r="78%">
      <stop offset="0" stop-color="#1b2949"/><stop offset="1" stop-color="{DEEP}"/>
    </radialGradient>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#ground)"/>
  <g stroke="{a}" fill="none" stroke-width="1">
    <path d="M-40 640 Q 420 430 1040 610" opacity="0.16"/>
    <path d="M-40 700 Q 420 490 1040 670" opacity="0.09"/>
  </g>
  {formation(slug, a, W, H)}
  <g transform="translate({M},{KICKER_Y - 18}) scale(0.30)">{GLIDER.format(a=a, b=DEEP)}</g>
  <text x="{M + 42}" y="{KICKER_Y}" font-family="{LORA}" font-size="15" letter-spacing="4.2"
        fill="{INK}" fill-opacity="0.62">GLIDEARR</text>
  <text x="{W - M}" y="{KICKER_Y}" text-anchor="end" font-family="{LORA}" font-size="15"
        letter-spacing="4.2" fill="{a}" fill-opacity="0.75">{f["kicker"]}</text>
  <text x="{M}" y="{T1_Y}" font-family="{CORM}" font-weight="300" font-size="{TPX}"
        fill="{INK}" letter-spacing="-1.4">{t1}</text>
{line2}{band}  <line x1="{M}" y1="{RULE_Y}" x2="{W - M}" y2="{RULE_Y}" stroke="{a}" stroke-opacity="0.30" stroke-width="1"/>
  {why_markup(slug, a, WHY_Y)}
</svg>
'''


def template_path(slug: str, kind: str, root: Path | None = None) -> Path:
    return (root or ASSETS) / f"{kind}s" / f"{slug}.template.svg"


def is_stale(slug: str, kind: str, root: Path | None = None) -> bool:
    """True when the on-disk template is missing or built by an older version.

    Version-stamped rather than mtime-compared: a checkout, a copy or a restore
    all scramble mtimes, and the question that matters is whether the FILE was
    produced by the CURRENT design, not when it was touched.
    """
    path = template_path(slug, kind, root)
    try:
        head = path.read_text(encoding="utf-8")[:400]
    except OSError:
        return True
    return f'data-glidearr-template="{TEMPLATE_VERSION}"' not in head


def rebuild(root: Path | None = None, *, kinds=None, force: bool = False) -> list:
    """Write every stale/missing template. Returns the paths written."""
    written = []
    for kind in (kinds or GEOM):
        out = (root or ASSETS) / f"{kind}s"
        out.mkdir(parents=True, exist_ok=True)
        for slug in FAMILIES:
            if not force and not is_stale(slug, kind, root):
                continue
            path = out / f"{slug}.template.svg"
            path.write_text(build(slug, kind), encoding="utf-8")
            written.append(path)
    return written


if __name__ == "__main__":
    for p in rebuild(force=True):
        print(f"  wrote {p}")
