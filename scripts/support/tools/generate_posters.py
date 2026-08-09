"""generate_posters.py — regenerate the poster PNGs for BOTH targets.

Operator tool (NOT imported by the engine). The engine only ever READS the
committed PNGs under ``support/assets/playlists/``; this regenerates them.

    python -m scripts.support.tools.generate_posters                    # both kinds
    python -m scripts.support.tools.generate_posters --kind playlist    # one kind
    python -m scripts.support.tools.generate_posters --check            # preflight only
    python -m scripts.support.tools.generate_posters tonight            # one slug

RENAMED from generate_playlist_logos.py. It was never playlist-specific once the
drawing moved into templates: a collection needs the same twelve families, the
same tokens and the same copy - only the CANVAS differs, because Plex renders a
playlist in a 1:1 tile and a collection in the 2:3 library grid. Two thin callers
over one renderer beats two renderers that drift.

WHAT CHANGED. This used to draw every poster with ``PIL.ImageDraw`` primitives,
which meant each string was baked at draw time and the art could carry nothing
household-specific. It now fills a tokenised SVG template per family
(``<slug>.template.svg``) and rasterises it, so a franchise, genre or count can
be baked into the poster:

    render("the_long_glide", franchises=["Alien", "Predator"])
    render("fresh_arrivals", count=27)

All the substitution logic lives in :mod:`posters`; this file is argument
parsing, a preflight, and a loop.

STILL SQUARE (1000x1000). The Pillow version was square on purpose — Plex
renders a PLAYLIST poster in a 1:1 tile and centre-crops anything taller, so a
2:3 canvas loses its top and bottom bands. On the design bundle's 800x1200 that
discards the kicker AND the whole why line. Keep it square unless that is
re-verified against a live client.

Pillow is no longer used for drawing. It remains a project dependency for other
tooling; this module does not import it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.support.tools import poster_templates  # noqa: E402
from scripts.support.tools.posters import (  # noqa: E402
    ASSETS,
    KINDS,
    SPEC,
    PosterError,
    available_backends,
    backend_report,
    check_fonts,
    extract_text,
    rasterize,
    render,
    write_manifest,
)


def preflight() -> int:
    """Report the two things that fail SILENTLY rather than loudly."""
    ok = 0
    backends = available_backends()
    if backends:
        print(f"  rasteriser : {backends[0]}  (also available: {backends[1:] or 'none'})")
    else:
        # Never just say NONE. A backend can be pip-INSTALLED and still unusable
        # (cairosvg without the native cairo DLL is the standard Windows case),
        # and without the reason the obvious next move is to reinstall the thing
        # that cannot work.
        print("  rasteriser : NONE usable")
        for name, usable, reason in backend_report():
            print(f"               {'ok ' if usable else 'no '} {name:13s} {reason}")
        print("               FIX: pip install resvg-py  "
              "(no system dependency; reads the vendored fonts directly)")
        ok = 1

    missing = check_fonts()
    if missing:
        # A missing font never raises on its own: the rasteriser substitutes and
        # the poster ships in the wrong typeface. Measured: 'Cormorant Garamond'
        # resolving to DejaVu Sans, 25% wider, overrunning the title margin.
        print(f"  fonts      : PROBLEM {missing}")
        ok = 1
    elif backends:
        print(f"  fonts      : ok (verified for backend '{backends[0]}')")
    else:
        print("  fonts      : not checked - no usable rasteriser to check against")

    for kind in KINDS:
        miss = [s for s in SPEC if not (ASSETS / f"{kind}s" / f"{s}.template.svg").is_file()]
        stale = [s for s in SPEC if s not in miss and poster_templates.is_stale(s, kind)]
        print(f"  {kind + ' tpl':11s}: {len(SPEC) - len(miss)}/{len(SPEC)} present"
              + (f" — MISSING {miss}" if miss else "")
              + (f" — STALE {stale} (will rebuild)" if stale else ""))
        if miss:
            ok = 1
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slugs", nargs="*", help="slugs to render (default: all)")
    ap.add_argument("--check", action="store_true", help="preflight only, render nothing")
    ap.add_argument("--kind", choices=list(KINDS) + ["all"], default="all")
    ap.add_argument("--rebuild-templates", action="store_true",
                    help="rebuild every template even if the version matches")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    print(f"[posters] assets: {ASSETS}")
    # Templates are GENERATED from poster_templates.py, not hand-authored, and
    # are version-stamped. Rebuilding any that are missing or built by an older
    # design happens BEFORE the preflight counts them - otherwise a design change
    # ships silently against the previous template set, which is exactly how the
    # date band was added and then not rendered for a week.
    rebuilt = poster_templates.rebuild(force=args.rebuild_templates)
    if rebuilt:
        print(f"  templates  : rebuilt {len(rebuilt)} "
              f"(design v{poster_templates.TEMPLATE_VERSION})")
    rc = preflight()
    if args.check:
        return rc
    if rc:
        print("[posters] preflight failed — refusing to render (a wrong-typeface or "
              "unrasterised poster would upload to every profile).")
        return rc

    slugs = args.slugs or list(SPEC)
    unknown = [s for s in slugs if s not in SPEC]
    if unknown:
        print(f"[posters] unknown slug(s): {unknown}; known: {sorted(SPEC)}")
        return 2

    kinds = list(KINDS) if args.kind == "all" else [args.kind]
    total = 0
    for kind in kinds:
        out = args.out or (ASSETS / f"{kind}s")
        out.mkdir(parents=True, exist_ok=True)
        w, h = KINDS[kind]
        print(f"  -- {kind} ({w}x{h}) -> {out}")
        manifest = {}
        for slug in slugs:
            try:
                svg = render(slug, kind)
                png = rasterize(svg, kind)
            except PosterError as exc:
                print(f"     FAIL {slug}: {exc}")
                return 1
            (out / f"{slug}.png").write_bytes(png)
            # Echo the TEXT, not just the byte count. A poster is the one
            # artefact whose correctness cannot be checked from a size, and by
            # the time the write-back sees it the words are pixels.
            text = extract_text(svg)
            manifest[slug] = text
            bits = " | ".join(v for v in (text.get("date"), text.get("why")) if v)
            print(f"     {slug + '.png':30s} {len(png) / 1024:7.1f} KB  "
                  f"{text.get('title', ''):24s} {bits}")
            total += 1
        write_manifest(manifest, out)
    print(f"[posters] {total} poster(s) across {len(kinds)} kind(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
