"""posters.py — tokenised SVG playlist posters (replaces the Pillow drawing path).

Seven posters, one tokenised SVG template each, under
``support/assets/playlists/<slug>.template.svg``. This module fills the tokens,
rasterises to PNG, and writes the ``<slug>.png`` that
``plex/playlists/writeback._BRAND_ASSETS`` uploads.

    from scripts.support.tools.posters import render, rasterize
    svg = render("the_long_glide", franchises=["Alien", "Predator", "The Thing"])
    png = rasterize(svg)

WHY THIS REPLACED PILLOW. The old ``generate_playlist_logos.py`` drew each
poster with ``PIL.ImageDraw`` primitives, so every string was baked at draw time
and the art could not carry per-household values. A tokenised template can:
"continuing Alien . Predator" is the SAME file as "continuing Dune", filled
differently. Twenty genres taken three at a time would be 6,840 baked PNGs; this
is seven templates and a string replace.

SQUARE, 1000x1000 — NOT the 800x1200 of the design bundle. Plex renders a
PLAYLIST poster in a 1:1 tile and centre-crops anything taller: a 2:3 canvas
loses 200px top and bottom, which discards the kicker AND the entire why line —
the one thing these posters exist to show. That finding is inherited from the
Pillow generator's docstring and is the reason the canvas differs from the
design. Re-verify against a live Plex client before changing it back.

Contracts inherited from the playlists package:

* **Deterministic (G3).** Identical input gives byte-identical SVG, so the PNG
  digest is stable and ``writeback._asset_version`` (size + mtime) does not
  churn. No clock, no randomness; SPEC is walked in a fixed order.
* **cp1252-safe (I8).** Everything emitted is ASCII plus U+00B7. A genre or
  franchise name arriving from metadata is NOT guaranteed safe, so
  ``assert_cp1252`` rejects it at render time rather than letting it reach the
  Windows console log handler.
* **Pure layer separates from I/O.** ``render`` returns a string;
  ``rasterize`` and ``write_all`` are separate, so substitution stays testable
  with no rasteriser installed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "posters"

#: kind -> (width, height). ONE renderer serves both targets because they are
#: different SHAPES in Plex, not different styles:
#:
#:   playlist   - Plex renders a playlist in a 1:1 tile and CENTRE-CROPS anything
#:                taller, so a 2:3 poster loses its top and bottom bands - the
#:                kicker and the entire why line, which is the point of these.
#:   collection - a collection is a library ITEM and sits in the same 2:3 grid as
#:                a movie, so a square poster is pillarboxed or cropped sideways.
#:
#: Same SPEC, same tokens, same budgets (both are 1000 wide, so the why-line
#: measure is identical); only the canvas and the template file differ.
KINDS = {"playlist": (1000, 1000), "collection": (1000, 1500)}
DEFAULT_KIND = "playlist"
CANVAS = KINDS[DEFAULT_KIND][0]

#: Vendored OFL fonts, if present. Cormorant Garamond is NOT installed by
#: default on Windows or on a bare Linux box, and a missing font does not fail —
#: fontconfig silently substitutes (measured: DejaVu Sans, which is not even a
#: serif, and 25% wider, so a title overruns the margin). check_fonts() is the
#: guard; call it before a batch rather than trusting the render.
FONTS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
REQUIRED_FONTS = ("Cormorant Garamond", "Lora")


class PosterError(RuntimeError):
    """Raised rather than emitting a poster with a broken or unfilled line."""


@dataclass(frozen=True)
class PosterSpec:
    """What one template accepts.

    ``list_token`` posters take a ranked sequence under ``list_kwarg``; scalar
    posters take named values. Either may be omitted, in which case the
    template's shipped default stands and ``render(slug)`` is still valid.
    """

    slug: str
    #: Token stem for the ranked list: "GENRE" -> {{GENRE_1}}..{{GENRE_3}}.
    list_token: str | None = None
    list_slots: int = 0
    list_kwarg: str = ""
    #: Character budget for the JOINED list, measured against the why-line
    #: measure. On this square canvas the why line spans the full 840px inner
    #: width (x=80..920) — it is NOT in a two-column grid, which is what made
    #: the design bundle's budgets collide with its rationale block.
    list_max_chars: int = 0
    #: Shipped fallback for the ranked line. REQUIRED for a ranked poster:
    #: write-back uploads a poster for every enabled family whether or not that
    #: household has data yet, so ``render(slug)`` with no arguments must always
    #: produce a complete line rather than raising on an unfilled slot.
    list_default: tuple[str, ...] = ()
    scalars: Mapping[str, str] = field(default_factory=dict)


# ── date tokens ────────────────────────────────────────────
# Formatted HERE rather than by each caller, for two reasons that have already
# bitten this pipeline:
#   * cp1252 (I8). ``strftime("%b")`` under a non-English locale can emit a
#     non-cp1252 month abbreviation, which crashes the Windows console log
#     handler when the poster's name is echoed. The tables below are explicit
#     ASCII, so the output is locale-INDEPENDENT and always safe.
#   * a caller using strftime would get the HOST's locale, so two machines would
#     render different posters from the same date.
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def fmt_day(d) -> str:
    """``TUE`` - the weekday this list is FOR."""
    return _DAYS[d.weekday()]


def fmt_date(d) -> str:
    """``11 AUG`` - day-first, no year. The year is noise on a poster that is
    only ever about the next day or two."""
    return f"{d.day} {_MONTHS[d.month - 1]}"


def fmt_week(start, end) -> str:
    """``9 - 15 AUG`` inside one month, ``26 JUL - 1 AUG`` across two.

    Collapsing the repeated month is not cosmetic: at tile size every character
    costs legibility, and "9 AUG - 15 AUG" is a third longer for no information.
    """
    if start.month == end.month:
        return f"{start.day} - {end.day} {_MONTHS[end.month - 1]}"
    return f"{fmt_date(start)} - {fmt_date(end)}"


def week_bounds(d, *, start_weekday: int = 6):
    """(start, end) of the week containing ``d``. SUNDAY-start by default, which
    is how the household reads a week rather than ISO's Monday."""
    from datetime import timedelta
    back = (d.weekday() - start_weekday) % 7
    start = d - timedelta(days=back)
    return start, start + timedelta(days=6)


