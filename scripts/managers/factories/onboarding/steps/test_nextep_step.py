"""Tests for the NextEpisodeStep onboarding step — the three modes
(recommended / customize / off) write the expected acquisition.next_episode block.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.nextep import NextEpisodeStep
from scripts.managers.machine_learning.acquisition.next_episode_planner import (
    DEFAULT_BUDGET_RAMP,
    DEFAULT_GRADUATED_CAP,
    DEFAULT_RECENCY_GATE,
)


class _FakePrompter:
    """Scripted prompter: a fixed choice, and confirm/integer answers keyed by path
    (falling back to the prompt's own default when a path isn't scripted).
    choice_val=None returns the prompt's computed default (to exercise default mode).
    Captures notice() text so the customize "second gate" charts can be asserted."""

    def __init__(self, choice_val, confirms=None, integers=None, interactive=True):
        self.choice_val = choice_val
        self.confirms = confirms or {}
        self.integers = integers or {}
        self.is_interactive = interactive
        self.notices: list[str] = []

    def section(self, *a, **k): pass
    def notice(self, msg): self.notices.append(msg)
    def success(self, *a, **k): pass
    def warn(self, *a, **k): pass

    def choice(self, path, label, options, default=None, required=False):
        return default if self.choice_val is None else self.choice_val

    def confirm(self, path, label, default=False):
        return self.confirms.get(path, default)

    def integer(self, path, label, default=0, required=False):
        return self.integers.get(path, default)


def _run(prompter, cfg=None):
    cfg = cfg if cfg is not None else {}
    results = NextEpisodeStep(logger=None).run(prompter, cfg, {})
    return cfg["acquisition"]["next_episode"], results[0]


def _all_notices(p) -> str:
    return "\n".join(p.notices)


def test_recommended_writes_the_default_blocks():
    block, res = _run(_FakePrompter("recommended"))
    assert block["graduated_cap"] == DEFAULT_GRADUATED_CAP
    assert block["recency_gate"] == DEFAULT_RECENCY_GATE
    assert block["budget_ramp"] == DEFAULT_BUDGET_RAMP
    assert res.ok is True and res.skipped is False


def test_off_writes_enabled_false_blocks_to_disable():
    block, res = _run(_FakePrompter("off"))
    # canonical disable = {"enabled": False} (survives the onboarding schema re-merge,
    # unlike a bare {} which deep_merge would clobber back to ON)
    assert block == {
        "graduated_cap": {"enabled": False},
        "recency_gate": {"enabled": False},
        "budget_ramp": {"enabled": False},
    }
    assert res.ok is None and res.skipped is True   # skipped/legacy row in the summary


def test_customize_all_enabled_defaults_matches_recommended():
    # confirm() falls back to the step's default (True) for every enable prompt;
    # integer() falls back to each prompt's recommended default.
    block, res = _run(_FakePrompter("customize"))
    assert block["graduated_cap"]["enabled"] is True
    assert block["graduated_cap"]["base_cap"] == DEFAULT_GRADUATED_CAP["base_cap"]
    assert block["graduated_cap"]["hard_cap"] == DEFAULT_GRADUATED_CAP["hard_cap"]
    assert block["recency_gate"] == DEFAULT_RECENCY_GATE
    assert block["budget_ramp"] == DEFAULT_BUDGET_RAMP
    assert res.ok is True


def test_customize_can_disable_one_feature():
    p = _FakePrompter("customize", confirms={
        "acquisition.next_episode.graduated_cap.enabled": False,  # disable just graduated
    })
    block, _ = _run(p)
    assert block["graduated_cap"] == {"enabled": False}  # disabled -> enabled:False -> legacy cliff
    assert block["recency_gate"]["enabled"] is True      # others stay on
    assert block["budget_ramp"]["enabled"] is True


def test_customize_honours_custom_integers():
    p = _FakePrompter("customize", integers={
        "acquisition.next_episode.graduated_cap.base_cap": 8,
        "acquisition.next_episode.graduated_cap.hard_cap": 30,
        "acquisition.next_episode.recency_gate.cold_days": 45,
    })
    block, _ = _run(p)
    assert block["graduated_cap"]["base_cap"] == 8
    assert block["graduated_cap"]["hard_cap"] == 30
    assert block["recency_gate"]["cold_days"] == 45


# ── reconfigure preserves the prior choice (default mode from existing state) ────
def test_reconfigure_off_config_defaults_to_off():
    # a previously-disabled config: choice_val=None returns the step's computed default,
    # which must be "off" so a reconfigure (Enter / headless) doesn't silently re-enable.
    cfg = {"acquisition": {"next_episode": {
        "graduated_cap": {"enabled": False},
        "recency_gate": {"enabled": False},
        "budget_ramp": {"enabled": False},
    }}}
    block, res = _run(_FakePrompter(None), cfg=cfg)
    assert all(block[k] == {"enabled": False} for k in ("graduated_cap", "recency_gate", "budget_ramp"))
    assert res.skipped is True


# ── detailed charted guidance: shown once before the choice, in EVERY mode ──────
def test_charts_shown_before_choice_in_every_mode():
    for mode in ("recommended", "customize", "off"):
        p = _FakePrompter(mode)
        _run(p)
        text = _all_notices(p)
        assert "GRADUATED CAP" in text and "RECENCY GATE" in text and "BUDGET RAMP" in text
        assert "max episodes prefetched" in text       # a chart axis label
        assert "prefetch budget" in text               # the budget chart
        assert "EpisodeSearch" in text                 # the search-impact framing


def test_charts_shown_in_headless_setup_too():
    # headless (Docker/CI) setup logs should ALSO carry the guidance — no interactive gate.
    p = _FakePrompter("recommended", interactive=False)
    _run(p)
    assert "GRADUATED CAP" in _all_notices(p)


def test_feature_off_mirrors_runtime_enabled_truthiness():
    off = NextEpisodeStep._feature_off
    assert off(None) and off({}) and off({"enabled": False})
    assert off({"base_cap": 8})            # enabled-less partial → OFF, like the runtime
    assert not off({"enabled": True})
    assert not off({"enabled": True, "base_cap": 8})


def test_absent_block_defaults_to_recommended_not_off():
    # a fresh/empty block (no keys) must default to recommended (active-by-default),
    # NOT off — absent != explicitly disabled.
    block, res = _run(_FakePrompter(None), cfg={})
    assert block["graduated_cap"] == DEFAULT_GRADUATED_CAP and res.ok is True


def test_reconfigure_on_config_defaults_to_recommended():
    cfg = {"acquisition": {"next_episode": {
        "graduated_cap": dict(DEFAULT_GRADUATED_CAP),
        "recency_gate": dict(DEFAULT_RECENCY_GATE),
        "budget_ramp": dict(DEFAULT_BUDGET_RAMP),
    }}}
    block, res = _run(_FakePrompter(None), cfg=cfg)
    assert block["graduated_cap"] == DEFAULT_GRADUATED_CAP
    assert res.ok is True


def test_disable_survives_schema_deep_merge():
    # H1 regression: an explicit {"enabled": False} disable must NOT be resurrected to
    # ON when onboarding lays the existing config over the recommended skeleton.
    from scripts.managers.factories.onboarding import schema
    existing = {"acquisition": {"next_episode": {"graduated_cap": {"enabled": False}}}}
    merged = schema.deep_merge(schema.empty_config(), existing)
    assert merged["acquisition"]["next_episode"]["graduated_cap"]["enabled"] is False
    # a bare {} would be clobbered back to the ON skeleton — documents why we use enabled:False
    merged_empty = schema.deep_merge(schema.empty_config(), {"acquisition": {"next_episode": {"graduated_cap": {}}}})
    assert merged_empty["acquisition"]["next_episode"]["graduated_cap"].get("enabled") is True
