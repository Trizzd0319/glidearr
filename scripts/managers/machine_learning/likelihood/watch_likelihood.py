"""
watch_likelihood.py — estimate P(content will be watched) and map it to a quality
tier (an explicit Radarr profile, or a resolution cap for Sonarr).
================================================================================
RELOCATED into the brain (ML Step 4) — already pure (config + row reads only), so
the whole module moved here verbatim; ``scripts/support/utilities/watch_likelihood.py``
is now a re-export shim (deleted at MIGRATION.md Step 10).

Single source of truth for the likelihood-gated quality-upgrade rule used by the
Radarr universe pass, Radarr active-watcher upgrades, and the Sonarr JIT upgrade
pass — so "earn your quality tier" is applied consistently.

Likelihood (0–100) = max(engagement floor, affinity propensity):
  * ENGAGEMENT floors it, GRADED BY WATCH COUNT: watched once → watched_floor (50),
    each further rewatch adds ``rewatch_step`` up to rewatch_floor (90). So one watch
    → ~1080p, twice → high-1080p, but only REGULAR rewatches (≈3+) clear the 4K gate.
    Partial views: 20–90% → started_floor, abandoned (<20%) → ≤ abandoned_ceiling.
  * AFFINITY (cast/crew/studio/genre, via the watchability_score) raises it for
    UNTOUCHED titles, but is CAPPED at ``affinity_cap`` (75) which is kept BELOW
    ``uhd_cutoff`` (77) — so affinity alone reaches 1080p but NEVER 4K, which stays
    reserved for rewatched content. A cold unwatched title with no affinity lands low
    and stays at the 720p floor (the steep, sticky low end).

Two ladders:
  * RADARR — an EXPLICIT profile-id ladder (config ``radarr_quality_ladder``): an
    ascending list of [min_likelihood, profile_id]. profile_id_for_likelihood()
    returns the target profile id; ladder_rank() gives its quality rank so callers
    only ever UPGRADE (target rank > current rank). This distinguishes sub-tiers
    that share a resolution (low/high-1080p, low/high-4K).
  * SONARR — resolution_cap_for_likelihood() returns a target max resolution
    (2160/1080/720), since Sonarr profile ids differ from Radarr's.

All thresholds/weights are config-tunable via a ``watch_likelihood`` block and the
``radarr_quality_ladder`` list.
"""
from __future__ import annotations

_DEFAULTS = {
    # Engagement floors (0-100). GRADED by watch_count: floor = watched_floor + (wc-1)*rewatch_step,
    # capped at rewatch_floor. So 1 watch → 50, 2 → 64, 3 → 78, 4+ → 90 (with rewatch_step 14).
    "rewatch_floor":        90.0,   # the cap a regularly-rewatched title reaches → top-4K
    "rewatch_step":         14.0,   # likelihood added per rewatch beyond the first
    "watched_floor":        50.0,   # watched once / ≥90% done  → 1080p (+affinity may lift, never 4K)
    "started_floor":        40.0,   # 20–90% complete           → high-1080
    "abandoned_ceiling":    25.0,   # 0–20% complete (tried, stopped)
    # Untouched / affinity propensity (capped below the top-4K band).
    # untouched_mode: "absolute" (DEFAULT — a real bar: base + score*gain, so a title
    # earns its tier on genuine merit, never just for out-ranking the library) or
    # "percentile" (Option 1 — rank within the library; reads the watchability_percentile
    # column from refresh_scores and falls back to absolute if absent). untouched_pct_floor
    # (percentile mode only): percentiles <= this contribute 0, so only the top
    # (100-floor)% climb — the "only the top X% upgrade" knob.
    "untouched_mode":       "absolute",
    "untouched_pct_floor":  0.0,
    "untouched_base":       12.0,
    "untouched_score_gain": 1.0,
    "affinity_cap":         75.0,   # < uhd_cutoff ⇒ affinity alone reaches 1080p but NEVER 4K
    # Affinity weight multiplier applied to the scorer's cast/crew/studio/genre caps.
    "affinity_boost":       1.8,
    # Resolution cutoffs (Sonarr / fallback): likelihood → max resolution. uhd_cutoff is kept ABOVE
    # affinity_cap so only rewatch engagement (≈3+ watches) earns 4K — never taste alone.
    "uhd_cutoff":           77.0,
    "fhd_cutoff":           40.0,
    "hd_cutoff":            20.0,
    "uhd_res":              2160,
    "fhd_res":              1080,
    "hd_res":               720,
    "floor_res":            720,
    # Universe / franchise propagation: a title in a HOT universe (rewatched siblings) earns BORROWED
    # effective watch-count, so a single real watch can elevate immediately. heat = rewatched_siblings /
    # group_size (a loose mega-group self-dilutes); full credit at heat_full; recency-decayed.
    "universe_credit_cap":            2.0,    # max watch-counts a hot universe lends a sibling
    "universe_heat_full":             0.30,   # rewatched-fraction of the group that earns FULL credit
    "universe_recency_halflife_days": 30.0,   # lent credit halves every N days since the group's last watch
}

