"""
likelihood/survival.py — discrete-time hazard of the household's next watch.
(ML Stage 5a — importable API + report tool only; NO runtime wiring)
================================================================================
From the Tautulli history (n≈931 events household-wide) estimate, per entity:

    P(next watch / rewatch within `horizon` days | already `gap` days since last)

Method — classical discrete-time survival, sized for tiny n:
  * events per entity (movie tmdb / series) → inter-event GAPS in days; the time
    from the LAST event to `now` is a right-censored observation.
  * hazard curve over day-buckets (default 7-day buckets to `max_days`=364):
        h[b] = (# complete gaps ending in bucket b) / (# gaps at risk in b)
    where "at risk" = gaps (complete or censored) that reached the bucket start.
  * hierarchical pooling (empirical Bayes, weight n/(n+k), k=5, n = EVENT count
    at that level per the build brief):
        title (>=3 events)  →  group pool (franchise/collection or genre — the
        caller supplies the entity→group mapping; history alone carries none)
        →  household pool (all gaps).
    A sparse title borrows almost everything from its group/household; a
    much-rewatched title mostly speaks for itself.
  * residual probability over a window after surviving `gap` days:
        P = 1 - Π_b (1 - h[b] * frac_b)   over the buckets covered by
        (gap, gap+horizon], partial buckets scaled linearly; beyond the curve
        the last bucket's hazard extends flat.

PURE — no I/O, no clock (callers inject `now`). The CLI
(scripts/support/tools/ml_survival_report.py) does the cache reading and prints
household/per-title curves. Nothing in the run imports this module.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

DEFAULT_K = 5.0
DEFAULT_BUCKET_DAYS = 7
DEFAULT_MAX_DAYS = 364


# ── event plumbing (pure) ─────────────────────────────────────────────────────

def _to_dt(v) -> "datetime | None":
    """Coerce an event timestamp (datetime / epoch seconds / ISO) to aware UTC."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def entity_gaps(event_times, now) -> "tuple[list[float], float | None, int]":
    """(complete_gaps_days, censored_gap_days, n_events) for one entity.

    complete gaps = days between consecutive events; the censored gap = days
    from the last event to `now` (None when the entity has no events)."""
    times = sorted(t for t in (_to_dt(v) for v in (event_times or [])) if t is not None)
    n = len(times)
    if n == 0:
        return [], None, 0
    gaps = [max(0.0, (b - a).total_seconds() / 86400.0) for a, b in zip(times, times[1:])]
    now_dt = _to_dt(now) or datetime.now(tz=timezone.utc)
    censored = max(0.0, (now_dt - times[-1]).total_seconds() / 86400.0)
    return gaps, censored, n


# ── hazard estimation (pure) ──────────────────────────────────────────────────

