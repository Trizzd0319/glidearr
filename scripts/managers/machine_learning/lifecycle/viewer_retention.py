"""lifecycle/viewer_retention.py — per-VIEWER episode retention intervals (pure).
==============================================================================
The delete-guard that replaces "3 hours after ANYONE watched it, it may go".

THE BUG THIS EXISTS TO FIX
--------------------------
``sonarr/cache/episode_files._apply_grace_period`` marks an episode
``marked_for_deletion`` ``GRACE_HOURS`` (3) after the *first* watch by *anybody*.
Its only exemptions are the pilot, the ``next_episode`` prefetch (a ~3 h FORWARD
window), an episode aired within ``RECENT_AIR_DAYS``, and the ``keep_series`` /
``keep_season`` tags. There is NO backward cushion of any kind, and the household
guard is inert on a config with no ``rating_groups``. So a household mid-season-4
of a ten-season show has every watched episode of S01-S04 (bar the pilot) queued
for deletion: no room to step back one episode, and no protection at all for a
second viewer who is still on season 2.

THE RULE
--------
Position and pace are per-ACCOUNT, not a household quorum. A quorum asks "has
everyone finished?"; the question that actually predicts a delete regret is
"where is each viewer, and how fast are they moving?". For each (account, series)
we define a PROTECTED INTERVAL over the series' episode sequence::

    [ position − backward_buffer , position + ceil(pace × horizon_days) ]

* ``position``  — the furthest episode that account has watched, in absolute
  across-season order (the ``season*10_000 + episode`` ordinal that
  ``acquisition.next_episode_planner.last_watched_per_series`` already uses).
* ``backward_buffer`` — default 2 episodes. "In case the viewer wants to go
  backwards." A rewatch of the episode you just finished is the single most
  likely next action, and today it costs a re-download.
* ``pace`` — episodes/day for that account on that series, measured over a recent
  window ANCHORED ON THAT ACCOUNT'S OWN LAST PLAY (not on ``now``) — otherwise a
  viewer who paused for a month reads as pace 0 and loses the forward reach that
  describes how they were actually moving.
* ``horizon_days`` — default 14. "If someone will approach the episode within a
  decent timeframe, don't delete it."

An episode is PROTECTED if it falls inside ANY active account's interval.
Outside every interval — long since watched, or more than a fortnight out of
reach — it is delete-eligible, on the deliberate bet that re-acquisition is
cheap. (:meth:`episode_files.restore_recovered_episode_deletions` makes that bet
cheaper still by recording the release identity of what it removed.)

WHY THE INTERVAL WALKS THE EPISODE LIST, NOT THE ORDINAL
--------------------------------------------------------
``season*10_000 + episode`` orders episodes but does NOT count them: S02E01 minus
two is 19 999, i.e. "S01E9999", not S01E22. Every span here is therefore resolved
as an INDEX span into the series' sorted ordinal list, so a backward buffer at a
season boundary lands on the real last episodes of the previous season. The list
is the set of episodes the caller knows about (the parquet rows for that series),
which is exactly the set that can be deleted — protecting "two episodes back"
means two deletable episodes back.

DORMANT ACCOUNTS — HOLD THE POSITION, DROP THE REACH
----------------------------------------------------
Past ``dormant_days`` with no play on that series, an account keeps its resume
point and its backward cushion but stops projecting forward. It has not
abandoned the show (its resume point is still worth two episodes of disk), but
its pace no longer predicts anything. Dormancy is evaluated per (account,
series): an account that went quiet everywhere is dormant everywhere by
construction, and an account still bingeing show A but stalled on show B is
correctly dormant on B only. ``dormant_days`` reuses the semantics AND the
default source of ``next_episode_planner.is_cold_series``
(``acquisition.next_episode.recency_gate.cold_days``, 90) — the codebase already
has one definition of "this series went cold for this household" and a second,
parallel notion of coldness would be a bug farm. It can still be split by writing
``episode_retention.dormant_days`` explicitly.

WHAT "WATCHED" MEANS HERE
-------------------------
The same thing it means everywhere else — see ``lifecycle.watched_definition``,
which owns the rule and the knob. :func:`watched_by_tautulli` is re-exported from
here because this module is where it was born and several callers import it by
this path; it prefers Tautulli's OWN per-row ``watched_status`` verdict and falls
back to ``percent_complete >= watched_percent``. A sub-threshold sample therefore
does not move a viewer's POSITION — it is a sample, not progress.

NOT DOUBLE-APPLIED. The bar is applied ONCE per aggregation, and this rule and
the parquet's ``is_watched`` are two DIFFERENT aggregations of the same admitted
plays: this one is per-(account, series) and lives in a durable sidecar; that one
is per-episode-file. Nothing here reads ``is_watched`` back, so tightening the
parquet column cannot narrow a protected interval a second time.

PURE — stdlib only; no pandas, no HTTP, no global_cache, no service imports. The
Tautulli fetch, the parquet reads/writes and the durable position sidecar stay in
``sonarr/cache/episode_files``.

Public API:
  * DEFAULT_RETENTION                       — the ON-by-default knob block
  * resolve_retention_config(config)        — config → normalised knobs
  * episode_ordinal(season, episode)        — the across-season sort key
  * format_ordinal(ordinal)                 — "S02E15" for logs/reports
  * account_position(plays)                 — furthest WATCHED ordinal
  * account_pace(plays, *, window_days)     — episodes/day, or None if undefined
  * account_dormant(last_at, now, *, dormant_days)
  * forward_reach(pace, *, horizon_days, default_pace, dormant)
  * account_facts(plays, *, cfg)            — {position, last_at, pace, episodes}
  * merge_state(prior, current)             — sidecar merge (position is monotonic)
  * interval_from_state(account, state, series_order, now, *, cfg) -> dict | None
  * account_interval(account, plays, series_order, now, *, cfg) -> dict | None
  * series_intervals / series_intervals_from_states -> list[dict]
  * series_holds(intervals, series_order) -> dict[ordinal, list[account]]
  * series_protected_ordinals(plays_by_account, series_order, now, *, cfg)
  * series_protected_from_states(states_by_account, series_order, now, *, cfg)
        -> (holds, intervals)   — the one call the service makes per series
  * watched_by_tautulli(watched_status, percent_complete, *, threshold_pct)
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone
import math

# The system-level definition of "watched" (and its knob). Re-exported below so
# `from ...viewer_retention import watched_by_tautulli` keeps working.
from scripts.managers.machine_learning.lifecycle.watched_definition import (
    DEFAULT_WATCHED_PERCENT,
    resolve_watched_percent,
    watched_by_tautulli,
)

# ── recommended defaults (ON by default) ──────────────────────────────────────
# The retention rule is ON out of the box: the CURRENT behaviour (delete 3 h after
# any watch, no backward cushion, no second-viewer protection) is the bug, so an
# opt-in fix would leave every existing install broken. Keep these IN SYNC with
# the onboarding schema skeleton (schema.py "episode_retention") and env_map.
#
# ``dormant_days`` is deliberately ABSENT from this dict — unset it inherits
# ``acquisition.next_episode.recency_gate.cold_days`` (90), the one existing
# definition of "cold" (see the module docstring).
DEFAULT_RETENTION = {
    "enabled": True,
    "backward_buffer": 2,      # episodes kept BEHIND the resume point (rewatch cushion)
    "horizon_days": 14,        # how far ahead pace is projected
    "pace_window_days": 30,    # window (ending at the account's last play) pace is measured over
    "default_pace": 1.0,       # eps/day used when pace is UNDEFINED (a single play)
    # Fallback completion bar when Tautulli's watched_status is absent. MIRRORS the
    # system-level default (lifecycle.watched_definition.DEFAULT_WATCHED_PERCENT) —
    # resolve_retention_config resolves the LIVE value through
    # ``watched_threshold.percent`` first, so this dict entry is only the skeleton
    # value the onboarding schema is pinned against.
    "watched_percent": int(DEFAULT_WATCHED_PERCENT),
}

# Fallback when neither episode_retention.dormant_days nor the acquisition
# recency gate is present. Same number the recency gate ships with.
_FALLBACK_DORMANT_DAYS = 90

# A same-day binge has zero elapsed time between first and last play; the pace
# denominator is clamped to this so the rate is finite (never a divide-by-zero).
_MIN_PACE_SPAN_DAYS = 1.0


def _as_float(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) else out


def _as_int(value, default=None):
    out = _as_float(value, None)
    return default if out is None else int(out)


def _parse_dt(value):
    """ISO-8601 string / epoch seconds / datetime → aware UTC datetime; None when
    empty or unparseable (mirrors ``saga_retention._parse_dt``)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:                                    # Tautulli history 'date' is epoch seconds
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


