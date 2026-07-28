"""
ml_backfill_snapshots.py — reconstruct historical feature snapshots by TRUNCATED
REPLAY so today's ~931 real Tautulli watch events become supervised labels NOW.
(ML Stage 1 backfill — offline; writes ONLY ml/snapshots + ml/reports)
================================================================================
Standalone CLI (never imported by main.py). The prospective Stage-1 snapshot
pipeline (labels/snapshots.py) only started logging on 2026-07-25, so the months
of real household watch history preceding it would otherwise take a horizon per
week to become labels. This tool rebuilds "what would the scorer have believed
on day t" for a weekly grid of past dates by TRUNCATING the event history to the
prefix strictly BEFORE t and rescoring every then-existing movie with the REAL
``score_movie`` (via features/movie_features.score_movie_features — the exact
production call path), then appends the rows through the REAL
``labels/snapshots.append_snapshot`` writer.

WHAT IS TRUNCATED (recomputed from ONLY events < t — household state):
  * title-joined watch stats  — watch_count / percent_complete / last_watched_at
    per movie, the same aggregation RadarrMovieFilesCacheManager._fetch_watch_map
    applies to Tautulli history (title join, count, max pct, latest ts). Feeds
    A2 (completion), A3 (rewatch), G2 (abandoned), watched_before.
  * genre_affinity            — rebuilt with the SAME pure brain the production
    pipeline uses (affinity/genre_affinity.aggregate_affinity, incl. the
    library-first metadata merge) over the truncated entries. Feeds B1-B5, E*.
  * watched_tmdb_ids          — Trakt movie history rows with watched_at < t
    UNION Tautulli movie events < t resolved rating_key→tmdb via
    plex/movies/owned_inventory. Feeds C1 (collection), C2 (universe),
    C3 (related-graph) and, when a people matrix exists, the C4 person weights
    (aggregate_person_affinity over the truncated watched set).
  * days_since_last_watch     — extra backfill-only column (None if unwatched).

WHAT IS *NOT* TRUNCATED (today's unversioned state — the KNOWN LEAKAGE, stamped
on every row via ``leakage_flags``):
  * credits_today       — Trakt cast/crew read from today's daemon people bucket.
  * metadata_today      — the movie_files row itself (ratings, popularity,
    certification, release dates → F1/F2/F3/G3/G4 and the movie dict), today's
    collection membership, today's Tautulli metadata index (genre/actor lists
    behind the affinity tallies), today's related-graph neighbours, and today's
    device context (tautulli/platforms + tautulli/transcode; the history cache
    does not persist per-event stream codecs, so these cannot be replayed —
    note D1/D3 are structurally 0 in the movie path and D2 collapses to the
    codec-unknown +2.0 because the production movie dict carries no videoCodec).
    ``score_movie``'s internal now() (F3 recency / G4 availability) is also
    today's clock.
  * deletions_unknown   — the entity set is TODAY's movie_files.parquet: titles
    deleted before today are invisible (survivorship), and a file's presence at
    t is inferred only from the movie's Radarr 'added' date <= t.
  * no_added_date       — appended per-row when no Radarr 'added' date could be
    resolved (the row is then included at EVERY grid date).

PROVENANCE — the non-negotiable contract: every row written here carries
``source="backfill"``, the current ``reconstruction_version`` and ``leakage_flags``;
prospective rows default ``source="prospective"`` (labels/snapshots.py), old
parquets are read as prospective, and the offline consumers
(ml_forward_validation / ml_weight_refit / ml_train_challenger) EXCLUDE
backfill rows unless ``--include-backfill`` is passed. Additionally the grid is
hard-capped BELOW the earliest prospective snapshot_date on disk, so a backfill
row can never land on (or after) a date the live pipeline owns — the writer's
(snapshot_date, instance, entity_id) dedupe therefore cannot merge the two.

Movies only (radarr): the show path has no rating_key→series join in history and
its per-episode ownership at t is unreconstructable; shows accumulate
prospectively.

Usage
-----
    python scripts/support/tools/ml_backfill_snapshots.py                # real caches
    python scripts/support/tools/ml_backfill_snapshots.py --dry-run      # no writes
    python scripts/support/tools/ml_backfill_snapshots.py --grid-days 7 --horizon-days 14
    python scripts/support/tools/ml_backfill_snapshots.py --instance standard \
        --start 2026-02-01 --end 2026-07-11 --cache-base PATH --config PATH

Output: printed report + JSON at <cache>/ml/reports/backfill_snapshots_{date}.json
Writes ONLY <cache>/ml/snapshots/radarr/*.parquet and <cache>/ml/reports/.
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from scripts.managers.factories.cache.key_builder import CacheKeyBuilder            # noqa: E402
from scripts.managers.machine_learning.affinity.genre_affinity import (             # noqa: E402
    aggregate_affinity,
    aggregate_person_affinity,
    build_library_index,
    merge_library_first,
)
from scripts.managers.machine_learning.features.movie_features import (             # noqa: E402
    build_movie_feature_row,
    score_movie_features,
)
from scripts.managers.machine_learning.labels.labeling import (                     # noqa: E402
    build_labels,
    load_owned_movie_rating_keys,
    load_tmdb_completions,
)
from scripts.managers.machine_learning.labels.snapshots import (                    # noqa: E402
    SOURCE_BACKFILL,
    SOURCE_PROSPECTIVE,
    append_snapshot,
    build_movie_snapshot_rows,
    load_snapshots,
)
from scripts.managers.machine_learning.likelihood.watch_likelihood import (         # noqa: E402
    affinity_boost,
)
from scripts.managers.machine_learning.lifecycle.watched_definition import (        # noqa: E402
    DEFAULT_WATCHED_PERCENT,
    play_is_watched,
    resolve_watched_percent,
)
from scripts.managers.machine_learning.scoring._shared import (                     # noqa: E402
    resolve_person_affinity_inputs,
)

# BUMPED 1 -> 2 with the global watched bar. v1 rows reconstructed ``watch_count`` as
# a raw PLAY tally (every Tautulli row counted) and ``is_watched`` as ``plays > 0``;
# v2 counts only plays that clear lifecycle.watched_definition, matching what the
# production parquet now records. The two are NOT comparable: on the live history v1
# marks 162 movie titles watched where v2 marks 79. Anything that pools backfill rows
# across versions must split on this field.
RECONSTRUCTION_VERSION = 2
BASE_LEAKAGE_FLAGS = "credits_today,metadata_today,deletions_unknown"
_SAFE_USER_RE = re.compile(r'[\\/:*?"<>|]')


# ── tiny IO helpers (read-only; every failure degrades to a default) ──────────

def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_gz_json(path: Path, default):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, EOFError):
        return default


def _parse_iso(v):
    """ISO string (with or without Z / offset) -> aware UTC datetime, else None."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# ── history / trakt loaders ───────────────────────────────────────────────────

