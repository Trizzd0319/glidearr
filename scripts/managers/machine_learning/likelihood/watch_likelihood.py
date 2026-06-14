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
  * ENGAGEMENT floors it: rewatched → 90, watched once / ≥90% → watched_floor,
    20–90% → started_floor, abandoned (<20%) → ≤ abandoned_ceiling.
  * AFFINITY (cast/crew/studio/genre, via the watchability_score) raises it for
    UNTOUCHED titles, but is CAPPED at ``affinity_cap`` (75) which is BELOW the
    top-4K band — so affinity alone can reach high-4K but never the top-4K tier,
    which stays reserved for rewatched content. A cold unwatched title with no
    affinity (e.g. the Alien films) lands low and stays at the floor profile.

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
    # Engagement floors (0-100).
    "rewatch_floor":        90.0,   # watch_count >= 2          → top-4K
    "watched_floor":        50.0,   # watched once / ≥90% done  → high-1080 (+affinity may lift)
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
    "affinity_cap":         75.0,   # < top-4K cutoff ⇒ affinity alone never hits top-4K
    # Affinity weight multiplier applied to the scorer's cast/crew/studio/genre caps.
    "affinity_boost":       1.8,
    # Resolution cutoffs (Sonarr / fallback): likelihood → max resolution.
    "uhd_cutoff":           70.0,
    "fhd_cutoff":           40.0,
    "hd_cutoff":            20.0,
    "uhd_res":              2160,
    "fhd_res":              1080,
    "hd_res":               720,
    "floor_res":            720,
}

# Radarr explicit profile ladder (ascending [min_likelihood, profile_id]). Profiles:
#   3 HD-720p · 4 HD-1080p · 6 HD-720p/1080p · 7 HD Bluray+WEB · 8 Remux+WEB-1080p ·
#   5 Ultra-HD(low-4K) · 9 Remux 2160p(high-4K) · 10 UHD Bluray+WEB(top-4K).
_DEFAULT_RADARR_LADDER = [
    [0,  3],
    [20, 4],
    [30, 6],
    [40, 7],
    [55, 8],
    [65, 5],
    [70, 9],
    [85, 10],
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

    if wc >= 2:
        return _floor_branch("rewatched", _cfg(config, "rewatch_floor"))   # full top-tier floor
    if watched or comp >= 90 or wc >= 1:
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
