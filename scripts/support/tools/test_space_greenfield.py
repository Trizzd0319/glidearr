"""
test_space_greenfield.py — the greenfield rebuild simulator's non-negotiables.
================================================================================
Pure-logic assertions on the fill/diff core plus end-to-end runs of the REAL CLI
against a hand-built THROWAWAY cache-base:

  * BUDGET      — the fill stops at capacity − reserve and never crosses it.
  * PINS        — keep-policy pins are acquired BEFORE higher-scored unpinned
                  titles (they are the household's declared intent, not a score).
  * DIFF        — KEPT / PROMOTED / DEMOTED / DROPPED / ADDED are computed
                  correctly on a fixture with one known member of each.
  * TIER        — the shipped ladders are both honoured: score_to_profile
                  proposes and watch_likelihood's uhd_cutoff caps.
  * EXCLUSIONS  — a missing score (and an unsizeable missing runtime) excludes
                  the title and is COUNTED, never silently acquired for free.
  * TV          — episode rows aggregate to SERIES level (one title per series).
  * READ-ONLY   — a full run writes nothing outside <cache>/ml/reports.
  * ISOLATION   — --service radarr never opens the Sonarr parquet at all.

Run:  PYTHONPATH=. python3 -m pytest scripts/support/tools/test_space_greenfield.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_tool(name: str = "space_greenfield"):
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_gf_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: dataclasses resolves cls.__module__ through
    # sys.modules while scanning annotations, and blows up on a None module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


SG = _load_tool()


# ─────────────────────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────────────────────
def _title(key, *, score, est_gib, owned=True, owned_res=720, tier_res=720,
           pinned=False, owned_gib=1.0, service="radarr"):
    parts = key.split(":")
    instance = parts[1] if len(parts) == 3 else "standard"
    return SG.Title(
        key=key, service=service, instance=instance, name=key, score=score,
        likelihood=0.0, pinned=pinned, keep_policy=("keep_forever" if pinned else None),
        owned=owned, owned_gib=owned_gib, owned_res=owned_res, runtime_min=100.0,
        n_items=1, tier_label="HD 720p", tier_res=tier_res, profile_id=None,
        profile_name=None, quality_name="Bluray-720p", rate_source="test",
        est_gib=est_gib,
    )


_BREAKDOWN = json.dumps({"D1_device_capability": 1.0, "D2_transcode_avoidance": 2.0,
                         "D3_platform_ceiling": 0.0, "_total_final": 30})


def _movie(mid, title, score, *, runtime=100, size_gib=5.0, res=720,
           keep_policy=None, watch_count=0):
    return {
        "movie_id": mid, "tmdb_id": 1000 + mid, "title": title, "year": 2000,
        "runtime_minutes": runtime, "size_bytes": int(size_gib * 1024 ** 3),
        "resolution": float(res), "quality_name": "Bluray-720p", "has_file": True,
        "keep_policy": keep_policy, "watchability_score": score,
        "watchability_percentile": 50.0, "watchability_breakdown": _BREAKDOWN,
        "watch_count": watch_count, "percent_complete": None, "is_watched": False,
        "universe_credit": 0.0,
    }


def _episode(sid, series, score, ep, *, size_gib=1.0, res=720, runtime_s=2700,
             keep_policy=None, has_file=True):
    return {
        "series_id": sid, "series_title": series, "season_number": 1,
        "episode_number": ep, "episode_file_id": (sid * 100 + ep) if has_file else None,
        "is_pilot": ep == 1, "size_bytes": int(size_gib * 1024 ** 3) if has_file else 0,
        "resolution": float(res) if has_file else None,
        "runtime_seconds": float(runtime_s), "quality_name": "Bluray-720p",
        "keep_policy": keep_policy, "watchability_score": score,
        "watchability_percentile": 50.0, "watchability_breakdown": _BREAKDOWN,
        "watch_count": 0, "percent_complete": None, "is_watched": False,
        "universe_credit": 0.0,
    }


def _build_cache(tmp_path: Path, *, movies=None, episodes=None,
                 free_bytes=200 * 1024 ** 3) -> Path:
    base = tmp_path / "cache"
    if movies is not None:
        d = base / "radarr" / "standard"
        d.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(movies).to_parquet(d / "movie_files.parquet")
        (d / "storage").mkdir(exist_ok=True)
        (d / "storage" / "space_estimates.json").write_text(json.dumps(
            [{"path": "/data/movies", "freeSpace": free_bytes}]), encoding="utf-8")
    if episodes is not None:
        d = base / "sonarr" / "standard"
        d.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(episodes).to_parquet(d / "episode_files.parquet")
    (base / "ml").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    return base


def _run(base: Path, tmp_path: Path, *extra) -> dict:
    """Run the REAL CLI; return the report JSON it wrote."""
    argv = ["--cache-base", str(base), "--config", str(tmp_path / "config.json"), *extra]
    assert SG.main(argv) == 0
    reports = sorted((base / "ml" / "reports").glob("greenfield_*.json"))
    assert reports, "no report written"
    return json.loads(reports[-1].read_text(encoding="utf-8"))


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


# ─────────────────────────────────────────────────────────────────────────────
# BUDGET
# ─────────────────────────────────────────────────────────────────────────────
def test_fill_stops_at_budget_and_never_crosses_it():
    titles = [_title(f"m{i}", score=100 - i, est_gib=10.0) for i in range(5)]
    fill = SG.fill_budget(titles, 25.0)
    assert [t.key for t in fill.acquired] == ["m0", "m1"]
    assert fill.used_gib == 20.0 <= 25.0
    assert fill.stopped_at_rank == 2 and fill.stopped_on == "m2"
    # everything past the stop is skipped, even if a later title would have fit
    assert {t.key for t in fill.skipped} == {"m2", "m3", "m4"}


def test_fill_is_sequential_not_knapsack():
    """A live acquisition run walks the ranking; it stalls on the first title it
    cannot afford rather than cherry-picking a cheaper lower-ranked one."""
    titles = [_title("big", score=90, est_gib=100.0), _title("small", score=10, est_gib=1.0)]
    fill = SG.fill_budget(titles, 50.0)
    assert fill.acquired == [] and fill.used_gib == 0.0
    assert fill.stopped_on == "big"


def test_budget_respects_the_reserve_end_to_end(tmp_path):
    movies = [_movie(i, f"M{i}", score=50 - i, runtime=100, size_gib=5.0) for i in range(30)]
    base = _build_cache(tmp_path, movies=movies)
    rep = _run(base, tmp_path, "--service", "radarr",
               "--capacity-gb", "100", "--reserve-gb", "70")
    assert rep["capacity"]["capacity_gib"] == 100.0
    assert rep["capacity"]["reserve_gib"] == 70.0
    assert rep["capacity"]["budget_gib"] == 30.0
    assert rep["rebuild"]["estimated_gib"] <= 30.0
    assert rep["rebuild"]["headroom_gib"] >= 0.0
    assert 0 < rep["rebuild"]["titles"] < 30      # the reserve actually bit


def test_reserve_defaults_to_config_free_space_limit(tmp_path):
    base = _build_cache(tmp_path, movies=[_movie(1, "M", 40)])
    (tmp_path / "config.json").write_text(json.dumps({"free_space_limit": 42}), encoding="utf-8")
    rep = _run(base, tmp_path, "--service", "radarr", "--capacity-gb", "100")
    assert rep["capacity"]["reserve_gib"] == 42.0
    assert rep["capacity"]["budget_gib"] == 58.0
    assert rep["capacity"]["reserve_source"] == "config free_space_limit"


# ─────────────────────────────────────────────────────────────────────────────
# PINS
# ─────────────────────────────────────────────────────────────────────────────
def test_keep_pins_acquired_before_higher_scored_unpinned():
    pinned = _title("pinned-lowscore", score=1, est_gib=10.0, pinned=True)
    hot = _title("unpinned-topscore", score=99, est_gib=10.0)
    fill = SG.fill_budget([hot, pinned], 10.0)     # room for exactly one
    assert [t.key for t in fill.acquired] == ["pinned-lowscore"]


def test_pins_are_ordered_among_themselves_by_score():
    a = _title("pin-a", score=5, est_gib=1.0, pinned=True)
    b = _title("pin-b", score=50, est_gib=1.0, pinned=True)
    c = _title("free-c", score=99, est_gib=1.0)
    order = [t.key for t in sorted([a, b, c], key=SG.sort_key)]
    assert order == ["pin-b", "pin-a", "free-c"]


def test_pinned_policy_set_is_declared_intent_only():
    assert "keep_forever" in SG.PINNED_POLICIES and "keep_universe" in SG.PINNED_POLICIES
    # the auto-derived "universe" policy is inferred, not declared → never a pin
    assert "universe" not in SG.PINNED_POLICIES


def test_keep_policy_pin_survives_end_to_end(tmp_path):
    """A pinned dud outranks 40 better-scored films when only a few fit."""
    movies = [_movie(0, "PINNED DUD", 0, keep_policy="keep_forever")]
    movies += [_movie(i, f"Good {i}", 55, size_gib=5.0) for i in range(1, 40)]
    base = _build_cache(tmp_path, movies=movies)
    rep = _run(base, tmp_path, "--service", "radarr",
               "--capacity-gb", "40", "--reserve-gb", "0", "--top", "50")
    acquired = {r["title"] for r in rep["titles"][:rep["rebuild"]["titles"]]}
    assert "PINNED DUD" in acquired
    assert rep["rebuild"]["pinned_acquired"] == 1
    assert rep["titles"][0]["title"] == "PINNED DUD"     # ranked first


# ─────────────────────────────────────────────────────────────────────────────
# DIFF BUCKETS
# ─────────────────────────────────────────────────────────────────────────────
def test_diff_buckets_on_hand_built_fixture():
    kept = _title("kept", score=50, est_gib=1.0, owned_res=720, tier_res=720)
    promoted = _title("promoted", score=50, est_gib=1.0, owned_res=720, tier_res=1080)
    demoted = _title("demoted", score=50, est_gib=1.0, owned_res=2160, tier_res=720)
    dropped = _title("dropped", score=1, est_gib=1.0, owned_res=480, tier_res=720)
    added = _title("added", score=50, est_gib=1.0, owned=False, owned_res=None)
    unowned_skipped = _title("nofit", score=1, est_gib=1.0, owned=False, owned_res=None)
    titles = [kept, promoted, demoted, dropped, added, unowned_skipped]

    got = SG.diff_buckets(titles, {"kept", "promoted", "demoted", "added"})
    assert [t.key for t in got["kept"]] == ["kept"]
    assert [t.key for t in got["promoted"]] == ["promoted"]
    assert [t.key for t in got["demoted"]] == ["demoted"]
    assert [t.key for t in got["dropped"]] == ["dropped"]
    assert [t.key for t in got["added"]] == ["added"]
    assert [t.key for t in got["unowned_skipped"]] == ["nofit"]
    # disjoint + total
    assert sum(len(v) for v in got.values()) == len(titles)


def test_unknown_owned_tier_counts_as_kept_not_a_tier_change():
    t = _title("no-res", score=50, est_gib=1.0, owned_res=None, tier_res=1080)
    got = SG.diff_buckets([t], {"no-res"})
    assert len(got["kept"]) == 1 and not got["promoted"] and not got["demoted"]


def test_bucket_stats_reports_both_sides():
    t = _title("x", score=1, est_gib=3.0, owned_gib=9.0)
    st = SG.bucket_stats([t])
    assert st == {"count": 1, "today_gib": 9.0, "rebuild_gib": 3.0}


def test_overlap_statement_is_plain_english(tmp_path):
    base = _build_cache(tmp_path, movies=[_movie(i, f"M{i}", 40 - i) for i in range(6)])
    rep = _run(base, tmp_path, "--service", "radarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    ov = rep["overlap"]
    assert ov["statement"].startswith("the rebuild would re-acquire ")
    assert f"{ov['reacquired_titles']} of {ov['owned_titles']} titles" in ov["statement"]
    assert ov["reacquired_titles"] == (rep["buckets"]["kept"]["count"]
                                       + rep["buckets"]["promoted"]["count"]
                                       + rep["buckets"]["demoted"]["count"])


# ─────────────────────────────────────────────────────────────────────────────
# TIER POLICY (the shipped ladders)
# ─────────────────────────────────────────────────────────────────────────────
def test_assign_tier_uses_score_ladder_and_uhd_cutoff():
    # score 80 alone proposes Remux 2160p, but affinity is capped below
    # uhd_cutoff (75) → watch_likelihood caps the tier at 1080p.
    label, res, likelihood = SG.assign_tier(80, {"watchability_score": 80})
    assert label == "Remux 2160p" and res == 1080 and likelihood < 75

    # 3 watches → engagement floor 78 ≥ uhd_cutoff → the 4K tier unlocks.
    label, res, likelihood = SG.assign_tier(80, {"watchability_score": 80, "watch_count": 3})
    assert label == "Remux 2160p" and res == 2160 and likelihood >= 75

    # A rewatched dud still cannot exceed what the score ladder proposes.
    label, res, _ = SG.assign_tier(10, {"watchability_score": 10, "watch_count": 5})
    assert label == "HD 720p" and res == 720


def test_label_resolution_and_720_floor():
    assert SG.label_resolution("Remux 2160p") == 2160
    assert SG.label_resolution("Bluray 1080p") == 1080
    assert SG.label_resolution("HD 720p") == 720
    assert SG.label_resolution("SD") == 480
    # the shipped ladder never proposes below 720p for a real score
    assert SG.assign_tier(0, {"watchability_score": 0})[1] == 720


# ─────────────────────────────────────────────────────────────────────────────
# EXCLUSIONS
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_scores_are_excluded_and_counted(tmp_path):
    movies = [_movie(1, "Scored A", 40), _movie(2, "Scored B", 30), _movie(3, "No score", None)]
    base = _build_cache(tmp_path, movies=movies)
    rep = _run(base, tmp_path, "--service", "radarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    assert rep["excluded"]["movies_no_score"] == 1
    names = {r["title"] for r in rep["titles"]}
    assert names == {"Scored A", "Scored B"}


def test_unsizeable_titles_are_excluded_not_acquired_free(tmp_path):
    movies = [_movie(1, "Sizeable", 40), _movie(2, "No runtime", 99, runtime=0)]
    base = _build_cache(tmp_path, movies=movies)
    rep = _run(base, tmp_path, "--service", "radarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    assert rep["excluded"]["movies_no_runtime"] == 1
    assert {r["title"] for r in rep["titles"]} == {"Sizeable"}
    assert rep["rebuild"]["estimated_gib"] > 0


def test_honesty_block_is_in_the_report_itself(tmp_path):
    base = _build_cache(tmp_path, movies=[_movie(1, "M", 40)])
    rep = _run(base, tmp_path, "--service", "radarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    blob = " ".join(rep["honesty"]).lower()
    assert "estimate" in blob and "re-acquisition" in blob and "cold start" in blob
    assert "excluded" in blob and rep["read_only"] is True


# ─────────────────────────────────────────────────────────────────────────────
# TV: series-level aggregation
# ─────────────────────────────────────────────────────────────────────────────
def test_tv_aggregates_episode_rows_to_series_level(tmp_path):
    # SAME score + SAME runtime for both, so the only difference is depth.
    eps = [_episode(1, "Deep Show", 40, e) for e in range(1, 5)]     # 4 episode rows
    eps += [_episode(2, "Pilot Only", 40, 1)]                        # 1 row
    base = _build_cache(tmp_path, episodes=eps)
    rep = _run(base, tmp_path, "--service", "sonarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    rows = {r["title"]: r for r in rep["titles"]}
    assert set(rows) == {"Deep Show", "Pilot Only"}          # 5 rows → 2 titles
    assert rep["current_library"]["series"]["titles"] == 2
    # the series carries the episode COUNT, and sizing scales with it
    assert rows["Deep Show"]["items"] == 4 and rows["Pilot Only"]["items"] == 1
    assert rows["Deep Show"]["tier_res"] == rows["Pilot Only"]["tier_res"]
    assert rows["Deep Show"]["est_gib"] == pytest.approx(
        4 * rows["Pilot Only"]["est_gib"], rel=0.01)


def test_tv_series_score_is_the_broadcast_series_score(tmp_path):
    """_build_show_score_map broadcasts ONE score across a series' episode rows;
    the tool takes it as the series score and flags any disagreement."""
    eps = [_episode(1, "Consistent", 44, e) for e in range(1, 4)]
    base = _build_cache(tmp_path, episodes=eps)
    rep = _run(base, tmp_path, "--service", "sonarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    assert rep["titles"][0]["score"] == 44
    assert rep["excluded"]["series_score_disagreements"] == 0


def test_tv_series_with_no_files_is_an_add_not_a_keep(tmp_path):
    eps = [_episode(1, "Owned", 40, 1)]
    eps += [_episode(2, "Monitored only", 39, 1, has_file=False)]
    base = _build_cache(tmp_path, episodes=eps)
    rep = _run(base, tmp_path, "--service", "sonarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    assert rep["buckets"]["added"]["count"] == 1
    assert rep["current_library"]["series"]["titles"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# READ-ONLY / ISOLATION
# ─────────────────────────────────────────────────────────────────────────────
def test_writes_nothing_outside_ml_reports(tmp_path):
    movies = [_movie(i, f"M{i}", 40 - i) for i in range(8)]
    eps = [_episode(1, "S", 35, e) for e in range(1, 4)]
    base = _build_cache(tmp_path, movies=movies, episodes=eps)
    before = _snapshot(base)
    rep = _run(base, tmp_path, "--capacity-gb", "1000", "--reserve-gb", "0")
    after = _snapshot(base)

    changed = {p for p in after if before.get(p) != after[p]}
    assert changed, "the report itself must have been written"
    assert all(p.replace("\\", "/").startswith("ml/reports/") for p in changed), changed
    assert set(before) - set(after) == set(), "nothing may be deleted"
    assert rep["writes_only"].endswith("greenfield_{date}.json")


def test_service_radarr_skips_sonarr_entirely(tmp_path):
    movies = [_movie(1, "M", 40)]
    base = _build_cache(tmp_path, movies=movies)
    # A Sonarr parquet that CANNOT be parsed: if the tool touched it, it would
    # surface an error entry. --service radarr must never look.
    d = base / "sonarr" / "standard"
    d.mkdir(parents=True, exist_ok=True)
    (d / "episode_files.parquet").write_bytes(b"this is not a parquet file")

    rep = _run(base, tmp_path, "--service", "radarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    assert rep["sources"]["sonarr"] == []
    assert all(r["service"] == "radarr" for r in rep["titles"])
    assert rep["current_library"]["series"]["titles"] == 0
    assert rep["params"]["service"] == "radarr"


def test_service_sonarr_skips_radarr_entirely(tmp_path):
    base = _build_cache(tmp_path, episodes=[_episode(1, "S", 35, 1)])
    d = base / "radarr" / "standard"
    d.mkdir(parents=True, exist_ok=True)
    (d / "movie_files.parquet").write_bytes(b"nope")
    rep = _run(base, tmp_path, "--service", "sonarr", "--capacity-gb", "1000",
               "--reserve-gb", "0")
    assert rep["sources"]["radarr"] == []
    assert all(r["service"] == "sonarr" for r in rep["titles"])


def test_cross_instance_duplicates_flag_byte_identical_copies():
    """Same id in two instances with the SAME byte size = one file counted twice.
    A real dual-version (4K + HD) has two different sizes and must NOT be flagged."""
    dup_a = _title("radarr:standard:99", score=10, est_gib=1.0, owned_gib=50.0)
    dup_b = _title("radarr:ultra:99", score=10, est_gib=1.0, owned_gib=50.0)
    dual_a = _title("radarr:standard:77", score=10, est_gib=1.0, owned_gib=8.0)
    dual_b = _title("radarr:ultra:77", score=10, est_gib=1.0, owned_gib=40.0)
    got = SG.cross_instance_duplicates([dup_a, dup_b, dual_a, dual_b])
    assert got["redundant_copies"] == 1
    assert got["double_counted_gib"] == 50.0
    assert got["examples"][0]["instances"] == ["standard", "ultra"]


def test_instance_filter_is_repeatable(tmp_path):
    base = _build_cache(tmp_path, movies=[_movie(1, "Std", 40)])
    other = base / "radarr" / "ultra"
    other.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([_movie(2, "Ultra", 50)]).to_parquet(other / "movie_files.parquet")

    both = _run(base, tmp_path, "--service", "radarr", "--capacity-gb", "1000",
                "--reserve-gb", "0")
    assert {r["title"] for r in both["titles"]} == {"Std", "Ultra"}

    only = _run(base, tmp_path, "--service", "radarr", "--instance", "ultra",
                "--capacity-gb", "1000", "--reserve-gb", "0")
    assert {r["title"] for r in only["titles"]} == {"Ultra"}


# ─────────────────────────────────────────────────────────────────────────────
# SIZING
# ─────────────────────────────────────────────────────────────────────────────
def test_measured_tier_rate_wins_when_the_library_has_enough_samples(tmp_path):
    df = pd.DataFrame([{"size_bytes": 100 * 1024 ** 2, "resolution": 720.0,
                        "runtime_minutes": 100.0} for _ in range(SG.MIN_TIER_SAMPLES)])
    rates = SG.measured_tier_rates(df, "runtime_minutes", "minutes")
    assert rates[720]["mib_per_min"] == pytest.approx(1.0, rel=1e-3)
    assert rates[720]["n"] == SG.MIN_TIER_SAMPLES
    measured, src = SG.rate_for_tier(720, rates, "Bluray-720p")
    assert measured == {"Bluray-720p": 1.0} and src.startswith("measured@720p")


def test_thin_tier_falls_back_to_the_calibrated_table():
    rates = {720: {"mib_per_min": 999.0, "n": SG.MIN_TIER_SAMPLES - 1}}
    measured, src = SG.rate_for_tier(720, rates, "Bluray-720p")
    assert measured == {} and src == "calibrated-table"


def test_calibration_overlay_is_scoped_to_the_cache_base(tmp_path):
    """A --cache-base copy must not inherit another base's calibration overlay."""
    from scripts.managers.machine_learning.sizing import size_model
    size_model.set_calibration({"Bluray-720p": 123.0})
    assert SG.install_calibration(tmp_path / "empty") == 0
    assert size_model.get_calibration() == {}

    cal = tmp_path / "withcal" / "size_model"
    cal.mkdir(parents=True)
    (cal / "calibration.json").write_text(json.dumps({"table": {"Bluray-720p": 42.0}}),
                                          encoding="utf-8")
    assert SG.install_calibration(tmp_path / "withcal") == 1
    assert size_model.get_calibration() == {"Bluray-720p": 42.0}
    size_model.clear_calibration()


def test_capacity_derivation_and_flag_precedence(tmp_path):
    base = _build_cache(tmp_path, movies=[_movie(1, "M", 40, size_gib=10.0)],
                        free_bytes=90 * 1024 ** 3)
    derived = _run(base, tmp_path, "--service", "radarr", "--reserve-gb", "0")
    assert derived["capacity"]["capacity_source"].startswith("DERIVED")
    assert derived["capacity"]["capacity_gib"] == pytest.approx(100.0, abs=0.2)

    flagged = _run(base, tmp_path, "--service", "radarr", "--reserve-gb", "0",
                   "--capacity-gb", "12345")
    assert flagged["capacity"]["capacity_gib"] == 12345.0
    assert flagged["capacity"]["capacity_source"] == "flag --capacity-gb"
