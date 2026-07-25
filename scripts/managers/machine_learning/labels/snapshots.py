"""
labels/snapshots.py — append-only watchability snapshot pipeline (pure logging).
================================================================================
ML Stage 1. After each score-map build (``RadarrSpacePressureManager.refresh_scores``
/ ``SonarrEpisodeFilesCacheManager.refresh_scores``) the service appends one row
per scored entity (movie / series) to a month-partitioned Parquet under

    <global cache base dir>/ml/snapshots/{service}/{YYYY-MM}.parquet

so the offline harnesses (``ml_forward_validation`` / ``ml_weight_refit`` /
``ml_train_challenger``) can join "what did the scorer believe on day D" against
"what did the household actually watch after day D".

Row schema (one row per entity per snapshot):
    snapshot_ts     UTC ISO timestamp of the snapshot
    snapshot_date   YYYY-MM-DD (dedupe key — one row per entity per day survives)
    service         "radarr" | "sonarr"
    instance        e.g. "standard"
    entity_id       movies: str(tmdb_id); shows: "<series_id>:<tvdb_id>"
    tmdb_id / series_id / tvdb_id / title
    watchability_score      the persisted 0-100 score
    sig_<GROUP>             each signal-group value from the breakdown dict
                            (e.g. sig_A1_keep_policy) — "_total_*" meta keys are
                            NOT flattened (the score column already carries them)
    size_bytes / resolution
    watched_before  bool — any household watch recorded at snapshot time
    planned_action  the decision-ledger stamp on the row at snapshot time
                    (i.e. the ledger state as persisted — typically last run's plan)
    in_up_next      movies only: tmdb present in the Up Next plan tmdb sets
                    (plex/playlists/protected_movie_tmdbs/{movie,combined}) at
                    snapshot time. Always False for shows (no tmdb plan set).
    challenger_p    Stage-4 shadow GBT P(watch) — None unless
                    scoring.ml_challenger.enabled AND a trained model exists.

Config gate: ``ml.snapshots.enabled`` (DEFAULT TRUE — this is pure logging).
Every entry point is fully wrapped: a snapshot failure can NEVER affect the run,
any score, or any *arr write. Dedupe on (snapshot_date, instance, entity_id)
keeping last, so repeated runs in a day collapse to the day's final state.

PURE at the row-builder level (plain DataFrame in, list[dict] out); the writer
does only local Parquet I/O under the cache base dir. No HTTP, no manager graph.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SNAPSHOT_SUBDIR = ("ml", "snapshots")
_DEDUP_KEYS = ["snapshot_date", "instance", "entity_id"]


# ── small pure helpers ────────────────────────────────────────────────────────

def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def month_partition(snapshot_ts: str) -> str:
    """'2026-07-25T12:00:00+00:00' -> '2026-07' (the Parquet partition name)."""
    return str(snapshot_ts)[:7]


def snapshot_dir(base_dir, service: str) -> Path:
    """<base_dir>/ml/snapshots/<service>"""
    return Path(base_dir).joinpath(*SNAPSHOT_SUBDIR, service)


def flatten_breakdown(breakdown) -> dict:
    """Flatten a scorer breakdown dict to ``{"sig_<key>": float}`` columns.

    Accepts the dict itself or the JSON string persisted in the
    ``watchability_breakdown`` Parquet column. Meta keys starting with "_"
    (``_total_raw`` / ``_total_final``) are skipped — the score column already
    carries the total. Non-numeric values are skipped (defensive)."""
    if breakdown is None or (isinstance(breakdown, float) and pd.isna(breakdown)):
        return {}
    if isinstance(breakdown, str):
        try:
            breakdown = json.loads(breakdown)
        except (ValueError, TypeError):
            return {}
    if not isinstance(breakdown, dict):
        return {}
    out: dict = {}
    for k, v in breakdown.items():
        if str(k).startswith("_"):
            continue
        try:
            out[f"sig_{k}"] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _safe_int(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_str(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _truthy(v) -> bool:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    return bool(v)


# ── row builders (pure) ───────────────────────────────────────────────────────

def build_movie_snapshot_rows(df: pd.DataFrame, instance: str,
                              snapshot_ts: "str | None" = None,
                              up_next_tmdbs: "set | None" = None) -> list[dict]:
    """One snapshot row per movie_files row that carries a watchability_score.

    Reads the already-persisted ``watchability_score`` + ``watchability_breakdown``
    (JSON string) columns — i.e. the exact with_breakdown output of
    ``_build_score_map`` — so the snapshot can never diverge from what the run
    scored. Pure: no I/O, no clock beyond the injected ``snapshot_ts``."""
    ts = snapshot_ts or utc_now_iso()
    up_next = up_next_tmdbs or set()
    rows: list[dict] = []
    if df is None or df.empty or "watchability_score" not in df.columns:
        return rows
    for rec in df.to_dict("records"):
        score = _safe_float(rec.get("watchability_score"))
        tmdb = _safe_int(rec.get("tmdb_id"))
        if score is None or tmdb is None:
            continue
        watched_before = _truthy(rec.get("is_watched")) or (_safe_int(rec.get("watch_count")) or 0) > 0
        row = {
            "snapshot_ts": ts,
            "snapshot_date": ts[:10],
            "service": "radarr",
            "instance": str(instance),
            "entity_id": str(tmdb),
            "tmdb_id": tmdb,
            "series_id": None,
            "tvdb_id": None,
            "title": _safe_str(rec.get("title")),
            "watchability_score": score,
            "size_bytes": _safe_int(rec.get("size_bytes")),
            "resolution": _safe_int(rec.get("resolution")),
            "watched_before": bool(watched_before),
            "planned_action": _safe_str(rec.get("planned_action")),
            "in_up_next": tmdb in up_next,
            "challenger_p": None,
        }
        row.update(flatten_breakdown(rec.get("watchability_breakdown")))
        rows.append(row)
    return rows


def build_show_snapshot_rows(df: pd.DataFrame, instance: str,
                             snapshot_ts: "str | None" = None,
                             tvdb_by_series: "dict | None" = None) -> list[dict]:
    """One snapshot row per SERIES (episode rows aggregated up).

    score/breakdown are broadcast per-series in episode_files, so the first
    non-null value per group is the series value; size_bytes is the series
    total, resolution the modal value, watched_before any watched episode,
    planned_action the most common non-null episode stamp. Pure."""
    ts = snapshot_ts or utc_now_iso()
    tvdb_by_series = tvdb_by_series or {}
    rows: list[dict] = []
    if df is None or df.empty or "watchability_score" not in df.columns \
            or "series_id" not in df.columns:
        return rows
    for series_id, grp in df.groupby("series_id", sort=False):
        sid = _safe_int(series_id)
        if sid is None:
            continue
        score = None
        for v in grp["watchability_score"]:
            score = _safe_float(v)
            if score is not None:
                break
        if score is None:
            continue
        breakdown_raw = None
        if "watchability_breakdown" in grp.columns:
            for v in grp["watchability_breakdown"]:
                if isinstance(v, str) and v:
                    breakdown_raw = v
                    break
        size_bytes = _safe_int(pd.to_numeric(grp.get("size_bytes"), errors="coerce").sum()) \
            if "size_bytes" in grp.columns else None
        resolution = None
        if "resolution" in grp.columns:
            res_counts = Counter(
                r for r in (_safe_int(v) for v in grp["resolution"]) if r is not None)
            if res_counts:
                resolution = res_counts.most_common(1)[0][0]
        watched_before = False
        if "is_watched" in grp.columns:
            watched_before = bool((grp["is_watched"] == True).any())     # noqa: E712
        if not watched_before and "watch_count" in grp.columns:
            watched_before = bool(
                (pd.to_numeric(grp["watch_count"], errors="coerce").fillna(0) > 0).any())
        planned_action = None
        if "planned_action" in grp.columns:
            pa_counts = Counter(
                p for p in (_safe_str(v) for v in grp["planned_action"]) if p)
            if pa_counts:
                planned_action = pa_counts.most_common(1)[0][0]
        tvdb = _safe_int(tvdb_by_series.get(sid))
        title = None
        if "series_title" in grp.columns:
            for v in grp["series_title"]:
                title = _safe_str(v)
                if title:
                    break
        row = {
            "snapshot_ts": ts,
            "snapshot_date": ts[:10],
            "service": "sonarr",
            "instance": str(instance),
            "entity_id": f"{sid}:{tvdb if tvdb is not None else ''}",
            "tmdb_id": None,
            "series_id": sid,
            "tvdb_id": tvdb,
            "title": title,
            "watchability_score": score,
            "size_bytes": size_bytes,
            "resolution": resolution,
            "watched_before": watched_before,
            "planned_action": planned_action,
            "in_up_next": False,   # no tmdb-keyed Up Next plan set for shows (v1)
            "challenger_p": None,
        }
        row.update(flatten_breakdown(breakdown_raw))
        rows.append(row)
    return rows


# ── writer ────────────────────────────────────────────────────────────────────

def append_snapshot(base_dir, service: str, instance: str, rows: list) -> int:
    """Append *rows* to ``<base_dir>/ml/snapshots/<service>/<YYYY-MM>.parquet``.

    Read-modify-write append (fine at this scale: ~2k movies + ~1k series per
    day), deduped on (snapshot_date, instance, entity_id) keeping LAST — so the
    final run of a day wins. Atomic write (tmp + os.replace) so a crash can
    never leave a truncated partition. Returns the number of NEW rows appended
    (post-dedupe delta)."""
    if not rows:
        return 0
    target_dir = snapshot_dir(base_dir, service)
    target_dir.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(rows)
    written = 0
    for month, month_df in new_df.groupby(new_df["snapshot_ts"].astype(str).str[:7]):
        path = target_dir / f"{month}.parquet"
        if path.exists():
            try:
                old = pd.read_parquet(path)
            except Exception:
                # Unreadable partition → preserve it out of the way, start fresh.
                try:
                    os.replace(path, path.with_suffix(".parquet.corrupt"))
                except OSError:
                    pass
                old = pd.DataFrame()
        else:
            old = pd.DataFrame()
        before = len(old)
        if old.empty:
            merged = month_df
        else:
            import warnings
            with warnings.catch_warnings():
                # concat of frames with all-NA columns (e.g. challenger_p before a
                # model exists) emits a dtype FutureWarning — benign here.
                warnings.simplefilter("ignore", FutureWarning)
                merged = pd.concat([old, month_df], ignore_index=True)
        merged = merged.drop_duplicates(subset=_DEDUP_KEYS, keep="last").reset_index(drop=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        written += max(0, len(merged) - before)
    return written


def load_snapshots(base_dir, services=("radarr", "sonarr"),
                   instance: "str | None" = None) -> pd.DataFrame:
    """Read every month partition for *services* into one DataFrame (offline
    tools' entry point). Missing dirs/unreadable partitions are skipped.
    Optionally filter to one instance."""
    frames: list = []
    for service in services:
        d = snapshot_dir(base_dir, service)
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.parquet")):
            try:
                frames.append(pd.read_parquet(path))
            except Exception:
                continue
    if not frames:
        return pd.DataFrame()
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(frames, ignore_index=True)
    if instance and "instance" in df.columns:
        df = df[df["instance"] == instance].reset_index(drop=True)
    return df


class SnapshotWriter:
    """Thin OO wrapper over :func:`append_snapshot` for callers that prefer a
    bound base dir (e.g. tests injecting a tmp dir)."""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)

    def append(self, service: str, instance: str, rows: list) -> int:
        return append_snapshot(self.base_dir, service, instance, rows)


# ── service-facing gated hooks (never raise) ──────────────────────────────────

def _snapshots_enabled(config) -> bool:
    """config.ml.snapshots.enabled — DEFAULT TRUE (pure logging)."""
    ml_cfg = ((config or {}).get("ml", {}) or {})
    snap_cfg = (ml_cfg.get("snapshots", {}) or {}) if isinstance(ml_cfg, dict) else {}
    return bool(snap_cfg.get("enabled", True)) if isinstance(snap_cfg, dict) else True


def _resolve_base_dir(global_cache):
    """The global cache base dir — from the live manager when available, else the
    same default CacheKeyBuilder resolves (scripts/support/cache)."""
    root = getattr(global_cache, "cache_root", None)
    if root:
        return Path(root)
    from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
    return CacheKeyBuilder().base_dir


def _load_up_next_tmdbs(global_cache) -> set:
    """Union of the Up Next plan tmdb sets (movie + combined). Empty on any miss."""
    out: set = set()
    if not global_cache:
        return out
    for key in ("plex/playlists/protected_movie_tmdbs/movie",
                "plex/playlists/protected_movie_tmdbs/combined"):
        try:
            blob = global_cache.get(key) or {}
            for t in (blob.get("tmdbs") or []):
                ti = _safe_int(t)
                if ti is not None:
                    out.add(ti)
        except Exception:
            continue
    return out


def _attach_challenger(config, logger, base_dir, service: str, rows: list) -> list:
    """Stage-4 shadow hook: fill ``challenger_p`` + log divergence. Config-gated
    DEFAULT OFF inside gbt_shadow; ImportError/any failure → rows unchanged."""
    try:
        from scripts.managers.machine_learning.challenger.gbt_shadow import (
            attach_challenger_p,
        )
        return attach_challenger_p(config, logger, base_dir, service, rows)
    except Exception:
        return rows


def _log(logger, level: str, msg: str) -> None:
    try:
        getattr(logger, f"log_{level}")(msg)
    except Exception:
        pass


def maybe_snapshot_movies(config, global_cache, logger, instance: str,
                          df: pd.DataFrame) -> int:
    """Config-gated movie snapshot append. NEVER raises; returns rows appended."""
    try:
        if not _snapshots_enabled(config):
            return 0
        base = _resolve_base_dir(global_cache)
        rows = build_movie_snapshot_rows(
            df, instance, up_next_tmdbs=_load_up_next_tmdbs(global_cache))
        rows = _attach_challenger(config, logger, base, "radarr", rows)
        n = append_snapshot(base, "radarr", instance, rows)
        _log(logger, "info",
             f"[MLSnapshot] radarr/{instance}: appended {n} of {len(rows)} snapshot row(s).")
        return n
    except Exception as e:
        _log(logger, "debug", f"[MLSnapshot] radarr/{instance} snapshot skipped: {e}")
        return 0


def maybe_snapshot_shows(config, global_cache, logger, instance: str,
                         df: pd.DataFrame, tvdb_by_series: "dict | None" = None) -> int:
    """Config-gated show snapshot append. NEVER raises; returns rows appended."""
    try:
        if not _snapshots_enabled(config):
            return 0
        base = _resolve_base_dir(global_cache)
        rows = build_show_snapshot_rows(df, instance, tvdb_by_series=tvdb_by_series)
        rows = _attach_challenger(config, logger, base, "sonarr", rows)
        n = append_snapshot(base, "sonarr", instance, rows)
        _log(logger, "info",
             f"[MLSnapshot] sonarr/{instance}: appended {n} of {len(rows)} snapshot row(s).")
        return n
    except Exception as e:
        _log(logger, "debug", f"[MLSnapshot] sonarr/{instance} snapshot skipped: {e}")
        return 0