# ── config ────────────────────────────────────────────────────────────────────
def resolve_retention_config(config) -> dict:
    """``config`` → the normalised retention knobs, every value coerced and floored.

    Absent block → the recommended defaults (ON). ``{"enabled": False}`` turns the
    rule off entirely and the caller reverts to the legacy grace behaviour.
    ``dormant_days`` resolves in order: ``episode_retention.dormant_days`` →
    ``acquisition.next_episode.recency_gate.cold_days`` → 90, so a household that
    already tuned "cold" for the prefetch does not have to tune it twice.

    ``watched_percent`` resolves through :func:`watched_definition.resolve_watched_percent`
    (``watched_threshold.percent`` → ``episode_retention.watched_percent`` → 85) so
    this rule and the parquet's ``is_watched`` are guaranteed to admit the SAME
    plays. Tuning either key moves both."""
    cfg = ((config or {}).get("episode_retention") or {})
    if not isinstance(cfg, dict):
        cfg = {}
    out = dict(DEFAULT_RETENTION)
    out["enabled"] = bool(cfg.get("enabled", DEFAULT_RETENTION["enabled"]))
    out["backward_buffer"] = max(0, _as_int(cfg.get("backward_buffer"),
                                            DEFAULT_RETENTION["backward_buffer"]))
    out["horizon_days"] = max(0.0, _as_float(cfg.get("horizon_days"),
                                             DEFAULT_RETENTION["horizon_days"]))
    out["pace_window_days"] = max(1.0, _as_float(cfg.get("pace_window_days"),
                                                 DEFAULT_RETENTION["pace_window_days"]))
    out["default_pace"] = max(0.0, _as_float(cfg.get("default_pace"),
                                             DEFAULT_RETENTION["default_pace"]))
    out["watched_percent"] = resolve_watched_percent(config)

    dormant = _as_int(cfg.get("dormant_days"), None)
    if dormant is None:
        dormant = _as_int(
            (((config or {}).get("acquisition") or {}).get("next_episode") or {})
            .get("recency_gate", {}).get("cold_days"), None)
    out["dormant_days"] = max(0, dormant if dormant is not None else _FALLBACK_DORMANT_DAYS)
    return out


