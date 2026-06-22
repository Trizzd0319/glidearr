"""
generate_universe_timeline.py — standalone generator for the in-universe MOVIE+SHOW timeline catalog.
================================================================================
Builds the authoritative chronological watch order (films AND TV interleaved) for the supported
universes from **chronolists.com**, whose per-universe order is editorially sourced (it cites the
source for each — Digital Spy, StarWars.com, The Star Trek Chronology Project, Arrowverse.info, …).
Each chronolists page is a Next.js app that embeds its data in a ``__NEXT_DATA__`` JSON blob; we read
that directly. Every entry already carries the external ids glidearr needs — movies a **TMDB** id,
shows a **TVDB** id (from the page's metadata map) — so NO id-conversion service is required.

  python -m scripts.support.tools.generate_universe_timeline [--out PATH] [--only mcu,star] [--dry-run]

Output is JSON (PR-reviewable; the diff is the guard against an upstream order change slipping in
silently) at the package's ``universe_timeline.generated.json``. Shows are COLLAPSED to one entry at
their first chronological appearance (glidearr groups a show as one block, then expands its episodes
in air order) — a season-split interleave isn't representable in the playlist model. This tool does
the network fetch + shape; it NEVER auto-commits — DIFF-REVIEW the output before committing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

_BASE = "https://chronolists.com"
_UA = "glidearr-universe-timeline-gen/1.0 (offline catalog build; contact via project repo)"
_NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# chronolists slug -> (glidearr universe key, display). The key aligns with UNIVERSE_LISTS where a
# film universe already exists (mcu/star/trek/xmen/dcu/arrow), so chronolists supersedes the mdblist
# movie order for it; the TV-only / extra franchises get their own keys. The ``-extended`` / ``-b``
# alternates are skipped (one canonical timeline per universe; add them here if ever wanted).
SLUGS: dict[str, tuple[str, str]] = {
    "mcu":          ("mcu",         "Marvel Cinematic Universe"),
    "star-wars":    ("star",        "Star Wars"),
    "star-trek":    ("trek",        "Star Trek"),
    "dctv":         ("arrow",       "DC Arrowverse"),
    "dceu":         ("dcu",         "DC Extended Universe"),
    "xmen-a":       ("xmen",        "X-Men"),
    "harry-potter": ("wizard",      "Wizarding World"),
    "doctor-who":   ("doctorwho",   "Doctor Who"),
    "stargate":     ("stargate",    "Stargate"),
    "buffy":        ("buffyverse",  "Buffy & Angel"),
    "chicago":      ("one chicago", "Chicago"),
    "walking-dead": ("walkingdead", "The Walking Dead"),
    "bsg":          ("bsg",         "Battlestar Galactica"),
    "underworld":   ("underworld",  "Underworld"),
    "mi":           ("mi",          "Mission: Impossible"),
}

# Curated universes chronolists.com does NOT cover, emitted alongside the fetched ones in RELEASE order
# (earliest first = story continuity). These are plain movie franchises with no special in-universe
# chronology, so release order IS the watch order. Baking them here (rather than leaning on the public
# mdblist lists, several of which are sorted newest-first) gives a correct timeline AND start-first
# acquisition backfill. tmdb ids are TMDB-collection members; extend as new entries release.
_CURATED: dict[str, dict] = {
    "fast": {"display": "Fast & Furious",
             "sources": [{"title": "release order (curated; not on chronolists)", "link": ""}],
             "version": 1, "items": [
        {"media": "movie", "tmdb": 9799,    "title": "The Fast and the Furious"},                # 2001
        {"media": "movie", "tmdb": 584,     "title": "2 Fast 2 Furious"},                        # 2003
        {"media": "movie", "tmdb": 9615,    "title": "The Fast and the Furious: Tokyo Drift"},   # 2006
        {"media": "movie", "tmdb": 13804,   "title": "Fast & Furious"},                          # 2009
        {"media": "movie", "tmdb": 51497,   "title": "Fast Five"},                               # 2011
        {"media": "movie", "tmdb": 82992,   "title": "Fast & Furious 6"},                         # 2013
        {"media": "movie", "tmdb": 168259,  "title": "Furious 7"},                               # 2015
        {"media": "movie", "tmdb": 337339,  "title": "The Fate of the Furious"},                 # 2017
        {"media": "movie", "tmdb": 384018,  "title": "Fast & Furious Presents: Hobbs & Shaw"},   # 2019
        {"media": "movie", "tmdb": 385128,  "title": "F9"},                                      # 2021
        {"media": "movie", "tmdb": 385687,  "title": "Fast X"},                                  # 2023
    ]},
    "rocky": {"display": "Rocky & Creed",
              "sources": [{"title": "release order (curated; not on chronolists)", "link": ""}],
              "version": 1, "items": [
        {"media": "movie", "tmdb": 1366,    "title": "Rocky"},          # 1976
        {"media": "movie", "tmdb": 1367,    "title": "Rocky II"},       # 1979
        {"media": "movie", "tmdb": 1371,    "title": "Rocky III"},      # 1982
        {"media": "movie", "tmdb": 1374,    "title": "Rocky IV"},       # 1985
        {"media": "movie", "tmdb": 1375,    "title": "Rocky V"},        # 1990
        {"media": "movie", "tmdb": 1246,    "title": "Rocky Balboa"},   # 2006
        {"media": "movie", "tmdb": 312221,  "title": "Creed"},          # 2015
        {"media": "movie", "tmdb": 480530,  "title": "Creed II"},       # 2018
        {"media": "movie", "tmdb": 677179,  "title": "Creed III"},      # 2023
    ]},
}


def _fetch_universe(slug: str, *, timeout: float = 30.0) -> dict:
    """GET ``/<slug>`` and pull ``props.pageProps.universe`` out of the page's ``__NEXT_DATA__``."""
    req = urllib.request.Request(f"{_BASE}/{slug}", headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", "replace")
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError(f"{slug}: no __NEXT_DATA__ blob")
    data = json.loads(m.group(1))
    uni = (data.get("props", {}).get("pageProps", {}) or {}).get("universe")
    if not isinstance(uni, dict) or not (uni.get("list") or {}).get("l"):
        raise ValueError(f"{slug}: unexpected page shape (no universe.list.l)")
    return uni


def _extract(uni: dict) -> dict:
    """One chronolists ``universe`` dict → ``{display, slug, sources, version, items:[…]}``.

    ``items`` is the full chronological order: a movie row (``t == "movie"``) carries its own TMDB
    ``id``; a TV row carries a season id (``sid``) resolved through the metadata map ``list.m`` to the
    show's TVDB id + title. Consecutive seasons/episodes of one show collapse to a SINGLE entry at the
    show's first appearance (de-duped by TVDB). Rows we can't key (a show with no TVDB id) are dropped
    and counted so the summary surfaces them for review."""
    L = uni["list"]["l"]
    M = uni["list"].get("m") or {}
    items: list = []
    seen_show: set = set()
    seen_movie: set = set()
    dropped = 0
    for row in L:
        if row.get("t") == "movie":
            tmdb = row.get("id")
            if tmdb is None or tmdb in seen_movie:
                continue
            seen_movie.add(tmdb)
            items.append({"media": "movie", "tmdb": tmdb, "title": row.get("n"),
                          "imdb": row.get("imdb")})
        elif row.get("t") == "tv":
            meta = M.get(str(row.get("sid"))) or M.get(row.get("sid")) or {}
            tvdb = meta.get("tvdb")
            if tvdb is None:
                dropped += 1
                continue
            if tvdb in seen_show:
                continue
            seen_show.add(tvdb)
            items.append({"media": "show", "tvdb": tvdb, "tmdb": meta.get("id"),
                          "title": meta.get("n"), "imdb": meta.get("imdb")})
    return {"display": uni.get("name"), "slug": None, "sources": uni.get("sources") or [],
            "version": uni.get("version"), "items": items, "_dropped_shows_no_tvdb": dropped}


def generate(only: "set[str] | None" = None, *, sleep: float = 1.0) -> dict:
    """Fetch every configured slug (or the ``only`` subset) and return ``{universe_key: extracted}``.
    A slug that fails to fetch/parse is skipped with a stderr note (one bad page never aborts the run)."""
    out: dict = {}
    for slug, (key, display) in SLUGS.items():
        if only and slug not in only and key not in only:
            continue
        try:
            uni = _fetch_universe(slug)
            ex = _extract(uni)
            ex["slug"] = slug
            ex["display"] = display or ex.get("display")
            out[key] = ex
            n_mv = sum(1 for it in ex["items"] if it["media"] == "movie")
            n_sh = sum(1 for it in ex["items"] if it["media"] == "show")
            note = f" ({ex['_dropped_shows_no_tvdb']} show(s) dropped: no tvdb)" if ex["_dropped_shows_no_tvdb"] else ""
            src = ", ".join(s.get("title", "?") for s in ex["sources"]) or "?"
            print(f"  {key:14s} {n_mv:3d} films + {n_sh:3d} shows  [src: {src}]{note}")
        except (urllib.error.URLError, ValueError, json.JSONDecodeError, OSError) as e:
            print(f"  {key:14s} SKIPPED ({slug}): {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(max(0.0, sleep))                              # be polite to the source
    for key, entry in _CURATED.items():                          # universes chronolists doesn't cover
        if only and key not in only:
            continue
        out.setdefault(key, {**entry, "slug": None})             # never override a fetched universe
        print(f"  {key:14s} {len(entry['items']):3d} films + {0:3d} shows  [curated: release order]")
    return out


def _out_path() -> str:
    pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "managers", "services", "plex", "playlists")
    return os.path.join(pkg, "universe_timeline.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the universe MOVIE+SHOW timeline from chronolists.com.")
    ap.add_argument("--out", default=None, help="output JSON path (default: package universe_timeline.generated.json)")
    ap.add_argument("--only", default=None, help="comma-separated slugs/keys to fetch (default: all)")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between requests (politeness)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + print stats only, do not write")
    args = ap.parse_args(argv)
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    print("fetching chronolists.com universes …")
    catalog = generate(only, sleep=args.sleep)
    if not catalog:
        print("no universes fetched — nothing to write", file=sys.stderr)
        return 1
    films = sum(1 for v in catalog.values() for it in v["items"] if it["media"] == "movie")
    shows = sum(1 for v in catalog.values() for it in v["items"] if it["media"] == "show")
    print(f"\n{len(catalog)} universes, {films} films + {shows} shows total")
    if args.dry_run:
        print("(dry-run — nothing written)")
        return 0

    for v in catalog.values():
        v.pop("_dropped_shows_no_tvdb", None)                    # internal counter, not for the bake
    out = args.out or _out_path()
    with open(out, "w", encoding="utf-8") as f:
        json.dump({k: catalog[k] for k in sorted(catalog)}, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"-> wrote {out}  (DIFF-REVIEW before committing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