def default_day():
    from datetime import date
    return fmt_day(date.today())


def default_date():
    from datetime import date
    return fmt_date(date.today())


def default_week():
    from datetime import date
    return fmt_week(*week_bounds(date.today()))


def default_since(days: int = 30):
    from datetime import date, timedelta
    return fmt_date(date.today() - timedelta(days=days))


#: Slug -> spec. The slugs here MUST match ``writeback._BRAND_ASSETS``; that map
#: is what decides which PNG is uploaded for which playlist family.
SPEC: dict[str, PosterSpec] = {
    "up_next": PosterSpec(
        slug="up_next", list_token="SHOW", list_slots=3, list_kwarg="shows",
        list_max_chars=44, list_default=("your series",),
    ),
    "the_long_glide": PosterSpec(
        slug="the_long_glide", list_token="FRANCHISE", list_slots=3,
        list_kwarg="franchises",
        # Franchise names run long ("The Lord of the Rings"), and two is the
        # common case rather than three, so the budget is wider than genres.
        list_max_chars=50, list_default=("your sagas",),
    ),
    "touch_and_go": PosterSpec(
        slug="touch_and_go", list_token="GENRE", list_slots=3, list_kwarg="genres",
        list_max_chars=46, list_default=("every genre",),
    ),
    "hidden_gems": PosterSpec(
        slug="hidden_gems", list_token="GENRE", list_slots=3, list_kwarg="genres",
        list_max_chars=40,          # longest prefix of the four ranked families
        list_default=("your taste",),
    ),
    # The four DATED families. Their templates carry a date band beside the title
    # because at real Plex tile sizes (measured at 160 / 220 / 300 px) the kicker
    # is illegible and only title-scale type reads.
    #
    # A dated poster is NOT deterministic across days - that is the point - so it
    # re-rasterises whenever the date rolls. poster_sync keys its version on the
    # file's size+mtime, so a regenerated PNG re-uploads on its own. It does mean
    # generate_posters must run on a SCHEDULE, not as a one-off operator tool, or
    # Tonight will confidently announce the wrong evening.
    "fresh_arrivals": PosterSpec(
        slug="fresh_arrivals",
        scalars={"COUNT": "12", "SINCE": default_since()},
    ),
    "anniversary_picks": PosterSpec(
        slug="anniversary_picks",
        scalars={"COUNT": "8", "WEEK": default_week()},
    ),
    "on_this_week": PosterSpec(
        slug="on_this_week",
        scalars={"COUNT": "6", "WEEK": default_week()},
    ),

    # ── SHELF variants ──────────────────────────────────────────────
    # One PER SECTION, so LIBRARY is required rather than defaulted: a shelf
    # poster that fell back to a generic library name would be indistinguishable
    # from its three siblings, which is the exact problem these exist to fix.
    # The placeholder below is what an unfilled render shows, and it is
    # deliberately obvious rather than plausible.
    "shelf_just_landed": PosterSpec(
        slug="shelf_just_landed",
        scalars={"COUNT": "12", "SINCE": default_since(), "LIBRARY": "Library"},
    ),
    "shelf_this_week": PosterSpec(
        slug="shelf_this_week",
        scalars={"COUNT": "8", "WEEK": default_week(), "LIBRARY": "Library"},
    ),

    # ── the remaining families ───────────────────────────────────────────────
    # Franchise Run carries franchises and NOTHING else - no genres, no shows.
    # The list is the whole point of the poster, so the budget is the widest.
    "franchise_run": PosterSpec(
        slug="franchise_run", list_token="FRANCHISE", list_slots=3,
        list_kwarg="franchises", list_max_chars=52,
        list_default=("the sagas you follow",),
    ),
    # Tonight is the after-work slot: 5-10 SHORT items (sitcoms, anything under
    # the hour) to put on before bed. Two genre slots, not three - the line
    # already carries a count, and this poster wants to read fast.
    "tonight": PosterSpec(
        slug="tonight", list_token="GENRE", list_slots=2, list_kwarg="genres",
        list_max_chars=30, list_default=("something easy",),
        scalars={"COUNT": "7", "DAY": default_day(), "DATE": default_date()},
    ),
    "because_you_watched": PosterSpec(
        slug="because_you_watched", list_token="GENRE", list_slots=3,
        list_kwarg="genres", list_max_chars=38,
        list_default=("things like these",),
    ),
    "household_picks": PosterSpec(slug="household_picks"),
    "kids_safe": PosterSpec(slug="kids_safe", scalars={"CERT": "PG"}),
}