# ── ordinals ──────────────────────────────────────────────────────────────────
def episode_ordinal(season, episode):
    """``season * 10_000 + episode`` — the SAME across-season sort key
    ``next_episode_planner.last_watched_per_series`` uses, so the prefetch walk and
    this guard agree on what "further along" means. None when either index is
    missing/unparseable (a pilot stub row carries ``episode_number = NaN``)."""
    sn = _as_int(season, None)
    en = _as_int(episode, None)
    if sn is None or en is None:
        return None
    return sn * 10_000 + en


def format_ordinal(ordinal) -> str:
    """``20015`` → ``"S02E15"``. Display only (logs, reports, the run summary)."""
    o = _as_int(ordinal, None)
    if o is None:
        return "S??E??"
    return f"S{o // 10_000:02d}E{o % 10_000:02d}"


# ── per-account position / pace ───────────────────────────────────────────────
# ``plays`` is a list of per-EPISODE records for ONE (account, series):
#     {"ordinal": int, "at": iso|epoch|datetime|None, "watched": bool}
# One entry per distinct episode (an episode watched three times is ONE play with
# its latest timestamp) — pace measures how far the viewer ADVANCED, and a rewatch
# is not advancement.

def _watched_plays(plays):
    return [p for p in (plays or [])
            if p.get("watched") and _as_int(p.get("ordinal"), None) is not None]


