"""Tests for RoutingStep — captures 4K/anime routing prefs + reorg_mode, branch-skips
prompts that don't apply (no 4K/anime instance), and asks for relocation_consent only
when same_instance is chosen interactively. Defaults always preserve today's behaviour."""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.routing import RoutingStep
from scripts.managers.factories.onboarding.steps import STEP_CLASSES, build_steps, step_names


class _FakePrompter:
    """Scripted prompter: choice/confirm/integer answers keyed by config path; an
    unscripted path returns the prompt's own computed default (exercises defaults)."""

    def __init__(self, choices=None, confirms=None, integers=None, interactive=True):
        self.choices = choices or {}
        self.confirms = confirms or {}
        self.integers = integers or {}
        self.is_interactive = interactive
        self.notices: list[str] = []

    def section(self, *a, **k): pass
    def notice(self, msg): self.notices.append(msg)
    def success(self, *a, **k): pass
    def warn(self, *a, **k): pass

    def choice(self, path, label, options, default=None, required=False):
        return self.choices.get(path, default)

    def confirm(self, path, label, default=False):
        return self.confirms.get(path, default)

    def integer(self, path, label, default=0, required=False):
        return self.integers.get(path, default)


def _run(prompter, cfg=None):
    cfg = cfg if cfg is not None else {}
    res = RoutingStep(logger=None).run(prompter, cfg, {})
    return cfg, res[0]


# ── registration ─────────────────────────────────────────────────────────────
def test_registered_and_runnable_as_service():
    assert "routing" in step_names()
    assert RoutingStep in STEP_CLASSES
    only = build_steps(only_service="routing")
    assert len(only) == 1 and only[0].name == "routing"


# ── defaults preserve today's behaviour ──────────────────────────────────────
def test_empty_cfg_writes_defaults():
    cfg, res = _run(_FakePrompter())
    r = cfg["routing"]
    assert r["movies"]["4k_policy"] == "highest_only"
    assert r["movies"]["anime_policy"] == "dedicated"
    assert r["tv"]["anime_policy"] == "series_type"
    assert r["tv"]["4k_enabled"] is False
    assert r["reorg_mode"] == "log_only"
    assert cfg.get("relocation_consent", False) is False    # never armed without same_instance
    assert res.ok is True and res.service == "routing"


# ── 4K dual-version only offered when a DISTINCT 4K instance is mapped ────────
def test_4k_policy_offered_when_distinct_4k_instance():
    cfg = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}, "ultra": {}},
           "radarr_instances_categorized": {"4K": "ultra"}}
    p = _FakePrompter(choices={"routing.movies.4k_policy": "both"},
                      integers={"routing.movies.4k_dual_min_score": 40})
    cfg, _ = _run(p, cfg)
    assert cfg["routing"]["movies"]["4k_policy"] == "both"
    assert cfg["routing"]["movies"]["4k_dual_min_score"] == 40


def test_4k_policy_skipped_when_4k_maps_to_default():
    # "4K" maps to the default instance → not distinct → prompt skipped, stays highest_only
    cfg = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}},
           "radarr_instances_categorized": {"4K": "standard"}}
    p = _FakePrompter(choices={"routing.movies.4k_policy": "both"})   # would pick both IF asked
    cfg, _ = _run(p, cfg)
    assert cfg["routing"]["movies"]["4k_policy"] == "highest_only"


# ── anime policy only when an anime instance is mapped ───────────────────────
def test_anime_policy_offered_when_anime_instance_mapped():
    cfg = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}, "anime": {}},
           "radarr_instances_categorized": {"anime": "anime"}}
    cfg, _ = _run(_FakePrompter(choices={"routing.movies.anime_policy": "standard_only"}), cfg)
    assert cfg["routing"]["movies"]["anime_policy"] == "standard_only"


# ── reorg_mode + relocation consent ──────────────────────────────────────────
def test_same_instance_with_consent_arms_relocation():
    p = _FakePrompter(choices={"routing.reorg_mode": "same_instance"},
                      confirms={"relocation_consent": True})
    cfg, res = _run(p)
    assert cfg["routing"]["reorg_mode"] == "same_instance"
    assert cfg["relocation_consent"] is True
    assert res.ok is True and "ARMED" in res.detail


def test_same_instance_without_consent_warns():
    p = _FakePrompter(choices={"routing.reorg_mode": "same_instance"},
                      confirms={"relocation_consent": False})
    cfg, res = _run(p)
    assert cfg["relocation_consent"] is False
    assert res.ok is False


def test_headless_same_instance_does_not_prompt_consent():
    # headless: relocation consent comes from the env var at runtime, never prompted here
    p = _FakePrompter(choices={"routing.reorg_mode": "same_instance"},
                      confirms={"relocation_consent": True}, interactive=False)
    cfg, res = _run(p)
    assert "relocation_consent" not in cfg
    assert res.ok is False


def test_off_mode_is_skipped_row():
    cfg, res = _run(_FakePrompter(choices={"routing.reorg_mode": "off"}))
    assert cfg["routing"]["reorg_mode"] == "off"
    assert res.ok is None


# ── TV 4K instance mapping ───────────────────────────────────────────────────
def test_tv_4k_maps_instance_when_two_sonarr_sessions():
    cfg = {"sonarr_instances": {"default_instance": {"name": "sonarr"}, "sonarr": {}, "sonarr4k": {}}}
    p = _FakePrompter(confirms={"routing.tv.4k_enabled": True},
                      choices={"sonarr_instances_categorized.4k": "sonarr4k",
                               "routing.tv.dual_version": "both"})
    cfg, _ = _run(p, cfg)
    assert cfg["sonarr_instances_categorized"]["4k"] == "sonarr4k"
    assert cfg["routing"]["tv"]["dual_version"] == "both"


def test_tv_4k_enabled_but_one_session_notes_and_skips_mapping():
    cfg = {"sonarr_instances": {"default_instance": {"name": "sonarr"}, "sonarr": {}}}
    p = _FakePrompter(confirms={"routing.tv.4k_enabled": True})
    cfg, _ = _run(p, cfg)
    assert cfg["sonarr_instances_categorized"] == {}        # nothing mapped with <2 sessions
    assert cfg["routing"]["tv"]["dual_version"] == "highest_only"


# ── idempotent re-read: existing values become the defaults ──────────────────
def test_reconfigure_preserves_prior_values():
    cfg = {"routing": {"movies": {"4k_policy": "highest_only", "anime_policy": "standard_only"},
                       "tv": {"anime_policy": "series_type_plus_folder"},
                       "reorg_mode": "off"}}
    cfg, _ = _run(_FakePrompter(), cfg)   # unscripted → each prompt returns its existing-value default
    assert cfg["routing"]["movies"]["anime_policy"] == "standard_only"
    assert cfg["routing"]["tv"]["anime_policy"] == "series_type_plus_folder"
    assert cfg["routing"]["reorg_mode"] == "off"
