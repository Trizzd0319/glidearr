"""Tests for the plex.playlists schema block + its headless env contract.

The block must be fully-keyed and DEFAULT-INERT: every captured knob equals the
literal default the playlist builder hard-codes inline today, so laying the
skeleton over an existing config (deep_merge) is a no-op and behaviour is
byte-identical. The new write-back / recency-boost knobs are off and currently
UNREAD — pure placeholders. ``env_map`` exposes the headless-relevant subset.
"""
from __future__ import annotations

from scripts.managers.factories.config.secret_store import env_name
from scripts.managers.factories.onboarding import env_map
from scripts.managers.factories.onboarding.schema import deep_merge, empty_config
from scripts.managers.services.plex.playlists.builder import PlexPlaylistBuilderManager


def _playlists(cfg: dict) -> dict:
    return cfg["plex"]["playlists"]


# ── schema shape ──────────────────────────────────────────────────────────────
def test_playlists_block_is_fully_keyed_and_inert():
    pl = _playlists(empty_config())
    # New write-back knobs exist and are OFF by default.
    assert pl["writeback"] == {"enabled": False}
    assert pl["recency_boost"] == {"enabled": False, "window_days": 30}
    assert pl["fresh_arrivals"] == {"enabled": False, "acquired_window_days": 45}
    assert pl["home_collections"] == {"enabled": False, "promote_home": True, "promote_shared": False}
    # Captured builder knobs mirror the inline defaults.
    assert pl["max_items"] == 100
    assert pl["episode_cap"] == 5
    assert pl["genre_match_mode"] == "precision"
    assert pl["genre_match_soft_lambda"] == 0.5
    assert pl["genre_match_blend_weight"] == 0.85
    assert pl["affinity_weight"] == 0.9
    assert pl["household_weight"] == 0.1
    assert pl["jit_weight"] == 0.65
    assert pl["personal_tilt"] == 90
    assert pl["exclude_users"] == []
    assert pl["profile_ages"] == {}


def test_playlists_block_does_not_disturb_existing_plex_keys():
    plex = empty_config()["plex"]
    # The pre-existing plex leaves are untouched by adding the playlists block.
    assert plex["episodes"] == {"enabled": False}
    assert plex["movies"] == {"enabled": False}
    assert plex["port"] == 32400


def test_skeleton_overlay_preserves_a_populated_playlists_block():
    """deep_merge(skeleton, existing) must keep an operator's edits, not clobber them."""
    existing = {"plex": {"playlists": {"max_items": 50, "writeback": {"enabled": True}}}}
    merged = deep_merge(empty_config(), existing)
    pl = _playlists(merged)
    assert pl["max_items"] == 50                       # operator override wins
    assert pl["writeback"]["enabled"] is True
    assert pl["episode_cap"] == 5                       # untouched skeleton default fills in
    assert pl["recency_boost"] == {"enabled": False, "window_days": 30}


# ── byte-identical builder behaviour (the core invariant) ─────────────────────
def _builder(cfg) -> PlexPlaylistBuilderManager:
    return PlexPlaylistBuilderManager(config=cfg, global_cache=None, registry=None)


def test_builder_knobs_match_between_absent_and_skeleton_config():
    """Every knob the builder reads ad-hoc resolves to the SAME value whether the
    plex.playlists block is absent (legacy config) or supplied by the skeleton."""
    legacy = _builder({})                  # nothing under plex.playlists -> inline defaults
    skel = _builder(empty_config())        # skeleton supplies the block

    assert legacy._max_items() == skel._max_items() == 100
    assert legacy._episode_cap() == skel._episode_cap() == 5
    assert legacy._genre_match_opts() == skel._genre_match_opts()
    assert legacy._priority_weights() == skel._priority_weights()
    assert legacy._personal_tilt() == skel._personal_tilt() == 90.0
    assert legacy._profile_ages() == skel._profile_ages() == {}


def test_skeleton_genre_match_opts_equal_inline_defaults():
    opts = _builder(empty_config())._genre_match_opts()
    assert opts == {"mode": "precision", "soft_lambda": 0.5, "blend_weight": 0.85}


