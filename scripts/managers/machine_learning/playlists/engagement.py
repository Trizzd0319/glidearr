"""engagement.py — was a surfaced playlist item SKIPPED, or was the household just quiet?

THE DISTINCTION THIS EXISTS FOR. A show sitting unwatched at the top of someone's Up Next
means nothing on its own. If they watched nothing at all that fortnight, the show is
DORMANT — and the right response is to keep resurfacing it, because they never turned it
down. If they watched forty other things and walked past it every time, they have MOVED
ON, and continuing to pin it to the top is the system ignoring a decision the household
already made.

Elapsed time cannot tell those apart; only relative activity can. So a skip is counted
ONLY when the profile was demonstrably watching other things in the same window.

HOW A SKIP IS OBSERVED. The plan persisted for a profile last run is the list of things
that were PUT IN FRONT OF THEM. Each item carries its ``rating_key``; the profile's watched
identity set contains ratingKeys too, so "did they watch anything from this group" is a
direct set intersection — no timestamps, no per-episode bookkeeping.

Overall activity is the GROWTH of that watched set across the window. Storing only its size
keeps the artifact small and makes the comparison exact.

PURE — no I/O, no manager, no config. The caller loads the prior state, supplies the plan
and watched set, and persists the result.
"""
from __future__ import annotations

# EVERYTHING HERE IS MEASURED IN DAYS, NOT RUNS. This is designed to run hourly or daily
# in production, and a per-run counter would blow through its whole tolerance in an
# afternoon: three runs at hourly cadence is three HOURS. Worse, per-run activity would be
# 0-2 watches at that cadence, so the "was the household active" test would never fire and
# skips would accrue for the wrong reason. A skip can therefore accrue at most once per
# ``_SKIP_INTERVAL_DAYS``, and activity is measured over that same window — which makes the
# behaviour identical whether the script runs hourly, nightly or weekly.
_SKIP_INTERVAL_DAYS = 14

# Watches elsewhere WITHIN one interval before a pass-over counts as a decision rather than
# a quiet spell. Three finished items in a fortnight is unambiguous activity.
_ACTIVITY_THRESHOLD = 3

# Skips absorbed at FULL strength: 3 x 14d = ~6 weeks of being passed over while actively
# watching other things before anything decays at all. A two-month gap with NO activity
# still costs nothing, because a quiet window never produces a skip in the first place.
_SKIP_TOLERANCE = 3

# Past the tolerance the multiplier halves every this-many further skips — 2 x 14d, so a
# halving per month. The floor is reached after roughly four months of sustained
# ignore-while-active. Decay, never a cliff: a show is progressively de-emphasised, never
# hard-blocked, so a household that returns finds it climbing again immediately.
_SKIP_HALFLIFE = 2


def empty_engagement() -> dict:
    """Canonical empty shape, so a first run and a loaded one look identical."""
    return {"updated_at": None, "watched_count": 0, "groups": {},
            "last_eval_at": None, "watched_count_at_eval": 0}


