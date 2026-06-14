"""Tests for the EnglishDubStep onboarding step — the three modes
(recommended / customize / off) write the expected english_dub block, the detailed
guidance shows in every mode, and an explicit disable survives the schema re-merge.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.english_dub import EnglishDubStep

_PIECE_KEYS = ("cf_scoring", "theatrical_seek", "english_ladder", "lock_owned_dubs", "auto_enroll")


class _FakePrompter:
    """Scripted prompter: a fixed choice, confirm answers keyed by path (falling back
    to the prompt's own default). choice_val=None returns the prompt's computed default
    (to exercise reconfigure-default mode). Captures notice() text for guidance asserts."""

    def __init__(self, choice_val, confirms=None, interactive=True):
        self.choice_val = choice_val
        self.confirms = confirms or {}
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


def _run(prompter, cfg=None):
    cfg = cfg if cfg is not None else {}
    results = EnglishDubStep(logger=None).run(prompter, cfg, {})
    return cfg["english_dub"], results[0]


def _all_notices(p) -> str:
    return "\n".join(p.notices)


def test_recommended_enables_all_five_pieces():
    block, res = _run(_FakePrompter("recommended"))
    assert block == {k: {"enabled": True} for k in _PIECE_KEYS}
    assert res.ok is True and res.skipped is False and "5/5" in res.detail


def test_off_writes_enabled_false_for_every_piece():
    block, res = _run(_FakePrompter("off"))
    # canonical disable = {"enabled": False} per piece (consistent with nextep).
    assert block == {k: {"enabled": False} for k in _PIECE_KEYS}
    assert res.ok is None and res.skipped is True


def test_customize_all_defaults_matches_recommended():
    block, res = _run(_FakePrompter("customize"))
    assert all(block[k]["enabled"] is True for k in _PIECE_KEYS)
    assert res.ok is True and "5/5" in res.detail


def test_customize_can_disable_one_piece():
    p = _FakePrompter("customize", confirms={"english_dub.auto_enroll.enabled": False})
    block, res = _run(p)
    assert block["auto_enroll"] == {"enabled": False}
    assert all(block[k]["enabled"] is True for k in _PIECE_KEYS if k != "auto_enroll")
    assert "4/5" in res.detail


def test_absent_block_defaults_to_recommended_not_off():
    # absent != disabled — a fresh config must default to recommended (all on).
    block, res = _run(_FakePrompter(None), cfg={})
    assert all(block[k]["enabled"] is True for k in _PIECE_KEYS) and res.ok is True


def test_reconfigure_off_config_defaults_to_off():
    # a previously all-disabled block: the computed default must be "off" so a
    # reconfigure (Enter / headless) doesn't silently re-enable.
    cfg = {"english_dub": {k: {"enabled": False} for k in _PIECE_KEYS}}
    block, res = _run(_FakePrompter(None), cfg=cfg)
    assert all(block[k] == {"enabled": False} for k in _PIECE_KEYS)
    assert res.skipped is True


def test_feature_off_mirrors_runtime_enabled_truthiness():
    off = EnglishDubStep._feature_off
    assert off(None) and off({}) and off({"enabled": False})
    assert not off({"enabled": True})


def test_detailed_guidance_shown_in_every_mode():
    for mode in ("recommended", "customize", "off"):
        p = _FakePrompter(mode)
        _run(p)
        t = _all_notices(p)
        assert "CUSTOM-FORMAT SCORING" in t and "ENGLISH PROFILE LADDER" in t
        assert "AUTO-ENROLL ALL FOREIGN FILMS" in t and "LOCK OWNED DUBS" in t
        assert "_audio_lang_apply.py" in t and "_english_autoenroll.py" in t  # apply commands


def test_guidance_shown_in_headless_too():
    p = _FakePrompter("recommended", interactive=False)
    _run(p)
    assert "CUSTOM-FORMAT SCORING" in _all_notices(p)


def test_disable_survives_schema_deep_merge():
    # english_dub is NOT in the skeleton, so deep_merge must preserve whatever the step
    # wrote (both an explicit disable and an enable) — nothing clobbers it back.
    from scripts.managers.factories.onboarding import schema
    existing = {"english_dub": {"auto_enroll": {"enabled": False}}}
    merged = schema.deep_merge(schema.empty_config(), existing)
    assert merged["english_dub"]["auto_enroll"]["enabled"] is False
