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
