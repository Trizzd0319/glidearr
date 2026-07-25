"""
labels/labeling.py — join snapshots against Tautulli ground truth (pure).
================================================================================
ML Stage 1b. Given the snapshot rows (labels/snapshots.py) and the household
watch history, add per-row outcome labels:

    watched_within_h        the entity was watched within ``horizon_days`` AFTER
                            the snapshot timestamp
    label_mature            snapshot_ts + horizon has fully elapsed (an immature
                            row cannot yet be a trustworthy negative)
    recommended_not_watched the entity was in the Up Next plan at snapshot time
                            (snapshot column ``in_up_next``) and was NOT watched
                            within the horizon — the implicit-negative signal

CACHE SHAPES USED (inspected on disk 2026-07-25 — document per the build brief):

  tautulli/history/all.json — LIST of event dicts (n=931). Fields used:
      "date"                unix epoch SECONDS of the play event (the only
                            timestamp field present; there is no "started")
      "media_type"          "movie" | "episode"
      "rating_key"          Plex rating key of the item played
      "grandparent_title"   series title (episodes only)
      "percent_complete"    integer 0-100 for that session
    Fields present but NOT used: user/user_id (household-level labels),
    platform, transcode_decision, location, media_index, row_id, reference_id,
    grandparent_rating_key (no rating_key->series id map exists on disk).

  tautulli/group/household/tmdb_completions.json — {tmdb_str: {"pct","threshold"}}.
    NO timestamps — so it can NEVER time-scope a label on its own. Used only to
    RELAX the per-event completion cut for movies the household has completed
    overall (grouped sessions split percent_complete across events).

  plex/movies/owned_inventory.json — {tmdb_str: {"rating_key","title","year",..}}.
    Inverted to rating_key -> tmdb: the exact join from a movie history event to
    the snapshot's tmdb_id (title/year matching is NOT used for movies — the
    history records carry no year).

MATCHING RULES (documented, deliberately simple at n≈931 events):
  * movies  — event.media_type=="movie", rating_key->tmdb == snapshot tmdb_id,
              event ts in (snapshot_ts, snapshot_ts+horizon], and
              percent_complete >= movie_pct_min (default 90 ≈ the completions
              threshold 0.9) OR (tmdb completed per tmdb_completions AND
              percent_complete >= relaxed_pct_min, default 50).
  * shows   — event.media_type=="episode", normalised grandparent_title ==
              normalised snapshot title, event ts in window, and
              percent_complete >= episode_pct_min (default 50 — continued
              engagement, not per-episode completion). Title matching is the
              only join available for shows (history has no series id/tvdb);
              normalisation lowercases and strips punctuation/whitespace.

PURE — takes DataFrames/dicts, returns a labeled copy. All I/O lives in the
``load_*`` helpers so CLIs can inject fakes.
"""
from __future__ import annotations

import bisect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(t) -> str:
    """Lowercase, strip punctuation/whitespace — the show join key."""
    if t is None:
        return ""
    return _NORM_RE.sub("", str(t).lower())


# ── loaders (local JSON only — no HTTP, no manager graph) ─────────────────────

def load_owned_movie_rating_keys(cache_base) -> dict:
    """rating_key(str) -> tmdb(int), inverted from plex/movies/owned_inventory."""
    path = Path(cache_base) / "plex" / "movies" / "owned_inventory.json"
    out: dict = {}
    try:
        inv = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(inv, dict):
        return out
    for tmdb_str, meta in inv.items():
        try:
            rk = str((meta or {}).get("rating_key"))
            if rk and rk != "None":
                out[rk] = int(tmdb_str)
        except (TypeError, ValueError):
            continue
    return out


def load_tmdb_completions(cache_base, group: str = "household") -> dict:
    """tmdb(int) -> True for titles completed per the group's completion cache."""
    path = Path(cache_base) / "tautulli" / "group" / group / "tmdb_completions.json"
    out: dict = {}
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(blob, dict):
        return out
    for tmdb_str, meta in blob.items():
        try:
            pct = float((meta or {}).get("pct", 0) or 0)
            thr = float((meta or {}).get("threshold", 0.9) or 0.9)
            out[int(tmdb_str)] = pct >= thr
        except (TypeError, ValueError):
            continue
    return out


