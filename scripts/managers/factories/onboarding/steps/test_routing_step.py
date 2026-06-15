"""Tests for RoutingStep — captures 4K/anime routing prefs + reorg_mode, INLINE-captures
the kids/anime/4K root folders (pre-selecting an existing matching folder as the default),
branch-skips inapplicable prompts, asks for relocation_consent only on same_instance
interactive, and ends with a resolved-routing summary that flags buckets with no folder."""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.routing import RoutingStep, _auto_match
from scripts.managers.factories.onboarding.steps import STEP_CLASSES, build_steps, step_names


class _FakePrompter:
    """Scripted prompter: choice/confirm/integer/text answers keyed by config path; an
    unscripted path returns the prompt's computed default (which is how the auto-detected
    folder becomes the captured value)."""

    def __init__(self, choices=None, confirms=None, integers=None, texts=None, interactive=True):
        self.choices = choices or {}
        self.confirms = confirms or {}
        self.integers = integers or {}
        self.texts = texts or {}
        self.is_interactive = interactive
        self.notices: list[str] = []

    def section(self, *a, **k): pass
    def notice(self, msg): self.notices.append(msg)
    def success(self, *a, **k): pass
    def warn(self, msg=""): self.notices.append(f"WARN:{msg}")

    def choice(self, path, label, options, default=None, required=False):
        return self.choices.get(path, default)

    def confirm(self, path, label, default=False):
        return self.confirms.get(path, default)

    def integer(self, path, label, default=0, required=False):
        return self.integers.get(path, default)

    def text(self, path, label, default="", required=False, secret=False):
        return self.texts.get(path, default)


def _run(prompter, cfg=None, ctx=None):
    cfg = cfg if cfg is not None else {}
    res = RoutingStep(logger=None).run(prompter, cfg, ctx if ctx is not None else {})
    return cfg, res[0]


def _notices(p) -> str:
    return "\n".join(p.notices)


# ── registration ─────────────────────────────────────────────────────────────
def test_registered_and_runnable_as_service():
    assert "routing" in step_names()
    assert RoutingStep in STEP_CLASSES
    only = build_steps(only_service="routing")
    assert len(only) == 1 and only[0].name == "routing"


# ── auto-detect: an existing kids/anime/uhd folder becomes the default ─────────
def test_auto_match_picks_folder_by_leaf_name():
    folders = ["/data/movies", "/data/movies-kids", "/data/anime", "/tank/uhd-films"]
    assert _auto_match(folders, ("kids", "child")) == "/data/movies-kids"
    assert _auto_match(folders, ("anime",)) == "/data/anime"
    assert _auto_match(folders, ("4k", "uhd", "2160")) == "/tank/uhd-films"
    assert _auto_match(folders, ("documentary",)) is None


def test_auto_detects_kids_folder_as_default():
    ctx = {"root_folders": ["/data/movies", "/data/movies-kids", "/data/anime"]}
    p = _FakePrompter(confirms={"routing.movies.kids_bucket_enabled": True})
    cfg, _ = _run(p, ctx=ctx)
    assert cfg["movieRootFolders"]["kids"] == "/data/movies-kids"     # auto-detected, used as default
    assert "Auto-detected" in _notices(p)


def test_auto_detects_4k_folder_when_dual_version():
    ctx = {"root_folders": ["/data/movies", "/data/movies-uhd"]}
    cfg = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}, "ultra": {}},
           "radarr_instances_categorized": {"4K": "ultra"}}
    p = _FakePrompter(choices={"routing.movies.4k_policy": "both"})
    cfg, _ = _run(p, cfg, ctx=ctx)
    assert cfg["movieRootFolders"]["4k"] == "/data/movies-uhd"        # matched "uhd"


def test_inline_capture_overrides_with_explicit_choice():
    ctx = {"root_folders": ["/data/movies", "/data/movies-kids", "/data/kids2"]}
    p = _FakePrompter(confirms={"routing.movies.kids_bucket_enabled": True},
                      choices={"movieRootFolders.kids": "/data/kids2"})   # operator picks a different one
    cfg, _ = _run(p, ctx=ctx)
    assert cfg["movieRootFolders"]["kids"] == "/data/kids2"


