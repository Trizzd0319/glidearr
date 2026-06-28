"""Tests for routing_targets — the re-organizer (file relocation) gate, mirroring the
deletions consent gate in space_targets. Consent off by default; actuation requires BOTH
consent AND reorg_mode=="same_instance"; log_only/off never move files."""
from __future__ import annotations

import pytest

from scripts.managers.machine_learning.space.routing_targets import (
    cross_instance_dedup_consented,
    cross_instance_dedup_enabled,
    cross_instance_move_consented,
    cross_instance_move_enabled,
    evict_uhd_first,
    proactive_4k_enabled,
    rehome_4k_only_enabled,
    relocation_consented,
    relocation_enabled,
    reorg_mode,
    transcode_gate_enabled,
    uhd_remote_play_ok,
)

_ENV = ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT",
        "RECOMMENDARR_CROSS_INSTANCE_MOVE_CONSENT", "GLIDEARR_CROSS_INSTANCE_MOVE_CONSENT",
        "RECOMMENDARR_CROSS_INSTANCE_DEDUP_CONSENT", "GLIDEARR_CROSS_INSTANCE_DEDUP_CONSENT")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    # Every test starts with no relocation/cross-instance consent env override.
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
    assert reorg_mode({"routing": {"reorg_mode": "cross_instance"}}) == "cross_instance"
    assert reorg_mode({"routing": {"reorg_mode": "CROSS_INSTANCE"}}) == "cross_instance"  # case-normalised
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


def test_proactive_4k_enabled_under_cross_instance():
    # cross_instance mode + move consent is ALSO a valid actuation gate (couples with the cross
    # mode's acquire), so proactive 4K works without same_instance.
    cfg = {"routing": {"reorg_mode": "cross_instance",
                       "movies": {"proactive_4k": True, "4k_policy": "both"}},
           "cross_instance_move_consent": True}
    assert proactive_4k_enabled(cfg) is True


def test_proactive_4k_false_cross_instance_without_move_consent():
    cfg = {"routing": {"reorg_mode": "cross_instance",
                       "movies": {"proactive_4k": True, "4k_policy": "both"}}}
    assert proactive_4k_enabled(cfg) is False              # cross mode but no move consent


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


# ── rehome_4k_only (FORK-D gate — deletion ownership, NOT dual-version policy) ─
def _rehome_cfg(*, flag=True, coordinator=True, consent=True, floor=2500, policy="highest_only"):
    cfg = {"routing": {"movies": {"rehome_4k_only": flag, "4k_policy": policy}},
           "free_space_limit": floor}
    if coordinator:
        cfg["space_coordinator_enabled"] = True
    if consent:
        cfg["deletions_consent"] = True
    return cfg


def test_rehome_4k_only_enabled_when_owned(_clear_delete_env):
    # Enabled on the flag + deletion ownership; does NOT require 4k_policy=='both'
    # (precondition is a split 4K instance, checked at runtime).
    assert rehome_4k_only_enabled(_rehome_cfg()) is True
    assert rehome_4k_only_enabled(_rehome_cfg(policy="both")) is True


def test_rehome_4k_only_defaults_false(_clear_delete_env):
    assert rehome_4k_only_enabled({}) is False
    assert rehome_4k_only_enabled(None) is False
    assert rehome_4k_only_enabled(_rehome_cfg(flag=False)) is False


def test_rehome_4k_only_needs_deletion_ownership(_clear_delete_env):
    assert rehome_4k_only_enabled(_rehome_cfg(coordinator=False)) is False
    assert rehome_4k_only_enabled(_rehome_cfg(consent=False)) is False
    assert rehome_4k_only_enabled(_rehome_cfg(floor=0)) is False


def test_rehome_4k_only_handles_malformed_config(_clear_delete_env):
    assert rehome_4k_only_enabled({"routing": "oops"}) is False
    assert rehome_4k_only_enabled({"routing": {"movies": "oops"}}) is False


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


# ── uhd_remote_play_ok (the shared add-time/reconcile wiring authority) ────────
def _fp_record(*, transcode, direct, device="Chromecast", res="2160p_sdr"):
    # a serialized fingerprint cell whose (codec,res,loc) matches the helper's HEVC/2160p
    # source fingerprint at the drop_audio fallback level
    return [{"device": device, "fingerprint": ["hevc", "eac3", "none", res, "unknown"],
             "transcode": transcode, "direct": direct, "last_seen": 0, "n": transcode + direct}]


