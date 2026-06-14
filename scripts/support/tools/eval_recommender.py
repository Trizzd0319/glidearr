"""eval_recommender.py — offline recommendation-eval driver (Phase 0 data adapter).
================================================================================
Wires the household's real caches into the pure ``machine_learning/eval`` core and
reports how well the watchability scorecard predicts what the household actually
watched next — the baseline any learned re-ranker must beat.

Two baselines are reported SIDE BY SIDE so the leakage gap is visible:
  * stamped     — ranks movies by the ``watchability_score`` already in
                  movie_files.parquet. Optimistic: those scores were computed using
                  the FULL watch history (including the held-out future), so they
                  leak. Read this as an upper bound.
  * recomputed  — rebuilds the household state as of the split cutoff (eval/replay)
                  and re-runs the REAL ``score_movie`` on each candidate with only
                  pre-cutoff completion / watch_count / watched-set / genre_affinity.
                  Leakage-free → the honest number.

This file lives in the tools layer (NOT the brain): it does the I/O and reuses the
production scorer + ``compute_genre_affinity``, then hands plain ids to the pure
metrics. Run ``--synthetic`` to exercise the whole pipeline without any caches.

USAGE
  python scripts/support/tools/eval_recommender.py --synthetic     # self-test, no caches
  python scripts/support/tools/eval_recommender.py                 # real eval; Radarr instances
                                                                   #   auto-discovered from config.json
  python scripts/support/tools/eval_recommender.py --instance ultra --watched-threshold 0.5

CACHE CONTRACTS (validated against a live cache):
  * tautulli watch history : tautulli/history/all   — entries: rating_key, date (epoch),
                             percent_complete (0-100), media_type  (gives completion + affinity)
  * trakt watch history    : trakt/history/movies   — entries: movie.ids.tmdb, watched_at (ISO);
                             every record is a completed watch  (broadens the watched-set/timeline)
  * metadata index         : tautulli/metadata/index — rating_key -> {tmdb_id, genres, ...}
  * candidates             : <base>/radarr/<instance>/movie_files.parquet
                             columns incl. tmdb_id, watchability_score, ...
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Standalone bootstrap: put the repo root on sys.path so `scripts.*` imports resolve
# when run directly (python scripts/support/tools/eval_recommender.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.machine_learning.eval import metrics, replay, stratify  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── pure orchestration (testable; no I/O) ───────────────────────────────────────
def run_eval(
    events,
    rankers: dict,
    *,
    universe: set | None = None,
    ks=(5, 10, 20),
    holdout_frac: float = 0.2,
    watched_threshold: float = 0.9,
    item_key: str = "item",
    time_key: str = "ts",
    completion_key: str = "completion",
) -> dict:
    """Temporal-split ``events``, build the held-out relevant set, and score each
    ranker (name -> callable(train_events) -> ordered item ids). Returns a report
    dict with overall metrics + head/torso/tail recall per ranker. Pure.

    ``universe`` (optional set of rankable item ids — e.g. the owned library) restricts
    the held-out relevant set, so the eval doesn't penalise a ranker for not surfacing a
    watch of something it can't even rank (a movie no longer in the library)."""
    evs = sorted(events, key=lambda e: e[time_key])
    if len(evs) < 5:
        return {"error": "insufficient events (need >=5)", "n_events": len(evs)}

    cut_idx = int(round(len(evs) * (1.0 - holdout_frac)))
    cut_idx = min(max(cut_idx, 1), len(evs) - 1)
    cutoff = evs[cut_idx][time_key]

    train = [e for e in evs if e[time_key] < cutoff]
    pre_watched = replay.household_state_at(
        train, item_key=item_key, time_key=time_key,
        completion_key=completion_key, watched_threshold=watched_threshold,
    )["watched_ids"]
    relevant = replay.future_watched_items(
        evs, cutoff, item_key=item_key, time_key=time_key,
        completion_key=completion_key, watched_threshold=watched_threshold,
        exclude=pre_watched,
    )
    if universe is not None:
        relevant = {it for it in relevant if it in universe}

    completed = [e for e in evs if float(e.get(completion_key) or 0.0) >= watched_threshold]
    diag = {
        "n_events": len(evs), "n_train": len(train), "n_holdout_events": len(evs) - len(train),
        "n_completed": len(completed), "n_distinct_completed": len({e[item_key] for e in completed}),
        "n_pre_watched": len(pre_watched), "n_relevant": len(relevant),
        "watched_threshold": watched_threshold, "holdout_frac": holdout_frac,
    }
    if not relevant:
        return {"error": "no NEW held-out completed watches after the cutoff (within the owned "
                         "library) — too sparse to evaluate. Try --holdout-frac smaller, a lower "
                         "--watched-threshold, or feed richer history (e.g. Trakt watched).",
                "diagnostics": diag}

    segs = stratify.popularity_segments(stratify.popularity_counts(train, item_key=item_key))
    rel_by_seg = stratify.relevant_by_segment(relevant, segs)
    kmax = max(ks)

    report = {"n_events": len(evs), "n_train": len(train), "cutoff": cutoff,
              "n_relevant": len(relevant), "diagnostics": diag, "rankers": {}}
    for name, ranker in rankers.items():
        ranked = list(ranker(train))
        report["rankers"][name] = {
            "overall": metrics.evaluate_ranking(ranked, relevant, ks=ks),
            "by_segment": {
                seg: {"n": len(rels),
                      f"recall@{kmax}": (metrics.recall_at_k(ranked, rels, kmax) if rels else None)}
                for seg, rels in rel_by_seg.items()
            },
        }
    return report


