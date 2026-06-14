"""
playlists/per_user.py — rank a series for ONE user (affinity > JIT > household).
================================================================================
The playlist ordering ranks series groups by a per-series score. By default that's
the HOUSEHOLD watchability score, so every profile leads with whatever the whole
house watches most (e.g. a kids show that dominates the household). This module
PERSONALIZES that order with three signals, in strict precedence:

    1. USER AFFINITY  — how well the series' genres match THIS user's taste (top)
    2. JIT            — the series was just-in-time grabbed FOR this user (active
                        viewing); sits ABOVE household but is *measured against*
                        affinity (a strong affinity match still outranks it)
    3. HOUSEHOLD      — the household watchability score, NORMALISED so raw
                        magnitude can't dominate (baseline)

``priority_score`` is the live ranker (additive, weighted). ``tilt_score`` is the
older multiplicative personaliser, kept for callers/tests that still use it.
Pure + deterministic.
"""
from __future__ import annotations


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _genre_weights(series_genres, aff, mx):
    """Per-show-genre normalised affinity weights (``aff[g]/mx``, 0 for unknown genres)."""
    return [aff.get(str(g).strip().lower(), 0.0) / mx for g in series_genres if g is not None]


GENRE_MATCH_MODES = ("precision", "soft", "coverage", "blend")


def genre_match(series_genres, user_genre_affinity, *, mode: str = "precision",
                soft_lambda: float = 0.5, blend_weight: float = 0.85):
    """How well a series' genres fit ONE user's taste, in [0, 1]. Returns ``None`` when there's
    no affinity / no genres / no usable genre — the caller decides the fallback.

    ``user_genre_affinity`` is ``{genre: weight}`` (per-user watch counts). ``mode`` selects the
    shape (all share the same per-genre normalised weight ``aff[g]/max_weight``):

      • ``precision`` (default, legacy): MEAN weight over ALL the show's genres. Measures "what
        fraction of the SHOW is on-taste" — so an extra genre the user doesn't watch DILUTES the
        score (Bluey's ``Children`` tag dropped it below shows with the same liked genres).
      • ``soft``: like precision but a zero-affinity genre counts only ``soft_lambda`` (default
        0.5) in the denominator instead of 1 — extra off-taste tags hurt LESS, not fully.
      • ``coverage``: affinity-weighted RECALL — covered affinity / total user affinity. Measures
        "how much of the USER's taste the show covers", so off-taste tags are ignored and two
        shows matching the SAME liked genres TIE regardless of genre count; a tiny precision term
        breaks ties toward the more on-target show.
      • ``blend``: ``blend_weight``·coverage + ``(1-blend_weight)``·precision (default 0.85)."""
    if not user_genre_affinity or not series_genres:
        return None
    aff = {str(k).strip().lower(): float(v) for k, v in user_genre_affinity.items()}
    mx = max(aff.values(), default=0.0) or 1.0
    ws = _genre_weights(series_genres, aff, mx)
    if not ws:
        return None
    mode = (mode or "precision").strip().lower()

    precision = sum(ws) / len(ws)
    if mode == "precision":
        return _clamp(precision)
    if mode == "soft":
        pos = sum(1 for w in ws if w > 0)
        denom = pos + max(0.0, float(soft_lambda)) * (len(ws) - pos)
        return _clamp(sum(ws) / denom) if denom > 0 else 0.0

    # coverage / blend both need affinity-weighted recall.
    total = sum(aff.values())
    gset = {str(g).strip().lower() for g in series_genres if g is not None}
    coverage = (sum(w for g, w in aff.items() if g in gset) / total) if total > 0 else 0.0
    if mode == "coverage":
        # recall first, on-target purity only as a hair-thin tiebreaker (never overrides a real
        # coverage gap — coverage steps are far larger than 1e-3).
        return _clamp(min(coverage, 1.0 - 1e-3) + 1e-3 * precision)
    if mode == "blend":
        w = _clamp(blend_weight)
        return _clamp(w * coverage + (1.0 - w) * precision)
    return _clamp(precision)                       # unknown mode → safe default


def priority_score(household_norm, affinity_match, *, is_jit: bool = False,
                   affinity_weight: float = 0.9, jit_weight: float = 0.5,
                   household_weight: float = 0.1) -> float:
    """Rank a series for ONE user as ``w_aff·A + w_jit·J + w_hh·h``.

    ``A`` = ``affinity_match`` in [0, 1] (``None`` → 0, i.e. no taste signal); ``J`` = 1
    when the series was JIT-grabbed for this user; ``h`` = ``household_norm`` in [0, 1]
    (the household watchability score divided by the run's max). The caller MUST keep the
    weights ordered ``affinity_weight > jit_weight > household_weight`` (the builder's
    _priority_weights enforces this intrinsically). Then: a JIT item (→ w_jit) always
    outranks a merely household-popular show (→ w_hh·h, capped at w_hh < w_jit); and a
    STRONG affinity match outranks a JIT item once ``A > jit_weight/affinity_weight``
    (≈0.72 at the defaults — reached by the user's top-genre series, ~10-25% of the
    library). A series that is BOTH on-taste and JIT'd tops everything. JIT is a weighted
    boost, never a hard pin."""
    a = 0.0 if affinity_match is None else max(0.0, min(1.0, float(affinity_match)))
    h = max(0.0, min(1.0, float(household_norm or 0.0)))
    return (float(affinity_weight) * a
            + float(jit_weight) * (1.0 if is_jit else 0.0)
            + float(household_weight) * h)


def tilt_score(household_score, series_genres, user_genre_affinity, *, tilt_pct: float = 0.0) -> float:
    """Personalize a household watchability score for one user (legacy multiplicative form).

    ``tilt_pct`` (0–100) is how far a series the user NEVER watches can be discounted:
    0 = no change (household), 50 = a zero-affinity series keeps half its score, 100 = it
    drops to 0. Returns the household score unchanged when there's no affinity / genres /
    tilt. Retained for backward compatibility; new ranking uses ``priority_score``."""
    base = float(household_score or 0.0)
    if base == 0.0 or tilt_pct <= 0:
        return base
    m = genre_match(series_genres, user_genre_affinity)
    if m is None:
        return base
    floor = max(0.0, 1.0 - float(tilt_pct) / 100.0)
    return base * (floor + (1.0 - floor) * m)
