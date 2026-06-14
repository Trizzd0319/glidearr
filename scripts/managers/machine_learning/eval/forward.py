"""eval/forward.py — forward (online) validation for explicit-intent signals.
==============================================================================
The retrospective eval (metrics/split) CANNOT measure the watchlist: a watchlist is
forward-looking, watched items typically *leave* it, and there are no historical
snapshots — so current-watchlist ∩ past-held-out ≈ 0 (DESIGN §8 blind spot).

Forward validation instead measures the watchlist the only honest way:
  1. snapshot the watchlist union at time T  (the Plex service already does this →
     ``plex/watchlist/snapshot/{ts}``, retention-bounded);
  2. LATER, ask — of the items on the list at T, how many were watched within window W?
  3. and crucially, is that **hit-rate a LIFT over the base watch-rate** of comparable
     non-watchlisted owned items? Without the lift the hit-rate is meaningless — a
     household that watches everything "hits" trivially.

PURE: stdlib only (brain-purity safe). Timestamps are plain numbers (epoch seconds);
the driver parses snapshot ts strings and reads caches.
"""
from __future__ import annotations

from typing import Hashable, Mapping, Sequence

Item = Hashable


def watched_in_window(
    watch_events: Sequence[Mapping], t0: float, t1: float, *,
    item_key: str = "item", time_key: str = "ts", completion_key: str = "completion",
    watched_threshold: float = 0.9,
) -> set:
    """Set of item ids COMPLETED in the half-open window (t0, t1]."""
    out: set = set()
    for e in watch_events:
        ts = e.get(time_key)
        if ts is None or not (t0 < ts <= t1):
            continue
        if float(e.get(completion_key) or 0.0) >= watched_threshold:
            out.add(e[item_key])
    return out


def evaluate_snapshot(predicted, owned_universe, watched) -> dict:
    """Forward metrics for ONE snapshot.

    ``predicted``      — item ids on the watchlist at T (the prediction).
    ``owned_universe`` — all owned item ids (the things that COULD be watched).
    ``watched``        — item ids completed in the window (t0, t1] after T.

    Returns hit_rate (over predicted), base_rate (over owned∖predicted — the
    counterfactual), and **lift = hit_rate / base_rate**. lift > 1 ⇒ the watchlist
    predicts watching better than chance; ≈1 ⇒ no signal."""
    pred = set(predicted)
    owned = set(owned_universe)
    won = set(watched)
    if not pred:
        return {"n_predicted": 0, "n_hits": 0, "hit_rate": None,
                "n_base": None, "base_rate": None, "lift": None}
    hits = pred & won
    hit_rate = len(hits) / len(pred)
    base_pool = owned - pred
    base_rate = (len(base_pool & won) / len(base_pool)) if base_pool else None
    lift = (hit_rate / base_rate) if (base_rate and base_rate > 0) else None
    return {"n_predicted": len(pred), "n_hits": len(hits), "hit_rate": hit_rate,
            "n_base": len(base_pool), "base_rate": base_rate, "lift": lift}


def aggregate_forward(per_snapshot: Sequence[dict]) -> dict:
    """Mean hit_rate / base_rate / lift across snapshots that produced a prediction.
    (A snapshot with no predictions or no base pool contributes nothing.)"""
    rows = [r for r in per_snapshot if r and r.get("hit_rate") is not None]
    if not rows:
        return {"n_snapshots": 0}

    def _mean(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    return {
        "n_snapshots":     len(rows),
        "hit_rate":        _mean("hit_rate"),
        "base_rate":       _mean("base_rate"),
        "lift":            _mean("lift"),
        "total_predicted": sum(r["n_predicted"] for r in rows),
        "total_hits":      sum(r["n_hits"] for r in rows),
    }