def _fmt_diag(d: dict) -> str:
    if not d:
        return ""
    return (f"  diagnostics: {d.get('n_events')} events · "
            f"{d.get('n_completed')} completed (≥{d.get('watched_threshold')}) · "
            f"{d.get('n_distinct_completed')} distinct movies · "
            f"{d.get('n_train')} train / {d.get('n_holdout_events')} holdout · "
            f"{d.get('n_pre_watched')} watched pre-cutoff · {d.get('n_relevant')} held-out & owned")


def format_report(report: dict) -> str:
    if "error" in report:
        return f"  ⚠️  {report['error']}\n{_fmt_diag(report.get('diagnostics', {}))}"
    # discover the cutoffs actually computed so columns adapt to --ks
    any_overall = next(iter(report["rankers"].values()))["overall"]
    ks = sorted({int(k.split("@")[1]) for k in any_overall if "@" in k})
    k_lo, k_hi = (ks[0], ks[-1]) if ks else (0, 0)
    lines = [
        f"  events={report['n_events']}  train={report['n_train']}  "
        f"held-out NEW watches={report['n_relevant']}  (cutoff ts={report['cutoff']})",
        "",
        f"  {'ranker':<12} {'MAP':>6} {'P@'+str(k_lo):>6} {'R@'+str(k_hi):>6} "
        f"{'NDCG@'+str(k_hi):>9} {'HR@'+str(k_hi):>7}   head/torso/tail recall@{k_hi}",
        "  " + "-" * 96,
    ]
    for name, r in report["rankers"].items():
        o = r["overall"]
        seg = r["by_segment"]
        def _seg(s):
            v = seg.get(s, {})
            rv = next((v[k] for k in v if k.startswith("recall@")), None)
            return f"{rv:.2f}" if rv is not None else " -  "
        lines.append(
            f"  {name:<12} {o.get('map',0):6.3f} {o.get(f'precision@{k_lo}',0):6.3f} "
            f"{o.get(f'recall@{k_hi}',0):6.3f} {o.get(f'ndcg@{k_hi}',0):9.3f} "
            f"{o.get(f'hit_rate@{k_hi}',0):7.3f}   {_seg('head')} / {_seg('torso')} / {_seg('tail')}"
        )
    return "\n".join(lines)