# ── defaults preserve today's behaviour ──────────────────────────────────────
def test_empty_cfg_writes_defaults():
    cfg, res = _run(_FakePrompter())
    r = cfg["routing"]
    assert r["movies"]["4k_policy"] == "highest_only"
    assert r["movies"]["anime_policy"] == "dedicated"
    assert r["tv"]["anime_policy"] == "series_type"
    assert r["tv"]["4k_enabled"] is False
    assert r["reorg_mode"] == "log_only"
    assert cfg.get("relocation_consent", False) is False
    assert res.ok is True and res.service == "routing"


# ── branch-skip + capture ─────────────────────────────────────────────────────
def test_4k_policy_offered_when_distinct_4k_instance():
    cfg = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}, "ultra": {}},
           "radarr_instances_categorized": {"4K": "ultra"}}
    p = _FakePrompter(choices={"routing.movies.4k_policy": "both"},
                      integers={"routing.movies.4k_dual_min_score": 40},
                      texts={"movieRootFolders.4k": "/data/4k"})
    cfg, _ = _run(p, cfg)
    assert cfg["routing"]["movies"]["4k_policy"] == "both"
    assert cfg["routing"]["movies"]["4k_dual_min_score"] == 40
    assert cfg["movieRootFolders"]["4k"] == "/data/4k"


def test_4k_policy_skipped_when_4k_maps_to_default():
    cfg = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}},
           "radarr_instances_categorized": {"4K": "standard"}}
    p = _FakePrompter(choices={"routing.movies.4k_policy": "both"})
    cfg, _ = _run(p, cfg)
    assert cfg["routing"]["movies"]["4k_policy"] == "highest_only"


def test_anime_standard_only_skips_folder_capture():
    cfg = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}, "anime": {}},
           "radarr_instances_categorized": {"anime": "anime"}}
    cfg, res = _run(_FakePrompter(choices={"routing.movies.anime_policy": "standard_only"}), cfg)
    assert cfg["routing"]["movies"]["anime_policy"] == "standard_only"
    # standard_only routes anime to standard → no anime folder needed, not flagged missing
    assert res.ok is True


# ── reorg_mode + relocation consent ──────────────────────────────────────────
def test_same_instance_with_consent_arms_relocation():
    p = _FakePrompter(choices={"routing.reorg_mode": "same_instance"},
                      confirms={"relocation_consent": True})
    cfg, res = _run(p)
    assert cfg["routing"]["reorg_mode"] == "same_instance"
    assert cfg["relocation_consent"] is True
    assert res.ok is True and "same_instance" in res.detail


def test_same_instance_without_consent_warns():
    p = _FakePrompter(choices={"routing.reorg_mode": "same_instance"},
                      confirms={"relocation_consent": False})
    cfg, res = _run(p)
    assert cfg["relocation_consent"] is False
    assert res.ok is False


def test_headless_same_instance_does_not_prompt_consent():
    p = _FakePrompter(choices={"routing.reorg_mode": "same_instance"},
                      confirms={"relocation_consent": True}, interactive=False)
    cfg, res = _run(p)
    assert "relocation_consent" not in cfg
    assert res.ok is False


def test_off_mode_is_skipped_row():
    cfg, res = _run(_FakePrompter(choices={"routing.reorg_mode": "off"}))
    assert cfg["routing"]["reorg_mode"] == "off"
    assert res.ok is None


# ── summary flags a bucket with no destination folder ────────────────────────
def test_summary_flags_missing_kids_folder():
    # kids enabled but no folder discovered or set → flagged, ok=False, warned
    p = _FakePrompter(confirms={"routing.movies.kids_bucket_enabled": True})
    cfg, res = _run(p)
    assert cfg["routing"]["movies"]["kids_bucket_enabled"] is True
    assert not cfg["movieRootFolders"].get("kids")
    assert res.ok is False and "kids" in res.detail
    assert "WARN:" in _notices(p)


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
    assert cfg["sonarr_instances_categorized"] == {}
    assert cfg["routing"]["tv"]["dual_version"] == "highest_only"


# ── idempotent re-read: existing values become the defaults ──────────────────
def test_reconfigure_preserves_prior_values():
    cfg = {"routing": {"movies": {"4k_policy": "highest_only", "anime_policy": "standard_only"},
                       "tv": {"anime_policy": "series_type"},
                       "reorg_mode": "off"}}
    cfg, _ = _run(_FakePrompter(), cfg)
    assert cfg["routing"]["movies"]["anime_policy"] == "standard_only"
    assert cfg["routing"]["tv"]["anime_policy"] == "series_type"
    assert cfg["routing"]["reorg_mode"] == "off"
