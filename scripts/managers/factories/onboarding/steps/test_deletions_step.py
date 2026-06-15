"""Tests for the DeletionsStep onboarding step — explicit deletion consent + floor."""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.deletions import DeletionsStep


class _FakePrompter:
    def __init__(self, confirms=None, integers=None, interactive=True):
        self.confirms = confirms or {}
        self.integers = integers or {}
        self.is_interactive = interactive
        self.notices: list[str] = []

    def section(self, *a, **k): pass
    def notice(self, msg): self.notices.append(msg)
    def confirm(self, path, label, default=False): return self.confirms.get(path, default)
    def integer(self, path, label, default=0, required=False): return self.integers.get(path, default)


def _run(prompter, cfg=None):
    cfg = cfg if cfg is not None else {}
    res = DeletionsStep(logger=None).run(prompter, cfg, {})
    return cfg, res[0]


def test_consent_with_floor_arms_deletion():
    cfg, res = _run(_FakePrompter(confirms={"deletions_consent": True},
                                  integers={"free_space_limit": 2500}))
    assert cfg["deletions_consent"] is True
    assert cfg["free_space_limit"] == 2500
    assert res.ok is True and "ARMED" in res.detail


def test_consent_without_floor_stays_off():
    cfg, res = _run(_FakePrompter(confirms={"deletions_consent": True},
                                  integers={"free_space_limit": 0}))
    assert cfg["deletions_consent"] is True and cfg["free_space_limit"] == 0
    assert res.ok is False                      # consented but not armed


def test_decline_keeps_floor_for_acquisition_pause():
    cfg, res = _run(_FakePrompter(confirms={"deletions_consent": False},
                                  integers={"free_space_limit": 1000}))
    assert cfg["deletions_consent"] is False and cfg["free_space_limit"] == 1000
    assert res.skipped is True and "1000" in res.detail


def test_default_is_no_consent():
    cfg, _ = _run(_FakePrompter())              # unscripted confirm -> default False
    assert cfg["deletions_consent"] is False


def test_headless_skips_and_points_to_env_var():
    cfg, res = _run(_FakePrompter(interactive=False))
    assert "deletions_consent" not in cfg       # never defaulted on headlessly
    assert res.skipped is True and "RECOMMENDARR_DELETIONS_CONSENT" in res.detail


def test_explanation_always_shown():
    p = _FakePrompter()
    _run(p)
    text = "\n".join(p.notices)
    assert "DELETE" in text and "downgrade" in text.lower() and "dry_run" in text