def load_history_events(base: Path) -> "list[tuple[datetime, dict]]":
    """tautulli/history/all.json as a (ts, raw_entry) list sorted ascending.
    Entries with an unparseable epoch ``date`` are dropped (they cannot be
    placed on the timeline, so they can neither be replayed nor leak)."""
    records = _read_json(Path(base) / "tautulli" / "history" / "all.json", [])
    out: list = []
    if not isinstance(records, list):
        return out
    for r in records:
        if not isinstance(r, dict):
            continue
        try:
            ts = datetime.fromtimestamp(int(r.get("date")), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        out.append((ts, r))
    out.sort(key=lambda p: p[0])
    return out


def load_trakt_movie_watches(base: Path) -> "tuple[list[datetime], list[int]]":
    """trakt/history/movies.json -> parallel (sorted watched_at, tmdb) arrays.
    Rows without a parseable watched_at are skipped (untimestampable → cannot be
    truncated honestly)."""
    raw = _read_json(Path(base) / "trakt" / "history" / "movies.json", [])
    pairs: list = []
    if isinstance(raw, list):
        for e in raw:
            if not isinstance(e, dict):
                continue
            tmdb = ((e.get("movie") or {}).get("ids") or {}).get("tmdb")
            ts = _parse_iso(e.get("watched_at"))
            if tmdb and ts is not None:
                try:
                    pairs.append((ts, int(tmdb)))
                except (TypeError, ValueError):
                    continue
    pairs.sort(key=lambda p: p[0])
    return [t for t, _ in pairs], [m for _, m in pairs]


# ── as-of-t reconstructions (pure over the event prefix) ──────────────────────

def title_watch_map(prefix: "list[tuple[datetime, dict]]", *,
                    watched_pct: float = DEFAULT_WATCHED_PERCENT) -> dict:
    """The as-of-t mirror of RadarrMovieFilesCacheManager._fetch_watch_map:
    Tautulli MOVIE events aggregated by title -> {watch_count, percent_complete
    (max, 0-100), last_watched_at (datetime)}.

    MIRROR, so it applies the SAME watched bar (lifecycle.watched_definition):
    ``watch_count`` counts only plays Tautulli calls watched. Leaving it as a raw
    play tally would systematically INFLATE backfilled engagement against the
    production rows the model is trained to predict — the reconstruction would be
    measuring a definition that no longer exists. ``percent_complete`` and
    ``last_watched_at`` stay threshold-free, exactly as in production."""
    agg: dict = {}
    for ts, r in prefix:
        if r.get("media_type") != "movie":
            continue
        title = r.get("title") or r.get("grandparent_title")
        if not title:
            continue
        rec = agg.setdefault(str(title), {"watch_count": 0, "percent_complete": 0,
                                          "last_watched_at": None})
        if play_is_watched(r, threshold_pct=watched_pct):
            rec["watch_count"] += 1
        try:
            pct = float(r.get("percent_complete") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        rec["percent_complete"] = max(rec["percent_complete"], pct)
        if rec["last_watched_at"] is None or ts > rec["last_watched_at"]:
            rec["last_watched_at"] = ts
    return agg


def watched_tmdbs_at(prefix: "list[tuple[datetime, dict]]", rk_to_tmdb: dict,
                     trakt_ts: list, trakt_tmdb: list, t: datetime) -> "set[int]":
    """As-of-t watched_tmdb_ids: the union production builds from Trakt movie
    history + Tautulli completions keys, time-scoped. Tautulli side: any movie
    event < t whose rating_key resolves via owned_inventory (today's inventory —
    metadata_today; the completions cache itself has no timestamps so it is
    mirrored through the raw events instead)."""
    out: set = set()
    i = bisect.bisect_left(trakt_ts, t)
    out.update(trakt_tmdb[:i])
    for _, r in prefix:
        if r.get("media_type") != "movie":
            continue
        tmdb = rk_to_tmdb.get(str(r.get("rating_key")))
        if tmdb is not None:
            out.add(int(tmdb))
    return out


# ── today-state loaders (the documented leakage surface) ──────────────────────

def load_radarr_full(base: Path, instances: list) -> "tuple[dict, dict, dict]":
    """(collection_members, added_by_tmdb, movie_genres_by_title) from the
    radarr.movies.<inst>.full caches. collection_members/added map come from the
    FIRST instance (the one being backfilled is passed first); the genre map
    unions every instance, mirroring TautulliManager._library_genre_maps."""
    collection_members: dict = {}
    added_by_tmdb: dict = {}
    movie_by_title: dict = {}
    for pos, inst in enumerate(instances):
        movies = _read_json(Path(base) / f"radarr.movies.{inst}.full.json", [])
        if not isinstance(movies, list):
            continue
        for m in movies:
            if not isinstance(m, dict):
                continue
            if m.get("genres") and m.get("title"):
                movie_by_title.setdefault(m["title"], list(m["genres"]))
            if pos == 0:
                mid = m.get("tmdbId")
                coll = (m.get("collection") or {}).get("tmdbId")
                if coll and mid:
                    collection_members.setdefault(int(coll), set()).add(int(mid))
                if mid and m.get("added"):
                    ts = _parse_iso(m.get("added"))
                    if ts is not None:
                        added_by_tmdb[int(mid)] = ts
    return collection_members, added_by_tmdb, movie_by_title


def load_series_genre_map(base: Path) -> dict:
    """series_title -> genres from any sonarr/**/episode_files.parquet (the
    library-first affinity enrichment's TV half). Best-effort."""
    out: dict = {}
    root = Path(base) / "sonarr"
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("episode_files.parquet")):
        try:
            df = pd.read_parquet(p, columns=["series_title", "genres"])
        except Exception:
            continue
        for st, g in zip(df["series_title"], df["genres"]):
            if not st or st in out:
                continue
            if isinstance(g, str):
                try:
                    g = json.loads(g)
                except (ValueError, TypeError):
                    continue
            if isinstance(g, (list, tuple)) and len(g):
                out[str(st)] = list(g)
    return out


def load_metadata_index(base: Path, all_entries: list, movie_by_title: dict,
                        series_by_title: dict) -> dict:
    """Today's Tautulli metadata index + the library-first genre merge — the
    SAME index production hands aggregate_affinity (metadata_today leakage: the
    index itself is unversioned)."""
    idx = _read_json(Path(base) / "tautulli" / "metadata" / "index.json", {})
    if not isinstance(idx, dict):
        idx = {}
    lib = build_library_index(all_entries, movie_by_title, series_by_title)
    return merge_library_first(lib, idx)


def load_per_user_context(base: Path, config: dict) -> "tuple[dict, list, list]":
    """(per_user_affinity, kids_users, adult_users) mirroring
    RadarrSpacePressureManager._build_score_map's config-rating_groups walk.
    No rating_groups configured (the current deployment) -> ({}, [], [])."""
    per_user: dict = {}
    kids: list = []
    adults: list = []
    groups = (config or {}).get("rating_groups", {}) or {}
    for group in groups.values():
        if not isinstance(group, dict):
            continue
        for member in (group.get("members") or []):
            safe = _SAFE_USER_RE.sub("_", str(member)).strip()
            ua = _read_json(Path(base) / "tautulli" / "users" / safe / "affinity.json", None)
            if ua:
                per_user[member] = ua
        for member in (group.get("grace_members") or []):
            kids.append(member)
        for member in (group.get("members") or []):
            if member not in kids:
                adults.append(member)
    return per_user, kids, adults


class GzBucketReader:
    """Memoized reader over a daemon bucket dir of {tmdb}.json.gz files
    (trakt/movies = credits, trakt/movie_related = related). Misses/garbage
    return the default, exactly like the production cache-only readers."""

    def __init__(self, bucket_dir: Path):
        self.dir = Path(bucket_dir)
        self._memo: dict = {}

    def get(self, tmdb_id) -> "dict | list | None":
        try:
            key = int(tmdb_id)
        except (TypeError, ValueError):
            return None
        if key in self._memo:
            return self._memo[key]
        val = _read_gz_json(self.dir / f"{key}.json.gz", None)
        self._memo[key] = val
        return val

    def credits(self, tmdb_id) -> dict:
        val = self.get(tmdb_id)
        return val if isinstance(val, dict) else {}

    def related_tmdbs(self, tmdb_id) -> "set[int]":
        val = self.get(tmdb_id)
        out: set = set()
        if isinstance(val, list):
            for entry in val:
                tid = ((entry or {}).get("ids") or {}).get("tmdb") \
                    if isinstance(entry, dict) else None
                if tid:
                    try:
                        out.add(int(tid))
                    except (TypeError, ValueError):
                        continue
        return out


def load_people_forward(base: Path) -> "dict | None":
    """The people-matrix forward map {(medium, ext_id): {role: [pid]}} for the
    as-of-t C4 person-affinity rebuild. Global-cache JSON first, gz fallback,
    None when the matrix was never built (C4 then stays 0 — production parity)."""
    raw = _read_json(Path(base) / "people_matrix" / "forward.json", None)
    if raw is None:
        raw = _read_gz_json(Path(base) / "trakt" / "people_matrix.json.gz", None)
    if not raw:
        return None
    try:
        from scripts.managers.machine_learning.people_matrix import deserialize_forward
        return deserialize_forward(raw) or None
    except Exception:
        return None


# ── grid ──────────────────────────────────────────────────────────────────────

def build_grid(start: datetime, end: datetime, grid_days: int) -> "list[datetime]":
    """Midnight-UTC snapshot instants from *start*'s date, stepping *grid_days*,
    while <= *end*."""
    t = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    stop = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    out = []
    step = timedelta(days=max(1, int(grid_days)))
    while t <= stop:
        out.append(t)
        t = t + step
    return out


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grid-days", type=int, default=7,
                    help="days between snapshot grid points (default 7)")
    ap.add_argument("--horizon-days", type=int, default=14,
                    help="label horizon; the grid ends at today - horizon so every "
                         "backfilled row is label-mature (default 14)")
    ap.add_argument("--instance", default="standard",
                    help="radarr instance whose movie_files.parquet defines the "
                         "entity set (default standard)")
    ap.add_argument("--start", default=None,
                    help="YYYY-MM-DD grid start (default: first history event)")
    ap.add_argument("--end", default=None,
                    help="YYYY-MM-DD grid end (always clamped to today - horizon "
                         "AND below the first prospective snapshot date)")
    ap.add_argument("--cache-base", default=None,
                    help="override the global cache base dir (tests)")
    ap.add_argument("--config", default=None,
                    help="config.json path (default: the repo config; missing -> {})")
    ap.add_argument("--dry-run", action="store_true",
                    help="reconstruct + report only; write NOTHING")
    ap.add_argument("--no-write", action="store_true",
                    help="skip the ml/reports JSON (snapshot append still happens "
                         "unless --dry-run)")
    args = ap.parse_args(argv)

    base = Path(args.cache_base) if args.cache_base else CacheKeyBuilder().base_dir
    instance = str(args.instance)
    horizon = max(1, int(args.horizon_days))
    now = datetime.now(tz=timezone.utc)

    if args.config is not None:
        config = _read_json(Path(args.config), {})
    else:
        from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH
        config = _read_json(CONFIG_PATH, {})
    if not isinstance(config, dict):
        config = {}

    print("=" * 78)
    print("SNAPSHOT BACKFILL — truncated replay of the real watch history "
          f"(v{RECONSTRUCTION_VERSION})")
    print("=" * 78)
    print(f"cache base: {base}")
    print(f"instance:   {instance}   grid: every {args.grid_days}d   "
          f"horizon: {horizon}d   dry_run: {bool(args.dry_run)}")

    # ── inputs ────────────────────────────────────────────────────────────────
    events = load_history_events(base)
    if not events:
        print("No usable tautulli/history/all.json events — nothing to replay.")
        return 1
    ts_list = [t for t, _ in events]
    raw_entries = [r for _, r in events]
    rk_to_tmdb = load_owned_movie_rating_keys(base)
    completions = load_tmdb_completions(base)
    trakt_ts, trakt_tmdb = load_trakt_movie_watches(base)

    mf_path = Path(base) / "radarr" / instance / "movie_files.parquet"
    try:
        mf = pd.read_parquet(mf_path)
    except Exception as e:
        print(f"Cannot read {mf_path}: {e}")
        return 1
    if mf.empty or "tmdb_id" not in mf.columns:
        print(f"{mf_path} is empty / has no tmdb_id column — nothing to score.")
        return 1

    inst_cfg = [k for k in (config.get("radarr_instances", {}) or {})
                if k != "default_instance"]
    instances = [instance] + [i for i in inst_cfg if i != instance] \
        if inst_cfg else [instance]
    collection_members, added_by_tmdb, movie_by_title = load_radarr_full(base, instances)
    series_by_title = load_series_genre_map(base)
    metadata_index = load_metadata_index(base, raw_entries, movie_by_title, series_by_title)

    platform_usage = _read_json(Path(base) / "tautulli" / "platforms.json", None) or None
    transcode_stats = _read_json(Path(base) / "tautulli" / "transcode.json", None) or None
    per_user_affinity, kids_users, adult_users = load_per_user_context(base, config)

    scoring_cfg = (config.get("scoring", {}) or {})
    rg_cfg = (scoring_cfg.get("related_graph", {}) or {})
    related_enabled = bool(rg_cfg.get("enabled", True))
    try:
        related_graph_cap = float(rg_cfg.get("cap", 4.0))
    except (TypeError, ValueError):
        related_graph_cap = 4.0
    lc_cfg = scoring_cfg.get("language_consumability", {}) or {}
    language_consumability = bool(lc_cfg.get("enabled", False)) \
        if isinstance(lc_cfg, dict) else bool(lc_cfg)
    half_life = scoring_cfg.get("affinity_half_life_days")
    boost = affinity_boost(config)

    credits_bucket = GzBucketReader(Path(base) / "trakt" / "movies")
    related_bucket = GzBucketReader(Path(base) / "trakt" / "movie_related")
    people_fwd = load_people_forward(base)

    # ── events-usable report block (the labeler's own loaders/cuts) ───────────
    movie_events = [(t, r) for t, r in events if r.get("media_type") == "movie"]
    resolvable = [(t, r) for t, r in movie_events
                  if rk_to_tmdb.get(str(r.get("rating_key"))) is not None]
    qualifying = 0
    for _, r in resolvable:
        tmdb = rk_to_tmdb[str(r.get("rating_key"))]
        try:
            pct = float(r.get("percent_complete") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        cut = 50.0 if completions.get(int(tmdb)) else 90.0
        if pct >= cut:
            qualifying += 1
    events_block = {
        "total_events": len(events),
        "movie_events": len(movie_events),
        "movie_events_tmdb_resolvable": len(resolvable),
        "qualifying_movie_label_events": qualifying,
        "first_event": ts_list[0].isoformat(),
        "last_event": ts_list[-1].isoformat(),
        "trakt_movie_watches_timestamped": len(trakt_ts),
    }
    print(f"history: {len(events)} events ({events_block['first_event'][:10]} .. "
          f"{events_block['last_event'][:10]}); {len(movie_events)} movie plays, "
          f"{len(resolvable)} tmdb-resolvable, {qualifying} qualifying label events")

    # ── grid + never-mix guard ────────────────────────────────────────────────
    start = _parse_iso(args.start) if args.start else ts_list[0]
    if start is None:
        print(f"Bad --start {args.start!r}")
        return 1
    end_cap = now - timedelta(days=horizon)
    end = _parse_iso(args.end) if args.end else end_cap
    if end is None:
        print(f"Bad --end {args.end!r}")
        return 1
    end = min(end, end_cap)

    existing = load_snapshots(base, services=("radarr",))
    first_prospective = None
    if not existing.empty and "snapshot_date" in existing.columns:
        prosp = existing[existing["source"] == SOURCE_PROSPECTIVE]
        if not prosp.empty:
            first_prospective = str(prosp["snapshot_date"].min())

    grid_all = build_grid(start, end, args.grid_days)
    grid, skipped_guard = [], []
    for t in grid_all:
        if first_prospective is not None and t.date().isoformat() >= first_prospective:
            skipped_guard.append(t.date().isoformat())
        else:
            grid.append(t)
    if skipped_guard:
        print(f"GUARD: {len(skipped_guard)} grid date(s) on/after the first "
              f"prospective snapshot ({first_prospective}) skipped: "
              f"{', '.join(skipped_guard)}")
    if not grid:
        print("Empty snapshot grid after clamping — nothing to backfill.")
        return 1
    print(f"grid: {len(grid)} date(s)  {grid[0].date()} .. {grid[-1].date()}"
          + (f"  (prospective rows begin {first_prospective})" if first_prospective else ""))

    # ── entity base (today's movie_files rows + added dates) ──────────────────
    entity_rows = []
    for row in mf.to_dict("records"):
        tmdb = row.get("tmdb_id")
        if tmdb is None or pd.isna(tmdb):
            continue
        added = _parse_iso(row.get("added_at"))
        if added is None:
            added = added_by_tmdb.get(int(tmdb))
        entity_rows.append((int(tmdb), added, row))
    n_no_added = sum(1 for _, a, _r in entity_rows if a is None)
    print(f"entities: {len(entity_rows)} movie_files rows "
          f"({n_no_added} with no resolvable added date -> flagged, always included)")

    # ── replay loop ───────────────────────────────────────────────────────────
    all_rows: list = []
    grid_report: list = []
    for t in grid:
        t_iso = t.isoformat()
        i = bisect.bisect_left(ts_list, t)          # events strictly BEFORE t
        prefix = events[:i]
        prefix_raw = raw_entries[:i]

        genre_affinity_t = aggregate_affinity(
            prefix_raw, metadata_index, half_life_days=half_life, now=t)
        watch_map_t = title_watch_map(prefix, watched_pct=resolve_watched_percent(config))
        watched_t = watched_tmdbs_at(prefix, rk_to_tmdb, trakt_ts, trakt_tmdb, t)
        if people_fwd:
            pw_raw = {str(k): v for k, v in aggregate_person_affinity(
                {("movie", m) for m in watched_t}, people_fwd).items()}
        else:
            pw_raw = None
        person_weights, person_cap = resolve_person_affinity_inputs(config, pw_raw)

        recs: list = []
        extras: dict = {}
        for tmdb, added, row in entity_rows:
            flags = BASE_LEAKAGE_FLAGS
            if added is None:
                flags += ",no_added_date"
            elif added > t:
                continue                             # not in the library yet at t
            wm = watch_map_t.get(str(row.get("title") or ""))
            wc_t = int(wm["watch_count"]) if wm else 0
            pct_t = float(wm["percent_complete"]) if wm else 0.0
            last_t = wm["last_watched_at"] if wm else None

            row_t = dict(row)
            # ``wc_t`` is now a WATCH count (title_watch_map applies the bar), so this
            # derivation mirrors the production parquet exactly — as it always claimed to.
            row_t["watch_count"] = wc_t
            row_t["percent_complete"] = pct_t        # 0-100, matching the parquet
            row_t["is_watched"] = wc_t > 0
            row_t["last_watched_at"] = last_t.isoformat() if last_t else None

            fr = build_movie_feature_row(
                row_t,
                credits=credits_bucket.credits(tmdb),
                related_tmdb_ids=(related_bucket.related_tmdbs(tmdb)
                                  if related_enabled else None),
            )
            score, breakdown = score_movie_features(
                fr,
                genre_affinity=genre_affinity_t,
                watched_tmdb_ids=watched_t,
                collection_members=collection_members,
                platform_usage=platform_usage,
                transcode_stats=transcode_stats,
                per_user_affinity=per_user_affinity or None,
                kids_users=kids_users,
                adult_users=adult_users,
                completion_threshold=0.9,
                affinity_boost=boost,
                related_graph_cap=related_graph_cap,
                person_weights=person_weights,
                person_affinity_cap=person_cap,
                language_consumability=language_consumability,
                return_breakdown=True,
            )
            recs.append({
                "tmdb_id": tmdb,
                "title": row.get("title"),
                "watchability_score": float(score),
                "watchability_breakdown": json.dumps(breakdown, separators=(",", ":")),
                "size_bytes": row.get("size_bytes"),
                "resolution": row.get("resolution"),
                "is_watched": wc_t > 0,
                "watch_count": wc_t,
                "planned_action": None,              # ledger state at t is unknowable
            })
            extras[str(tmdb)] = {
                "leakage_flags": flags,
                "days_since_last_watch": (
                    round((t - last_t).total_seconds() / 86400.0, 2)
                    if last_t is not None else None),
            }

        rows = build_movie_snapshot_rows(pd.DataFrame(recs), instance,
                                         snapshot_ts=t_iso) if recs else []
        for r in rows:
            ex = extras.get(r["entity_id"], {})
            r["source"] = SOURCE_BACKFILL
            r["reconstruction_version"] = RECONSTRUCTION_VERSION
            r["leakage_flags"] = ex.get("leakage_flags", BASE_LEAKAGE_FLAGS)
            r["days_since_last_watch"] = ex.get("days_since_last_watch")
        all_rows.extend(rows)
        n_watched = sum(1 for r in rows if r["watched_before"])
        grid_report.append({"date": t.date().isoformat(), "entities": len(rows),
                            "watched_before": n_watched,
                            "events_before_t": i})
        print(f"  {t.date()}  entities={len(rows):<5} watched_before={n_watched:<4} "
              f"events<t={i}")

    print(f"reconstructed rows: {len(all_rows)}")

    # ── write (the ONLY snapshot write) ───────────────────────────────────────
    written = 0
    if args.dry_run:
        print("dry-run: NOT writing snapshots.")
    else:
        written = append_snapshot(base, "radarr", instance, all_rows)
        print(f"appended {written} new row(s) via labels/snapshots.append_snapshot "
              f"-> {Path(base) / 'ml' / 'snapshots' / 'radarr'}")

    # ── label the backfilled rows (n_pos evidence) ────────────────────────────
    if args.dry_run:
        bf = pd.DataFrame(all_rows)
    else:
        store = load_snapshots(base, services=("radarr",), instance=instance)
        bf = store[store["source"] == SOURCE_BACKFILL].reset_index(drop=True)
    label_block: dict = {}
    for h in (7, horizon) if horizon != 7 else (7,):
        labeled = build_labels(bf, base, horizon_days=h, now=now)
        mature = labeled[labeled["label_mature"]] if "label_mature" in labeled.columns \
            else labeled
        n_pos = int(labeled.get("watched_within_h", pd.Series(dtype=bool)).sum())
        n_pos_mature = int(mature.get("watched_within_h", pd.Series(dtype=bool)).sum())
        by_date = {}
        if n_pos:
            pos = labeled[labeled["watched_within_h"]]
            by_date = pos.groupby("snapshot_date").size().astype(int).to_dict()
        label_block[f"horizon_{h}d"] = {
            "n_rows": int(len(labeled)),
            "n_mature": int(len(mature)),
            "n_pos": n_pos,
            "n_pos_mature": n_pos_mature,
            "n_pos_by_snapshot_date": {str(k): int(v) for k, v in sorted(by_date.items())},
        }
        print(f"labels @ horizon {h}d: rows={len(labeled)} mature={len(mature)} "
              f"n_pos={n_pos} (mature n_pos={n_pos_mature})")
        if by_date:
            print("    positives by snapshot date: "
                  + ", ".join(f"{k}:{v}" for k, v in sorted(by_date.items())))

    print("LEAKAGE (stamped on every backfilled row): " + BASE_LEAKAGE_FLAGS
          + " (+no_added_date where applicable) — see module docstring; these rows "
            "are EXCLUDED from the offline tools unless --include-backfill.")

    # ── report JSON ───────────────────────────────────────────────────────────
    report = {
        "generated_at": now.isoformat(),
        "tool": "ml_backfill_snapshots",
        "reconstruction_version": RECONSTRUCTION_VERSION,
        "instance": instance,
        "grid_days": int(args.grid_days),
        "horizon_days": horizon,
        "grid_start": grid[0].date().isoformat(),
        "grid_end": grid[-1].date().isoformat(),
        "first_prospective_snapshot_date": first_prospective,
        "grid_dates_skipped_by_guard": skipped_guard,
        "events": events_block,
        "entities_total": len(entity_rows),
        "entities_no_added_date": n_no_added,
        "grid": grid_report,
        "rows_reconstructed": len(all_rows),
        "rows_appended": written,
        "dry_run": bool(args.dry_run),
        "labels": label_block,
        "leakage_flags": BASE_LEAKAGE_FLAGS,
        "leakage_notes": [
            "credits_today: cast/crew from today's trakt people bucket",
            "metadata_today: today's movie row (ratings/popularity/certification/"
            "release dates), collection membership, Tautulli metadata index, "
            "related-graph neighbours, device context (platforms/transcode), and "
            "score_movie's internal clock (F3/G4)",
            "deletions_unknown: entity set is today's movie_files.parquet — titles "
            "deleted before today are invisible (survivorship bias); presence at t "
            "is inferred from the Radarr 'added' date only",
            "no_added_date (per-row): no Radarr added date — included at every grid date",
        ],
    }
    if not args.no_write and not args.dry_run:
        out_dir = Path(base) / "ml" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"backfill_snapshots_{now:%Y-%m-%d}.json"
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