def hazard_curve(complete_gaps: list, censored_gaps: list,
                 bucket_days: int = DEFAULT_BUCKET_DAYS,
                 max_days: int = DEFAULT_MAX_DAYS) -> "tuple[np.ndarray, np.ndarray]":
    """Empirical discrete-time hazard over day-buckets.

    Returns (hazard, at_risk) arrays of length ceil(max_days / bucket_days).
    hazard[b] = events ending in bucket b / gaps at risk at bucket b's start;
    0 where nothing was at risk."""
    n_buckets = max(1, math.ceil(max_days / bucket_days))
    events = np.zeros(n_buckets)
    at_risk = np.zeros(n_buckets)
    for g in complete_gaps:
        b_end = min(n_buckets - 1, int(g // bucket_days))
        events[b_end] += 1
        at_risk[:b_end + 1] += 1
    for c in censored_gaps:
        if c is None:
            continue
        b_cens = min(n_buckets - 1, int(c // bucket_days))
        at_risk[:b_cens + 1] += 1
    hazard = np.divide(events, at_risk, out=np.zeros(n_buckets), where=at_risk > 0)
    return hazard, at_risk


def blend_hazards(child: "np.ndarray | None", child_n: float,
                  parent: np.ndarray, k: float = DEFAULT_K) -> np.ndarray:
    """Empirical-Bayes blend: w*child + (1-w)*parent with w = n/(n+k)."""
    if child is None or child_n <= 0:
        return parent.copy()
    w = float(child_n) / (float(child_n) + float(k))
    return w * child + (1.0 - w) * parent


def residual_probability_from_hazard(hazard: np.ndarray, days_since_last: float,
                                     horizon: float,
                                     bucket_days: int = DEFAULT_BUCKET_DAYS) -> float:
    """P(event in (gap, gap+horizon]) given survival to `gap`, from a bucket
    hazard curve. Partial bucket coverage scales the bucket hazard linearly;
    past the end of the curve the LAST bucket's hazard extends flat."""
    if horizon <= 0 or len(hazard) == 0:
        return 0.0
    start = max(0.0, float(days_since_last))
    end = start + float(horizon)
    surv = 1.0
    b = int(start // bucket_days)
    pos = start
    last = len(hazard) - 1
    while pos < end:
        bucket_end = (b + 1) * bucket_days
        seg = min(end, bucket_end) - pos
        h = float(hazard[min(b, last)])
        frac = seg / bucket_days
        surv *= max(0.0, 1.0 - h * frac)
        pos = min(end, bucket_end)
        b += 1
    return max(0.0, min(1.0, 1.0 - surv))


# ── the fitted model (pure container) ─────────────────────────────────────────

class SurvivalModel:
    """Hierarchically-pooled hazard curves: household → group → title.

    Built by :func:`fit_survival_model`. All curves share bucket_days/max_days.
    ``curve_for(entity)`` returns the fully-blended per-title curve."""

    def __init__(self, bucket_days: int, max_days: int, k: float,
                 household: np.ndarray, household_events: int,
                 titles: dict, groups: dict, group_of: dict):
        self.bucket_days = bucket_days
        self.max_days = max_days
        self.k = k
        self.household = household
        self.household_events = household_events
        self.titles = titles          # entity -> (hazard, n_events)
        self.groups = groups          # group  -> (hazard, n_events)
        self.group_of = group_of      # entity -> group

    def curve_for(self, entity) -> np.ndarray:
        group = self.group_of.get(entity)
        g = self.groups.get(group)
        pooled = blend_hazards(g[0] if g else None, g[1] if g else 0.0,
                               self.household, self.k)
        t = self.titles.get(entity)
        return blend_hazards(t[0] if t else None, t[1] if t else 0.0, pooled, self.k)

    def residual_watch_probability(self, entity, days_since_last: float,
                                   horizon: float) -> float:
        return residual_probability_from_hazard(
            self.curve_for(entity), days_since_last, horizon, self.bucket_days)


def fit_survival_model(events_by_entity: dict, *, now=None,
                       groups_by_entity: "dict | None" = None,
                       k: float = DEFAULT_K,
                       bucket_days: int = DEFAULT_BUCKET_DAYS,
                       max_days: int = DEFAULT_MAX_DAYS,
                       min_title_events: int = 3) -> SurvivalModel:
    """Fit the pooled model from ``{entity: [event timestamps]}``.

    ``groups_by_entity`` maps entity → franchise/collection/genre pool name
    (optional — without it the hierarchy is title → household). Titles with
    fewer than ``min_title_events`` events contribute their gaps to the pools
    but get NO per-title curve (they ride on the pool)."""
    groups_by_entity = groups_by_entity or {}
    all_complete: list = []
    all_censored: list = []
    per_title: dict = {}
    group_samples: dict = {}
    for entity, events in (events_by_entity or {}).items():
        gaps, censored, n = entity_gaps(events, now)
        if n == 0:
            continue
        all_complete.extend(gaps)
        if censored is not None:
            all_censored.append(censored)
        grp = groups_by_entity.get(entity)
        if grp is not None:
            gs = group_samples.setdefault(grp, {"gaps": [], "cens": [], "n": 0})
            gs["gaps"].extend(gaps)
            gs["cens"].append(censored)
            gs["n"] += n
        if n >= min_title_events:
            hz, _ = hazard_curve(gaps, [censored], bucket_days, max_days)
            per_title[entity] = (hz, float(n))
    household_hz, _ = hazard_curve(all_complete, all_censored, bucket_days, max_days)
    groups: dict = {}
    for grp, gs in group_samples.items():
        hz, _ = hazard_curve(gs["gaps"], gs["cens"], bucket_days, max_days)
        groups[grp] = (hz, float(gs["n"]))
    return SurvivalModel(bucket_days, max_days, k, household_hz,
                         sum(len(v or []) for v in (events_by_entity or {}).values()),
                         per_title, groups, dict(groups_by_entity))


# ── brief-specified pure convenience function ─────────────────────────────────

def residual_watch_probability(entity_history, days_since_last: float,
                               horizon: float, *,
                               pooled_hazard: "np.ndarray | None" = None,
                               now=None, k: float = DEFAULT_K,
                               bucket_days: int = DEFAULT_BUCKET_DAYS,
                               max_days: int = DEFAULT_MAX_DAYS) -> float:
    """P(watch within `horizon` days | `days_since_last` days since the last
    watch), from ONE entity's event history blended with an optional pooled
    (household/group) hazard curve.

    ``entity_history`` = iterable of event timestamps (datetime / epoch seconds
    / ISO strings). With no pooled curve and <3 events the estimate is the
    title's own (possibly empty → 0.0) curve — callers that want the pooled
    behaviour should pass ``pooled_hazard`` (e.g. ``model.household``) or use
    :class:`SurvivalModel` directly. Pure — inject ``now`` for determinism."""
    gaps, censored, n = entity_gaps(entity_history, now)
    title_hz = None
    if n >= 1 and gaps:
        title_hz, _ = hazard_curve(gaps, [censored], bucket_days, max_days)
    if pooled_hazard is not None:
        child_n = float(n) if gaps else 0.0    # no complete gap → all weight to the pool
        hz = blend_hazards(title_hz, child_n, pooled_hazard, k)
    elif title_hz is not None:
        hz = title_hz
    else:
        return 0.0
    return residual_probability_from_hazard(hz, days_since_last, horizon, bucket_days)
