"""backtest.py — would this habit model have been right, on the history we hold?

The thresholds in `habits.py` (``DEFAULT_DAY_THRESHOLD = 0.28``,
``DEFAULT_MIN_PLAYS = 3``) were set on SYNTHETIC data, which is tidier than any
real household. This replays the actual watch history day by day and reports
what those numbers would have bought, so they can be set from evidence.

NO LOOKAHEAD, and this is the whole correctness of the thing. For a target day
D the profile is built ONLY from plays strictly before D's local midnight. A
backtest that lets the target day into the training window scores itself on the
answer sheet: it reports a beautiful hit rate and means nothing. Every split is
walk-forward.

A DAY NOBODY WATCHED IS NOT A MISS. Same discipline as `engagement.py` and
`outcomes.py`: with no plays at all there was nothing to be right or wrong
about, so the day is DORMANT and excluded from the rate rather than counted
against the model. Including them would make a quiet fortnight look like a
broken recommender.

WARM-UP. The first ``warmup_days`` are training-only. Predicting Tuesday from
one prior Tuesday is not a test of the model, it is a test of luck.

ERROR-SAFE BY CONSTRUCTION. Every entry point tolerates missing, empty or
malformed input and returns a result carrying a ``reason`` rather than raising.
This is a diagnostic run by an operator against whatever happens to be cached;
it must never be the thing that breaks a run.

PURE. No I/O, no manager, no config. Rows in, numbers out.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.managers.machine_learning.playlists import habits as H

_DAY = 86400.0

#: Days of history reserved for training before the first prediction is scored.
DEFAULT_WARMUP_DAYS = 45

#: Thresholds swept by default. 1/7 = 0.143 is the uniform floor - a show watched
#: on every day scores that on each - so anything at or below it would surface
#: shows with no weekday preference at all.
DEFAULT_THRESHOLDS = (0.16, 0.20, 0.24, 0.28, 0.34, 0.40, 0.50)

#: min_plays values swept by default.
DEFAULT_MIN_PLAYS = (2, 3, 4, 5)


def _local_day(ts, tz_offset_hours: float):
    """The local calendar date of a unix ts, or None if unusable."""
    try:
        return datetime.fromtimestamp(float(ts) + tz_offset_hours * 3600.0,
                                      tz=timezone.utc).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _day_start_ts(day, tz_offset_hours: float) -> float:
    """Unix ts of local midnight opening ``day`` - the training cutoff."""
    naive = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return naive.timestamp() - tz_offset_hours * 3600.0


def _usable(rows):
    return [r for r in (rows or ())
            if isinstance(r, dict) and r.get("date") is not None]


def backtest(rows, *, key=None, tz_offset_hours: float = 0.0,
             warmup_days: int = DEFAULT_WARMUP_DAYS,
             thresholds=DEFAULT_THRESHOLDS,
             min_plays_values=DEFAULT_MIN_PLAYS,
             limit: int = 8, halflife_days: float = H.DEFAULT_HALFLIFE_DAYS) -> dict:
    """Walk-forward evaluation over every eligible day in ``rows``.

    Returns ``{"reason", "days_total", "days_scored", "days_dormant", "grid"}``
    where ``grid`` is ``{(threshold, min_plays): metrics}``.

    Metrics per cell:
      ``hit_rate``   share of scored days where a predicted group was watched
      ``precision``  of groups predicted, the share that were watched
      ``recall``     of groups actually watched, the share that were predicted
      ``silent``     days the model predicted NOTHING (not a miss, but not use)
    """
    rows = _usable(rows)
    if not rows:
        return {"reason": "no usable history rows", "days_total": 0,
                "days_scored": 0, "days_dormant": 0, "grid": {}}

    key = key or (lambda r: H.derive_key(r))

    # group plays by local day, and remember each day's actually-watched groups
    by_day: dict = {}
    for r in rows:
        d = _local_day(r["date"], tz_offset_hours)
        if d is None:
            continue
        g = key(r)
        if g in (None, ""):
            continue
        by_day.setdefault(d, set()).add(str(g))
    if not by_day:
        return {"reason": "no rows resolved to a group key", "days_total": 0,
                "days_scored": 0, "days_dormant": 0, "grid": {}}

    first, last = min(by_day), max(by_day)
    start = first + timedelta(days=int(warmup_days))
    if start > last:
        return {"reason": (f"history spans {(last - first).days} days, shorter than the "
                           f"{warmup_days}-day warm-up - nothing left to score"),
                "days_total": (last - first).days, "days_scored": 0,
                "days_dormant": 0, "grid": {}}

    grid = {(t, m): {"hits": 0, "scored": 0, "silent": 0,
                     "pred": 0, "pred_hit": 0, "actual": 0, "actual_hit": 0}
            for t in thresholds for m in min_plays_values}

    days_scored = days_dormant = days_total = 0
    day = start
    while day <= last:
        days_total += 1
        actual = by_day.get(day)
        if not actual:
            days_dormant += 1                      # nobody watched: not a miss
            day += timedelta(days=1)
            continue
        days_scored += 1
        cutoff = _day_start_ts(day, tz_offset_hours)
        train = [r for r in rows if float(r["date"]) < cutoff]
        weekday = day.weekday()
        for m in min_plays_values:
            prof = H.weekday_profile(train, now_ts=cutoff, key=key,
                                     tz_offset_hours=tz_offset_hours,
                                     halflife_days=halflife_days, min_plays=m)
            for t in thresholds:
                pred = set(H.shows_for_day(prof, weekday, limit=limit, threshold=t))
                cell = grid[(t, m)]
                cell["scored"] += 1
                if not pred:
                    cell["silent"] += 1
                    continue
                overlap = pred & actual
                cell["pred"] += len(pred)
                cell["pred_hit"] += len(overlap)
                cell["actual"] += len(actual)
                cell["actual_hit"] += len(overlap)
                if overlap:
                    cell["hits"] += 1
        day += timedelta(days=1)

    out = {}
    for (t, m), c in grid.items():
        scored = c["scored"] or 1
        out[(t, m)] = {
            "hit_rate": c["hits"] / scored,
            "precision": (c["pred_hit"] / c["pred"]) if c["pred"] else None,
            "recall": (c["actual_hit"] / c["actual"]) if c["actual"] else None,
            "silent_rate": c["silent"] / scored,
            "hits": c["hits"], "scored": c["scored"], "silent": c["silent"],
        }
    return {"reason": "", "days_total": days_total, "days_scored": days_scored,
            "days_dormant": days_dormant, "grid": out}


def best_cell(result: dict, *, min_coverage: float = 0.5):
    """The (threshold, min_plays) with the best hit rate among cells that
    actually SPEAK often enough to be useful.

    ``min_coverage`` guards the degenerate optimum: a very high threshold
    predicts almost nothing, and the handful of days it does speak are its most
    confident - so it scores a superb hit rate while being silent most of the
    time. A recommender that is right 90% of the time and absent six days a week
    is worse than one that is right 60% of the time and always there.
    """
    best = None
    for cell, m in (result.get("grid") or {}).items():
        if (1.0 - m["silent_rate"]) < min_coverage:
            continue
        score = (m["hit_rate"], -(cell[0]))
        if best is None or score > best[1]:
            best = (cell, score, m)
    return None if best is None else (best[0], best[2])