def load_watch_events(cache_base) -> pd.DataFrame:
    """Tautulli history as a flat events DataFrame.

    Columns: ts (tz-aware UTC), media_type, rating_key (str), tmdb_id (movies,
    via owned_inventory rating_key join; <NA> when unresolvable),
    series_title_norm (episodes), percent_complete (float)."""
    path = Path(cache_base) / "tautulli" / "history" / "all.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        records = []
    if not isinstance(records, list):
        records = []
    rk_to_tmdb = load_owned_movie_rating_keys(cache_base)
    rows = []
    for r in records:
        if not isinstance(r, dict):
            continue
        try:
            ts = datetime.fromtimestamp(int(r.get("date")), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        rk = str(r.get("rating_key")) if r.get("rating_key") is not None else ""
        rows.append({
            "ts": ts,
            "media_type": str(r.get("media_type") or ""),
            "rating_key": rk,
            "tmdb_id": rk_to_tmdb.get(rk),
            "series_title_norm": normalize_title(r.get("grandparent_title")),
            "percent_complete": float(r.get("percent_complete") or 0),
        })
    df = pd.DataFrame(rows, columns=["ts", "media_type", "rating_key", "tmdb_id",
                                     "series_title_norm", "percent_complete"])
    return df


# ── labeling (pure) ───────────────────────────────────────────────────────────

def _event_index(history: pd.DataFrame, completions: dict,
                 movie_pct_min: float, relaxed_pct_min: float,
                 episode_pct_min: float) -> "tuple[dict, dict]":
    """Pre-sort qualifying event timestamps per entity for bisect lookups.

    Returns (movie_ts_by_tmdb, episode_ts_by_title_norm) — each value a sorted
    list of tz-aware datetimes."""
    movie_ts: dict = {}
    episode_ts: dict = {}
    if history is None or history.empty:
        return movie_ts, episode_ts
    for rec in history.to_dict("records"):
        ts = rec.get("ts")
        if ts is None:
            continue
        pct = float(rec.get("percent_complete") or 0)
        mt = rec.get("media_type")
        if mt == "movie":
            tmdb = rec.get("tmdb_id")
            if tmdb is None or (isinstance(tmdb, float) and pd.isna(tmdb)):
                continue
            tmdb = int(tmdb)
            cut = relaxed_pct_min if completions.get(tmdb) else movie_pct_min
            if pct >= cut:
                movie_ts.setdefault(tmdb, []).append(ts)
        elif mt == "episode":
            key = rec.get("series_title_norm") or ""
            if key and pct >= episode_pct_min:
                episode_ts.setdefault(key, []).append(ts)
    for d in (movie_ts, episode_ts):
        for k in d:
            d[k].sort()
    return movie_ts, episode_ts


def _any_in_window(sorted_ts: list, start, end) -> bool:
    """True when any event timestamp falls in (start, end]."""
    if not sorted_ts:
        return False
    i = bisect.bisect_right(sorted_ts, start)
    return i < len(sorted_ts) and sorted_ts[i] <= end


def build_labels(snapshots_df: pd.DataFrame, history, horizon_days: int = 14, *,
                 completions: "dict | None" = None,
                 movie_pct_min: float = 90.0,
                 relaxed_pct_min: float = 50.0,
                 episode_pct_min: float = 50.0,
                 now=None) -> pd.DataFrame:
    """Return a labeled COPY of *snapshots_df* (see module docstring for rules).

    ``history`` is the events DataFrame from :func:`load_watch_events`, or a
    cache base dir (str/Path) to load from. ``completions`` is the dict from
    :func:`load_tmdb_completions` (auto-loaded when a base dir was given).
    Adds: watched_within_h, label_mature, recommended_not_watched."""
    if snapshots_df is None or snapshots_df.empty:
        out = pd.DataFrame() if snapshots_df is None else snapshots_df.copy()
        for col in ("watched_within_h", "label_mature", "recommended_not_watched"):
            out[col] = pd.Series(dtype=bool)
        return out
    if isinstance(history, (str, Path)):
        base = history
        history = load_watch_events(base)
        if completions is None:
            completions = load_tmdb_completions(base)
    completions = completions or {}
    now = now or datetime.now(tz=timezone.utc)
    horizon = timedelta(days=float(horizon_days))

    movie_ts, episode_ts = _event_index(
        history, completions, movie_pct_min, relaxed_pct_min, episode_pct_min)

    out = snapshots_df.copy().reset_index(drop=True)
    watched: list = []
    mature: list = []
    for rec in out.to_dict("records"):
        try:
            snap_ts = pd.to_datetime(rec.get("snapshot_ts"), utc=True).to_pydatetime()
        except (ValueError, TypeError):
            watched.append(False)
            mature.append(False)
            continue
        end = snap_ts + horizon
        mature.append(end <= now)
        service = str(rec.get("service") or "")
        hit = False
        if service == "radarr":
            tmdb = rec.get("tmdb_id")
            if tmdb is not None and not (isinstance(tmdb, float) and pd.isna(tmdb)):
                hit = _any_in_window(movie_ts.get(int(tmdb), []), snap_ts, end)
        elif service == "sonarr":
            key = normalize_title(rec.get("title"))
            if key:
                hit = _any_in_window(episode_ts.get(key, []), snap_ts, end)
        watched.append(bool(hit))
    out["watched_within_h"] = pd.Series(watched, dtype=bool)
    out["label_mature"] = pd.Series(mature, dtype=bool)
    in_up_next = out["in_up_next"].fillna(False).astype(bool) \
        if "in_up_next" in out.columns else pd.Series(False, index=out.index)
    out["recommended_not_watched"] = in_up_next & ~out["watched_within_h"]
    return out
