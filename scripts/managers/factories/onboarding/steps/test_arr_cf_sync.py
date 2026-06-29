"""ArrStep._collect_cf_sync — the cross-instance CF/QP sync confirmations shown when a service has
2+ sessions. Proves it auto-ons (no explicit flag) on confirm, records the source-of-truth session,
arms overwrite + consent only when asked, opts out cleanly, and is silent for a single session or
headless."""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.arr import RadarrStep


class _FakePrompter:
    def __init__(self, choices=None, confirms=None, interactive=True):
        self.choices = choices or {}
        self.confirms = confirms or {}
        self.is_interactive = interactive
        self.notices: list[str] = []

    def section(self, *a, **k): pass
    def notice(self, msg): self.notices.append(msg)
    def choice(self, path, label, options, default=None, required=False): return self.choices.get(path, default)
    def confirm(self, path, label, default=False): return self.confirms.get(path, default)


def _collect(prompter, cfg, names):
    RadarrStep(logger=None)._collect_cf_sync(prompter, cfg, names)
    return cfg


def _cfg(default="standard"):
    return {"radarr_instances": {"default_instance": {"name": default},
                                 "standard": {}, "ultra": {}, "test": {}}}


def test_confirm_enables_auto_sync_fill_only():
    p = _FakePrompter(confirms={"radarr.cf_sync.enabled": True, "radarr.cf_sync.overwrite": False},
                      choices={"radarr.cf_sync.source_instance": "standard"})
    cs = _collect(p, _cfg(), ["standard", "ultra", "test"])["scoring"]["cf_sync"]
    assert "enabled" not in cs                          # auto-on, no explicit flag
    assert cs["source_instance"] == "standard"
    assert cs["overwrite_existing"] is False
    assert p.notices                                    # explained the feature


def test_opt_out_sets_enabled_false_and_stops():
    cfg = _collect(_FakePrompter(confirms={"radarr.cf_sync.enabled": False}), _cfg(), ["standard", "ultra"])
    cs = cfg["scoring"]["cf_sync"]
    assert cs["enabled"] is False
    assert "source_instance" not in cs                  # stopped before the source prompt
    assert "cf_sync_overwrite_consent" not in cfg


def test_overwrite_arms_flag_and_consent():
    p = _FakePrompter(confirms={"radarr.cf_sync.enabled": True, "radarr.cf_sync.overwrite": True},
                      choices={"radarr.cf_sync.source_instance": "ultra"})
    cfg = _collect(p, _cfg(), ["standard", "ultra"])
    assert cfg["scoring"]["cf_sync"]["overwrite_existing"] is True
    assert cfg["cf_sync_overwrite_consent"] is True
    assert cfg["scoring"]["cf_sync"]["source_instance"] == "ultra"


def test_include_test_opt_out():
    p = _FakePrompter(confirms={"radarr.cf_sync.enabled": True, "radarr.cf_sync.include_test": False,
                                "radarr.cf_sync.overwrite": False},
                      choices={"radarr.cf_sync.source_instance": "standard"})
    cfg = _collect(p, _cfg(), ["standard", "ultra", "test"])
    assert cfg["scoring"]["cf_sync"]["include_test"] is False


def test_single_session_is_silent():
    cfg = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}}}
    p = _FakePrompter()
    _collect(p, cfg, ["standard"])
    assert "scoring" not in cfg and p.notices == []     # nothing to sync → no prompt


def test_headless_is_silent():
    cfg = _cfg()
    _collect(_FakePrompter(interactive=False), cfg, ["standard", "ultra"])
    assert "scoring" not in cfg                          # headless uses schema defaults + env consent


def test_source_default_is_the_default_session():
    # source-of-truth pre-selects the default session when the operator doesn't pick one
    p = _FakePrompter(confirms={"radarr.cf_sync.enabled": True, "radarr.cf_sync.overwrite": False})
    cfg = _collect(p, _cfg(default="ultra"), ["standard", "ultra", "test"])
    assert cfg["scoring"]["cf_sync"]["source_instance"] == "ultra"   # the default session