def test_uhd_remote_play_ok_true_when_gate_off():
    # flag OFF → always True regardless of matrix (zero behaviour change)
    cfg = _transcode_cfg(flag=False)
    assert uhd_remote_play_ok(cfg, _fp_record(transcode=9, direct=0), {"Chromecast": 1}) is True
    assert uhd_remote_play_ok({}, None, None) is True


def test_uhd_remote_play_ok_false_when_household_transcodes_hevc():
    cfg = _transcode_cfg()  # flag on, 4k_policy both
    recs = _fp_record(transcode=4, direct=0)
    assert uhd_remote_play_ok(cfg, recs, {"Chromecast": 1}) is False


def test_uhd_remote_play_ok_true_when_household_direct_plays_hevc():
    cfg = _transcode_cfg()
    recs = _fp_record(transcode=0, direct=4)
    assert uhd_remote_play_ok(cfg, recs, {"Chromecast": 1}) is True


def test_uhd_remote_play_ok_true_on_no_data_even_with_gate_on():
    # gate on but the matrix is empty/cold → explore (acquire) so a fresh household isn't denied
    cfg = _transcode_cfg()
    assert uhd_remote_play_ok(cfg, None, None) is True
    assert uhd_remote_play_ok(cfg, [], {"Chromecast": 1}) is True


# ── cross-instance move / dedup gates (FORK 1 + FORK 4 — un-conflated, default OFF) ──
def _move_cfg(*, mode="cross_instance", consent=True):
    cfg = {"routing": {"reorg_mode": mode}}
    if consent:
        cfg["cross_instance_move_consent"] = True
    return cfg


def _dedup_cfg(*, mode="cross_instance", consent=True):
    cfg = {"routing": {"reorg_mode": mode}}
    if consent:
        cfg["cross_instance_dedup_consent"] = True
    return cfg


def test_cross_instance_consents_default_false():
    assert cross_instance_move_consented({}) is False
    assert cross_instance_move_consented(None) is False
    assert cross_instance_dedup_consented({}) is False
    assert cross_instance_dedup_consented(None) is False


def test_cross_instance_consents_from_config():
    assert cross_instance_move_consented({"cross_instance_move_consent": True}) is True
    assert cross_instance_dedup_consented({"cross_instance_dedup_consent": True}) is True


def test_cross_instance_move_consent_env_overrides_config(monkeypatch):
    monkeypatch.setenv("GLIDEARR_CROSS_INSTANCE_MOVE_CONSENT", "true")
    assert cross_instance_move_consented({"cross_instance_move_consent": False}) is True
    monkeypatch.setenv("GLIDEARR_CROSS_INSTANCE_MOVE_CONSENT", "false")
    assert cross_instance_move_consented({"cross_instance_move_consent": True}) is False


def test_cross_instance_dedup_consent_env_overrides_config(monkeypatch):
    monkeypatch.setenv("RECOMMENDARR_CROSS_INSTANCE_DEDUP_CONSENT", "yes")
    assert cross_instance_dedup_consented({"cross_instance_dedup_consent": False}) is True


def test_cross_instance_move_enabled_requires_mode_and_consent():
    assert cross_instance_move_enabled(_move_cfg()) is True
    assert cross_instance_move_enabled(_move_cfg(consent=False)) is False          # no consent
    assert cross_instance_move_enabled(_move_cfg(mode="same_instance")) is False   # wrong mode
    assert cross_instance_move_enabled(_move_cfg(mode="log_only")) is False
    assert cross_instance_move_enabled(_move_cfg(mode="off")) is False


def test_cross_instance_dedup_enabled_requires_mode_and_consent():
    assert cross_instance_dedup_enabled(_dedup_cfg()) is True
    assert cross_instance_dedup_enabled(_dedup_cfg(consent=False)) is False
    assert cross_instance_dedup_enabled(_dedup_cfg(mode="same_instance")) is False
    assert cross_instance_dedup_enabled(_dedup_cfg(mode="log_only")) is False


def test_cross_instance_gates_are_independent():
    # move consent alone does not arm dedup, and vice-versa (FORK 4 — separate opt-ins).
    move_only = {"routing": {"reorg_mode": "cross_instance"}, "cross_instance_move_consent": True}
    assert cross_instance_move_enabled(move_only) is True
    assert cross_instance_dedup_enabled(move_only) is False
    dedup_only = {"routing": {"reorg_mode": "cross_instance"}, "cross_instance_dedup_consent": True}
    assert cross_instance_dedup_enabled(dedup_only) is True
    assert cross_instance_move_enabled(dedup_only) is False


def test_cross_instance_move_does_not_arm_same_instance_relocation():
    # un-conflation: arming the cross-instance move must NOT enable same-instance folder moves.
    cfg = _move_cfg()
    assert relocation_enabled(cfg) is False
