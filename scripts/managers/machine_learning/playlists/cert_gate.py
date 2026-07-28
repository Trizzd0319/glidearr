"""
playlists/cert_gate.py — age-appropriate content gating for per-profile playlists.
================================================================================
Plex Home managed profiles carry a RESTRICTION PROFILE (Little Kid / Older Kid /
Teen) — the same age tiers a content rating maps to. This gates a profile's playlist
to age-appropriate content: a Little Kid sees only G/TV-Y/TV-G, an Older Kid adds
PG/TV-Y7/TV-PG, a Teen adds PG-13/TV-14, and an unrestricted (adult) profile sees
everything. https://support.plex.tv/articles/parental-controls/

Deterministic (same input → same tier). The ONLY side-effect is a diagnostic: an
unrecognised config age-override is logged once and then IGNORED (never honoured — a
config typo must not silently un-gate a child). Fail-CLOSED for restricted profiles:
an unknown/unrated cert is EXCLUDED for a kid (never show a child content we can't
vouch for), but allowed for an adult.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# Unrecognised override values we've already warned about — so a typo'd config override is
# surfaced ONCE, not once per (user × playlist builder) call that re-resolves the same tier.
_warned_overrides: set = set()

# Age tier levels: 0 = little kid … 3 = adult / unrestricted.
LITTLE_KID, OLDER_KID, TEEN, ADULT = 0, 1, 2, 3

# Content rating → the LOWEST age tier allowed to see it (movies + TV on one scale).
_CERT_TIER = {
    "g": LITTLE_KID, "tv-y": LITTLE_KID, "tv-g": LITTLE_KID,
    "tv-y7": OLDER_KID, "tv-y7-fv": OLDER_KID, "pg": OLDER_KID, "tv-pg": OLDER_KID,
    "pg-13": TEEN, "tv-14": TEEN,
    "r": ADULT, "nc-17": ADULT, "tv-ma": ADULT, "nr": ADULT, "unrated": ADULT,
    "not rated": ADULT, "18": ADULT, "ma": ADULT, "m": ADULT, "x": ADULT,
}

# Plex restriction-profile name (and friendly variants) → tier level.
_RESTRICTION_TIER = {
    "little_kid": LITTLE_KID, "littlekid": LITTLE_KID, "little kid": LITTLE_KID,
    "older_kid": OLDER_KID, "olderkid": OLDER_KID, "older kid": OLDER_KID,
    "teen": TEEN, "teenager": TEEN,
    "adult": ADULT, "none": ADULT, "unrestricted": ADULT, "": ADULT,
}

# Common Sense Media recommended age (years) → the LOWEST age tier allowed to see it —
# the FALLBACK when a title carries no recognised certification (~41% of the library has
# no Sonarr/Radarr cert). Aligned with the cert tiers above (TV-Y7 ≈ age 7 → older kid;
# PG-13 / TV-14 ≈ age 13-14 → teen; R / TV-MA ≈ age 16-17 → adult) and biased toward the
# MORE restrictive side at each boundary, since this gate decides what a child may see.
# (ceiling_age, tier) checked low→high; an age above every ceiling is ADULT.
_CSM_AGE_BANDS = ((6, LITTLE_KID), (9, OLDER_KID), (14, TEEN))


def _resolve_tier(name) -> "int | None":
    """Normalise a restriction-profile / override name to a tier level, or ``None`` when it
    names no tier we recognise. Handles case, surrounding space, and ``-``/``_``/space
    variants (``Older-Kid`` ≡ ``older_kid`` ≡ ``older kid``)."""
    key = str(name).strip().lower().replace("-", "_")
    if key in _RESTRICTION_TIER:
        return _RESTRICTION_TIER[key]
    key2 = key.replace("_", " ")
    if key2 in _RESTRICTION_TIER:
        return _RESTRICTION_TIER[key2]
    return None


def _warn_unknown_override(override) -> None:
    """Warn ONCE per distinct unrecognised override value. A typo'd ``profile_ages`` override
    (``"kiddo"``, ``"pg13"`` — none are tier names) must never silently un-gate a child, so we
    surface it for the operator to fix instead of letting a kid profile fail open to ADULT."""
    val = str(override).strip()
    if val.lower() in _warned_overrides:
        return
    _warned_overrides.add(val.lower())
    _log.warning(
        "playlists age-gate: unrecognised profile age override %r — ignoring it and falling "
        "back to the Plex restriction profile. Use one of: little_kid, older_kid, teen, adult. "
        "A typo here would otherwise leave a managed (kid/teen) profile ungated.", val)


def tier_level(restriction_profile=None, override=None) -> int:
    """Resolve a profile's age-tier level (0 little kid … 3 adult/unrestricted).

    A RECOGNISED config ``override`` wins; otherwise Plex's ``restriction_profile``; else
    unrestricted (ADULT). An ``override`` that is empty/whitespace counts as "not set" and
    falls through. An ``override`` that is set but names NO known tier (a config typo) is NOT
    honoured — letting it resolve to ADULT would silently un-gate a child — so it is IGNORED
    (fall back to ``restriction_profile``) and logged once (see ``_warn_unknown_override``)."""
    if override is not None and str(override).strip():
        tier = _resolve_tier(override)
        if tier is not None:
            return tier
        _warn_unknown_override(override)        # set but unrecognised → warn + ignore, never fail open
    if restriction_profile is not None:
        tier = _resolve_tier(restriction_profile)
        if tier is not None:
            return tier
    return ADULT


def csm_age_tier(csm_age) -> "int | None":
    """Map a Common Sense Media recommended age (years) to an age tier, or ``None`` when
    there's no usable age. Pure lookup over ``_CSM_AGE_BANDS`` — used as the cert fallback."""
    if csm_age is None:
        return None
    try:
        a = int(csm_age)
    except (TypeError, ValueError):
        return None
    for ceiling, tier in _CSM_AGE_BANDS:
        if a <= ceiling:
            return tier
    return ADULT