# ── real-cache rankers (I/O; imported lazily so --synthetic needs no heavy deps) ──
def build_stamped_ranker(candidate_rows):
    """Rank by the watchability_score already in movie_files.parquet (leaky/optimistic)."""
    scored = [(int(r["tmdb_id"]), float(r.get("watchability_score") or 0.0))
              for r in candidate_rows if r.get("tmdb_id") is not None]
    ordered = [tid for tid, _ in sorted(scored, key=lambda kv: -kv[1])]
    return lambda _train: ordered


def build_recomputed_ranker(candidate_rows, all_events, metadata_index, affinity_fn):
    """Rank by re-running the production scorer on pre-cutoff household state.
    ``affinity_fn(entries, metadata_index)`` is the real compute_genre_affinity."""
    import dataclasses
    from scripts.managers.machine_learning.features.movie_features import (
        build_movie_feature_row, score_movie_features,
    )

    def ranker(train_events):
        state = replay.household_state_at(train_events)
        watched = state["watched_ids"]
        completion, counts = state["completion"], state["watch_count"]
        # recompute affinity from the pre-cutoff entries (production fn, truncated input)
        entries = [e["_raw"] for e in train_events if e.get("_raw") is not None]
        genre_affinity = affinity_fn(entries, metadata_index) if entries else {}
        scored = []
        for row in candidate_rows:
            tid = row.get("tmdb_id")
            if tid is None:
                continue
            tid = int(tid)
            fr = build_movie_feature_row(row)
            fr = dataclasses.replace(
                fr,
                percent_complete=float(completion.get(tid, 0.0)),
                watch_count=int(counts.get(tid, 0)),
            )
            s = score_movie_features(
                fr, genre_affinity=genre_affinity, watched_tmdb_ids=watched,
                collection_members={},
            )
            scored.append((tid, s))
        return [tid for tid, _ in sorted(scored, key=lambda kv: -kv[1])]

    return ranker


