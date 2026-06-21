"""
playlists/ordering.py — THE crown-jewel ordering (Hard-req #4).
================================================================================
Pipeline (pure, deterministic):
  1. Drop items the user already watched (Hard-req #5 per-user filter).
  2. GROUP by connected components over shared series/franchise/universe (grouping.py)
     so co-affiliated items stay contiguous.
  3. Order WITHIN each group by spoiler-safe timeline (timeline.py).
  4. Rank GROUPS by watchability — a group is as compelling as its strongest still-
     watchable entry — with optional per-medium percentile normalization so a movie
     score and a (different-scorer) show score are comparable across groups.
  5. Apply the group-atomic size cap (caps.py).
  6. Emit ``PlaylistItemPlan``s with ordinal + rationale and a coverage stat.

Every tie ends in a deterministic key (size, lead chrono, title, group/rating key),
so a golden corpus pins the exact order regardless of input order.
"""
from __future__ import annotations

from datetime import date

from scripts.managers.machine_learning.playlists.caps import apply_size_cap
from scripts.managers.machine_learning.playlists.grouping import (
    _affinity_keys,
    coverage_stats,
    group_items,
)
from scripts.managers.machine_learning.playlists.models import (
    STANDALONE,
    PlaylistInput,
    PlaylistItemPlan,
    PlaylistPlan,
)
from scripts.managers.machine_learning.playlists.timeline import (
    chrono_value,
    order_within_group,
    recency_value,
)

_NEG_INF = float("-inf")


def _medium_percentile(items: list[PlaylistInput]) -> dict[str, float]:
    """rating_key → percentile (0,1] of its score WITHIN its medium. Lets a movie
    score and a show score (computed by different scorers on different signal mixes)
    be ranked on one comparable axis for cross-group ordering. Items with no score
    are omitted (resolver returns None for them)."""
    by_medium: dict[str, list[float]] = {}
    for it in items:
        if it.score is not None:
            by_medium.setdefault(it.medium, []).append(it.score)
    for scores in by_medium.values():
        scores.sort()
    out: dict[str, float] = {}
    for it in items:
        if it.score is None:
            continue
        scores = by_medium[it.medium]
        # fraction of same-medium items at-or-below this score → (0, 1]
        lo, hi = 0, len(scores)
        while lo < hi:                     # rightmost index where scores[i] <= score
            mid = (lo + hi) // 2
            if scores[mid] <= it.score:
                lo = mid + 1
            else:
                hi = mid
        out[it.rating_key] = lo / len(scores)
    return out


def _score_resolver(items: list[PlaylistInput], normalize_per_medium: bool):
    if not normalize_per_medium:
        return lambda it: it.score
    pct = _medium_percentile(items)
    return lambda it: pct.get(it.rating_key)


def _group_recency_boost(members: list[PlaylistInput], watched: list[PlaylistInput],
                         *, window_days: int, now: date) -> bool:
    """Does THIS group earn the caught-up recency boost? True only when BOTH hold:

      (a) CAUGHT UP — the group's surviving (unwatched) members are its FRESHEST: no
          unwatched item is older than a watched one in the same group. Formally
          ``min(recency of unwatched) >= max(recency of watched)``. An undated
          unwatched member (``recency -inf``) fails this, which is the safe direction
          — we can't prove it's the freshest, so we don't boost.
      (b) FRESH — the freshest unwatched member landed within ``window_days`` of
          ``now`` (by the added_at / air-date recency blend).

    ``watched`` is the watched items that share this group's affinity (the saga the
    user has already burned through). A standalone with nothing watched before it is
    trivially "caught up" — it qualifies purely on freshness."""
    live_rec = [recency_value(m, now=now) for m in members]
    freshest = max(live_rec)
    if freshest == _NEG_INF:                       # no usable date → never "fresh"
        return False
    if now.toordinal() - freshest > window_days:   # freshest member is stale
        return False
    if watched:                                    # caught-up: nothing newer is unseen
        seen = max(recency_value(w, now=now) for w in watched)
        if min(live_rec) < seen:
            return False
    return True


def _watched_by_group(groups, watched: list[PlaylistInput]):
    """Map each group → the watched items sharing ANY of its affinity tokens (the
    already-seen part of that saga). A watched item that bridges two un-merged live
    groups counts against both — conservatively safe for the caught-up test."""
    out = {id(g): [] for g in groups}
    if not watched:
        return out
    keys_by_group = {id(g): {k for m in g.members for k in _affinity_keys(m)}
                     for g in groups}
    for w in watched:
        wkeys = set(_affinity_keys(w))
        if not wkeys:
            continue
        for g in groups:
            if wkeys & keys_by_group[id(g)]:
                out[id(g)].append(w)
    return out


