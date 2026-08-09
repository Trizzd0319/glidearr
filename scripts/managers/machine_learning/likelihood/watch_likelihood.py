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

WHAT "WATCHED" MEANS HERE (changed — the global watched bar)
--------------------------------------------------------------------------------
``watch_count`` now counts WATCHES, not plays: the producers admit a Tautulli row
only when it clears the bar (``lifecycle.watched_definition`` — Tautulli's own
``watched_status``, else ``percent_complete >= watched_threshold.percent``, 85).
A 30-second sample therefore no longer reaches the ``ewc >= 1`` branch and no
longer buys ``watched_floor`` 50 (≈ WEB-1080p).

THE INTERACTION THAT MAKES THIS SAFE. The partial-view signal is preserved
INDEPENDENTLY, on two threshold-free fields, and is what those plays now land on:
``percent_complete`` (20–90% → ``started_floor`` 45; 0–20% → ``abandoned_ceiling``
25) and ``last_watched_at`` (a play with no completion figure at all → abandoned).
Both branches are evaluated BEFORE the untouched branch, so a sub-threshold play
can never fall through to ``untouched_base + score`` — which would have meant that
abandoning a title RAISES its quality target. Measured on the live cache after the
change: 0 titles moved from a floor branch to UNTOUCHED.

Likelihood (0–100) = max(engagement floor, affinity propensity):
  * ENGAGEMENT floors it, GRADED BY WATCH COUNT: watched once → watched_floor (50),
    each further rewatch adds ``rewatch_step`` up to rewatch_floor (90). So one watch
    → ~1080p, twice → high-1080p, but only REGULAR rewatches (≈3+) clear the 4K gate.
    Partial views: 20–90% → started_floor, abandoned (<20%) → ≤ abandoned_ceiling.
  * AFFINITY (cast/crew/studio/genre, via the watchability_score) raises it for
    UNTOUCHED titles, but is CAPPED at ``affinity_cap`` (74) which is kept BELOW
    ``uhd_cutoff`` (75) — so affinity alone reaches Remux-1080p but NEVER 4K, which
    stays reserved for rewatched content. A cold unwatched title with no affinity lands
    low and stays at the 720p floor (the steep, sticky low end).

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

AXIS ANCHOR — ``untouched_base`` 12 -> 25 (SCORER_REVISION 4 / Group D v2)
--------------------------------------------------------------------------------
THIS BRANCH CONSUMES A RAW ``watchability_score`` AT GAIN 1.0, so it is welded to
the 0-100 scoring axis and every translation of that axis lands on it in full. The
engagement floors (50/64/78/90) and the cutoffs (75/45/20) are CONSTANTS on the
likelihood scale and did not move; only the affinity term did.

Group D v2 (``scoring/device_fit.py``) replaced a near-constant +12 bonus that 92%
of the library received with a 0-to-negative transcode-risk penalty, translating the
score axis DOWN by ~13 points (owned-movie median 21 -> 8, p99 49 -> 34, max 71 -> 58;
file-owning series median 21 -> 8, p99 43 -> 30). With ``untouched_base`` left at 12,
an untouched title still needed score >= 33 to clear ``fhd_cutoff`` — a bar that used
to sit at the top ~8% of untouched movies and now sits past p99.9. MEASURED on the
real cache: untouched titles reaching 1080p collapsed 456 -> 8 (-98.2%).

WHY A TRANSLATION AND NOT A GAIN CHANGE. What Group D did to the axis IS a
translation: it removed a constant (+12 on 92% of titles) and added a low-variance
term (D4 mean -2.51, sd 2.01). The inverse of a translation is a translation. Three
independent checks agree, and all three say ~+13:

  boundary            v1 score needed   untouched titles   v2 score with the SAME
                                        at/above it        tail mass    => base'
  ------------------  ----------------  -----------------  ---------------------
  fhd_cutoff / WEB      33                456                20.0         25.0
  Bluray-1080p rung     43                 34                29.0         26.0
  Remux-1080p rung      53                  1                38.0         27.0

25 is also exactly the median shift of the axis (21 -> 8). It restores the FILE-OWNING
untouched population that reaches 1080p to 461 against 456 before (+1.1%): movies
134 vs 145, file-owning series 327 vs 311. The neighbouring integers do far worse
(24 -> 351, -23%; 26 -> 573, +26%) because the v2 score is integer-valued and heavily
tied, so no mapping can land between them.