def build_nextwatch_ranker(candidate_rows, tv_entries, show_taste_by_title):
    """Deterministic NEXT-WATCH baseline (design §0a): rank *unwatched* movie candidates by
    pre-cutoff household taste across GENRE + CAST + CREW affinity — actors weighted highest —
    excluding already-watched.

    Taste is **cross-medium**, and BOTH mediums read from their merged parquet columns (one
    source): movie taste from the watched-OWNED ``movie_files`` people/genre columns (~238
    movies); TV taste from ``show_taste_by_title`` — the per-series genres + cast/crew read from
    the merged ``episode_files`` parquet (populated by Sonarr's refresh_enrichment from the
    daemon's ~3,800-show people, NOT the sparse Tautulli metadata index). Watched shows are
    matched by ``grandparent_title``. Each medium is normalised per category, then combined with
    a cross-medium discount (TV_W). (Watchlist intent is stronger still but not backtestable here.)"""
    import json

    # (parquet field, affinity key, weight) — actors highest (people watch for actors)
    CATS = [
        ("cast_names",     "actors",    1.0),   # pipe-separated, top-10 billed
        ("director_names", "directors", 0.8),
        ("genres",         "genres",    0.7),   # json list
        ("composer_names", "composers", 0.4),
        ("producer_names", "producers", 0.3),
    ]
    TV_W = 0.5   # cross-medium discount: TV-watched taste counts half vs movie-watched
    by_tmdb = {int(r["tmdb_id"]): r for r in candidate_rows if r.get("tmdb_id") is not None}
    show_taste_by_title = show_taste_by_title or {}

    def _members(row, field):
        v = row.get(field)
        if v is None:
            return []
        if field == "genres":
            try:
                items = json.loads(v) if isinstance(v, str) else (v or [])
            except Exception:
                items = []
        else:
            items = str(v).split("|")
        return [str(x).strip().lower() for x in items if str(x).strip()]

    def _norm(d):
        """tally {name: count} → {name: count/max} in [0,1] so mediums/categories compare."""
        m = max(d.values()) if d else 0.0
        return {k: v / m for k, v in d.items()} if m > 0 else {}

    def ranker(train_events):
        watched = replay.household_state_at(train_events)["watched_ids"]
        # MOVIE taste — watched-owned movies' parquet genres + cast/crew (full watched-set)
        movie_tally = {akey: {} for _f, akey, _w in CATS}
        for tid in watched:
            row = by_tmdb.get(tid)
            if not row:
                continue
            for field, akey, _w in CATS:
                t = movie_tally[akey]
                for m in _members(row, field):
                    t[m] = t.get(m, 0.0) + 1.0
        # TV taste (cross-medium) — pre-cutoff watched shows, columns from the episode_files
        # parquet (one source). Each distinct show contributes ONCE (not per episode).
        tv_tally = {akey: {} for _f, akey, _w in CATS}
        cutoff = max((e["ts"] for e in train_events), default=None)
        if show_taste_by_title and tv_entries and cutoff is not None:
            seen_shows: set = set()
            for e in tv_entries:
                d = e.get("date")
                if d is None or d >= cutoff:
                    continue
                title = _norm_title(e.get("grandparent_title"))
                if not title or title in seen_shows:
                    continue
                cols = show_taste_by_title.get(title)
                if not cols:
                    continue
                seen_shows.add(title)
                for field, akey, _w in CATS:
                    t = tv_tally[akey]
                    for m in _members(cols, field):
                        t[m] = t.get(m, 0.0) + 1.0
        # per-medium normalise, then weighted-combine (TV cross-medium discount)
        aff = {}
        for _f, akey, _w in CATS:
            mn, tn = _norm(movie_tally[akey]), _norm(tv_tally[akey])
            aff[akey] = {k: mn.get(k, 0.0) + TV_W * tn.get(k, 0.0) for k in set(mn) | set(tn)}
        scored = []
        for row in candidate_rows:
            tid = row.get("tmdb_id")
            if tid is None:
                continue
            tid = int(tid)
            if tid in watched:                      # next-watch = unwatched only
                continue
            score = 0.0
            for field, akey, w in CATS:
                a = aff[akey]
                if a:
                    score += w * sum(a.get(m, 0.0) for m in _members(row, field))
            rating = float(row.get("imdb_rating") or row.get("tmdb_rating") or 0.0)
            scored.append((tid, score, rating))
        scored.sort(key=lambda t: (-t[1], -t[2]))
        return [tid for tid, _, _ in scored]

    return ranker


# ── cache loaders (I/O) ─────────────────────────────────────────────────────────
def _global_cache(cache_dir: str | None = None):
    """A GlobalCacheManager pointed at the on-disk caches. Built with NO ConfigManager
    (base dir comes from CacheKeyBuilder, not config) so it never triggers the
    secret/onboarding path. ``cache_dir`` overrides the default cache root."""
    from scripts.support.utilities.logger.logger import LoggerManager
    from scripts.managers.factories.cache import GlobalCacheManager
    gcm = GlobalCacheManager(logger=LoggerManager())
    if cache_dir:
        from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
        from scripts.managers.factories.cache.json_handler import CacheJsonManager
        kb = CacheKeyBuilder(base_dir=Path(cache_dir))
        gcm.key_builder = kb
        gcm.cache_root = kb.base_dir
        gcm.json_handler = CacheJsonManager(logger=gcm.logger, base_dir=kb.base_dir)
    return gcm


def load_watch_events(gcm, metadata_index, *, media_type: str = "movie") -> list:
    """Read ``tautulli/history/all`` → eval events. Maps rating_key→tmdb_id via the
    metadata index; ``date`` is the unix timestamp, ``percent_complete`` (0-100) the
    completion fraction. Keeps the raw record under ``_raw`` for affinity recompute."""
    hist = gcm.get("tautulli/history/all") or []
    events = []
    for e in hist:
        if not isinstance(e, dict):
            continue
        if media_type and e.get("media_type") != media_type:
            continue
        md = metadata_index.get(str(e.get("rating_key"))) or {}
        tmdb, ts = md.get("tmdb_id"), e.get("date")
        if tmdb is None or ts is None:
            continue
        pct = e.get("percent_complete")
        events.append({
            "user": e.get("user") or "household",
            "item": int(tmdb),
            "ts": int(ts),
            "completion": (float(pct) / 100.0) if pct is not None else 0.0,
            "_raw": e,
        })
    return events