# ── validation ──────────────────────────────────────────────────────────────
def assert_cp1252(value: str, what: str) -> str:
    """Reject text that would break I8. Metadata is the untrusted edge: a title
    with a curly apostrophe or an em dash arrives from Plex routinely."""
    try:
        value.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise PosterError(
            f"{what} is not cp1252-encodable: {value!r} (offending char at {exc.start})"
        ) from exc
    return value


_XML_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def _escape(value: str) -> str:
    for char, entity in _XML_ESCAPES:
        value = value.replace(char, entity)
    return value


def _normalise(items: Iterable[str]) -> list[str]:
    """Strip, drop empties, collapse internal whitespace; order preserved."""
    out: list[str] = []
    for item in items:
        cleaned = " ".join(str(item).split())
        if cleaned:
            out.append(cleaned)
    return out


def fit_list(items: Sequence[str], max_chars: int) -> list[str]:
    """Drop trailing items until the joined line fits the measure.

    Never returns empty for a non-empty input: one over-long name is kept and
    allowed to run slightly wide, because a tight line beats no line.
    """
    picked = list(items)
    while len(picked) > 1 and len(" \u00b7 ".join(picked)) > max_chars:
        picked.pop()
    return picked


# ── substitution ────────────────────────────────────────────────────────────
def template_path(slug: str, kind: str = DEFAULT_KIND) -> Path:
    """``assets/posters/{kind}s/{slug}.template.svg``."""
    if kind not in KINDS:
        raise PosterError(f"unknown kind {kind!r}; known: {sorted(KINDS)}")
    return ASSETS / f"{kind}s" / f"{slug}.template.svg"