A GAIN CHANGE WAS REJECTED, for three measured reasons:
  * It over-corrects the upper rungs. Any gain that reproduces the 1080p count also
    multiplies the spread, so Bluray-1080p goes 34 -> 51-61 and Remux-1080p 1 -> 5.
    base=25/gain=1.0 gives 28 and 1.
  * IT RE-CREATES THE PROBLEM THIS EXERCISE REMOVED. Gain saturates titles against
    ``affinity_cap``, and titles pinned at the cap are indistinguishable — a constant.
    At gain 2.2, 36 untouched titles (and 132 library-wide) sit at exactly 74. At
    base=25/gain=1.0, ZERO untouched titles reach the cap, so the affinity ordering
    is preserved EXACTLY — an unclamped affine map is order-isomorphic.
  * It makes the low end LESS sticky, not more. The cold floor is defined by how much
    score it takes to escape 720p: 20 points at base=25/gain=1.0, only 15 at gain 2.2.

INVARIANTS THIS PRESERVES (all pinned by tests in test_untouched_anchor.py):
  * ``affinity_cap`` (74) < ``uhd_cutoff`` (75) — taste alone still reaches
    Remux-1080p and NEVER 4K. Unchanged: the cap is not touched.
  * A cold unwatched title with no affinity (score 0) lands at likelihood 25, which is
    below ``fhd_cutoff`` (45) and therefore still on the 720p floor, and below every
    ladder rung above [0, 3]. ``hd_cutoff`` (20) is crossed, but ``hd_res`` and
    ``floor_res`` are BOTH 720 — the crossing has no behavioural consequence.
  * Watched titles keep their engagement-decided tier byte-for-byte: for every title
    whose floor won under v1 (>= 50 for one watch), the floor still wins, because the
    floor did not move and affinity is compared to it with the SAME max(). The 4K trio
    (19 entry-4K + 8 Bluray-2160p + 75 Remux-2160p) is identical under every mapping
    tried. The minority where affinity BEAT the floor under v1 (49 of 302 engaged
    titles) is re-anchored with the untouched branch, which is the point: it is the
    same affinity axis. base=25 restores that count to 36 where the un-anchored v2
    axis had cut it to 5.

``untouched_mode: "percentile"`` NEEDS NO RE-ANCHOR — VERIFIED, NOT ASSUMED. It reads
``watchability_percentile``, which ``refresh_scores`` computes as
``rank(pct=True) * 100`` — a pure rank, so its distribution is uniform whatever the
score axis does. Measured on the real cache, untouched titles reaching 1080p under
percentile mode: radarr 652 -> 652 (0.0%), sonarr 4,690 -> 4,922 (+4.9%); the residual
is tie-structure churn inside ``method="average"``, not a level shift, and it is two
orders of magnitude smaller than absolute mode's -98.2%. ``untouched_pct_floor`` (0.0)
is a percentile threshold on the same rank scale and is likewise unaffected.