def cert_allowed(cert, level: int, *, csm_age=None) -> bool:
    """True if content rated ``cert`` may appear in a profile at age ``level``.

    Adult/unrestricted allows everything (incl. unknown). A restricted profile resolves the
    content's tier from its ``cert``; when that's unknown/unrated it FALLS BACK to the Common
    Sense Media ``csm_age`` (the kids signal we cache for titles with no cert). Only when BOTH
    are unknown does it fail-closed (never show a child content we can't vouch for)."""
    if level >= ADULT:
        return True
    ctier = _CERT_TIER.get(str(cert or "").strip().lower())
    if ctier is None:
        ctier = csm_age_tier(csm_age)     # cert unknown → fall back to Common Sense age
    if ctier is None:
        return False                      # cert AND age unknown + restricted → fail-closed
    return ctier <= level


def is_restricted(level: int) -> bool:
    """True when the profile is age-restricted (not an adult/unrestricted profile)."""
    return level < ADULT


# ── certification EVIDENCE (ordinal ladder + per-plan summary) ────────────────────
# The gate above DECIDES what a profile may see; the helpers below let a caller PROVE
# after the fact that a generated plan respected it (the playlist run-log summary table).
# They read the SAME ``_CERT_TIER`` model — no second cert taxonomy — and only add an
# ordinal ORDER so "the strictest certification in this plan" is a well-defined answer.
#
# Certifications are ordinal, not alphabetical. The primary key is the gate TIER (so the
# ordering can never disagree with the gate: a cert that ranks at or below the profile's
# ceiling is exactly a cert the gate would have admitted). Within a tier the sub-order
# follows the published ladders — TV: TV-Y < TV-Y7 < TV-G < TV-PG < TV-14 < TV-MA;
# MPAA: G < PG < PG-13 < R < NC-17 — with the two scales interleaved at their shared
# tier boundaries. NOTE the one place tier-order and the published TV ladder differ:
# TV-G is LITTLE_KID here while TV-Y7 is OLDER_KID, so TV-G sorts BELOW TV-Y7. That is
# deliberate — the gate is the authority, and the alternative would hide a real violation
# (a TV-Y7 item in a little-kid plan) behind a "TV-G is stricter" reading.
_CERT_SUBRANK = {
    "tv-y": 0, "g": 1, "tv-g": 2,                                    # LITTLE_KID
    "tv-y7": 0, "tv-y7-fv": 1, "pg": 2, "tv-pg": 3,                  # OLDER_KID
    "pg-13": 0, "tv-14": 1,                                          # TEEN
    "r": 0, "tv-ma": 1, "nc-17": 2,                                  # ADULT (rated)
    "nr": 3, "unrated": 3, "not rated": 3, "18": 3, "ma": 3, "m": 3, "x": 3,
}