def account_position(plays):
    """The furthest episode this account has WATCHED, as an ordinal; None when the
    account has no qualifying play on the series (it then protects nothing there —
    which is the correct answer, not a bug)."""
    watched = _watched_plays(plays)
    if not watched:
        return None
    return max(_as_int(p["ordinal"], 0) for p in watched)


def account_last_watched(plays):
    """Latest watch timestamp across this account's plays on the series (aware UTC
    datetime), or None. Drives dormancy."""
    stamps = [d for d in (_parse_dt(p.get("at")) for p in _watched_plays(plays))
              if d is not None]
    return max(stamps) if stamps else None


def account_pace(plays, *, window_days):
    """Episodes/day for this account on this series, or **None when undefined**.

    Measured over the ``window_days`` ending at THIS ACCOUNT'S LAST PLAY on this
    series — not at ``now``. Pace answers "how fast were they moving?"; whether
    that still predicts anything is :func:`account_dormant`'s job, and conflating
    the two would read every paused viewer as pace 0.

    Rate = ``(distinct episodes in window − 1) / elapsed days``, i.e. the average
    gap between consecutive advances. UNDEFINED (None) with fewer than two
    distinct timestamped episodes — one play tells you a viewer started, not how
    fast they go. The denominator is clamped at ``_MIN_PACE_SPAN_DAYS`` so a
    same-day binge yields a large finite pace rather than dividing by zero."""
    dated = [(d, _as_int(p["ordinal"], 0))
             for p in _watched_plays(plays)
             if (d := _parse_dt(p.get("at"))) is not None]
    if len(dated) < 2:
        return None
    last = max(d for d, _ in dated)
    cutoff = last.timestamp() - float(window_days) * 86_400.0
    win = [(d, o) for d, o in dated if d.timestamp() >= cutoff]
    # Distinct EPISODES, so a rewatch burst inside the window can't inflate pace.
    ordinals = {o for _, o in win}
    if len(ordinals) < 2:
        return None
    span_days = (max(d for d, _ in win) - min(d for d, _ in win)).total_seconds() / 86_400.0
    return (len(ordinals) - 1) / max(span_days, _MIN_PACE_SPAN_DAYS)


def account_dormant(last_at, now, *, dormant_days) -> bool:
    """True ⇒ this account stopped moving on this series: keep the position + the
    backward cushion, drop the forward projection.

    Mirrors ``next_episode_planner.is_cold_series``' arithmetic (``.days >
    cold_days``) and its fail-open on a missing/unparseable timestamp — no
    information is not evidence of abandonment, and for a DELETE guard the
    non-dormant (more protective) read is the safe one. There is deliberately no
    ``has_upcoming`` exemption: a series airing tonight does not make a viewer who
    stopped six months ago start moving again, and the airing episodes are already
    covered by ``RECENT_AIR_DAYS``."""
    dt = _parse_dt(last_at)
    if dt is None or dormant_days is None:
        return False
    now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    return (now_dt - dt).days > int(dormant_days)


def forward_reach(pace, *, horizon_days, default_pace, dormant) -> int:
    """How many episodes AHEAD of the resume point stay protected.

    ``0`` for a dormant account (the decided behaviour: hold the position, drop the
    reach). Otherwise ``ceil(pace × horizon_days)`` — rounded UP so a slow viewer
    still keeps at least the next episode. An UNDEFINED pace (a single play) falls
    back to ``default_pace``: a viewer one episode into a series is at their most
    likely to continue, and refusing to project would delete the very episodes
    they are about to reach."""
    if dormant:
        return 0
    rate = pace if pace is not None else default_pace
    rate = _as_float(rate, 0.0) or 0.0
    if rate <= 0:
        return 0
    return int(math.ceil(rate * float(horizon_days)))


# ── the interval ──────────────────────────────────────────────────────────────
def _index_span(series_order, position, *, backward, forward):
    """(lo_idx, hi_idx) into the sorted ``series_order`` for the protected interval
    around ``position``, or None when the span is empty.

    ``position`` need not itself be present in ``series_order`` (a watched episode
    whose file was already deleted, or an episode the cache has never seen): the
    anchor is the last index at or below it, so the buffer still lands on real,
    deletable episodes. A position below everything owned anchors "before index
    0", so the backward buffer contributes nothing and the forward reach starts at
    the first owned episode."""
    n = len(series_order)
    if n == 0 or position is None:
        return None
    anchor = bisect_right(series_order, position) - 1     # -1 ⇒ before the first owned ep
    lo = max(0, anchor - int(backward))
    hi = min(n - 1, anchor + int(forward))
    if hi < lo:
        return None
    return lo, hi


