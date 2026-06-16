"""Tests for routing_targets — the re-organizer (file relocation) gate, mirroring the
deletions consent gate in space_targets. Consent off by default; actuation requires BOTH
consent AND reorg_mode=="same_instance"; log_only/off never move files."""
from __future__ import annotations

import pytest

from scripts.managers.machine_learning.space.routing_targets import (
    evict_uhd_first,
    proactive_4k_enabled,
    relocation_consented,
    relocation_enabled,
    reorg_mode,
    transcode_gate_enabled,
)

_ENV = ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    # Every test starts with no relocation-consent env override.
    for v in _ENV:
        monkeypatch.delenv(v, raising=False)


# ── consent ──────────────────────────────────────────────────────────────────
def test_consent_defaults_false():
    assert relocation_consented({}) is False
    assert relocation_consented(None) is False


def test_consent_from_config():
    assert relocation_consented({"relocation_consent": True}) is True
    assert relocation_consented({"relocation_consent": False}) is False


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv("GLIDEARR_RELOCATION_CONSENT", "true")
    assert relocation_consented({"relocation_consent": False}) is True    # env on beats config off
    monkeypatch.setenv("GLIDEARR_RELOCATION_CONSENT", "false")
    assert relocation_consented({"relocation_consent": True}) is False    # env off beats config on


def test_empty_env_is_ignored(monkeypatch):
    monkeypatch.setenv("RECOMMENDARR_RELOCATION_CONSENT", "")
    assert relocation_consented({"relocation_consent": True}) is True     # blank env falls through to config


# ── reorg_mode ───────────────────────────────────────────────────────────────
def test_reorg_mode_defaults_log_only():
    assert reorg_mode({}) == "log_only"
    assert reorg_mode(None) == "log_only"
    assert reorg_mode({"routing": {}}) == "log_only"


def test_reorg_mode_reads_and_normalises():
    assert reorg_mode({"routing": {"reorg_mode": "same_instance"}}) == "same_instance"
    assert reorg_mode({"routing": {"reorg_mode": "off"}}) == "off"
    assert reorg_mode({"routing": {"reorg_mode": "OFF"}}) == "off"        # case-normalised


def test_reorg_mode_invalid_falls_back():
    assert reorg_mode({"routing": {"reorg_mode": "bogus"}}) == "log_only"


# ── relocation_enabled (consent AND same_instance) ───────────────────────────
def test_enabled_requires_both():
    cfg = {"routing": {"reorg_mode": "same_instance"}, "relocation_consent": True}
    assert relocation_enabled(cfg) is True


def test_enabled_false_without_consent():
    assert relocation_enabled({"routing": {"reorg_mode": "same_instance"}}) is False


def test_enabled_false_in_log_only_even_with_consent():
    cfg = {"routing": {"reorg_mode": "log_only"}, "relocation_consent": True}
    assert relocation_enabled(cfg) is False


def test_enabled_false_when_off():
    cfg = {"routing": {"reorg_mode": "off"}, "relocation_consent": True}
    assert relocation_enabled(cfg) is False


def test_enabled_env_consent_with_same_instance(monkeypatch):
    monkeypatch.setenv("GLIDEARR_RELOCATION_CONSENT", "yes")
    assert relocation_enabled({"routing": {"reorg_mode": "same_instance"}}) is True


# ── proactive_4k_enabled (proactive_4k AND 4k_policy==both AND relocation_enabled) ───
def _proactive_cfg(*, proactive=True, policy="both", reorg="same_instance", consent=True):
    cfg = {"routing": {"reorg_mode": reorg,
                       "movies": {"proactive_4k": proactive, "4k_policy": policy}}}
    if consent:
        cfg["relocation_consent"] = True
    return cfg


def test_proactive_4k_defaults_false():
    assert proactive_4k_enabled({}) is False
    assert proactive_4k_enabled(None) is False


def test_proactive_4k_enabled_when_all_set():
    assert proactive_4k_enabled(_proactive_cfg()) is True