def load_tv_history(gcm):
    """Tautulli TV episode watch entries (media_type=='episode'), for CROSS-MEDIUM affinity.
    Keeps rating_key + date so the next-watch ranker can resolve genres/cast/crew via the
    metadata index and filter to pre-cutoff. TV is not a movie candidate — it only enriches taste."""
    hist = gcm.get("tautulli/history/all") or []
    return [e for e in hist if isinstance(e, dict) and e.get("media_type") == "episode" and e.get("date")]


def _norm_title(s) -> str:
    """Loose title key for matching Tautulli grandparent_title ↔ Sonarr series title."""
    import re
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def load_show_taste(gcm) -> dict:
    """Per-SERIES enrichment columns (genres + cast/crew) read from the merged
    **episode_files.parquet** — the SINGLE source, populated by Sonarr's
    ``refresh_enrichment`` (Sonarr genres + the daemon's show people, via bucket_merge).
    Keyed by normalised ``series_title`` so the next-watch ranker can match Tautulli's
    ``grandparent_title``. Empty until refresh_enrichment has run (a Sonarr pass)."""
    import pandas as pd
    base = gcm.key_builder.base_dir / "sonarr"
    want = ["series_title", "genres", "cast_names", "director_names",
            "producer_names", "writer_names", "composer_names"]
    out: dict[str, dict] = {}
    for path in base.glob("*/episode_files.parquet"):
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        have = [c for c in want if c in df.columns]
        if "series_title" not in have:
            continue
        for _, r in df[have].drop_duplicates(subset=["series_title"]).iterrows():
            t = _norm_title(r.get("series_title"))
            rec = {c: r.get(c) for c in have if c != "series_title"}
            if t and t not in out and any(rec.get(c) is not None and str(rec.get(c)) != "" for c in rec):
                out[t] = rec
    return out


def _iso_to_epoch(s):
    """Trakt ISO-8601 (…Z) timestamp → unix epoch seconds, matching Tautulli's `date` unit
    so events from both sources sort on one timeline."""
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def load_trakt_watch_events(gcm, *, user: str = "household") -> list:
    """Read `trakt/history/movies` (Trakt sync/history?type=movie) → eval events. Every record
    is a completed watch (completion=1.0); `movie.ids.tmdb` is the item id and `watched_at` the
    timestamp. No `_raw` — Trakt entries carry no Plex metadata, so they enrich the
    watched-set/timeline (Group A) but not the genre-affinity recompute (Group B, Tautulli-only)."""
    hist = gcm.get("trakt/history/movies") or []
    events = []
    for it in hist:
        if not isinstance(it, dict):
            continue
        tmdb = ((it.get("movie") or {}).get("ids") or {}).get("tmdb")
        ts = _iso_to_epoch(it.get("watched_at"))
        if tmdb is None or ts is None:
            continue
        events.append({"user": user, "item": int(tmdb), "ts": ts, "completion": 1.0, "_raw": None})
    return events


def discover_radarr_instances(config_path: str | None = None):
    """Read instance names from config.json's ``radarr_instances`` (keys minus the
    ``default_instance`` meta entry). Returns ``(names, default_name)``."""
    import json
    p = Path(config_path) if config_path else (_REPO_ROOT / "scripts" / "support" / "config" / "config.json")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return [], None
    ri = cfg.get("radarr_instances", {}) or {}
    default = (ri.get("default_instance") or {}).get("name")
    names = [k for k in ri if k != "default_instance"]
    return names, default


