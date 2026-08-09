"""trakt_id_repair — verify Trakt id addressing, then purge the poisoned enrich buckets.

BACKGROUND
----------
The enrich daemon fetched ``movies/{id}/…`` and ``shows/{id}/…`` using a TMDB / TVDB id.
Those Trakt paths resolve a Trakt id, a Trakt slug or an IMDB id — never an external id.
Where the external id happened to collide with a real Trakt id, Trakt returned a
DIFFERENT title and the daemon cached it under the external id as though it were right:

    movies/65      8 Mile (tmdb 65)          -> Beverly Hills Cop  (trakt id 65)
    movies/1271    300 (tmdb 1271)           -> an unrelated film  (300 is trakt 884)
    shows/75710    Criminal Minds (tvdb)     -> Ray Evernham, a NASCAR crew chief

So the buckets are not sparse, they are WRONG — and every bucket is affected, not just
``people``, because all twelve endpoints share the ``{id}`` path segment. ``summary``
feeds TV genre affinity and ``related`` is read by ``machine_learning/scoring/movie_scorer``,
so the blast radius is the whole enrichment corpus.

The daemon now sends an imdb/trakt id (see ``enrich_pool``'s ``fetch_map``). This tool
proves that end-to-end against titles with known casts, and only then deletes the bad
cache so the daemon can refetch it correctly.

USAGE
-----
    python scripts/support/tools/trakt_id_repair.py --verify
    python scripts/support/tools/trakt_id_repair.py --purge          # verifies first
    python scripts/support/tools/trakt_id_repair.py --purge --dry-run

``--purge`` REFUSES to delete anything unless the verification passes, so a still-broken
fetch path can't cost you a 247k-call refetch for nothing.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader      # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import (                 # noqa: E402
    CONFIG_PATH, CURSOR_PATH, MOVIE_BUCKETS, PEOPLE_AFFINITY_PATH,
    PEOPLE_MATRIX_PATH, PEOPLE_MATRIX_STATE, PEOPLE_MOVIES_SIDECAR,
    PEOPLE_NAMES_PATH, PEOPLE_SHOWS_SIDECAR, SHOW_BUCKETS, SUPPORT_DIR,
)
from scripts.support.daemons.enrich_daemon import (                           # noqa: E402
    TraktClient, normalise_people,
)

# Titles whose casts are unambiguous, paired with the id the daemon SHOULD now send and
# one name that must appear. Deliberately a name check rather than a full-cast diff: the
# question is "did we get the right film", and one unmistakable credit answers it without
# breaking every time Trakt reorders its billing.
CHECKS = [
    # (medium, external id (bucket key), fetch id, title, expected cast member)
    ("movie", 65,     "tt0298203", "8 Mile",         "Eminem"),
    ("movie", 419430, "tt5052448", "Get Out",        "Daniel Kaluuya"),
    ("movie", 1271,   "tt0416449", "300",            "Gerard Butler"),
    ("show",  75710,  "tt0452046", "Criminal Minds", "Matthew Gray Gubler"),
]


# Everything DERIVED from the buckets. Emptying the buckets alone is not enough: these
# are the files the scorers actually read, and they keep serving the poisoned credits
# until something happens to trigger a rebuild. The build fingerprint does now cover both
# bucket directories, so a rebuild WOULD eventually correct them — but "eventually" means
# an unknown number of runs during which people_affinity and the co-occurrence proposer
# are still scoring against another film's cast. Deleting them makes the bad data
# unreachable immediately; a missing matrix is a clean rebuild, not an error.
_DERIVED = (
    PEOPLE_MATRIX_PATH, PEOPLE_AFFINITY_PATH, PEOPLE_NAMES_PATH,
    PEOPLE_SHOWS_SIDECAR, PEOPLE_MOVIES_SIDECAR, PEOPLE_MATRIX_STATE,
    SUPPORT_DIR / "cache" / "people_matrix" / "forward.json",
    SUPPORT_DIR / "cache" / "people_matrix" / "affinity.json",
    SUPPORT_DIR / "cache" / "people_matrix" / "names.json",
    SUPPORT_DIR / "cache" / "people_matrix" / "run_stats.json",
)


def _names(payload: dict) -> list[str]:
    out = []
    for person in (payload.get("cast") or []):
        src = person.get("person") or person
        name = src.get("name")
        if name:
            out.append(name)
    return out


def verify(trakt: TraktClient, *, verbose: bool = True) -> bool:
    ok = True
    print("=" * 78)
    print("  Verifying Trakt id addressing")
    print("=" * 78)
    for medium, ext, fetch, title, expect in CHECKS:
        ep = f"{'movies' if medium == 'movie' else 'shows'}/{fetch}/people"
        raw = trakt.get(ep)
        names = _names(normalise_people(raw)) if isinstance(raw, dict) else []
        hit = any(expect.lower() in n.lower() for n in names)
        ok = ok and hit
        print(f"\n  {title}  (bucket key {ext} -> fetch {fetch})")
        print(f"    {'PASS' if hit else 'FAIL'}  expected to find: {expect}")
        if verbose:
            print(f"    got: {', '.join(names[:6]) if names else '(no cast returned)'}")
        if not hit:
            print(f"    -> {ep} did not return {title}'s cast.")
    print()
    print("  RESULT:", "PASS - fetch path is correct" if ok else "FAIL - do NOT purge")
    print("=" * 78)
    return ok


def purge(dry_run: bool) -> None:
    """Delete every enrich bucket + the cursor, so the daemon refetches from scratch.

    All twelve buckets go, not just ``people``: they were all addressed by the same bad
    id. The cursor goes too — its per-pool positions describe a walk over data that no
    longer exists, and leaving it would skip the very titles that need refetching.
    """
    total = 0
    for label, buckets in (("movie", MOVIE_BUCKETS), ("show", SHOW_BUCKETS)):
        for name, path in buckets.items():
            if not path.exists():
                continue
            n = sum(1 for _ in path.glob("*.json.gz"))
            total += n
            print(f"  {'would delete' if dry_run else 'deleting'}  {label}/{name:<13} {n:>7,} files")
            if not dry_run:
                shutil.rmtree(path, ignore_errors=True)
                path.mkdir(parents=True, exist_ok=True)
    for path in _DERIVED:
        if not path.exists():
            continue
        kb = path.stat().st_size / 1024
        print(f"  {'would delete' if dry_run else 'deleting'}  derived/{path.name:<28} {kb:>8,.0f} KB")
        if not dry_run:
            path.unlink(missing_ok=True)
    if CURSOR_PATH.exists():
        print(f"  {'would reset' if dry_run else 'resetting'}   cursor {CURSOR_PATH.name}")
        if not dry_run:
            CURSOR_PATH.unlink()
    print(f"\n  {total:,} cached bucket file(s) {'would be' if dry_run else ''} removed.")
    if not dry_run:
        print("  The daemon will refetch on its next cycle — watched and watchlist tiers first.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify Trakt id addressing and purge bad enrich caches")
    ap.add_argument("--verify", action="store_true", help="Spot-check the fetch path only")
    ap.add_argument("--purge", action="store_true", help="Verify, then delete every enrich bucket")
    ap.add_argument("--dry-run", action="store_true", help="With --purge: report, delete nothing")
    args = ap.parse_args()
    if not (args.verify or args.purge):
        ap.print_help()
        return 2

    # Same construction the daemon uses (enrich_daemon.py:1739) — CONFIG_PATH is required,
    # and load() overlays the Trakt secrets from the keyring / env.
    loader = ConfigLoader(CONFIG_PATH)
    cfg = loader.load() or {}
    trakt = TraktClient(cfg, loader)
    if not trakt.client_id:
        print("Trakt is not configured (no client_id) — cannot verify.")
        return 1

    if not verify(trakt):
        if args.purge:
            print("\nRefusing to purge: the fetch path is still returning the wrong titles.")
        return 1
    if args.purge:
        print("\nVerification passed — purging.\n")
        purge(args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())