def _slot_pattern(token: str, index: int) -> re.Pattern[str]:
    """Match one UNFILLED ranked slot together with its leading separator.

    Slots 2..n are emitted as::

        ' <tspan fill-opacity="0.5">.</tspan> <tspan fill-opacity="X">{{TOKEN_n}}</tspan>'

    Removing the separator along with the slot is what makes a two-item line
    read "sci-fi . drama" and not "sci-fi . drama ." — the trailing-separator
    bug is the entire reason this is a regex and not a plain replace.

    It therefore matches the template markup CHARACTER FOR CHARACTER, including
    the single spaces around the separator tspan. Re-typing a why line by hand,
    or running an XML prettifier over a template, breaks the collapse silently.
    """
    return re.compile(
        r' <tspan fill-opacity="0\.5">\u00b7</tspan> '
        r'<tspan fill-opacity="[^"]*">\{\{' + re.escape(f"{token}_{index}") + r"\}\}</tspan>"
    )


def ensure_templates(force: bool = False) -> int:
    """Rebuild any missing or version-stale template. Returns the number written.

    CALL THIS BEFORE RENDERING FROM A MANAGER. Templates are GENERATED from
    ``poster_templates.py``, and for a long time the only thing that generated
    them was the ``generate_posters`` CLI. That was fine while rendering was an
    operator step; it stopped being fine the moment the engine started rendering
    per-user and per-section posters, because a design change then shipped code
    that referenced templates nothing had written yet. A live run duly failed
    eight times with ``cannot read template ... shelf_this_week.template.svg``.

    Cheap and idempotent: ``is_stale`` compares a version stamp inside each file,
    so a steady run writes nothing and a version bump rewrites everything exactly
    once.

    NEVER RAISES. A read-only assets directory or a permissions failure must cost
    the poster, not the run - the caller's own render will then fail with a clear
    message about the specific template rather than an opaque error here.
    """
    try:
        from scripts.support.tools import poster_templates
        return len(poster_templates.rebuild(force=force))
    except Exception:
        return 0


def write_if_changed(path, data: bytes) -> bool:
    """Write ``data`` to ``path`` ONLY when the bytes differ. True when written.

    THIS IS WHAT KEEPS THE UPLOAD GATE HONEST. ``poster_sync`` versions an asset
    on size+mtime, and every render site used to ``write_bytes`` unconditionally -
    so each pass gave every PNG a fresh mtime, every version token changed, and
    every poster re-uploaded on every run. The gate was fine; its producer was
    invalidating the key each pass. An armed run's "45 branded" would have
    repeated forever.

    Byte-compare works here because rendering is DETERMINISTIC by construction:
    resvg-py with vendored fonts and skip_system_fonts yields byte-identical
    output for identical input (proved cross-platform when the fonts were
    vendored). Identical values -> identical bytes -> skipped write -> preserved
    mtime -> gated upload. A poster whose VALUES changed (a date roll, a new
    count) produces different bytes and re-uploads - exactly the intended
    behaviour. A non-deterministic backend degrades to always-write, which is
    precisely today's behaviour: no regression, just no benefit.
    """
    from pathlib import Path
    path = Path(path)
    try:
        if path.is_file() and path.read_bytes() == data:
            return False
    except OSError:
        pass                              # unreadable -> attempt the write
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


#: Sentinel for "this poster has no number to report". Distinct from omitting the
#: argument, which keeps the SPEC default — the two mean opposite things and
#: conflating them is what put "12 just landed this week" on a shared fallback.
NO_VALUE = object()