# Radarr explicit profile ladder (ascending [min_likelihood, profile_id]). Profiles:
#   3 HD-720p · 4 HD-1080p · 6 HD-720p/1080p · 7 HD Bluray+WEB · 8 Remux+WEB-1080p ·
#   5 Ultra-HD(low-4K) · 9 Remux 2160p(high-4K) · 10 UHD Bluray+WEB(top-4K).
# Recalibrated: 720p is the sticky low end (<40), 1080p is the broad default (40–77, incl. all
# affinity-only titles), and the 4K trio (5/9/10) sits at ≥77 so ONLY rewatch engagement reaches it
# (watched 3× → ~78 → Ultra-HD; 4×+ → 90 → UHD Bluray+WEB). Affinity (≤75) tops out at Remux+WEB 1080p.
_DEFAULT_RADARR_LADDER = [
    [0,  3],
    [40, 4],
    [45, 7],
    [55, 8],
    [77, 5],
    [85, 9],
    [90, 10],
]


def _cfg(config, key: str) -> float:
    try:
        blk = (config or {}).get("watch_likelihood", {}) or {}
        return float(blk.get(key, _DEFAULTS[key]))
    except Exception:
        return float(_DEFAULTS[key])


def _cfg_str(config, key: str) -> str:
    try:
        blk = (config or {}).get("watch_likelihood", {}) or {}
        return str(blk.get(key, _DEFAULTS[key]))
    except Exception:
        return str(_DEFAULTS[key])


def affinity_boost(config=None) -> float:
    """Multiplier applied to the scorer's cast/crew/studio/genre weight caps."""
    return _cfg(config, "affinity_boost")


def radarr_ladder(config=None) -> list:
    """The ascending [min_likelihood, profile_id] ladder (config or default)."""
    try:
        lad = (config or {}).get("radarr_quality_ladder")
        if isinstance(lad, list) and lad:
            out = [[float(t), int(pid)] for t, pid in lad]
            out.sort(key=lambda e: e[0])
            return out
    except Exception:
        pass
    return [list(e) for e in _DEFAULT_RADARR_LADDER]


def radarr_ladder_english(config=None) -> list:
    """The PARALLEL English-only twin ladder (config ``radarr_quality_ladder_english``):
    ascending [min_likelihood, english_profile_id]. Empty list when unset — callers then
    fall back to the normal ladder, so the feature is fully opt-in / byte-identical off."""
    try:
        lad = (config or {}).get("radarr_quality_ladder_english")
        if isinstance(lad, list) and lad:
            out = [[float(t), int(pid)] for t, pid in lad]
            out.sort(key=lambda e: e[0])
            return out
    except Exception:
        pass
    return []


def _ladder_for(config, english: bool) -> list:
    """Pick the English twin ladder when ``english`` and one is configured, else normal."""
    if english:
        eng = radarr_ladder_english(config)
        if eng:
            return eng
    return radarr_ladder(config)


def english_ladder_ids(config=None) -> set:
    """The set of English-twin profile ids (used to detect 'this film is English-locked')."""
    return {int(pid) for _t, pid in radarr_ladder_english(config)}


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        f = float(value)
        return default if f != f else f   # NaN guard
    except (TypeError, ValueError):
        return default


