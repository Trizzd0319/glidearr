"""Tests for cache_reset — the selective cache wipe for a clean test rebuild.

CLEARS rebuildable sonarr/radarr/trakt-library/Plex/Tautulli/operational caches; PRESERVES the
expensive internet enrichment (trakt enrich buckets + daemon files) and the collection/saga/universe
data (universe/, people_matrix/, discovery/, mdblist/, mal/, franchise_catalog_state.json, and
plex/playlists). Default-preserve: an unrecognised namespace is kept (and flagged).
"""
from __future__ import annotations

from scripts.support.tools.cache_reset import plan_cache_reset, run_cache_reset


def _rel(paths, root):
    return {str(p.relative_to(root)).replace("\\", "/") for p in paths}


def _build(root):
    def mk(*parts, data="x"):
        p = root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
        return p
    # CLEAR — rebuildable *arr / library / operational
    mk("sonarr", "standard", "episode_files.parquet")
    mk("sonarr", "jit", "inflight_qp", "standard.json")
    mk("radarr", "standard", "x.json")
    mk("radarr.movies.standard.json", data="x" * 200)
    mk("radarr.monitoring.standard.json")
    mk("radarr.cf_sync.snapshot.json")
    mk("tautulli", "history", "x.json")
    mk("pilot_search", "queue", "standard__jit.json")
    for d in ("acquisition", "lifecycle", "size_model", "system", "notifications"):
        mk(d, "x.json")
    # CLEAR — trakt user-account state
    mk("trakt", "history", "movies.json")
    mk("trakt", "BuckITrizzd", "ratings", "movies.json")
    # CLEAR — plex library/user state
    mk("plex", "users", "u.json")
    mk("plex", "watchlist", "w.json")
    # PRESERVE — trakt internet enrichment buckets + ALL enrich-daemon state files
    for b in ("movie_summary", "movie_ratings", "movie_related", "movies", "show_summary", "shows"):
        mk("trakt", b, "1.json", data="x" * 50)
    mk("trakt", "enrich_daemon.pid", data="123")
    mk("trakt", "main_run.active", data="{}")
    mk("trakt", "enrichment_cursor.json", data="{}")        # daemon progress cursor → keep
    mk("trakt", "daemon_radarr_movies.json", data="[]")     # daemon input list → keep
    mk("trakt", "daemon_sonarr_series.json", data="[]")
    # PRESERVE — collection / saga / universe + people enrichment (internet)
    mk("universe", "saga_credit_preview", "p.json")
    root.joinpath("people_matrix").mkdir(parents=True, exist_ok=True)
    mk("discovery", "this_week", "tw.json")
    mk("mdblist", "list.json")
    mk("mal", "seasonal", "2024.json")
    mk("franchise_catalog_state.json", data="x" * 300)
    # PRESERVE — plex universe/saga membership lives under playlists/
    mk("plex", "playlists", "kometa_franchises.json")
    mk("plex", "playlists", "saga_member_titles.json")
    # default-PRESERVE — an unrecognised future namespace
    mk("brand_new_cache", "x.json")


def test_classification_clear_vs_preserve(tmp_path):
    _build(tmp_path)
    plan = plan_cache_reset(tmp_path)
    clear, preserve = _rel(plan["clear"], tmp_path), _rel(plan["preserve"], tmp_path)

    # CLEAR
    for c in ("sonarr", "radarr", "tautulli", "pilot_search", "acquisition", "lifecycle",
              "size_model", "system", "notifications",
              "radarr.movies.standard.json", "radarr.monitoring.standard.json",
              "radarr.cf_sync.snapshot.json",
              "trakt/history", "trakt/BuckITrizzd", "plex/users", "plex/watchlist"):
        assert c in clear, f"{c} should be CLEARED"

    # PRESERVE
    for p in ("trakt/movie_summary", "trakt/movie_ratings", "trakt/movies", "trakt/shows",
              "trakt/show_summary", "trakt/enrich_daemon.pid", "trakt/main_run.active",
              "trakt/enrichment_cursor.json", "trakt/daemon_radarr_movies.json",
              "trakt/daemon_sonarr_series.json",
              "universe", "people_matrix", "discovery", "mdblist", "mal",
              "franchise_catalog_state.json", "plex/playlists", "brand_new_cache"):
        assert p in preserve, f"{p} should be PRESERVED"

    assert not (clear & preserve)                       # never both
    assert any(p.name == "brand_new_cache" for p in plan["unknown"])   # unknown flagged


def test_dry_run_deletes_nothing(tmp_path):
    _build(tmp_path)
    res = run_cache_reset(apply=False, root=tmp_path, stop_daemons=False)
    assert res["applied"] is False
    assert res["reclaimed_bytes"] > 0                   # a plan was produced
    assert (tmp_path / "sonarr").exists()               # but nothing was deleted
    assert (tmp_path / "radarr.movies.standard.json").exists()
    assert (tmp_path / "trakt" / "history").exists()


def test_apply_clears_only_the_clear_set(tmp_path):
    _build(tmp_path)
    res = run_cache_reset(apply=True, root=tmp_path, stop_daemons=False)
    assert res["applied"] is True

    # CLEAR set is gone
    for gone in ("sonarr", "radarr", "tautulli", "pilot_search", "size_model",
                 "radarr.movies.standard.json", "radarr.cf_sync.snapshot.json"):
        assert not (tmp_path / gone).exists(), f"{gone} should be deleted"
    assert not (tmp_path / "trakt" / "history").exists()
    assert not (tmp_path / "trakt" / "BuckITrizzd").exists()
    assert not (tmp_path / "plex" / "users").exists()

    # PRESERVE set survives — the expensive enrichment + universe data
    assert (tmp_path / "trakt" / "movie_summary" / "1.json").exists()
    assert (tmp_path / "trakt" / "movies" / "1.json").exists()
    assert (tmp_path / "trakt" / "enrich_daemon.pid").exists()
    assert (tmp_path / "universe" / "saga_credit_preview" / "p.json").exists()
    assert (tmp_path / "people_matrix").exists()
    assert (tmp_path / "discovery" / "this_week" / "tw.json").exists()
    assert (tmp_path / "mdblist" / "list.json").exists()
    assert (tmp_path / "mal" / "seasonal" / "2024.json").exists()
    assert (tmp_path / "franchise_catalog_state.json").exists()
    assert (tmp_path / "plex" / "playlists" / "kometa_franchises.json").exists()
    assert (tmp_path / "brand_new_cache" / "x.json").exists()   # unknown preserved


def test_missing_root_is_safe(tmp_path):
    plan = plan_cache_reset(tmp_path / "does_not_exist")
    assert plan == {"clear": [], "preserve": [], "unknown": []}