def render(slug: str, kind: str = DEFAULT_KIND, **values) -> str:
    """Return finished SVG source for ``slug``.

    Ranked posters take their list under the spec's ``list_kwarg`` (``shows``,
    ``franchises``, ``genres``); scalar posters take tokens by name (``count``).
    Anything omitted keeps the template's shipped default, so ``render(slug)``
    with no arguments is valid and complete for every slug.

    Pass :data:`NO_VALUE` to DROP a token rather than default it. ``count`` is
    the one that matters: a poster rendered for a profile with no plan has no
    honest number, so it says "just landed this week" rather than inventing
    "12 just landed this week". Omission and NO_VALUE are deliberately different
    — one means "use the default", the other "there is no such number".
    """
    try:
        spec = SPEC[slug]
    except KeyError:
        raise PosterError(f"unknown poster slug {slug!r}; known: {sorted(SPEC)}") from None

    path = template_path(slug, kind)
    try:
        svg = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PosterError(f"cannot read template {path}: {exc}") from exc

    if spec.list_token:
        items = _normalise(values.get(spec.list_kwarg) or ()) or list(spec.list_default)
        if items:
            for item in items:
                assert_cp1252(item, f"{slug} {spec.list_kwarg} item")
            items = fit_list(items[: spec.list_slots], spec.list_max_chars)
            for index, item in enumerate(items, start=1):
                svg = svg.replace(f"{{{{{spec.list_token}_{index}}}}}", _escape(item))
            # Collapse the slots that got no value, highest first so the
            # separator of slot n is gone before slot n-1 is considered.
            for index in range(spec.list_slots, len(items), -1):
                svg = _slot_pattern(spec.list_token, index).sub("", svg)

    for name, default in spec.scalars.items():
        supplied = values.get(name.lower())
        if supplied is NO_VALUE:
            # DROP the token and the single space that follows it, so
            # "{{COUNT}} just landed this week" becomes "just landed this week"
            # rather than a line with a stray leading gap. Every counted why-line
            # is written as "{{TOKEN}} <phrase>" precisely so this reads.
            svg = svg.replace(f"{{{{{name}}}}} ", "").replace(f"{{{{{name}}}}}", "")
            continue
        text = str(default if supplied is None else supplied)
        assert_cp1252(text, f"{slug} {name}")
        svg = svg.replace(f"{{{{{name}}}}}", _escape(text))

    leftover = re.findall(r"\{\{[A-Z_0-9]+\}\}", svg)
    if leftover:
        raise PosterError(
            f"{slug}: unfilled token(s) {sorted(set(leftover))} — the template and "
            f"SPEC disagree; fix one or the other rather than shipping a broken line."
        )
    return svg


# ── rasterisation ───────────────────────────────────────────────────────────
#: Backends tried in order. cairosvg is best quality but on WINDOWS it needs the
#: native cairo DLL (the GTK runtime), which is real friction; resvg is a single
#: self-contained binary with no system dependency and is the easier Windows
#: answer. Every backend is optional — the failure lists them all.
#: Backends tried in order. resvg-py is FIRST and is the recommended install:
#: it is a pip wheel with no system dependency, and crucially it accepts a FONT
#: DIRECTORY, so the vendored OFL fonts are used directly and nothing has to be
#: installed into the OS at all. That removes the entire class of "fontconfig
#: silently substituted DejaVu Sans and the poster shipped in the wrong face".
#: The resvg CLI takes the same directory via --use-fonts-dir. cairosvg is LAST
#: despite good output: it resolves fonts only through the host (so the fonts
#: must be installed) and on Windows it needs the native cairo DLL.
_BACKENDS = ("resvg-py", "resvg", "rsvg-convert", "inkscape", "cairosvg")

#: Backends that can be handed FONTS_DIR directly and therefore need no OS-level
#: font installation. Anything outside this set depends on host fonts.
_FONT_DIR_BACKENDS = frozenset({"resvg-py", "resvg"})


def font_dirs() -> list[str]:
    """Directories handed to a font-dir-capable backend: the vendored fonts only.

    BOTH families are vendored, deliberately. Cormorant Garamond is absent almost
    everywhere so it was obvious. Lora was not vendored at first on the reasoning
    that it is "commonly a system font" — true on a Linux box carrying the
    google-fonts package, FALSE on stock Windows, where ``'Lora', Georgia,
    'Times New Roman', serif`` falls through to Georgia and the kicker and why
    line render in the wrong face with nothing raising.

    Vendoring both is what makes the render deterministic rather than lucky.
    """
    out = [FONTS_DIR / "cormorant-garamond", FONTS_DIR / "lora", FONTS_DIR]
    return [str(d) for d in out if d.is_dir()]


def available_backends() -> list[str]:
    return [name for name, ok, _ in backend_report() if ok]


