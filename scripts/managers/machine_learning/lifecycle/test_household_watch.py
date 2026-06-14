"""Tests for lifecycle.household_watch — the pure all-household-watched resolution (ML
Step 8). The Tautulli per-user history FETCH stays in the service; this covers the
extracted core, including the new per-member quorum (default = require every member,
byte-identical to the original all-watched gate).
"""
from __future__ import annotations

from scripts.managers.machine_learning.lifecycle.household_watch import (
    resolve_household_watch,
)

_MEMBERS = ["Alice", "Bob", "Cara"]


# ── default (quorum None) reproduces the original all-watched gate ────────────────
def test_no_members_is_untracked():
    assert resolve_household_watch({}, []) == (True, None)


def test_all_watched_required_by_default():
    # every member present (case-insensitive) -> watched, latest ts
    per_user = {"alice": "2026-06-01T00:00:00Z", "BOB": "2026-06-03T00:00:00Z",
                "cara": "2026-06-02T00:00:00Z"}
    assert resolve_household_watch(per_user, _MEMBERS) == (True, "2026-06-03T00:00:00Z")
    # one member missing -> not watched (the original early-out)
    assert resolve_household_watch({"alice": "x", "bob": "y"}, _MEMBERS) == (False, None)


def test_default_quorum_ge_total_is_identical():
    per_user = {"alice": "x", "bob": "y"}
    # quorum >= member count behaves exactly like the require-all default
    assert resolve_household_watch(per_user, _MEMBERS, quorum=3) == (False, None)
    assert resolve_household_watch(per_user, _MEMBERS, quorum=99) == (False, None)


def test_all_watched_no_timestamps():
    # present but None timestamps -> watched True, ts None (mirrors original)
    assert resolve_household_watch({"alice": None, "bob": None, "cara": None}, _MEMBERS) == (True, None)


# ── quorum loosens the gate ───────────────────────────────────────────────────────
def test_quorum_majority_passes_with_two_of_three():
    per_user = {"alice": "2026-06-01T00:00:00Z", "bob": "2026-06-05T00:00:00Z"}  # Cara hasn't
    # require all -> blocked
    assert resolve_household_watch(per_user, _MEMBERS) == (False, None)
    # quorum 2 -> watched, latest ts among the two who watched
    assert resolve_household_watch(per_user, _MEMBERS, quorum=2) == (True, "2026-06-05T00:00:00Z")


def test_quorum_below_threshold_still_blocks():
    per_user = {"alice": "2026-06-01T00:00:00Z"}  # only one of three
    assert resolve_household_watch(per_user, _MEMBERS, quorum=2) == (False, None)
