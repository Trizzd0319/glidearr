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


# ── backup safety + size-anomaly knobs ────────────────────────────────────────
def test_backup_and_size_anomaly_defaults_on_when_unscripted():
    cfg, _ = _run(_FakePrompter(confirms={"deletions_consent": True},
                                integers={"free_space_limit": 2500}))
    assert cfg["backup_before_destructive"] is True
    assert cfg["size_anomaly"]["enabled"] is True
    assert cfg["size_anomaly"]["remediate"] is False        # remediation is opt-in


def test_size_anomaly_remediate_opt_in():
    cfg, _ = _run(_FakePrompter(confirms={
        "deletions_consent": True, "size_anomaly.enabled": True, "size_anomaly.remediate": True,
    }, integers={"free_space_limit": 2500}))
    assert cfg["size_anomaly"]["remediate"] is True


def test_size_anomaly_disabled_skips_remediate_prompt():
    asked: list[str] = []

    class _P(_FakePrompter):
        def confirm(self, path, label, default=False):
            asked.append(path)
            return self.confirms.get(path, default)

    cfg, _ = _run(_P(confirms={"deletions_consent": True, "size_anomaly.enabled": False},
                     integers={"free_space_limit": 2500}))
    assert cfg["size_anomaly"]["enabled"] is False
    assert cfg["size_anomaly"]["remediate"] is False
    assert "size_anomaly.remediate" not in asked            # never offered when detection is off


def test_backup_can_be_declined():
    cfg, _ = _run(_FakePrompter(
        confirms={"deletions_consent": True, "backup_before_destructive": False},
        integers={"free_space_limit": 2500}))
    assert cfg["backup_before_destructive"] is False


def test_headless_leaves_backup_and_size_anomaly_to_schema_defaults():
    cfg, _ = _run(_FakePrompter(interactive=False))
    assert "backup_before_destructive" not in cfg
    assert "size_anomaly" not in cfg