def backend_report() -> list[tuple[str, bool, str]]:
    """Every candidate backend as ``(name, usable, reason)``.

    :func:`available_backends` swallows the reason, which on Windows produces the
    worst possible message: ``pip install cairosvg`` SUCCEEDS (it is pure Python
    plus cffi), the import then fails at runtime because the native cairo DLL is
    absent, and the tool just reports NONE. An installed-but-broken backend has
    to explain itself, or the operator reinstalls the thing that cannot work.
    """
    out: list[tuple[str, bool, str]] = []
    try:
        import resvg_py  # noqa: F401
        out.append(("resvg-py", True, "reads the vendored fonts directly"))
    except Exception as exc:
        out.append(("resvg-py", False, f"not installed ({type(exc).__name__})"))
    for exe in ("resvg", "rsvg-convert", "inkscape"):
        path = shutil.which(exe)
        out.append((exe, bool(path), path or "not on PATH"))
    try:
        import cairosvg  # noqa: F401
        out.append(("cairosvg", True, "resolves fonts through the HOST - "
                                      "they must be installed system-wide"))
    except Exception as exc:
        # The usual Windows case: the pip package is present but libcairo-2.dll
        # is not, so cairocffi raises on import. Installing cairosvg again will
        # not help - it needs the GTK runtime, or a different backend.
        out.append(("cairosvg", False,
                    f"installed but UNUSABLE: {type(exc).__name__}: {exc}"))
    return out