def order_items(items: list[PlaylistInput], *, family: str = "up_next",
                max_items: int | None = None, normalize_per_medium: bool = False,
                include_specials: bool = False, recency_boost: bool = False,
                window_days: int = 30, now: date | None = None,
                resume_boost: bool = False, resume_order: str = "recency",
                resume_weight: float = 0.0,
                progress_filter: str | None = None, series_recency=None) -> PlaylistPlan:
    """Order candidate items into a spoiler-safe, group-contiguous, watchability-ranked
    :class:`PlaylistPlan`. See module docstring for the pipeline.

    ``recency_boost`` (default OFF → byte-identical output) lifts a GROUP above
    higher-watchability groups, but ONLY when the user is CAUGHT UP on it (its unwatched
    items are its freshest) AND its freshest item is RECENT (within ``window_days`` by
    the added_at / air-date blend). It is a GROUP-rank tiebreak that sits ABOVE
    watchability for qualifying groups — never an item-level sort, so group contiguity
    and spoiler order are untouched. ``now`` overrides "today" (clamps future dates;
    testability).

    ``resume_boost`` (default OFF → byte-identical) lifts an IN-PROGRESS saga (a group with
    ≥1 watched member AND unwatched members queued — MCU/X-Men/Freddy collection/series alike)
    above not-started groups and standalones, so you continue what you started. Among the
    in-progress sagas the PRIMARY key is ``resume_order``: ``"recency"`` (default) → the saga
    you watched MOST RECENTLY first (max ``last_watched`` over its watched members); ``"progress"``
    → the saga you're DEEPEST into first (most watched members). Either way watchability+affinity
    breaks the primary's ties. A standalone (e.g. a high-affinity one-off) is never in-progress,
    so it stays in the lower tier — finishing a saga beats a fresh one-off. Mutually exclusive
    with ``recency_boost`` (recency_boost wins if both set).

    ``resume_weight`` (0..1) makes the resume boost a TUNABLE bonus instead of a hard tier in the
    BLENDED list (no ``progress_filter``): each group's watchability is min-max normalized to
    [0,1] across the plan and an in-progress saga gets ``+resume_weight`` — so it wins ties and
    gaps up to the weight, but a standalone whose normalized affinity is more than ``resume_weight``
    higher still leads (your casual-night exploration). 0 = pure affinity (in-progress only breaks
    ties); ~1 ≈ the old hard tier (saga almost always first). In a filtered mood list
    (``progress_filter="in"`` = The Long Glide) everything is in-progress, so the weight is moot and
    ``resume_order`` (recency/progress) is the PRIMARY order there.

    ``progress_filter`` splits the candidate pool into the two mood lists from ONE plan:
    ``"in"`` keeps ONLY in-progress sagas/franchises/series ("The Long Glide"); ``"out"`` keeps
    ONLY the rest — standalones + not-started groups ("Touch & Go", the low-commitment one-offs).
    ``None`` (default) keeps everything (the blended "Up Next"). A group is in-progress when it has
    a watched member sharing its affinity (movies) OR a member whose ``series_id`` is in
    ``series_recency`` (TV — whose watched episodes are pre-filtered out upstream).

    ``series_recency`` (``{series_id: (last_watch_ts, watched_count)}``) supplies the TV side of the
    resume keys (recency + depth) that watched episodes can't, since they're filtered out before
    here — so an in-progress SHOW ranks in The Long Glide like an in-progress movie saga."""
    items = list(items)
    considered = len(items)

    live = [it for it in items if not it.watched]
    dropped_watched = considered - len(live)
    if not include_specials:
        live = [it for it in live if not (it.is_special or it.season == 0)]

    if not live:
        return PlaylistPlan(family=family, items=(), considered=considered,
                            dropped_watched=dropped_watched, truncated=0, coverage={})

    groups = group_items(live)

    # IN-PROGRESS detection (shared by the resume boost + the progress filter): a group is
    # in-progress if it has a watched member sharing its affinity (movies carry their watched
    # entries through) OR a member whose series_id is in started_series (TV drops watched eps
    # upstream, so the builder passes the started-series set explicitly).
    resume: dict[int, tuple] = {}
    in_progress: dict[int, bool] = {}
    if resume_boost or progress_filter:
        srec = series_recency or {}
        wfor = _watched_by_group(groups, [it for it in items if it.watched])
        for g in groups:
            # watched MOVIE members only — TV's watched eps are pre-filtered upstream and its
            # signal comes from series_recency, so excluding episodes here keeps depth from
            # double-counting were a watched episode ever to reach the candidate pool.
            w = [m for m in wfor[id(g)] if m.medium != "episode"]
            # DISTINCT in-progress series in the group — a series contributes ONE (ts, count),
            # not one per queued episode (a series group holds many next-unwatched members).
            tv = [srec[sid] for sid in {m.series_id for m in g.members if m.series_id in srec}]
            ip = bool(w) or bool(tv)
            in_progress[id(g)] = ip
            # recency = latest watch across watched movies + the group's in-progress shows;
            # depth = watched movies + watched episodes — so TV and movie sagas rank on one axis.
            last = max([m.last_watched or 0 for m in w] + [t[0] for t in tv], default=0)
            depth = len(w) + sum(t[1] for t in tv)
            resume[id(g)] = (1 if ip else 0, last, depth)

    # progress filter — slice the pool into the two mood lists (The Long Glide / Touch & Go).
    if progress_filter == "in":
        groups = [g for g in groups if in_progress.get(id(g))]
    elif progress_filter == "out":
        groups = [g for g in groups if not in_progress.get(id(g))]
    if not groups:
        return PlaylistPlan(family=family, items=(), considered=considered,
                            dropped_watched=dropped_watched, truncated=0, coverage={})

    coverage = coverage_stats(groups)
    score_of = _score_resolver(live, normalize_per_medium)

    # the caught-up boost (when enabled) needs the watched history per group.
    boosted: dict[int, bool] = {}
    if recency_boost:
        clock = now or date.today()
        # mirror the live filter on watched items: a watched SPECIAL carries no
        # "must precede" relationship (see spoiler.py), so it must not fail caught-up.
        seen = [it for it in items if it.watched
                and (include_specials or not (it.is_special or it.season == 0))]
        watched_for = _watched_by_group(groups, seen)
        boosted = {id(g): _group_recency_boost(
            order_within_group(list(g.members)), watched_for[id(g)],
            window_days=window_days, now=clock) for g in groups}

    # order within each group, compute the group's ranking score (max over members)
    rendered = []
    for g in groups:
        members = order_within_group(list(g.members))
        scores = [score_of(m) for m in members if score_of(m) is not None]
        top = max(scores) if scores else _NEG_INF
        rendered.append((g, members, top))

    # min-max normalize the group watchability scores to [0,1] so resume_weight is a meaningful,
    # scale-independent bonus whether scores are raw (movie-only) or percentile (combined).
    _tops = [t for _, _, t in rendered if t != _NEG_INF]
    _lo = min(_tops) if _tops else 0.0
    _rng = (max(_tops) - _lo) if len(_tops) >= 2 else 0.0

    def _norm(t):
        return ((t - _lo) / _rng) if (_rng and t != _NEG_INF) else 0.0

    # rank groups: watchability DESC, then size DESC, then earliest lead date,
    # then lead title, then group key — fully deterministic. When the recency boost
    # is on, a qualifying (caught-up + fresh) group sorts ABOVE everything else first
    # (0 < 1), THEN watchability orders within each tier — so OFF is byte-identical.
    def _rank(entry):
        g, members, top = entry
        lead = members[0]
        base = (-top, -len(members), chrono_value(lead), lead.title.casefold(), g.key)
        if recency_boost:
            return (0 if boosted[id(g)] else 1,) + base
        if progress_filter == "in":
            # The Long Glide (pure resume list): recency/progress is the PRIMARY order.
            _, last, depth = resume[id(g)]
            order_key = (-last, -depth) if resume_order == "recency" else (-depth, -last)
            return order_key + base
        if resume_boost:
            # blended Up Next: in-progress saga gets a TUNABLE watchability bonus, then the
            # recency/progress tiebreak among comparable groups, then the deterministic keys.
            in_prog, last, depth = resume[id(g)]
            order_key = (-last, -depth) if resume_order == "recency" else (-depth, -last)
            eff = _norm(top) + (resume_weight if in_prog else 0.0)
            return (-eff,) + order_key + base
        return base

    rendered.sort(key=_rank)

    blocks = [members for _, members, _ in rendered]
    kept, truncated = apply_size_cap(blocks, max_items)
    kept_ids = {id(it) for it in kept}      # cap may SKIP an oversized group, so kept is
                                            # NOT necessarily a prefix — align by identity

    # flatten in ranked order, carrying group identity + rank score per item, keeping
    # only the items the cap retained (each kept group stays whole; idx/n stay the
    # member's position within its FULL group, so the "k/n" rationale is honest)
    flat: list[tuple[PlaylistInput, object, float, int, int]] = []
    for g, members, top in rendered:
        n = len(members)
        for idx, m in enumerate(members):
            if id(m) in kept_ids:
                flat.append((m, g, top, idx, n))

    plans = []
    for ordinal, (m, g, top, idx, n) in enumerate(flat):
        score = None if top == _NEG_INF else top
        if g.kind == STANDALONE:
            reason = f"watchability {score:.0f}" if score is not None else "owned"
        else:
            reason = f"{g.kind} '{g.key}' · {idx + 1}/{n}"
        plans.append(PlaylistItemPlan(
            rating_key=m.rating_key, ordinal=ordinal, group_key=g.key,
            group_kind=g.kind, score=score, reason=reason))

    return PlaylistPlan(
        family=family, items=tuple(plans), considered=considered,
        dropped_watched=dropped_watched, truncated=truncated, coverage=coverage)