def account_facts(plays, *, cfg) -> "dict | None":
    """The three durable facts about one (account, series): where they are, when
    they were last there, and how fast they were moving. None when the account has
    no watched play on the series.

    Separated from :func:`account_interval` because these are exactly what the
    service persists to its position sidecar — the raw Tautulli history they are
    derived from is a VOLATILE source (``tautulli/history/all`` is overwritten
    hourly and Tautulli itself prunes), so the facts have to outlive it."""
    position = account_position(plays)
    if position is None:
        return None
    last = account_last_watched(plays)
    return {
        "position": position,
        "last_at": last.isoformat() if last else None,
        "pace": account_pace(plays, window_days=cfg["pace_window_days"]),
        "episodes": len(_watched_plays(plays)),
    }


def merge_state(prior, current) -> "dict | None":
    """Merge a REMEMBERED (sidecar) state with the state derived from the history
    available right now. The remembered side wins wherever it is further along:

    * ``position`` — MONOTONIC max. A viewer's furthest-watched episode cannot
      un-happen; if Tautulli pruned the play that established it, forgetting the
      position would silently un-protect the resume point and delete it.
    * ``last_at``  — max, so dormancy is measured from the most recent evidence.
    * ``pace``     — the current measurement when it is defined, else the last one
      that was. Pace is the perishable fact, so it degrades to the remembered
      value rather than to zero.
    * ``episodes`` — max (a report field only).

    Either side may be None (first run / history vanished)."""
    if prior is None:
        return dict(current) if current else None
    if current is None:
        return dict(prior)
    pos_p = _as_int(prior.get("position"), None)
    pos_c = _as_int(current.get("position"), None)
    at_p, at_c = _parse_dt(prior.get("last_at")), _parse_dt(current.get("last_at"))
    latest = max([d for d in (at_p, at_c) if d is not None], default=None)
    return {
        "position": max([p for p in (pos_p, pos_c) if p is not None], default=None),
        "last_at": latest.isoformat() if latest else None,
        "pace": current.get("pace") if current.get("pace") is not None else prior.get("pace"),
        "episodes": max(_as_int(prior.get("episodes"), 0) or 0,
                        _as_int(current.get("episodes"), 0) or 0),
    }


def interval_from_state(account, state, series_order, now, *, cfg) -> "dict | None":
    """The protected interval for ONE (account, series) from its already-derived
    :func:`account_facts` state — the reporting record AND the protection span.

    ``series_order`` is the SORTED list of ordinals present for the series.
    Returned keys: account, position, position_label, episodes, last_watched_at,
    pace (measured or None), pace_used, dormant, backward, forward, lo/hi
    (ordinals), lo_label/hi_label, lo_idx/hi_idx, span (episode count)."""
    if not state:
        return None
    position = _as_int(state.get("position"), None)
    if position is None:
        return None
    last = state.get("last_at")
    pace = _as_float(state.get("pace"), None)
    dormant = account_dormant(last, now, dormant_days=cfg["dormant_days"])
    fwd = forward_reach(pace, horizon_days=cfg["horizon_days"],
                        default_pace=cfg["default_pace"], dormant=dormant)
    span = _index_span(series_order, position,
                       backward=cfg["backward_buffer"], forward=fwd)
    rec = {
        "account": account,
        "position": position,
        "position_label": format_ordinal(position),
        "episodes": _as_int(state.get("episodes"), 0) or 0,
        "last_watched_at": last,
        "pace": pace,
        "pace_used": pace if pace is not None else (0.0 if dormant else cfg["default_pace"]),
        "dormant": dormant,
        "backward": cfg["backward_buffer"],
        "forward": fwd,
        "lo": None, "hi": None, "lo_label": None, "hi_label": None,
        "lo_idx": None, "hi_idx": None, "span": 0,
    }
    if span is None:
        return rec
    lo_idx, hi_idx = span
    rec.update({
        "lo": series_order[lo_idx], "hi": series_order[hi_idx],
        "lo_label": format_ordinal(series_order[lo_idx]),
        "hi_label": format_ordinal(series_order[hi_idx]),
        "lo_idx": lo_idx, "hi_idx": hi_idx,
        "span": hi_idx - lo_idx + 1,
    })
    return rec