def rasterize(svg: str, kind: str = DEFAULT_KIND, backend: str | None = None,
              size: tuple[int, int] | None = None) -> bytes:
    """Render SVG source to PNG bytes at the canvas for ``kind``.

    Fonts resolve through the HOST, not through this file: see check_fonts().
    """
    have = available_backends()
    if backend and backend not in have:
        raise PosterError(f"requested backend {backend!r} unavailable; have {have or 'none'}")
    chosen = backend or (have[0] if have else None)
    if chosen is None:
        raise PosterError(
            "no SVG rasteriser available. RECOMMENDED:\n"
            "  pip install resvg-py     no system dependency, and it reads the\n"
            "                           vendored fonts directly, so nothing has\n"
            "                           to be installed into the OS\n"
            "Alternatives: the resvg binary on PATH, rsvg-convert, inkscape, or\n"
            "cairosvg (Windows also needs the GTK runtime for cairo, AND the\n"
            "fonts installed system-wide).\n"
            f"tried: {', '.join(_BACKENDS)}"
        )

    w, h = size or KINDS[kind]

    if chosen == "resvg-py":
        import resvg_py
        # skip_system_fonts=True is the whole point: the vendored files become the
        # ONLY faces in the database, so the output is identical on Windows, on
        # Linux and in a container, and a host font can never quietly win a
        # family-name match. With it False the render silently depends on what the
        # machine happens to have installed — which is how the same twelve posters
        # came out a consistent 0.3-0.7% larger on Windows than on Linux.
        return bytes(resvg_py.svg_to_bytes(
            svg_string=svg, width=w, height=h,
            skip_system_fonts=True, font_dirs=font_dirs()))

    if chosen == "cairosvg":
        import cairosvg
        return cairosvg.svg2png(bytestring=svg.encode("utf-8"),
                                output_width=w, output_height=h)

    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.svg", Path(tmp) / "out.png"
        src.write_text(svg, encoding="utf-8")
        if chosen == "resvg":
            cmd = ["resvg", "-w", str(w), "-h", str(h)]
            for d in font_dirs():          # same vendored fonts as the resvg-py path
                cmd += ["--use-fonts-dir", d]
            cmd += [str(src), str(dst)]
        elif chosen == "rsvg-convert":
            cmd = ["rsvg-convert", "-w", str(w), "-h", str(h), "-o", str(dst), str(src)]
        else:
            cmd = ["inkscape", str(src), "--export-type=png", f"--export-filename={dst}",
                   f"--export-width={w}", f"--export-height={h}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not dst.exists():
            raise PosterError(f"{chosen} failed: {proc.stderr.strip() or proc.returncode}")
        return dst.read_bytes()


def check_fonts() -> list[str]:
    """Return what would make the type render WRONG, as human-readable strings.

    A missing font never raises on its own — the rasteriser substitutes silently
    and the poster ships in the wrong typeface, which is the same shape as the
    branding endpoint that 404'd and cached as success.

    Two different questions, depending on the backend:

    * a FONT-DIR backend (resvg) is handed :func:`font_dirs` directly, so the
      only question is whether the vendored files are on disk. That is checkable
      on EVERY platform, including Windows.
    * cairosvg resolves through the host instead, so the fonts must actually be
      installed — and on Windows there is no fc-match to ask. That case is
      reported as UNVERIFIABLE rather than "ok", because the previous version
      returned [] there and printed "ok" having checked precisely nothing.
    """
    backends = available_backends()
    if not backends:
        return []                       # the rasteriser check already fails this run

    if backends[0] in _FONT_DIR_BACKENDS:
        dirs = font_dirs()
        if not dirs:
            return [f"no vendored font directory at {FONTS_DIR}"]
        found = {p.stem.split("[")[0].split("-")[0].lower()
                 for d in dirs for p in Path(d).glob("*.tt[fc]")}
        # BOTH families are required and BOTH are vendored. The earlier version
        # excluded Lora here with a comment calling it "commonly a system font",
        # which meant the one family most likely to be missing on Windows was the
        # one the check refused to look at.
        return [f"{fam} not found in {dirs}" for fam in REQUIRED_FONTS
                if fam.replace(" ", "").lower() not in found]

    if not shutil.which("fc-match"):
        return [f"cannot verify fonts for backend {backends[0]!r} on this platform "
                f"(no fc-match). Install resvg-py instead — it takes the vendored "
                f"fonts directly and needs no OS font install."]
    missing: list[str] = []
    for family in REQUIRED_FONTS:
        got = subprocess.run(["fc-match", "-f", "%{family}", family],
                             capture_output=True, text=True).stdout
        if family.split()[0].lower() not in got.lower():
            missing.append(f"{family} (resolved to {got.strip()!r})")
    return missing


# ── readable text (for logs, and for the write-back to echo) ───────────────
#: Written beside the PNGs by generate_posters. The write-back uploads a PNG and
#: therefore CANNOT know what is printed on it - by that point the text is pixels.
#: The manifest is how the rendered strings reach the run log without re-parsing
#: an image or re-rendering the SVG.
MANIFEST_NAME = "_poster_text.json"

_TEXT_PATTERNS = (
    ("kicker", r'letter-spacing="4\.2"[^>]*>(.*?)</text>'),
    ("date", r'letter-spacing="2\.2"[^>]*>(.*?)</text>'),
    ("why", r'font-style="italic"[^>]*>(.*?)</text>'),
)


def _strip_tags(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", "", fragment or "").split())


def extract_text(svg: str) -> dict:
    """The human-readable strings on a rendered poster.

    Pulled from the FINISHED SVG rather than rebuilt from SPEC, so what gets
    logged is what was actually drawn - including a truncated list or a
    collapsed separator, which a re-derivation from the inputs would miss.
    """
    out: dict = {}
    for name, pattern in _TEXT_PATTERNS:
        m = re.search(pattern, svg, re.S)
        if m:
            out[name] = _strip_tags(m.group(1))
    titles = [_strip_tags(t) for t in
              re.findall(r'font-weight="300"[^>]*>(.*?)</text>', svg, re.S)]
    out["title"] = " ".join(t for t in titles if t)
    return out


def write_manifest(entries: Mapping[str, dict], out_dir: Path) -> Path:
    """Persist ``{slug: {title, date, why, kicker}}`` beside the PNGs."""
    path = Path(out_dir) / MANIFEST_NAME
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=True) + "\n",
                    encoding="utf-8")
    return path


def read_manifest(out_dir: Path) -> dict:
    """The manifest for a kind, or {} when absent. Missing is NOT an error - a
    poster set generated before manifests existed simply logs no text."""
    try:
        return json.loads((Path(out_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_all(kind: str = DEFAULT_KIND, out_dir: Path | None = None, *,
              values: Mapping[str, dict] | None = None) -> list[Path]:
    """Render + rasterise every slug for ``kind`` to ``<slug>.png``.

    This is the single entry point both consumers call. The playlist write-back
    reads ``assets/posters/playlists/<slug>.png``; a collection promoter would
    read ``assets/posters/collections/<slug>.png``. Neither needs to know how a
    poster is made - they ask for an asset and get one.
    """
    out_dir = out_dir or (ASSETS / f"{kind}s")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for slug in SPEC:
        png = rasterize(render(slug, kind, **(values or {}).get(slug, {})), kind)
        path = out_dir / f"{slug}.png"
        path.write_bytes(png)
        written.append(path)
    return written
