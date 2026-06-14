"""Tests for playlists/cert_gate — age-tier resolution + content-rating gating."""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.cert_gate import (
    ADULT,
    LITTLE_KID,
    OLDER_KID,
    TEEN,
    cert_allowed,
    csm_age_tier,
    is_restricted,
    tier_level,
)


def test_tier_level_from_restriction_profile():
    assert tier_level("little_kid") == LITTLE_KID
    assert tier_level("Older Kid") == OLDER_KID          # friendly variant, case-insensitive
    assert tier_level("teen") == TEEN
    assert tier_level(None) == ADULT                     # unrestricted
    assert tier_level("adult") == ADULT
    assert tier_level("something_else") == ADULT         # unknown → unrestricted


def test_config_override_wins():
    assert tier_level("teen", override="little_kid") == LITTLE_KID
    assert tier_level(None, override="teen") == TEEN


def test_little_kid_allows_only_youngest_and_fails_closed_on_unknown():
    for ok in ("G", "TV-Y", "TV-G", "tv-y"):
        assert cert_allowed(ok, LITTLE_KID)
    for blocked in ("PG", "TV-Y7", "PG-13", "TV-14", "R", "TV-MA"):
        assert not cert_allowed(blocked, LITTLE_KID)
    assert not cert_allowed(None, LITTLE_KID) and not cert_allowed("", LITTLE_KID)   # fail-closed


def test_older_kid_and_teen_tiers():
    assert cert_allowed("PG", OLDER_KID) and cert_allowed("TV-Y7", OLDER_KID) and cert_allowed("TV-PG", OLDER_KID)
    assert not cert_allowed("PG-13", OLDER_KID)
    assert cert_allowed("PG-13", TEEN) and cert_allowed("TV-14", TEEN)
    assert not cert_allowed("R", TEEN) and not cert_allowed("TV-MA", TEEN)


def test_adult_allows_everything_including_unknown():
    for c in ("TV-MA", "R", "NC-17", None, "", "anything"):
        assert cert_allowed(c, ADULT)


def test_is_restricted():
    assert is_restricted(LITTLE_KID) and is_restricted(OLDER_KID) and is_restricted(TEEN)
    assert not is_restricted(ADULT)


def test_csm_age_tier_mapping():
    assert csm_age_tier(2) == LITTLE_KID and csm_age_tier(6) == LITTLE_KID
    assert csm_age_tier(7) == OLDER_KID and csm_age_tier(9) == OLDER_KID
    assert csm_age_tier(10) == TEEN and csm_age_tier(14) == TEEN
    assert csm_age_tier(15) == ADULT and csm_age_tier(18) == ADULT
    assert csm_age_tier(None) is None                    # no age → no tier (caller fails closed)
    assert csm_age_tier("not a number") is None
    assert csm_age_tier("8") == OLDER_KID                # numeric string coerces


def test_csm_age_is_fallback_only_when_cert_unknown():
    # The ~41% of titles with NO cert: fail-closed WITHOUT a CSM age, but admitted WITH one.
    assert not cert_allowed(None, LITTLE_KID)                       # no cert + no age → closed
    assert cert_allowed(None, LITTLE_KID, csm_age=3)               # CSM age 3 → little-kid OK
    assert cert_allowed("", OLDER_KID, csm_age=8)                  # uncertified, CSM 8 → older-kid OK
    assert not cert_allowed(None, LITTLE_KID, csm_age=10)          # CSM 10 → teen, not little kid


def test_real_cert_beats_csm_age():
    # A recognised cert ALWAYS decides; the CSM age is consulted only when the cert is unknown.
    assert not cert_allowed("TV-MA", LITTLE_KID, csm_age=3)        # adult cert wins over a low age
    assert cert_allowed("TV-G", LITTLE_KID, csm_age=99)           # kid cert wins over a high age
    assert cert_allowed("anything", ADULT, csm_age=99)           # adult profile still sees all