RE-DERIVE THIS THE NEXT TIME THE SCORE AXIS MOVES. The reconstruction is: take each
title's persisted ``watchability_breakdown``, rebuild the axis under both revisions
(``_total_raw`` minus the old Group D plus the new one, re-clamped to 0-100), restrict
to titles that OWN A FILE, and match tail masses at 45/55/65. The long-term
replacement is ``ml.thresholds`` — a calibrated P(watch within H) — which removes the
need for an anchor entirely.
"""
from __future__ import annotations

_DEFAULTS = {
    # Engagement floors (0-100). GRADED by watch_count: floor = watched_floor + (wc-1)*rewatch_step,
    # capped at rewatch_floor. So 1 watch → 50, 2 → 64, 3 → 78, 4+ → 90 (with rewatch_step 14).
    "rewatch_floor":        90.0,   # the cap a regularly-rewatched title reaches → top-4K
    "rewatch_step":         14.0,   # likelihood added per rewatch beyond the first
    "watched_floor":        50.0,   # watched once / ≥90% done  → WEB-1080p (+affinity may lift, never 4K)
    "started_floor":        45.0,   # 20–90% complete           → entry 1080p (WEB)
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
    # ── untouched_base RE-ANCHORED 12 -> 25 (see the "AXIS ANCHOR" block below) ──
    "untouched_base":       25.0,
    "untouched_score_gain": 1.0,
    "affinity_cap":         74.0,   # < uhd_cutoff ⇒ affinity alone reaches Remux-1080p but NEVER 4K
    # Affinity weight multiplier applied to the scorer's cast/crew/studio/genre caps.
    "affinity_boost":       1.8,
    # Resolution cutoffs (Sonarr / fallback): likelihood → max resolution. uhd_cutoff is kept ABOVE
    # affinity_cap so only rewatch engagement (≈3+ watches) earns 4K — never taste alone.
    "uhd_cutoff":           75.0,
    "fhd_cutoff":           45.0,
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
    # Saga CAUGHT-UP / DEPTH credit (household, cross-media, timeline-aware). Distinct from the
    # rewatched-fraction credit above: lends borrowed watch-count from how CAUGHT-UP the household is on
    # a saga's timeline (all priors watched) OR how DEEP into it overall, so even an UNWATCHED frontier
    # entry (e.g. Eternals once you've seen everything before it) reaches Remux before first play. The
    # LEASH is RELEASE-RELATIVE: full for a grace window after the entry becomes available, then fades —
    # so being caught-up never expires (a dormant saga relights when its next entry drops), but an
    # ignored new entry decays back to its own tier. The grace window scales by household size.
    "saga_credit_cap":            6.0,    # max borrowed watch-counts from saga caught-up/depth
    "saga_engagement_full":       0.5,    # caught-up OR overall-watched fraction at/above which FULL
                                          # credit is lent (>50% of the saga, or ALL of an entry's priors)
    "saga_grace_days":            90.0,   # a caught-up UNWATCHED new entry holds FULL Remux credit this
                                          # long after it becomes available (release/acquisition), at the
                                          # reference household size — the window to watch it in Remux
    "saga_grace_ref_members":     4.0,    # reference household size; the grace window scales √(ref/N) so a
                                          # solo viewer gets a longer leash, a big household a shorter one
    "saga_postgrace_halflife_days": 30.0, # past the grace window the credit halves every N days (an
                                          # unwatched entry fades back to its own tier)
}

# Radarr explicit profile ladder (ascending [min_likelihood, profile_id]). Profiles:
#   3 HD-720p · 4 HD-1080p(WEB) · 6 HD-720p/1080p · 7 HD Bluray+WEB · 8 Remux+WEB-1080p ·
#   5 Ultra-HD(entry-4K) · 10 UHD Bluray+WEB(Bluray-2160p) · 9 Remux 2160p(epitome).
# SYMMETRIC by quality CLASS — web → bluray → remux at BOTH resolutions, Remux the epitome of each:
#   <45 720p (sticky cold floor) · 45 WEB-1080p · 55 Bluray-1080p · 65 Remux-1080p ·
#   75 entry-4K · 82 Bluray-2160p · 90 Remux-2160p (top).
# The 4K trio (5/10/9) sits at ≥75 so ONLY rewatch/universe engagement reaches it (watched 3× → ~78
# → entry-4K; 4×+ → 90 → Remux-2160p). Affinity (≤74) tops out at Remux+WEB-1080p — taste never buys 4K.
_DEFAULT_RADARR_LADDER = [
    [0,  3],
    [45, 4],
    [55, 7],
    [65, 8],
    [75, 5],
    [82, 10],
    [90, 9],
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
    # Completion, on ONE scale (0-100), resolved by WHICH COLUMN the row carries —
    # not by guessing from the magnitude:
    #   * ``percent_complete`` is the PARQUET column and is always 0-100. Taken as-is.
    #   * ``completion_pct`` is the movie_scorer contract and is a 0-1 FRACTION
    #     (``completion_threshold`` defaults to 0.9). Scaled up.
    # The old code sniffed both from the value alone (``0 < v <= 1 -> ×100``), which
    # reads a parquet row at 1% complete as 100% complete. That was harmless while
    # ``watch_count > 0`` short-circuited every played row into the "watched" branch;
    # under the watched bar those rows fall through to here, and six live movie rows
    # sit at exactly percent_complete == 1. A 1% sample must not grade as fully watched.
    _pc = _get(row, "percent_complete", None)
    if _pc is not None and _num(_pc, None) is not None:
        comp = _num(_pc, 0.0)
    else:
        comp = _num(_get(row, "completion_pct", None), 0.0)
        if 0.0 < comp <= 1.0:
            comp *= 100.0
    _iw     = _get(row, "is_watched")
    watched = bool(_iw) if _iw is not None else False
    score   = _num(_get(row, "watchability_score"), 0.0)
    # "Was this ever played?", independent of whether the play counted as a WATCH.
    # ``last_watched_at`` is deliberately threshold-free (see
    # lifecycle.watched_definition), so it is the one signal that survives for a play
    # Tautulli reported at 0% complete — which ``percent_complete`` cannot express and
    # which would otherwise land on the UNTOUCHED branch below at ``untouched_base +
    # score``, i.e. abandoning a title could RAISE its quality target. Two live rows
    # are in exactly that state (The Big Bang Theory S02E03, The Seven Deadly Sins S01E01).
    _lw = _get(row, "last_watched_at", None)
    played = bool(_lw) and str(_lw).strip().lower() not in ("nat", "nan", "none")

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
    #
    # BOTH columns, combined via max. The pre-passes write them SEPARATELY:
    # ``universe_credit`` = rewatched-sibling heat, ``saga_credit`` = caught-up/depth. They are
    # split at the source so the DELETE guards can read the first alone - caught-up/depth is a
    # forward-looking acquisition signal and holding old files on it inverts its meaning. The
    # QUALITY path (here) wants both, so it recombines them, preserving the exact value the
    # single blended column used to carry. ``saga_credit`` absent → identical to before.
    credit = max(0.0,
                 _num(_get(row, "universe_credit"), 0.0),
                 _num(_get(row, "saga_credit"), 0.0))
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
    if comp > 0 or played:      # abandoned: tried & stopped (``played`` catches the
        # 0%-complete play — evidence of an attempt with no completion figure. Without
        # it that row reads UNTOUCHED and scores HIGHER for having been abandoned.)
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
    ``watch_count`` (WATCHES, not plays — see lifecycle.watched_definition),
    ``percent_complete`` (0–100) or ``completion_pct`` (0–1), ``is_watched``,
    ``last_watched_at`` (the "was this tried?" bit), ``watchability_score`` (the
    affinity-bearing composite) and ``universe_credit``. Delegates to
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


def saga_credit(*, caught_up_frac=0.0, saga_watched_frac=0.0, days_since_available=0.0,
                household_members=None, config=None) -> float:
    """Borrowed effective-watch-count a saga member earns from how CAUGHT-UP / DEEP the HOUSEHOLD is —
    the value a pre-pass writes into a member's ``universe_credit`` (combined via ``max`` with the
    rewatched-fraction :func:`universe_credit`, so it only ever lifts).

    ELIGIBILITY (never decays): ``engagement = max(caught_up_frac, saga_watched_frac)`` (both 0–1).
    ``caught_up_frac`` = fraction of THIS entry's timeline-priors the household has watched (1.0 = you've
    seen everything before it → you're at the frontier); ``saga_watched_frac`` = fraction of the WHOLE
    saga watched (the looser ">50% of it" signal). FULL strength once engagement reaches
    ``saga_engagement_full`` (0.5), scaling down linearly below.

    LEASH (release-relative): full credit for a GRACE WINDOW after the entry became AVAILABLE
    (``days_since_available`` = days since its release / acquisition), then halves every
    ``saga_postgrace_halflife_days`` (30). So a caught-up household gets a window to watch a NEW entry in
    Remux; ignore it past the window and it fades to its own tier — while a freshly-released entry always
    opens a NEW window (a saga you finished years ago relights the moment its next entry drops). Being
    caught-up never expires; only the unwatched entry's window does.

    HOUSEHOLD SIZE: the window scales ``√(saga_grace_ref_members / household_members)`` — a solo viewer
    gets a longer leash than a 6-person house, so per-person pressure to keep up is comparable
    (``household_members`` None → reference size → the base window). Cap (6.0) sits well above the ~3.86
    effective-watches the Remux gate needs. 0 when nothing of the saga is watched — purely additive."""
    cap = _cfg(config, "saga_credit_cap")
    full = max(1e-6, _cfg(config, "saga_engagement_full"))
    base_window = _cfg(config, "saga_grace_days")
    ref = max(1.0, _cfg(config, "saga_grace_ref_members"))
    post_hl = _cfg(config, "saga_postgrace_halflife_days")
    engagement = max(0.0, min(1.0, max(_num(caught_up_frac), _num(saga_watched_frac))))
    if engagement <= 0.0:
        return 0.0
    frac = min(1.0, engagement / full)
    # Size-normalised grace window: a solo household gets a longer leash (√-dampened); unknown → ref.
    n = max(1.0, _num(household_members, ref))
    window = base_window * (ref / n) ** 0.5
    # Clamp age >= 0 so a future-dated availability (clock skew) is treated as just-released.
    age = max(0.0, _num(days_since_available))
    if age <= window:
        decay = 1.0
    elif post_hl > 0:
        decay = 0.5 ** ((age - window) / post_hl)
    else:
        decay = 0.0
    return round(cap * frac * decay, 3)


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