def _get(row, key, default=None):
    try:
        getter = getattr(row, "get", None)
        if callable(getter):
            return getter(key, default)
        return row[key] if key in row else default
    except Exception:
        return default


def explain_likelihood(row, *, config=None) -> dict:
    """Decompose the likelihood into its engagement vs affinity parts.

    Returns the SAME number ``watch_likelihood`` does (it delegates here), plus
    the derivation needed to EXPLAIN an upgrade decision:

        {
          "likelihood":       float,   # the 0-100 result (== watch_likelihood)
          "engagement":       float,   # the engagement floor (or ceiling) that applied
          "engagement_tier":  str,     # rewatched|watched|started|abandoned|untouched
          "affinity":         float,   # affinity propensity (already capped)
          "winner":           str,     # "engagement" | "affinity" — which one set it
        }

    ``winner`` answers "why this tier": engagement when the floor (or, for the
    abandoned case, the ceiling) decided the result, affinity when the
    cast/crew/studio/genre propensity climbed above it.
    """
    wc      = _num(_get(row, "watch_count"), 0.0)
    # Completion column is `percent_complete` in both schemas (alias completion_pct);
    # normalise a 0-1 fraction to 0-100.
    _comp_raw = _get(row, "percent_complete", None)
    if _comp_raw is None:
        _comp_raw = _get(row, "completion_pct", None)
    comp = _num(_comp_raw, 0.0)
    if 0.0 < comp <= 1.0:
        comp *= 100.0
    _iw     = _get(row, "is_watched")
    watched = bool(_iw) if _iw is not None else False
    score   = _num(_get(row, "watchability_score"), 0.0)

    # Affinity propensity (capped below the top-4K band). Either rank-based
    # (percentile mode — spreads affinity across the tiers) or absolute.
    cap = _cfg(config, "affinity_cap")
    pct = _get(row, "watchability_percentile", None)
    if _cfg_str(config, "untouched_mode") == "percentile" and pct is not None:
        floor = _cfg(config, "untouched_pct_floor")
        span  = max(1.0, 100.0 - floor)
        frac  = max(0.0, _num(pct) - floor) / span
        affinity = frac * cap
    else:
        affinity = _cfg(config, "untouched_base") + score * _cfg(config, "untouched_score_gain")
    affinity = max(0.0, min(cap, affinity))

    def _floor_branch(tier: str, floor: float) -> dict:
        # L = max(floor, affinity): engagement wins on a tie (>=), affinity only
        # when it strictly climbs above the floor.
        return {
            "likelihood": max(floor, affinity), "engagement": floor,
            "engagement_tier": tier, "affinity": affinity,
            "winner": "engagement" if floor >= affinity else "affinity",
        }

    # Borrowed "universe credit": a hot franchise/universe (rewatched siblings) lends extra effective
    # watch-count, so a single real watch elevates immediately. 0 until a pre-pass injects it → byte-
    # identical when absent. Added to wc BEFORE the graded floor, so 1 watch + ~2 credit ⇒ 3 ⇒ 4K.
    credit = max(0.0, _num(_get(row, "universe_credit"), 0.0))
    ewc = wc + credit
    if ewc >= 1:
        # GRADED by EFFECTIVE watch count: 1→50, 2→64, 3→78, 4+→90 — 4K earned by regular (or
        # universe-elevated) rewatching, never one cold play.
        floor = min(_cfg(config, "rewatch_floor"),
                    _cfg(config, "watched_floor") + (ewc - 1) * _cfg(config, "rewatch_step"))
        tier = "rewatched" if ewc >= 3 else ("universe" if wc < 1 and credit > 0 else "watched")
        return _floor_branch(tier, floor)
    if watched or comp >= 90:
        return _floor_branch("watched", _cfg(config, "watched_floor"))
    if comp >= 20:
        return _floor_branch("started", _cfg(config, "started_floor"))
    if comp > 0:                                       # abandoned: tried & stopped
        ceil_ = _cfg(config, "abandoned_ceiling")
        return {
            "likelihood": max(0.0, min(ceil_, affinity)), "engagement": ceil_,
            "engagement_tier": "abandoned", "affinity": affinity,
            # The ceiling decides only when affinity would have exceeded it.
            "winner": "engagement" if affinity > ceil_ else "affinity",
        }
    # Untouched: affinity only.
    return {
        "likelihood": affinity, "engagement": 0.0,
        "engagement_tier": "untouched", "affinity": affinity, "winner": "affinity",
    }