def load_candidates_multi(gcm, instances):
    """Load + union movie_files.parquet across ``instances`` (a movie owned in either
    counts). Dedupes by tmdb_id keeping the higher watchability_score. Returns
    ``(rows, [(instance, n_rows, path, status)])``."""
    import pandas as pd
    base = gcm.key_builder.base_dir / "radarr"
    by_tmdb: dict[int, dict] = {}
    loaded = []
    for inst in instances:
        path = base / inst / "movie_files.parquet"
        if not path.exists():
            loaded.append((inst, 0, str(path), "missing"))
            continue
        rows = pd.read_parquet(path).to_dict("records")
        for r in rows:
            tid = r.get("tmdb_id")
            if tid is None:
                continue
            tid = int(tid)
            prev = by_tmdb.get(tid)
            if prev is None or float(r.get("watchability_score") or 0) > float(prev.get("watchability_score") or 0):
                by_tmdb[tid] = r
        loaded.append((inst, len(rows), str(path), "ok"))
    return list(by_tmdb.values()), loaded


def _run_real(args) -> int:
    from scripts.managers.machine_learning.affinity.genre_affinity import aggregate_affinity

    gcm = _global_cache(args.cache_dir)
    ks = tuple(int(x) for x in args.ks.split(",") if x.strip())

    names, default = discover_radarr_instances(args.config)
    if args.instance:
        instances = [args.instance]
    elif names:
        instances = names                       # all configured instances (union)
    else:
        instances = [default or "default"]

    metadata_index = gcm.get("tautulli/metadata/index") or {}
    tautulli_events = load_watch_events(gcm, metadata_index)
    trakt_events = [] if args.no_trakt else load_trakt_watch_events(gcm)
    events = tautulli_events + trakt_events       # item-level aggregation in the metrics dedupes
    tv_entries = [] if args.no_tv else load_tv_history(gcm)   # cross-medium affinity (not candidates)
    show_taste = {} if args.no_tv else load_show_taste(gcm)   # per-series cols from episode_files parquet
    candidate_rows, loaded = load_candidates_multi(gcm, instances)

    distinct = len({e["item"] for e in events})
    print(f"  cache root      : {gcm.key_builder.base_dir}")
    print(f"  config instances: {names or '(config not found)'}  default={default}")
    print(f"  metadata index  : {len(metadata_index)} items")
    print(f"  movie watch evts: {len(events)}  (tautulli {len(tautulli_events)} + trakt "
          f"{len(trakt_events)}) · {distinct} distinct movies")
    print(f"  tv watch evts   : {len(tv_entries)}  (Tautulli episodes → cross-medium affinity)")
    print(f"  tv show taste   : {len(show_taste)} series enriched in episode_files parquet "
          f"({'run Sonarr refresh_enrichment to populate' if not show_taste else 'one source'})")
    for inst, n, path, status in loaded:
        print(f"    radarr/{inst:<10} {n:>5} movies  [{status}]")
    print(f"  candidate movies: {len(candidate_rows)}  (union across {len(instances)} instance(s), deduped)")
    if not events or not candidate_rows:
        print("  ⚠️  missing cached data — run `python scripts/main.py` once to populate "
              "tautulli/history/all, tautulli/metadata/index and movie_files.parquet.")
        return 2

    universe = {int(r["tmdb_id"]) for r in candidate_rows if r.get("tmdb_id") is not None}
    rankers = {
        "stamped(leaky)": build_stamped_ranker(candidate_rows),
        "recomputed": build_recomputed_ranker(candidate_rows, events, metadata_index, aggregate_affinity),
        "nextwatch": build_nextwatch_ranker(candidate_rows, tv_entries, show_taste),
    }
    report = run_eval(events, rankers, universe=universe, ks=ks,
                      holdout_frac=args.holdout_frac, watched_threshold=args.watched_threshold)
    print()
    print(format_report(report))
    print("\n  Read: 'stamped' is the optimistic upper bound (scores used full history);")
    print("  'recomputed' is leakage-free (pre-cutoff state only) — the honest baseline a")
    print("  learned re-ranker must beat. A large stamped≫recomputed gap = heavy leakage.")
    return 0 if "error" not in report else 2


