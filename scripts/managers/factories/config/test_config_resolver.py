"""Tests for ConfigResolver.get_default_instance — the default-instance lookup.

Regression focus: the live config stores ``default_instance`` as a dict
``{"name": "<instance>"}`` (e.g. sonarr → ``{"name": "sonarr"}``, radarr →
``{"name": "standard"}``). The old implementation passed that dict straight to
``all_instances.get(default, {})``, which raises ``TypeError: unhashable type:
'dict'``. The fix pulls the name out of the marker first and falls back to the
first real instance entry, mirroring ArrGateway.default_instance /
SonarrInstanceManager.get_default_instance.
"""
from __future__ import annotations

from scripts.managers.factories.config.config_resolver import ConfigResolver
from scripts.managers.factories.config import ConfigManager


def _resolver(config: dict) -> ConfigResolver:
    # ConfigResolver only ever calls config.get(...); a plain dict suffices and
    # the logger is never touched by get_default_instance.
    return ConfigResolver(config, logger=None)


# ── the dict marker shape (the actual live config / the bug repro) ────────────────
def test_default_instance_dict_marker_returns_subdict():
    cfg = {
        "sonarr_instances": {
            "default_instance": {"name": "sonarr"},   # dict marker — used to raise TypeError
            "sonarr": {"url": "http://sonarr", "apiKey": "abc"},
        }
    }
    assert _resolver(cfg).get_default_instance("sonarr") == {"url": "http://sonarr", "apiKey": "abc"}


def test_default_instance_dict_marker_multi_instance_honours_configured_name():
    # Radarr is multi-instance — the configured default must win, not dict order.
    cfg = {
        "radarr_instances": {
            "default_instance": {"name": "ultra"},
            "standard": {"url": "http://standard"},
            "ultra": {"url": "http://ultra"},
        }
    }
    assert _resolver(cfg).get_default_instance("radarr") == {"url": "http://ultra"}


# ── legacy bare-string shape (pre-collapse configs) ──────────────────────────────
def test_default_instance_bare_string_marker_returns_subdict():
    cfg = {
        "sonarr_instances": {
            "default_instance": "720",
            "720": {"url": "http://720"},
        }
    }
    assert _resolver(cfg).get_default_instance("sonarr") == {"url": "http://720"}


# ── fallbacks ────────────────────────────────────────────────────────────────────
def test_missing_default_falls_back_to_first_real_instance():
    cfg = {"sonarr_instances": {"sonarr": {"url": "http://sonarr"}}}
    assert _resolver(cfg).get_default_instance("sonarr") == {"url": "http://sonarr"}


def test_default_points_at_unknown_name_falls_back_to_first_real_instance():
    cfg = {
        "radarr_instances": {
            "default_instance": {"name": "gone"},     # not present
            "standard": {"url": "http://standard"},
        }
    }
    assert _resolver(cfg).get_default_instance("radarr") == {"url": "http://standard"}


def test_empty_or_absent_instances_return_empty_dict():
    assert _resolver({}).get_default_instance("sonarr") == {}
    assert _resolver({"sonarr_instances": {}}).get_default_instance("sonarr") == {}
    # only the marker, no real entries
    assert _resolver(
        {"sonarr_instances": {"default_instance": {"name": "sonarr"}}}
    ).get_default_instance("sonarr") == {}


# ── caller wiring: ConfigManager.get_default_sonarr_instance delegates here ───────
def test_config_manager_get_default_sonarr_instance_delegates():
    # Build a bare ConfigManager and attach a resolver directly, avoiding the
    # heavy load()/SecretBootstrap path — we only assert the delegation contract.
    cm = ConfigManager.__new__(ConfigManager)
    cm.resolver = _resolver(
        {"sonarr_instances": {"default_instance": {"name": "sonarr"}, "sonarr": {"url": "http://sonarr"}}}
    )
    assert cm.get_default_sonarr_instance() == {"url": "http://sonarr"}
