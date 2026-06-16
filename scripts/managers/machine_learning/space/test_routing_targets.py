"""Tests for routing_targets — the re-organizer (file relocation) gate, mirroring the
deletions consent gate in space_targets. Consent off by default; actuation requires BOTH
consent AND reorg_mode=="same_instance"; log_only/off never move files."""
from __future__ import annotations

import pytest

from scripts.managers.machine_learning.space.routing_targets import (
    proactive_4k_enabled,
    relocation_consented,
    relocation_enabled,
    reorg_mode,
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
