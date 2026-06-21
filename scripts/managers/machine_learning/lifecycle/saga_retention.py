"""lifecycle/saga_retention.py — engagement-derived, per-saga deletion-retention gates (pure).
==============================================================================================
The pure decision core of catch-up (trailing-viewer) retention. For each SAGA the set of viewers
who can BLOCK deletion — G(S) — is DERIVED FROM DATA (never a configured roster): a viewer who
WATCHED (meaningfully, or started within a grace window) OR WATCHLISTED any member of S. A title T
in S is HELD while any still-climbing member of G(S) hasn't passed it; as a member nears release
(dormancy lapse / watchlist intent expiry) the title is flagged "expiring" so the surfacing layer
can lift it to the top of that viewer's playlists ("use it or lose it"). The downstream delete
guards exclude held titles; the free-space floor downgrades them instead of deleting.

The ENGAGEMENT BAR (≥completion_threshold, or a sub-threshold start within engagement_grace_days)
is applied UPSTREAM by the producer, which has the raw Tautulli percent_complete + timestamps; this
module receives already-classified per-user signals so it stays a pure set/date computation.

PURE — stdlib only; no HTTP, no global_cache, no service imports.

Public API:
  * compute_saga_gates(member_sets, per_user, *, now, dormancy_days, expiry_boost_days,
        watchlist_hold_policy, exclude_users, quorum)
        -> {"movies": {tmdb: [keys]}, "shows": {tvdb: [keys]},
            "gate_user_count": {key: int}, "expiring_by_user": {user_id: {"movies":[…],"shows":[…]}}}

``member_sets`` is :func:`plex.playlists.universe_order.saga_member_sets`.
``per_user`` is ``{user_id: {"watched":{"movies":{tmdb:iso},"shows":{tvdb:iso}},
                            "started":{…same…}, "watchlist":{"movies":{tmdb:iso|None},"shows":{…}}}}``
where ``watched`` = passed (≥threshold), ``started`` = engaged-not-passed (within grace), ``iso`` =
last watch (watched/started) or watchlist-add time (watchlist).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _parse_dt(value):
    """Best-effort parse of an ISO-8601 string / epoch-seconds / datetime → aware UTC datetime;
    None on empty/unparseable (so a missing timestamp errs toward HOLD, never a crash)."""
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
    try:                                              # Tautulli history 'date' is epoch seconds
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _max_dt(isos):
    dts = [d for d in (_parse_dt(x) for x in isos) if d is not None]
    return max(dts) if dts else None


def compute_saga_gates(member_sets, per_user, *, now, dormancy_days=90, expiry_boost_days=30,
                       watchlist_hold_policy="windowed", exclude_users=(), quorum=None):
    """Resolve, per saga, which OWNED members are held + the per-viewer expiring set. See module
    docstring for the contract. Deterministic; no I/O; fail-OPEN — a member with no signal simply
    isn't held (never holds-everything on missing data)."""
    now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    excl = {str(u) for u in (exclude_users or ())}
    dormancy = timedelta(days=max(0, int(dormancy_days)))
    boost = timedelta(days=max(0, int(expiry_boost_days)))
    q_on = bool(quorum and quorum.get("enabled"))
    q_frac = float(quorum.get("fraction", 1.0)) if q_on else 1.0
    policy = watchlist_hold_policy or "windowed"

    movies_out: dict = {}
    shows_out: dict = {}
    counts: dict = {}
    expiring: dict = {}

    for key, members in (member_sets or {}).items():
        mv_ranks = (members or {}).get("movies") or {}
        sh_ranks = (members or {}).get("shows") or {}
        mv_ids, sh_ids = set(mv_ranks), set(sh_ranks)
        if not (mv_ids or sh_ids):
            continue

        active: list = []                             # the still-climbing gate members of THIS saga
        for uid, sig in (per_user or {}).items():
            if str(uid) in excl:
                continue
            sig = sig or {}
            w = sig.get("watched") or {}
            st = sig.get("started") or {}
            wl = sig.get("watchlist") or {}
            w_mv, w_sh = w.get("movies") or {}, w.get("shows") or {}
            st_mv, st_sh = st.get("movies") or {}, st.get("shows") or {}
            wl_mv, wl_sh = wl.get("movies") or {}, wl.get("shows") or {}

            watched_mv, watched_sh = mv_ids & set(w_mv), sh_ids & set(w_sh)
            started_mv, started_sh = mv_ids & set(st_mv), sh_ids & set(st_sh)
            wl_mv_here, wl_sh_here = mv_ids & set(wl_mv), sh_ids & set(wl_sh)

            engaged_watch = bool(watched_mv or watched_sh or started_mv or started_sh)
            engaged_wl = bool(wl_mv_here or wl_sh_here)
            if not (engaged_watch or engaged_wl):
                continue                              # not engaged with this saga → not in G(S)

            scope_max = None                          # None → whole saga in scope
            release_dt = None

            if engaged_watch:
                last = _max_dt([w_mv[t] for t in watched_mv] + [w_sh[v] for v in watched_sh]
                               + [st_mv[t] for t in started_mv] + [st_sh[v] for v in started_sh])
                if last is not None:
                    release_dt = last + dormancy
                    if now_dt >= release_dt:
                        continue                      # dormant: no saga activity in dormancy_days → drop
            else:
                # watchlist-only intent → scope the hold to the PREFIX up to the watchlisted title
                ranks = [mv_ranks[t] for t in wl_mv_here] + [sh_ranks[v] for v in wl_sh_here]
                scope_max = max(ranks) if ranks else None
                if policy == "windowed":
                    last = _max_dt([wl_mv[t] for t in wl_mv_here] + [wl_sh[v] for v in wl_sh_here])
                    if last is not None:
                        release_dt = last + dormancy
                        if now_dt >= release_dt:
                            continue                  # stale watchlist intent expired
                # 'until_start' / 'indefinite' → release_dt stays None (held until they start / remove)

            active.append({
                "uid": str(uid),
                "passed_mv": watched_mv, "passed_sh": watched_sh,
                "scope_max": scope_max, "release_dt": release_dt,
            })

        if not active:
            continue
        counts[key] = len(active)
        _gate_titles(key, mv_ranks, "movies", active, movies_out, expiring, now_dt, boost, q_frac)
        _gate_titles(key, sh_ranks, "shows", active, shows_out, expiring, now_dt, boost, q_frac)

    expiring_out = {uid: {"movies": sorted(d["movies"]), "shows": sorted(d["shows"])}
                    for uid, d in expiring.items()}
    return {"movies": movies_out, "shows": shows_out,
            "gate_user_count": counts, "expiring_by_user": expiring_out}


def _gate_titles(key, ranks, media, active, out, expiring, now_dt, boost, q_frac):
    """Decide which members of ``ranks`` (a {id: rank} map for one media) are held for saga ``key``,
    and record per-viewer 'expiring' flags. A title is held when, among the gate users for whom it is
    IN SCOPE, the passed fraction is below the quorum (default 1.0 → held if anyone hasn't passed)."""
    passed_attr = "passed_mv" if media == "movies" else "passed_sh"
    for tid, rank in ranks.items():
        in_scope = [u for u in active if u["scope_max"] is None or rank <= u["scope_max"]]
        if not in_scope:
            continue
        needers = [u for u in in_scope if tid not in u[passed_attr]]
        if not needers:
            continue
        if (len(in_scope) - len(needers)) / len(in_scope) >= q_frac:   # quorum passed → release
            continue
        bucket = out.setdefault(tid, [])
        if key not in bucket:
            bucket.append(key)
        for u in needers:                             # "use it or lose it": flag the final window
            rdt = u["release_dt"]
            if rdt is not None and now_dt >= (rdt - boost):
                expiring.setdefault(u["uid"], {"movies": set(), "shows": set()})[media].add(tid)
