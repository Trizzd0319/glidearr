"""
safe_cache_clear.py — drop locally-derived caches, keep everything expensive.
================================================================================
Clears the caches that are re-derived from the Radarr / Sonarr / Plex / Tautulli
APIs on the next run, so a schema/layout change starts from clean data. It never
touches the payloads that cost real time or are impossible to rebuild.

PRESERVED (and why)
    trakt/                enrich-daemon output — rate-limited (~1k calls/day), days
                          to weeks of accumulation. The single most valuable thing here.
    mdblist/  mal/        external service payloads.
    ml/                   label pipeline: score/feature snapshots + backfill. Snapshots
                          are POINT-IN-TIME; a deleted one can never be reconstructed,
                          and the forward-validation clock restarts from zero.
    people_matrix/        computed from the trakt enrichment above.
    *.parquet             movie_files / episode_files / owned_episodes / relational.
                          Row CONTENT refreshes from the APIs each run, but these carry
                          accumulated state that does not: watch stats, grace marks,
                          plan stamps, pilot backoff, restore ledgers.
    owned_episodes.fingerprints.json   pairs with its parquet; losing it alone forces a
                          full 12k-series rebuild for no benefit.
    sonarr/jit/           episodes CURRENTLY JIT-upgraded, awaiting restore to their
                          pre-upgrade profile. Deleting this strands them at the wrong
                          quality permanently.
    sonarr/pilot/         unacquirable ledger + search cooldowns. Losing it re-hammers
                          indexers for titles already proven to have no release.
    sonarr/legacy_regrab/ re-grab cooldown ledger.
    radarr/*/monitor_demote_clock.json   dwell clocks.
    system/               backup-gate safety state.
    notifications/        notification dedupe state.

SAFE DEFAULT: dry-run. Nothing is deleted until you pass --yes.

    python scripts/support/safe_cache_clear.py            # show what would go
    python scripts/support/safe_cache_clear.py --yes      # actually delete
    python scripts/support/safe_cache_clear.py --cache-base <path> --yes

After clearing, the next run is slower by design: full series + movie library
re-fetch, one full rescore to reseed both score memos, and a pilot-batch cold
start (jittered + capped, so it trickles rather than spiking).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Roots that must survive. Any delete target resolving inside one of these is
# refused — defence in depth, so a future edit to the target list cannot quietly
# destroy the expensive payloads.
PRESERVE_ROOTS = (
    "trakt", "mdblist", "mal", "ml", "people_matrix",
    "system", "notifications",
    "sonarr/jit", "sonarr/pilot", "sonarr/legacy_regrab",
)
# Individual files that must survive even inside a cleared directory.
PRESERVE_SUFFIXES = (".parquet",)
PRESERVE_NAMES = ("owned_episodes.fingerprints.json", "monitor_demote_clock.json")


def _default_cache_base() -> Path:
    """The repo's cache dir, preferring the app's own key builder when importable."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from scripts.managers.factories.cache.key_builder import CacheKeyBuilder  # type: ignore
        return Path(CacheKeyBuilder().base_dir)
    except Exception:
        return Path(__file__).resolve().parent / "cache"


def _is_preserved(path: Path, base: Path) -> str | None:
    """Return the reason this path must not be deleted, or None when clearable."""
    try:
        rel = path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return "outside the cache base"
    for root in PRESERVE_ROOTS:
        if rel == root or rel.startswith(root + "/"):
            return f"preserved root '{root}'"
    if path.is_file():
        if path.suffix in PRESERVE_SUFFIXES:
            return f"preserved suffix '{path.suffix}'"
        if path.name in PRESERVE_NAMES:
            return f"preserved file '{path.name}'"
    return None


def _targets(base: Path) -> list[Path]:
    """Locally-derived caches, all rebuilt from the *arr / Plex / Tautulli APIs."""
    out: list[Path] = []
    # Radarr: whole-library API snapshots + per-instance API caches
    out += sorted(base.glob("radarr.*.json"))
    out += [base / "radarr" / "custom_formats", base / "radarr" / "metadata"]
    radarr_dir = base / "radarr"
    if radarr_dir.is_dir():
        for inst in sorted(p for p in radarr_dir.iterdir() if p.is_dir()):
            out += [inst / "movie_library", inst / "storage",
                    inst / "movie_score_memo.json"]   # stale after the collection fix
    # Sonarr: letter-bucket library + per-series episode/file API caches
    sonarr_dir = base / "sonarr"
    if sonarr_dir.is_dir():
        for inst in sorted(p for p in sonarr_dir.iterdir()
                           if p.is_dir() and p.name not in ("jit", "pilot", "legacy_regrab")):
            out += [inst / "library", inst / "episodefiles", inst / "episodes",
                    inst / "history", inst / "show_score_memo.json"]
            out += [inst / n for n in (
                "cache_fallback.json", "cache_timestamps.json", "episodes_deletion.json",
                "episodes_file.json", "episodes_history.json", "episodes_monitoring.json",
                "episodes_sharding.json", "errors.json")]
    # Re-fetched wholesale next run (Tautulli keeps the authoritative history server-side)
    out += [base / "plex", base / "tautulli"]
    # Computed previews / calibrations
    out += [base / "universe" / "saga_credit_preview", base / "discovery",
            base / "size_model", base / "franchise_catalog_state.json"]
    return [p for p in out if p.exists()]


def _count(path: Path) -> int:
    if path.is_file():
        return 1
    return sum(1 for _ in path.rglob("*") if _.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-base", type=Path, default=None,
                    help="cache directory (default: the repo's own)")
    ap.add_argument("--yes", action="store_true",
                    help="actually delete (default is a dry run)")
    args = ap.parse_args()

    base = (args.cache_base or _default_cache_base()).resolve()
    if not base.is_dir():
        print(f"cache base not found: {base}")
        return 1

    print(f"cache base: {base}")
    print("DRY RUN — nothing will be deleted (pass --yes to execute)\n" if not args.yes
          else "DELETING\n")

    targets, refused, total = _targets(base), [], 0
    for t in targets:
        reason = _is_preserved(t, base)
        if reason:
            refused.append((t, reason))
            continue
        n = _count(t)
        total += n
        rel = t.relative_to(base).as_posix()
        print(f"  {'remove' if args.yes else 'would remove'}  {rel:<52} {n:>7} file(s)")
        if args.yes:
            try:
                shutil.rmtree(t) if t.is_dir() else t.unlink()
            except OSError as e:
                print(f"    ! failed: {e}")

    if refused:
        print("\nREFUSED (safety guard):")
        for t, reason in refused:
            print(f"  {t.relative_to(base).as_posix():<52} {reason}")

    print(f"\n{'removed' if args.yes else 'would remove'}: {total} file(s)")
    print("preserved:")
    for root in PRESERVE_ROOTS:
        p = base / root
        if p.exists():
            print(f"  {root:<24} {_count(p):>8} file(s)")
    print(f"  {'*.parquet (all)':<24} {sum(1 for _ in base.rglob('*.parquet')):>8} file(s)")

    if not args.yes:
        print("\nre-run with --yes to delete.")
    else:
        print("\nNext run will be slower by design: full library re-fetch, one full "
              "rescore, and a pilot cold start (jittered + capped).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