def watch_likelihood(row, *, config=None) -> float:
    """Estimated 0–100 chance the content will be watched.

    = max(engagement floor, affinity propensity). Reads (all optional):
    ``watch_count``, ``completion_pct`` (0–100), ``is_watched``,
    ``watchability_score`` (the affinity-bearing composite). Delegates to
    ``explain_likelihood`` so the number and its explanation never drift.
    """
    return explain_likelihood(row, config=config)["likelihood"]


# ── Radarr: explicit profile-id ladder ────────────────────────────────────────
def profile_id_for_likelihood(likelihood, *, config=None, english: bool = False) -> int:
    """Target Radarr profile id for a likelihood (highest ladder entry ≤ L). With
    ``english=True`` resolves against the English twin ladder so an English-locked film
    climbs to the English tier for its likelihood."""
    L = _num(likelihood, 0.0)
    lad = _ladder_for(config, english)
    target = lad[0][1]
    for thresh, pid in lad:
        if L >= thresh:
            target = pid
        else:
            break
    return int(target)


def ladder_rank(profile_id, *, config=None, english: bool = False) -> int:
    """Quality rank of a profile id in the ladder (index; -1 if absent). ``english=True``
    ranks against the English twin ladder (so an English-twin id ranks correctly instead
    of returning -1, which would otherwise mis-trigger an upgrade onto a normal tier)."""
    if profile_id is None:
        return -1
    lad = _ladder_for(config, english)
    for i, (_t, pid) in enumerate(lad):
        if int(pid) == int(profile_id):
            return i
    return -1


# ── Sonarr / fallback: resolution cap ─────────────────────────────────────────
def resolution_cap_for_likelihood(likelihood, *, config=None) -> int:
    """Target MAX resolution (pixel height: 2160/1080/720) for a likelihood."""
    L = _num(likelihood, 0.0)
    if L >= _cfg(config, "uhd_cutoff"):
        return int(_cfg(config, "uhd_res"))
    if L >= _cfg(config, "fhd_cutoff"):
        return int(_cfg(config, "fhd_res"))
    if L >= _cfg(config, "hd_cutoff"):
        return int(_cfg(config, "hd_res"))
    return int(_cfg(config, "floor_res"))


def universe_credit(rewatched_siblings, group_size, *, days_since_watch=0.0, config=None) -> float:
    """Borrowed effective-watch-count a title earns from a HOT franchise/universe — the value a pre-pass
    writes into a title's ``universe_credit`` row field.

    ``heat = rewatched_siblings / group_size`` self-dilutes loose mega-groups (4/167 ≈ 0.02 → ~nothing,
    while MCU 10/34 ≈ 0.29 → ~full); FULL credit at ``universe_heat_full``; halved every
    ``universe_recency_halflife_days`` since the group's last watch; capped at ``universe_credit_cap``
    (in watch-counts). 0 for a cold or single-member group — so it's purely additive, never a penalty."""
    gs = _num(group_size, 0.0)
    rw = _num(rewatched_siblings, 0.0)
    if gs < 2 or rw < 1:
        return 0.0
    cap = _cfg(config, "universe_credit_cap")
    full = max(1e-6, _cfg(config, "universe_heat_full"))
    hl = _cfg(config, "universe_recency_halflife_days")
    heat = min(1.0, (rw / gs) / full)
    # Clamp to >= 0 so a future-dated last_watched (clock skew / bad metadata) decays as "just watched"
    # (decay <= 1.0) instead of overshooting the cap via a negative exponent.
    days = max(0.0, _num(days_since_watch))
    decay = 0.5 ** (days / hl) if hl > 0 else 1.0
    return round(cap * heat * decay, 3)