def account_interval(account, plays, series_order, now, *, cfg) -> "dict | None":
    """:func:`account_facts` + :func:`interval_from_state` in one call — the
    straight-from-history path. None when the account has no watched play."""
    return interval_from_state(account, account_facts(plays, cfg=cfg),
                               series_order, now, cfg=cfg)


def series_intervals(plays_by_account, series_order, now, *, cfg) -> list:
    """One :func:`account_interval` per account with history on the series, in
    stable account order. Accounts with no watched play are omitted."""
    order = sorted(series_order or ())
    out = []
    for account in sorted((plays_by_account or {}).keys(), key=str):
        rec = account_interval(account, plays_by_account[account], order, now, cfg=cfg)
        if rec is not None:
            out.append(rec)
    return out


def series_intervals_from_states(states_by_account, series_order, now, *, cfg) -> list:
    """:func:`series_intervals` over already-merged sidecar states — the path the
    service takes so a remembered position survives a pruned history."""
    order = sorted(series_order or ())
    out = []
    for account in sorted((states_by_account or {}).keys(), key=str):
        rec = interval_from_state(account, states_by_account[account], order, now, cfg=cfg)
        if rec is not None:
            out.append(rec)
    return out


def series_holds(intervals, series_order) -> dict:
    """``{ordinal: [account, …]}`` — every episode protected by at least one
    account's interval, and by WHOM (the "and by whom" half of the predicate: a
    hold nobody can attribute is a hold nobody can audit).

    The union across accounts is the whole rule: an episode inside ANY active
    account's interval is protected."""
    order = sorted(series_order or ())
    holds: dict = {}
    for rec in intervals or ():
        if rec.get("lo_idx") is None:
            continue
        for ordinal in order[rec["lo_idx"]: rec["hi_idx"] + 1]:
            holds.setdefault(ordinal, []).append(rec["account"])
    return holds


def series_protected_ordinals(plays_by_account, series_order, now, *, cfg):
    """``(holds, intervals)`` for one series, straight from history.

    ``holds`` is ``{ordinal: [account, …]}``; ``intervals`` is the per-account
    reporting record list. Returns ``({}, [])`` when the rule is disabled, so the
    caller's guard collapses to the legacy behaviour with no branch of its own."""
    if not cfg.get("enabled", True):
        return {}, []
    order = sorted(series_order or ())
    intervals = series_intervals(plays_by_account, order, now, cfg=cfg)
    return series_holds(intervals, order), intervals


def series_protected_from_states(states_by_account, series_order, now, *, cfg):
    """:func:`series_protected_ordinals` over merged sidecar states — the single
    call the service makes per series."""
    if not cfg.get("enabled", True):
        return {}, []
    order = sorted(series_order or ())
    intervals = series_intervals_from_states(states_by_account, order, now, cfg=cfg)
    return series_holds(intervals, order), intervals


# ── what Tautulli calls "watched" ─────────────────────────────────────────────
# MOVED to lifecycle.watched_definition when the bar went system-wide (it now also
# builds the parquet's is_watched / watch_count, for movies as well as episodes),
# and re-exported here — this is the path it shipped under and several callers and
# tests import it from here. One implementation, one knob, no drift.
__all__ = [
    "DEFAULT_RETENTION", "DEFAULT_WATCHED_PERCENT", "resolve_retention_config",
    "resolve_watched_percent", "episode_ordinal", "format_ordinal",
    "account_position", "account_last_watched", "account_pace", "account_dormant",
    "forward_reach", "account_facts", "merge_state", "interval_from_state",
    "account_interval", "series_intervals", "series_intervals_from_states",
    "series_holds", "series_protected_ordinals", "series_protected_from_states",
    "watched_by_tautulli",
]