def _run_forward(args) -> int:
    """FORWARD validation of the watchlist (design §8 blind spot — the watchlist can't be
    backtested). Reads the Plex watchlist SNAPSHOTS (plex/watchlist/snapshot/<ts>, written by
    the Plex service) and asks, for each snapshot older than the window: of the movies on the
    list at T, how many were watched within W days — and is that a LIFT over the base watch-rate
    of non-watchlisted owned movies? (Lift, not hit-rate, is the load-bearing number.)"""
    from datetime import datetime, timezone
    from scripts.managers.machine_learning.eval import forward

    gcm = _global_cache(args.cache_dir)
    window_s = max(1, int(args.window_days)) * 86400
    now = datetime.now(timezone.utc).timestamp()

    metadata_index = gcm.get("tautulli/metadata/index") or {}
    events = load_watch_events(gcm, metadata_index) + ([] if args.no_trakt else load_trakt_watch_events(gcm))
    names, default = discover_radarr_instances(args.config)
    instances = [args.instance] if args.instance else (names or [default or "default"])
    candidate_rows, _loaded = load_candidates_multi(gcm, instances)
    owned = {int(r["tmdb_id"]) for r in candidate_rows if r.get("tmdb_id") is not None}

    index = gcm.get("plex/watchlist/snapshots_index") or []
    print(f"  watchlist snapshots : {len(index)}  (plex/watchlist/snapshots_index)")
    print(f"  owned movies (base) : {len(owned)}   movie watch events: {len(events)}")
    print(f"  window              : {args.window_days} days")
    if not index:
        print("  ⚠️  no watchlist snapshots yet — run `python scripts/main.py` with Plex configured to start")
        print("      writing plex/watchlist/snapshot/<ts>. Forward validation matures as snapshots age past")
        print("      the window and watching happens. (This is expected on a fresh Plex install.)")
        return 2

    def _ts_to_epoch(ts):
        try:
            return datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return None

    per_snap, mature = [], 0
    for ts in index:
        t0 = _ts_to_epoch(ts)
        if t0 is None or (t0 + window_s) > now:        # skip unparseable / immature
            continue
        items = (gcm.get(f"plex/watchlist/snapshot/{ts}") or {}).get("items") or []
        predicted = {int((it.get("ids") or {}).get("tmdb"))
                     for it in items
                     if it.get("type") == "movie" and (it.get("ids") or {}).get("tmdb")}
        if not predicted:
            continue
        watched = forward.watched_in_window(events, t0, t0 + window_s)
        per_snap.append(forward.evaluate_snapshot(predicted, owned, watched))
        mature += 1

    print(f"  mature snapshots    : {mature}  (age ≥ window, with movie predictions)")
    if not per_snap:
        print("  ⚠️  snapshots exist but none are mature yet — forward validation lights up as they age.")
        print("      Try a smaller --window-days, or let it accumulate.")
        return 2
    agg = forward.aggregate_forward(per_snap)
    hr, br, lf = agg.get("hit_rate"), agg.get("base_rate"), agg.get("lift")
    print()
    print(f"  watchlist hit-rate  : {hr:.3f}  ({agg['total_hits']}/{agg['total_predicted']} watchlisted movies watched in-window)")
    print(f"  base watch-rate     : {br:.3f}  (non-watchlisted owned movies)" if br is not None else "  base watch-rate     : n/a")
    print(f"  LIFT                : {lf:.2f}x  (>1 ⇒ watchlist predicts next-watch · ~1 ⇒ no signal)"
          if lf is not None else "  LIFT                : n/a (no base pool)")
    return 0