def series_universe_credits(fran_map, series_stats, *, config=None, rewatch_min=2) -> dict:
    """``{series_id: universe_credit}`` from TV franchise membership + per-series watch stats.

    ``fran_map`` = ``{series_id: franchise/universe name}`` (e.g. from ``tv_group_maps_from_series``);
    ``series_stats`` = ``{series_id: {"watch_count": n, "days_since": days_since_last_watch}}``. For each
    franchise group it computes :func:`universe_credit` from the rewatched-sibling fraction (a sibling is
    "rewatched" at ``watch_count >= rewatch_min``), recency-decayed by the group's MOST-RECENT watch, and
    gives EVERY member that same borrowed credit — so a single-watch member of a hot saga is the one it
    actually elevates. Members of cold / single-member groups get nothing (key absent → caller reads 0)."""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for sid, uni in (fran_map or {}).items():
        if uni:
            groups[uni].append(sid)
    out: dict = {}
    for members in groups.values():
        size = len(members)
        if size < 2:
            continue
        rewatched = sum(1 for sid in members
                        if _num((series_stats.get(sid) or {}).get("watch_count")) >= rewatch_min)
        if rewatched < 1:
            continue
        days = min((_num((series_stats.get(sid) or {}).get("days_since"), 1e9) for sid in members),
                   default=1e9)
        credit = universe_credit(rewatched, size, days_since_watch=days, config=config)
        if credit > 0:
            for sid in members:
                out[sid] = credit
    return out


def movie_universe_credits(universe_map, movie_stats, *, config=None, rewatch_min=2,
                           drop_labels=frozenset()) -> dict:
    """``{movie_id: universe_credit}`` from movie universe membership + per-movie watch stats.

    The movie twin of :func:`series_universe_credits`, sharing the same :func:`universe_credit` math.
    The one difference is membership: ``universe_map`` = ``{movie_id: label}`` where ``label`` is a
    PIPE-SEPARATED universe string (e.g. ``"mcu"`` or ``"dc|mcu"``) — a film can belong to several
    universes at once (Radarr's ``universe_name`` column). ``movie_stats`` =
    ``{movie_id: {"watch_count": n, "days_since": days_since_last_watch}}``. Each universe is its own
    group; the credit is computed per group from the rewatched-sibling fraction (a sibling is
    "rewatched" at ``watch_count >= rewatch_min``), recency-decayed by the group's most-recent watch.
    A film in several universes keeps its HOTTEST (the max) — the liveliest saga it sits in protects
    it. Cold / single-member groups contribute nothing (key absent → caller reads 0).

    ``drop_labels`` (compared lower-cased) are junk/placeholder group names to ignore — e.g. the bare
    ``"universe"`` / ``"franchise"`` / ``"standalone"`` placeholders (playlists.models.PLACEHOLDER_AFFINITY)
    — so they never fuse unrelated films into one bogus saga group."""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for mid, label in (universe_map or {}).items():
        seen = set()   # dedupe repeated labels in one film's pipe string ("mcu|mcu") so it isn't
        for uni in str(label or "").split("|"):   # double-counted in group_size / rewatched-sibling count
            uni = uni.strip()
            if uni and uni.lower() not in drop_labels and uni not in seen:
                seen.add(uni)
                groups[uni].append(mid)
    out: dict = {}
    for members in groups.values():
        size = len(members)
        if size < 2:
            continue
        rewatched = sum(1 for mid in members
                        if _num((movie_stats.get(mid) or {}).get("watch_count")) >= rewatch_min)
        if rewatched < 1:
            continue
        days = min((_num((movie_stats.get(mid) or {}).get("days_since"), 1e9) for mid in members),
                   default=1e9)
        credit = universe_credit(rewatched, size, days_since_watch=days, config=config)
        if credit <= 0:
            continue
        for mid in members:
            if credit > out.get(mid, 0.0):   # a multi-universe film keeps its hottest saga's credit
                out[mid] = credit
    return out