def _to_unix(v) -> float:
    """ISO-8601 or unix seconds -> unix seconds. 0.0 when unparseable."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def update_engagement(prior, prior_plan, watched_now, *, now=None,
                      activity_threshold=_ACTIVITY_THRESHOLD,
                      skip_interval_days=_SKIP_INTERVAL_DAYS) -> tuple:
    """``(state, stats)`` — fold one run's observation into a profile's engagement history.

    ``prior_plan``  the plan SURFACED last run: ``{"items": [{rating_key, group_key, ...}]}``.
    ``watched_now`` the profile's current watched identity set (ratingKeys among others).
    ``now``         ISO-8601 or unix seconds.

    TWO CLOCKS, deliberately:

    * ENGAGEMENT is checked EVERY run and resets a group instantly. Watching one episode
      should undo the whole skip history the moment it happens, not at the next interval.
    * SKIPS accrue at most once per ``skip_interval_days``, and the activity that justifies
      one is measured over that SAME window. This is what makes the result identical at
      hourly, nightly or weekly cadence — a per-run counter would exhaust its tolerance in
      an afternoon and bury a show nobody had had a chance to watch.

    Below the activity threshold the window is DORMANT: nothing is held against any group,
    however long it lasts.
    """
    state = dict(prior or empty_engagement())
    state.setdefault("groups", {})
    groups = state["groups"]

    watched = watched_now or set()
    now_ts = _to_unix(now)
    last_eval = _to_unix(state.get("last_eval_at"))
    elapsed_days = ((now_ts - last_eval) / 86400.0) if last_eval else None

    # First observation establishes the baseline; it can never itself be a skip.
    due = last_eval == 0.0 or (elapsed_days is not None
                               and elapsed_days >= float(skip_interval_days))
    grew = max(0, len(watched) - int(state.get("watched_count_at_eval") or 0))
    active = due and last_eval != 0.0 and grew >= activity_threshold

    by_group: dict = {}
    for it in ((prior_plan or {}).get("items") or []):
        gk = it.get("group_key")
        if gk is None:
            continue
        by_group.setdefault(str(gk), []).append(str(it.get("rating_key")))

    stats = {"surfaced": len(by_group), "engaged": 0, "skipped": 0, "dormant": 0,
             "activity": grew, "active": bool(active), "window_due": bool(due),
             "days_since_eval": (round(elapsed_days, 1) if elapsed_days is not None else None)}

    for gk, rks in by_group.items():
        g = groups.setdefault(gk, {"surfaced": 0, "skips": 0,
                                   "last_surfaced": None, "last_engaged": None})
        g["surfaced"] = int(g.get("surfaced") or 0) + 1
        g["last_surfaced"] = now
        if any(rk in watched for rk in rks):
            g["skips"] = 0                      # engaged → the slate is wiped, immediately
            g["last_engaged"] = now
            stats["engaged"] += 1
        elif active:
            g["skips"] = int(g.get("skips") or 0) + 1
            stats["skipped"] += 1
        else:
            stats["dormant"] += 1               # quiet or mid-window: costs nothing

    if due:                                     # roll the window forward only when it closes
        state["last_eval_at"] = now
        state["watched_count_at_eval"] = len(watched)
    state["watched_count"] = len(watched)
    state["updated_at"] = now
    return state, stats


def engagement_multiplier(state, group_key, *, tolerance=_SKIP_TOLERANCE,
                          halflife=_SKIP_HALFLIFE, floor=0.15) -> float:
    """``0..1`` weight for a group's completion boost, from its skip history.

    1.0 until ``tolerance`` skips-while-active, then halving every ``halflife`` further
    skips, bottoming at ``floor`` rather than 0 — a de-emphasised saga must still be able to
    climb back the moment the household returns to it, which a hard 0 would prevent.
    """
    g = ((state or {}).get("groups") or {}).get(str(group_key)) or {}
    skips = int(g.get("skips") or 0)
    if skips <= tolerance:
        return 1.0
    if halflife <= 0:
        return floor
    return max(floor, 0.5 ** ((skips - tolerance) / float(halflife)))


def completion_boost(pct, *, floor_pct=5.0, max_boost=1.0) -> float:
    """``0..max_boost`` from how far through a saga the profile already is.

    Rises with completion, so a saga nearly finished is pushed hardest and one barely begun
    gets almost nothing — franchise membership alone must not buy a top slot, or every
    member of every large saga outranks the standalone somebody actually wants.

    Below ``floor_pct`` the boost is ZERO: a saga someone has merely brushed against is not
    in progress. Squared to keep the low end flat and let the top end pull away.
    """
    try:
        p = float(pct or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if p <= floor_pct:
        return 0.0
    span = max(1e-6, 100.0 - floor_pct)
    return max_boost * (((p - floor_pct) / span) ** 2)


def saga_boost(pct, state, group_key, **kw) -> float:
    """The composite: completion proximity, de-rated by how often it has been passed over.

    This is the number a caller adds to a group's normalised rank score.
    """
    return completion_boost(pct, **{k: v for k, v in kw.items()
                                    if k in ("floor_pct", "max_boost")}) * \
        engagement_multiplier(state, group_key,
                              **{k: v for k, v in kw.items()
                                 if k in ("tolerance", "halflife", "floor")})