# ── headless env contract ─────────────────────────────────────────────────────
def _doc_paths() -> set[str]:
    return {path for path, _ex, _note in env_map._DOC_LEAVES}


def test_doc_leaves_cover_the_headless_playlist_knobs():
    paths = _doc_paths()
    for p in (
        "plex.playlists.writeback.enabled",
        "plex.playlists.max_items",
        "plex.playlists.exclude_users",
        "plex.playlists.recency_boost.enabled",
        "plex.playlists.fresh_arrivals.enabled",
        "plex.playlists.fresh_arrivals.acquired_window_days",
        "plex.playlists.home_collections.enabled",
        "plex.playlists.home_collections.promote_home",
        "plex.playlists.home_collections.promote_shared",
    ):
        assert p in paths


# ── backup safety + size-anomaly schema + headless contract ───────────────────
def test_backup_and_size_anomaly_schema_defaults():
    cfg = empty_config()
    assert cfg["backup_before_destructive"] is True
    assert cfg["backup_deep_validate"] is False
    assert cfg["size_anomaly"] == {
        "enabled": True, "remediate": False, "over_ratio": 3.0,
        "under_ratio": 0.3, "min_samples": 8, "report_limit": 25,
    }


def test_skeleton_overlay_preserves_size_anomaly_overrides():
    merged = deep_merge(empty_config(), {"size_anomaly": {"remediate": True, "over_ratio": 2.5}})
    assert merged["size_anomaly"]["remediate"] is True          # operator override wins
    assert merged["size_anomaly"]["over_ratio"] == 2.5
    assert merged["size_anomaly"]["min_samples"] == 8           # untouched default fills in


def test_doc_leaves_cover_backup_and_size_anomaly():
    paths = _doc_paths()
    for p in ("backup_before_destructive", "backup_deep_validate",
              "size_anomaly.enabled", "size_anomaly.remediate"):
        assert p in paths


# ── skeleton completeness for the headless/Docker overlay ─────────────────────
def test_english_dub_and_reality_normalized_into_skeleton():
    cfg = empty_config()
    assert cfg["rootFolders"]["reality"] == ""                      # was step-only drift
    assert set(cfg["english_dub"]) == {
        "cf_scoring", "theatrical_seek", "english_ladder", "lock_owned_dubs", "auto_enroll"}
    assert cfg["english_dub"]["cf_scoring"] == {"enabled": True}    # recommended default


def test_doc_leaves_cover_routing_consent_and_feature_knobs():
    paths = _doc_paths()
    for p in ("relocation_consent", "deletions_consent", "routing.reorg_mode",
              "routing.movies.4k_policy", "routing.tv.kids_bucket_enabled",
              "english_dub.mode", "plex.playlists.cold_start_kids_prior"):
        assert p in paths


def test_writeback_doc_row_warns_about_dry_run_and_writes():
    note = next(n for path, _ex, n in env_map._DOC_LEAVES
                if path == "plex.playlists.writeback.enabled")
    assert "WRITES" in note
    assert "dry_run" in note


def test_doc_leaf_env_names_follow_the_recommendarr_convention():
    assert env_name("plex.playlists.writeback.enabled") == \
        "RECOMMENDARR_PLEX_PLAYLISTS_WRITEBACK_ENABLED"
    assert env_name("plex.playlists.recency_boost.enabled") == \
        "RECOMMENDARR_PLEX_PLAYLISTS_RECENCY_BOOST_ENABLED"


def test_env_example_and_markdown_render_the_new_rows():
    env_example = env_map.generate_env_example()
    md = env_map.generate_markdown_table()
    for var in (
        "RECOMMENDARR_PLEX_PLAYLISTS_WRITEBACK_ENABLED",
        "RECOMMENDARR_PLEX_PLAYLISTS_MAX_ITEMS",
        "RECOMMENDARR_PLEX_PLAYLISTS_EXCLUDE_USERS",
        "RECOMMENDARR_PLEX_PLAYLISTS_RECENCY_BOOST_ENABLED",
    ):
        assert var in env_example
        assert var in md