# Canonical display spelling for a recognised cert (log cells are plain ASCII).
_CERT_DISPLAY = {
    "tv-y": "TV-Y", "tv-y7": "TV-Y7", "tv-y7-fv": "TV-Y7-FV", "tv-g": "TV-G",
    "tv-pg": "TV-PG", "tv-14": "TV-14", "tv-ma": "TV-MA",
    "g": "G", "pg": "PG", "pg-13": "PG-13", "r": "R", "nc-17": "NC-17",
    "nr": "NR", "unrated": "Unrated", "not rated": "Not Rated",
    "18": "18", "ma": "MA", "m": "M", "x": "X",
}

# The bucket for a title whose certification is MISSING or in a scale we don't recognise
# (a regional "12A", a blank Sonarr/Radarr field). Its own bucket by design: it is NEVER
# counted as a violation (we can't prove one) but it IS surfaced as a count, because an
# uncertified title admitted by the Common Sense age fallback is the realistic leak path.
UNKNOWN_CERT = "?"

# What a profile at each tier is ALLOWED up to, as a "TV/movie" label pair — derived from
# the tier tables above (the most mature TV cert and the most mature MPAA cert the tier
# admits), so it can never drift from the gate.
_TIER_CEILING = {
    LITTLE_KID: "TV-G/G", OLDER_KID: "TV-PG/PG", TEEN: "TV-14/PG-13", ADULT: "any",
}


def cert_rank(cert):
    """``(tier, subrank)`` ordinal for a certification — comparable with ``<``/``>`` — or
    ``None`` when the cert is missing/unrecognised (the :data:`UNKNOWN_CERT` bucket).

    The leading element is the gate tier, so ``cert_rank(c)[0] > level`` is precisely
    "the age gate would have rejected ``c`` for this profile"."""
    key = str(cert or "").strip().lower()
    tier = _CERT_TIER.get(key)
    if tier is None:
        return None
    return (tier, _CERT_SUBRANK.get(key, 0))


def cert_display(cert) -> str:
    """Canonical ASCII label for a recognised cert, else :data:`UNKNOWN_CERT`."""
    key = str(cert or "").strip().lower()
    return _CERT_DISPLAY.get(key, UNKNOWN_CERT) if key in _CERT_TIER else UNKNOWN_CERT


def tier_ceiling(level: int) -> str:
    """The certification CEILING a profile at ``level`` permits (``'TV-PG/PG'``…), or
    ``'any'`` for an unrestricted profile. The (a) half of the run-log evidence pair."""
    return _TIER_CEILING.get(level, "any")


def cert_summary(certs, level: int) -> dict:
    """Certification EVIDENCE for one generated plan — the (b) half of the run-log pair.

    ``certs`` is the per-item certification of everything actually IN the plan (order
    irrelevant, duplicates fine). Returns ``{"ceiling", "strictest", "unknown",
    "violations"}``:

    * ``ceiling``    — what the profile permits (:func:`tier_ceiling`).
    * ``strictest``  — the MOST MATURE recognised cert present, or ``'?'`` when the plan
      holds nothing recognisable. Comparing it against ``ceiling`` is the eyeball check.
    * ``unknown``    — how many items carry no recognised cert (never a violation; the
      leak path an operator should still see).
    * ``violations`` — items whose cert the gate would have REJECTED at ``level``. Any
      non-zero value is a bug in the gating, not a preference."""
    worst = None
    label = UNKNOWN_CERT
    unknown = 0
    violations = 0
    for c in certs or []:
        rank = cert_rank(c)
        if rank is None:
            unknown += 1
            continue
        if worst is None or rank > worst:
            worst, label = rank, cert_display(c)
        if rank[0] > level:
            violations += 1
    return {"ceiling": tier_ceiling(level), "strictest": label,
            "unknown": unknown, "violations": violations}