def _forward_synthetic() -> int:
    """Prove the forward-validation pipeline (snapshot → window → lift) with no caches."""
    from scripts.managers.machine_learning.eval import forward
    # watchlist at T = {1,2,3}; owned = {1..6}; watched in window = {1,2,5}
    agg = forward.aggregate_forward([forward.evaluate_snapshot({1, 2, 3}, set(range(1, 7)), {1, 2, 5})])
    print("[eval_recommender --forward --synthetic]")
    print(f"  hit_rate={agg['hit_rate']:.3f}  base_rate={agg['base_rate']:.3f}  LIFT={agg['lift']:.2f}x")
    assert agg["lift"] and agg["lift"] > 1.0, agg
    print("  ✅ forward-validation pipeline OK — watchlist lift computed (2.0x on the fixture).")
    return 0


def _synthetic() -> int:
    """Prove the full split→rank→metrics→stratify pipeline runs end-to-end (no caches).
    Movie ids are ints (tmdb-like, since build_stamped_ranker int()-casts tmdb_id)."""
    def ev(item, ts, c=1.0):
        return {"item": item, "ts": ts, "completion": c}

    # household completes 1,2,3 early (1 re-watched → head); then 4,5 later (held out)
    events = [ev(1, 1), ev(1, 2), ev(1, 3), ev(2, 4), ev(3, 5), ev(4, 8), ev(5, 9)]
    good = {1: 90, 2: 40, 3: 50, 4: 90, 5: 85}   # scorecard ranks held-out 4,5 high
    pop = {1: 99, 2: 60, 3: 55, 4: 20, 5: 10}    # popularity ranks 1 high, buries 4,5
    rankers = {
        "scorecard": build_stamped_ranker([{"tmdb_id": k, "watchability_score": v} for k, v in good.items()]),
        "popularity": build_stamped_ranker([{"tmdb_id": k, "watchability_score": v} for k, v in pop.items()]),
    }
    report = run_eval(events, rankers, ks=(2, 5), holdout_frac=0.3)
    print("[eval_recommender --synthetic]")
    print(format_report(report))
    assert "error" not in report, report
    sc = report["rankers"]["scorecard"]["overall"]["ndcg@5"]
    pp = report["rankers"]["popularity"]["overall"]["ndcg@5"]
    assert sc > pp, f"scorecard NDCG@5 ({sc:.3f}) should beat popularity ({pp:.3f}) here"
    print("\n  ✅ synthetic pipeline OK — scorecard NDCG beats popularity as expected.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Offline recommendation eval (Phase 0).")
    ap.add_argument("--synthetic", action="store_true", help="run the built-in self-test (no caches needed)")
    ap.add_argument("--cache-dir", help="cache root holding tautulli/ + radarr/ data")
    ap.add_argument("--config", help="path to config.json (default: scripts/support/config/config.json)")
    ap.add_argument("--instance", default=None,
                    help="restrict to one Radarr instance; default = union of all configured instances")
    ap.add_argument("--holdout-frac", type=float, default=0.2)
    ap.add_argument("--watched-threshold", type=float, default=0.9,
                    help="completion fraction that counts as 'watched' (default 0.9)")
    ap.add_argument("--ks", default="5,10,20", help="comma-separated cutoffs")
    ap.add_argument("--no-trakt", action="store_true",
                    help="ignore trakt/history/movies (Tautulli-only, for A/B comparison)")
    ap.add_argument("--no-tv", action="store_true",
                    help="ignore TV episode history in the next-watch cross-medium affinity")
    ap.add_argument("--forward", action="store_true",
                    help="FORWARD-validate the Plex watchlist (snapshots → watched-in-window → lift)")
    ap.add_argument("--window-days", type=int, default=30,
                    help="forward-validation window: days after a snapshot to count a watch (default 30)")
    args = ap.parse_args(argv)

    if args.forward:
        return _forward_synthetic() if args.synthetic else _run_forward(args)
    if args.synthetic:
        return _synthetic()
    return _run_real(args)


if __name__ == "__main__":
    raise SystemExit(main())
