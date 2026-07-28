"""Tests for playlists/cert_gate — age-tier resolution + content-rating gating."""
from __future__ import annotations

import logging

from scripts.managers.machine_learning.playlists import cert_gate
from scripts.managers.machine_learning.playlists.cert_gate import (
    ADULT,
    LITTLE_KID,
    OLDER_KID,
    TEEN,
    UNKNOWN_CERT,
    cert_allowed,
    cert_display,
    cert_rank,
    cert_summary,
    csm_age_tier,
    is_restricted,
    tier_ceiling,
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


def test_unrecognised_override_is_ignored_not_failed_open(caplog):
    # A typo'd profile_ages override ("kiddo" — not a tier name) must NOT silently un-gate a
    # managed child to ADULT. It is IGNORED (we fall back to the Plex restriction profile) and
    # a warning naming the bad value is logged.
    cert_gate._warned_overrides.clear()
    with caplog.at_level(logging.WARNING, logger="scripts.managers.machine_learning.playlists.cert_gate"):
        # Plex DID report a tier → the typo is ignored, gating is preserved (kid stays a kid).
        assert tier_level("little_kid", override="kiddo") == LITTLE_KID
    assert any("kiddo" in r.getMessage() for r in caplog.records)


def test_unrecognised_override_with_no_plex_tier_warns_loudly(caplog):
    # The dangerous case: Plex OMITTED restrictionProfile and the only gate was a typo'd
    # override. It still falls through to ADULT (no tier to fall back to) — but LOUDLY, never
    # silently, so the operator can spot the typo.
    cert_gate._warned_overrides.clear()
    with caplog.at_level(logging.WARNING, logger="scripts.managers.machine_learning.playlists.cert_gate"):
        assert tier_level(None, override="pg13") == ADULT
    assert any("pg13" in r.getMessage() for r in caplog.records)


def test_unrecognised_override_warns_once_per_value(caplog):
    # Resolved per (user × builder), so dedupe: the same typo logs ONCE, not on every call.
    cert_gate._warned_overrides.clear()
    with caplog.at_level(logging.WARNING, logger="scripts.managers.machine_learning.playlists.cert_gate"):
        tier_level("teen", override="kid")
        tier_level("teen", override="kid")
        tier_level("teen", override="Kid")        # same value, different case
    assert len([r for r in caplog.records if "kid" in r.getMessage().lower()]) == 1


def test_empty_override_is_unset_not_adult():
    # An empty/whitespace override means "not set" — fall through to the restriction profile,
    # NOT un-gate to ADULT. (Empty string is an ADULT signal only as a Plex restriction_profile.)
    assert tier_level("little_kid", override="") == LITTLE_KID
    assert tier_level("little_kid", override="   ") == LITTLE_KID
    assert tier_level("little_kid", override=None) == LITTLE_KID


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


# ── certification EVIDENCE (ordinal ladder + per-plan summary) ────────────────────
def test_cert_rank_is_ordinal_not_alphabetical():
    # Certifications sort by MATURITY, interleaving the TV and MPAA scales.
    ladder = ["TV-Y", "G", "TV-G", "TV-Y7", "PG", "TV-PG", "PG-13", "TV-14",
              "R", "TV-MA", "NC-17", "NR"]
    ranks = [cert_rank(c) for c in ladder]
    assert ranks == sorted(ranks)                        # strictly non-decreasing as listed
    assert cert_rank("TV-MA") > cert_rank("G")           # not alphabetical (G > … as text)
    assert cert_rank("tv-14") == cert_rank("TV-14")      # case/space insensitive


def test_cert_rank_never_contradicts_the_gate():
    # The rank's leading element IS the gate tier, so "ranks above the ceiling" and "the gate
    # would reject it" can never disagree — that is what makes the VIOLATION flag trustworthy.
    for level in (LITTLE_KID, OLDER_KID, TEEN, ADULT):
        for cert in ("TV-Y", "G", "TV-G", "TV-Y7", "PG", "TV-PG", "PG-13", "TV-14",
                     "R", "TV-MA", "NC-17"):
            assert (cert_rank(cert)[0] <= level) is bool(cert_allowed(cert, level))


def test_unknown_cert_is_its_own_bucket():
    for missing in (None, "", "   ", "12A", "PG12"):     # absent, or a scale we don't model
        assert cert_rank(missing) is None
        assert cert_display(missing) == UNKNOWN_CERT == "?"
    assert cert_display("tv-y7") == "TV-Y7" and cert_display("nc-17") == "NC-17"


def test_tier_ceiling_labels_both_scales():
    assert tier_ceiling(LITTLE_KID) == "TV-G/G"
    assert tier_ceiling(OLDER_KID) == "TV-PG/PG"
    assert tier_ceiling(TEEN) == "TV-14/PG-13"
    assert tier_ceiling(ADULT) == "any"                  # unrestricted → no ceiling to compare


def test_cert_summary_reports_strictest_unknowns_and_violations():
    # A correctly-gated older-kid plan: strictest is at the ceiling, unrated counted, no flag.
    assert cert_summary(["TV-Y", "TV-PG", None, "12A"], OLDER_KID) == {
        "ceiling": "TV-PG/PG", "strictest": "TV-PG", "unknown": 2, "violations": 0}
    # A leak: two items the gate should have rejected for a little kid.
    assert cert_summary(["TV-Y", "TV-MA", "R"], LITTLE_KID) == {
        "ceiling": "TV-G/G", "strictest": "TV-MA", "unknown": 0, "violations": 2}
    # An adult profile can never violate, and an unrated item is still surfaced.
    assert cert_summary(["TV-MA", "NC-17", None], ADULT) == {
        "ceiling": "any", "strictest": "NC-17", "unknown": 1, "violations": 0}
    # An empty / all-unknown plan reports '?' rather than inventing a clean bill of health.
    assert cert_summary([], TEEN)["strictest"] == UNKNOWN_CERT
    assert cert_summary([None, "12A"], LITTLE_KID) == {
        "ceiling": "TV-G/G", "strictest": UNKNOWN_CERT, "unknown": 2, "violations": 0}


def test_cert_summary_treats_explicit_nr_as_adult_not_unknown():
    # 'NR'/'Unrated' are RECOGNISED adult ratings in the gate table (unlike a missing field),
    # so one reaching a kid's plan is a real violation — not swept into the unknown bucket.
    assert cert_summary(["NR"], LITTLE_KID) == {
        "ceiling": "TV-G/G", "strictest": "NR", "unknown": 0, "violations": 1}
