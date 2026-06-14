"""
compare_genre_match.py — preview how each genre_match MODE scores a profile's shows.
================================================================================
Standalone A/B harness for the playlist genre-affinity scorer. Loads a profile's genre
affinity (Tautulli cache) + the Sonarr series library (genres), then prints, side by side,
the genre_match value each MODE (precision | soft | coverage | blend) assigns every show —
so you can SEE the math before flipping ``plex.playlists.genre_match_mode`` and re-running
the playlist dry-run.

    python scripts/support/tools/compare_genre_match.py --list              # available profiles
    python scripts/support/tools/compare_genre_match.py --user Wyatt        # sort by coverage
    python scripts/support/tools/compare_genre_match.py --user Wyatt --sort precision --top 40
    python scripts/support/tools/compare_genre_match.py --user Wyatt --find Bluey

Reads only on-disk caches; writes nothing; no config.json, no API calls. TV only (the
Bluey-style dilution example); the same modes apply identically to movies.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import sys
from pathlib import Path

try:  # UTF-8 console so genre glyphs never crash on Windows cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_SCRIPTS = Path(__file__).resolve().parents[2]          # scripts/
if str(_SCRIPTS.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS.parent))
from scripts.managers.machine_learning.playlists.per_user import (  # noqa: E402
    GENRE_MATCH_MODES,
    genre_match,
)

_CACHE = _SCRIPTS / "support" / "cache"
_AFF_DIR = _CACHE / "tautulli" / "users"


def _list_users() -> list[str]:
    return sorted(p.name for p in _AFF_DIR.glob("*") if (p / "affinity.json").exists())


def _load_affinity(user: str) -> dict:
    try:
        d = json.load(open(_AFF_DIR / user / "affinity.json", encoding="utf-8"))
        return (d.get("genres") if isinstance(d, dict) else {}) or {}
    except Exception:
        return {}


def _load_series() -> dict:
    """{title: [genres]} across every Sonarr library letter bucket."""
    out: dict = {}
    for b in glob.glob(str(_CACHE / "sonarr" / "*" / "library" / "*.json.gz")):
        try:
            with gzip.open(b, "rt", encoding="utf-8") as f:
                for s in json.load(f):
                    if isinstance(s, dict) and s.get("title") and s.get("genres"):
                        out.setdefault(s["title"], s["genres"])
        except Exception:
            continue
    return out


def _fmt(v) -> str:
    return f"{v:.3f}" if v is not None else "  -  "


def main() -> None:
    ap = argparse.ArgumentParser(description="Preview genre_match modes for a profile.")
    ap.add_argument("--user", help="profile folder under cache/tautulli/users (e.g. Wyatt)")
    ap.add_argument("--sort", default="coverage", choices=GENRE_MATCH_MODES, help="mode to rank by")
    ap.add_argument("--top", type=int, default=30, help="rows to show (0 = all)")
    ap.add_argument("--find", help="only show titles containing this substring (case-insensitive)")
    ap.add_argument("--soft-lambda", type=float, default=0.5)
    ap.add_argument("--blend-weight", type=float, default=0.85)
    ap.add_argument("--list", action="store_true", help="list available profiles and exit")
    args = ap.parse_args()

    users = _list_users()
    if args.list or not args.user:
        print("Profiles with affinity:", ", ".join(users) or "(none)")
        if not args.user:
            return

    aff = _load_affinity(args.user)
    if not aff:
        sys.exit(f"No affinity for '{args.user}'. Available: {', '.join(users)}")
    print(f"\n{args.user} affinity: " +
          ", ".join(f"{k}={v}" for k, v in sorted(aff.items(), key=lambda kv: -kv[1])))

    kw = dict(soft_lambda=args.soft_lambda, blend_weight=args.blend_weight)
    needle = (args.find or "").lower()
    rows = []
    for title, genres in _load_series().items():
        if needle and needle not in title.lower():
            continue
        vals = {m: genre_match(genres, aff, mode=m, **kw) for m in GENRE_MATCH_MODES}
        if vals[args.sort] is None:
            continue
        rows.append((title, genres, vals))
    rows.sort(key=lambda r: -(r[2][args.sort] or 0.0))
    if args.top > 0 and not needle:
        rows = rows[: args.top]

    hdr = f"{'#':>3}  {'precision':>9} {'soft':>6} {'coverage':>8} {'blend':>6}   Title  [genres]"
    print(f"(ranked by {args.sort})")
    print(hdr)
    print("-" * len(hdr))
    for i, (title, genres, v) in enumerate(rows, 1):
        print(f"{i:>3}  {_fmt(v['precision']):>9} {_fmt(v['soft']):>6} {_fmt(v['coverage']):>8} "
              f"{_fmt(v['blend']):>6}   {title}  [{', '.join(genres)}]")
    if not rows:
        print("(no matching series with genres)")


if __name__ == "__main__":
    main()
