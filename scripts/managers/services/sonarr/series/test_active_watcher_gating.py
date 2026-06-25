"""The fixed active-watcher target selection: codec variants are never auto-upgrade targets, and the
resolution is capped by the recalibrated likelihood curve (single watch → 1080p, regular rewatch /
hot-universe → 2160p)."""
from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    resolution_cap_for_likelihood,
    watch_likelihood,
)
from scripts.managers.services.sonarr.series.quality import (
    _codec_variant,
    capped_target,
    select_upgrade_targets,
)


def _p(pid, name, res):
    return {"id": pid, "name": name, "items": [{"allowed": True, "quality": {"resolution": res}}]}


PROFILES = [
    _p(1, "HD-720p", 720),
    _p(2, "WEB-1080p", 1080),
    _p(3, "WEB-2160p (Combined)", 2160),
    _p(4, "WEB-1080p (AV1)", 1080),
    _p(5, "WEB-2160p (Combined) (AV1)", 2160),
    _p(6, "WEB-2160p (Combined) (HEVC)", 2160),
    _p(7, "[Anime] HD-1080p", 1080),
    _p(8, "[Anime] Ultra-HD", 2160),
]


def test_codec_variant_detection():
    assert _codec_variant({"name": "WEB-2160p (Combined) (AV1)"})
    assert _codec_variant({"name": "WEB-1080p (HEVC-DV)"})
    assert not _codec_variant({"name": "WEB-2160p (Combined)"})
    assert not _codec_variant({"name": "[Anime] Ultra-HD"})


def test_select_upgrade_targets_excludes_codec_variants():
    best_standard, best_anime = select_upgrade_targets(PROFILES)
    assert best_standard["name"] == "WEB-2160p (Combined)"      # agnostic 2160p, NOT the (AV1) twin
    assert best_anime["name"] == "[Anime] Ultra-HD"


def test_capped_target_respects_resolution_cap_and_excludes_variants():
    assert capped_target(PROFILES, is_anime=False, max_res=1080)["name"] == "WEB-1080p"
    assert capped_target(PROFILES, is_anime=False, max_res=2160)["name"] == "WEB-2160p (Combined)"
    assert capped_target(PROFILES, is_anime=False, max_res=720)["name"] == "HD-720p"
    assert capped_target(PROFILES, is_anime=True, max_res=1080)["name"] == "[Anime] HD-1080p"


def test_single_watch_caps_at_1080p_agnostic():
    # The Abbott & Costello case: 1 ep watched → likelihood 50 → 1080 cap → agnostic WEB-1080p
    L = watch_likelihood({"watch_count": 1}, config=None)
    cap = resolution_cap_for_likelihood(L, config=None)
    assert cap == 1080
    assert capped_target(PROFILES, is_anime=False, max_res=cap)["name"] == "WEB-1080p"


def test_regular_rewatch_reaches_2160p_agnostic():
    L = watch_likelihood({"watch_count": 3}, config=None)
    assert resolution_cap_for_likelihood(L, config=None) == 2160
    assert capped_target(PROFILES, is_anime=False, max_res=2160)["name"] == "WEB-2160p (Combined)"


def test_single_watch_plus_hot_universe_reaches_2160p():
    L = watch_likelihood({"watch_count": 1, "universe_credit": 2.0}, config=None)
    cap = resolution_cap_for_likelihood(L, config=None)
    assert cap == 2160
    assert capped_target(PROFILES, is_anime=False, max_res=cap)["name"] == "WEB-2160p (Combined)"