def test_proactive_4k_false_when_flag_off():
    assert proactive_4k_enabled(_proactive_cfg(proactive=False)) is False


def test_proactive_4k_false_without_both_policy():
    assert proactive_4k_enabled(_proactive_cfg(policy="highest_only")) is False


def test_proactive_4k_false_without_actuation_gate():
    assert proactive_4k_enabled(_proactive_cfg(consent=False)) is False        # needs relocation consent
    assert proactive_4k_enabled(_proactive_cfg(reorg="log_only")) is False     # needs same_instance


def test_proactive_4k_handles_malformed_config():
    assert proactive_4k_enabled({"routing": "oops"}) is False                  # routing not a dict
    assert proactive_4k_enabled({"routing": {"movies": "oops"}}) is False      # movies not a dict


# ── evict_uhd_first (deletion gate, INDEPENDENT of relocation consent) ────────
@pytest.fixture
def _clear_delete_env(monkeypatch):
    for v in ("RECOMMENDARR_DELETIONS_CONSENT", "GLIDEARR_DELETIONS_CONSENT"):
        monkeypatch.delenv(v, raising=False)


def _evict_cfg(*, flag=True, policy="both", coordinator=True, consent=True, floor=2500):
    cfg = {"routing": {"movies": {"evict_uhd_first": flag, "4k_policy": policy}},
           "free_space_limit": floor}
    if coordinator:
        cfg["space_coordinator_enabled"] = True
    if consent:
        cfg["deletions_consent"] = True
    return cfg


def test_evict_uhd_first_enabled_without_any_relocation_consent(_clear_delete_env):
    # the autouse fixture already cleared relocation env, and _evict_cfg sets NO relocation_consent
    # → proves eviction is gated only on DELETION ownership, never on move consent.
    assert evict_uhd_first(_evict_cfg()) is True


def test_evict_uhd_first_defaults_false(_clear_delete_env):
    assert evict_uhd_first({}) is False
    assert evict_uhd_first(None) is False
    assert evict_uhd_first(_evict_cfg(flag=False)) is False
    assert evict_uhd_first(_evict_cfg(policy="highest_only")) is False


def test_evict_uhd_first_needs_deletion_ownership(_clear_delete_env):
    assert evict_uhd_first(_evict_cfg(coordinator=False)) is False   # coordinator not owning deletion
    assert evict_uhd_first(_evict_cfg(consent=False)) is False       # no deletion consent
    assert evict_uhd_first(_evict_cfg(floor=0)) is False             # no free_space_limit floor


def test_evict_uhd_first_handles_malformed_config(_clear_delete_env):
    assert evict_uhd_first({"routing": "oops"}) is False
    assert evict_uhd_first({"routing": {"movies": "oops"}}) is False


# ── transcode_gate_enabled (transcode_gate AND 4k_policy==both; NO relocation/move dep) ───
def _transcode_cfg(*, flag=True, policy="both"):
    return {"routing": {"movies": {"transcode_gate": flag, "4k_policy": policy}}}


def test_transcode_gate_defaults_false():
    assert transcode_gate_enabled({}) is False
    assert transcode_gate_enabled(None) is False
    assert transcode_gate_enabled(_transcode_cfg(flag=False)) is False


def test_transcode_gate_enabled_when_flag_and_both():
    assert transcode_gate_enabled(_transcode_cfg()) is True


def test_transcode_gate_false_without_both_policy():
    assert transcode_gate_enabled(_transcode_cfg(policy="highest_only")) is False


def test_transcode_gate_independent_of_relocation_consent():
    # No relocation_consent and no reorg_mode set anywhere — the gate is a read-only acquire
    # suppressor, never a file move, so it must NOT require the relocation/move actuation gate.
    assert transcode_gate_enabled(_transcode_cfg()) is True


def test_transcode_gate_handles_malformed_config():
    assert transcode_gate_enabled({"routing": "oops"}) is False
    assert transcode_gate_enabled({"routing": {"movies": "oops"}}) is False
