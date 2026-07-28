"""Tests for thresholds/shadow.py + thresholds/report.py.

The flip counts are the whole product of shadow mode, so they are checked
against hand-computed examples small enough to verify by eye, then the report
layer is exercised end to end on a synthetic cache: table rows, audit JSON,
registry priming, and the "mode=off costs nothing" contract.

    python -m pytest scripts/managers/machine_learning/thresholds/test_shadow.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pandas as pd
import pytest

from scripts.managers.machine_learning.thresholds import registry, report, shadow
from scripts.managers.machine_learning.thresholds.shadow import compare

# The worked example, verifiable by hand:
#   scores            10  20  30  40  50
#   current = 35  ->  pass {40, 50}          = 2 at/above, 3 below
#   derived = 25  ->  pass {30, 40, 50}      = 3 at/above, 2 below
#   the ONLY entity that changes side is 30  -> 1 flip, derived is "looser"
SCORES = [10.0, 20.0, 30.0, 40.0, 50.0]


def test_flip_count_when_derived_is_looser():
    out = compare({"acquire": 35}, {"acquire": 25}, SCORES)
    row = out["thresholds"]["acquire"]
    assert row["n"] == 5
    assert (row["n_at_or_above_current"], row["n_below_current"]) == (2, 3)
    assert (row["n_at_or_above_effective"], row["n_below_effective"]) == (3, 2)
    assert row["n_flip"] == 1
    assert row["flip_pct"] == 20.0
    assert row["direction"] == "looser"
    assert row["delta"] == -10.0
    assert row["effective"] == 25.0          # no shrinkage supplied -> raw drives
    assert row["status"] == "ok"
    assert out["totals"] == {"n_thresholds": 1, "n_with_derived": 1,
                             "n_gated_out": 0, "n_flip_total": 1}


def test_flip_count_when_derived_is_stricter():
    #   current = 20 -> pass {20,30,40,50} = 4 ; derived = 45 -> pass {50} = 1
    #   flips: 20, 30, 40 -> 3
    row = compare({"delete": 20}, {"delete": 45}, SCORES)["thresholds"]["delete"]
    assert (row["n_at_or_above_current"], row["n_at_or_above_effective"]) == (4, 1)
    assert row["n_flip"] == 3 and row["direction"] == "stricter"


# ── the blend: counts follow the EFFECTIVE cutoff, not the raw one ────────────

def test_counts_follow_the_effective_cutoff_not_the_raw_derived():
    """derived=5 would flip four entities; shrunk halfway to a constant of 35 it
    lands at 20 and flips only one. The report must count what would HAPPEN."""
    out = compare({"acquire": 35}, {"acquire": 5.0}, SCORES,
                  effective={"acquire": 20.0}, weights={"acquire": 0.5})
    row = out["thresholds"]["acquire"]
    assert row["derived"] == 5.0 and row["effective"] == 20.0 and row["w"] == 0.5
    assert row["delta"] == -15.0            # effective - current
    assert row["delta_raw"] == -30.0        # raw - current, for the reader
    assert row["n_at_or_above_effective"] == 4          # 20,30,40,50
    assert row["n_flip"] == 2                           # 20 and 30 change side
    assert row["direction"] == "looser" and row["status"] == "ok"


def test_zero_weight_row_is_reported_as_no_change():
    """n_pos=0: effective == current, so the row exists, shows the raw derived
    value, and truthfully claims zero flips."""
    out = compare({"acquire": 35}, {"acquire": 5.0}, SCORES,
                  effective={"acquire": 35.0}, weights={"acquire": 0.0})
    row = out["thresholds"]["acquire"]
    assert row["effective"] == 35.0 and row["w"] == 0.0
    assert row["n_flip"] == 0 and row["direction"] == "identical"
    assert row["n_at_or_above_effective"] == row["n_at_or_above_current"] == 2


def test_identical_cutoffs_flip_nothing():
    row = compare({"t": 35}, {"t": 35.0}, SCORES)["thresholds"]["t"]
    assert row["n_flip"] == 0 and row["direction"] == "identical"


def test_a_move_inside_a_gap_flips_nothing_but_is_still_reported():
    """Derived 32 vs current 35: different numbers, same partition — the report
    must say 'looser' AND admit the effect is zero."""
    row = compare({"t": 35}, {"t": 32}, SCORES)["thresholds"]["t"]
    assert row["n_flip"] == 0
    assert row["direction"] == "looser_no_effect"


def test_boundary_scores_are_inclusive():
    #   a score exactly ON the cutoff passes (score >= t), both sides
    row = compare({"t": 30}, {"t": 40}, SCORES)["thresholds"]["t"]
    assert row["n_at_or_above_current"] == 3      # 30, 40, 50
    assert row["n_at_or_above_effective"] == 2    # 40, 50
    assert row["n_flip"] == 1                     # only 30


def test_row_without_a_derived_term_shows_its_constant_and_never_flips():
    out = compare({"a": 35, "b": 20}, {"a": None, "b": 15}, SCORES,
                  effective={"a": 35.0, "b": 15.0})
    a = out["thresholds"]["a"]
    assert a["status"] == "no_derived" and a["derived"] is None and a["w"] is None
    # the "effective" column still carries a real number — the constant itself
    assert a["effective"] == 35.0 and a["delta"] == 0.0
    assert a["n_flip"] == 0
    assert a["n_at_or_above_effective"] == a["n_at_or_above_current"] == 2
    assert out["totals"]["n_gated_out"] == 1
    assert out["totals"]["n_with_derived"] == 1


def test_scores_can_be_keyed_per_service():
    out = compare(
        {"movie_x": 35, "series_y": 35},
        {"movie_x": 25, "series_y": 25},
        {"radarr": [10.0, 30.0, 40.0], "sonarr": [30.0, 31.0, 32.0, 90.0]},
        score_key={"movie_x": "radarr", "series_y": "sonarr"})
    assert out["thresholds"]["movie_x"]["n"] == 3
    assert out["thresholds"]["movie_x"]["n_flip"] == 1        # 30
    assert out["thresholds"]["series_y"]["n"] == 4
    assert out["thresholds"]["series_y"]["n_flip"] == 3       # 30, 31, 32


def test_extra_columns_are_carried_through():
    out = compare({"t": 35}, {"t": 25}, SCORES,
                  calibrated_p_at_current={"t": 0.0378},
                  target_p={"t": 0.0378})
    row = out["thresholds"]["t"]
    assert row["p_at_current"] == 0.0378 and row["target_p"] == 0.0378


def test_empty_and_malformed_inputs_are_survivable():
    assert compare({}, {}, [])["totals"]["n_thresholds"] == 0
    row = compare({"t": 35}, {"t": 25}, [])["thresholds"]["t"]
    assert row["n"] == 0 and row["n_flip"] == 0 and row["flip_pct"] is None
    row = compare({"t": None}, {"t": 25}, SCORES)["thresholds"]["t"]
    assert row["status"] == "no_derived"          # a non-numeric current cannot compare
    row = compare({"t": 35}, {"t": "nope"}, SCORES)["thresholds"]["t"]
    assert row["status"] == "no_derived"
    # NaNs in the score column are dropped, not counted
    row = compare({"t": 35}, {"t": 25}, [10.0, float("nan"), 40.0])["thresholds"]["t"]
    assert row["n"] == 2


def test_render_rows_show_the_whole_blend():
    """current · derived · w · effective · n_pos(p+b) · confidence · would-flip."""
    out = compare({"movie_monitor": 30, "series_monitor": 35},
                  {"movie_monitor": 12.0, "series_monitor": None}, SCORES,
                  effective={"movie_monitor": 18.5, "series_monitor": 35.0},
                  weights={"movie_monitor": 0.73},
                  target_p={"movie_monitor": 0.0298},
                  calibrated_p_at_current={"movie_monitor": 0.0299})
    rows = shadow.render_rows(
        out, specs_by_name=registry.SPEC_BY_NAME,
        n_pos_by_name={"movie_monitor": 412, "series_monitor": 38},
        n_pos_split_by_name={"movie_monitor": {"prospective": 12, "backfill": 400},
                             "series_monitor": {"prospective": 38, "backfill": 0}},
        confidence_by_name={"movie_monitor": "magnitude-grade",
                            "series_monitor": "none"})
    assert shadow.RENDER_HEADERS == ["threshold", "current", "derived", "w",
                                     "effective", "n_pos (p+b)", "confidence",
                                     "would flip"]
    assert len(rows) == 2 and all(len(r) == len(shadow.RENDER_HEADERS) for r in rows)
    by_name = {r[0]: r for r in rows}
    assert by_name["movie_monitor"][1:] == [
        "30.0", "12.0", "0.73", "18.5", "412 (12p+400b)", "magnitude-grade",
        "1/5 (looser)"]
    # no derived term: dashes where there is no number, the CONSTANT where the
    # consumer would actually act, and an honest "none" for confidence.
    assert by_name["series_monitor"][1:] == [
        "35.0", "-", "-", "35.0", "38 (38p+0b)", "none", "-"]


# ── report layer ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


def _write_synthetic_cache(tmp_path: Path, *, n: int = 6000, seed: int = 42):
    """A snapshot store whose radarr rows carry the planted score→P relationship
    and whose labels are already stamped, so no Tautulli cache is needed."""
    from scripts.managers.machine_learning.thresholds.test_derive import synthetic
    df = synthetic(n=n, seed=seed, service="radarr")
    d = tmp_path / "ml" / "snapshots" / "radarr"
    d.mkdir(parents=True)
    df.to_parquet(d / "2026-06.parquet", index=False)
    return df


def test_report_end_to_end_on_synthetic_data(tmp_path):
    _write_synthetic_cache(tmp_path)
    rep = report.build_report(
        {}, base_dir=tmp_path,
        scores_by_service={"radarr": [10.0, 30.0, 40.0, 80.0],
                           "sonarr": [5.0, 50.0]})
    assert rep is not None
    assert rep["mode"] == "shadow"                       # the DEFAULT
    assert rep["horizon_days"] == 14
    assert rep["targets"] == registry.DEFAULT_TARGET_P
    assert rep["gates"]["radarr"]["gate_ok"] is True
    assert rep["gates"]["sonarr"]["gate_ok"] is False    # no sonarr rows at all
    assert rep["calibrators"]["radarr"] is not None
    assert rep["calibrators"]["sonarr"] is None

    rows = {r["name"]: r for r in rep["thresholds"]}
    assert set(rows) == {s.name for s in registry.THRESHOLD_SPECS}
    movie = rows["movie_monitor"]
    assert movie["gate_ok"] is True and movie["derived"] is not None
    assert movie["p_at_current"] is not None
    assert movie["service"] == "radarr" and movie["routed"] is True
    assert movie["n"] == 4                               # the radarr score list
    series = rows["series_monitor"]
    assert series["gate_ok"] is False and series["derived"] is None
    assert "insufficient data" in series["gate_reason"]


def test_report_writes_and_reprimes_the_registry(tmp_path):
    _write_synthetic_cache(tmp_path)
    rep = report.run({}, base_dir=tmp_path,
                     scores_by_service={"radarr": [10.0, 40.0]}, logger=None)
    assert rep is not None
    path = registry.latest_report_path(tmp_path)
    assert path is not None and path.name.startswith("thresholds_")
    blob = json.loads(path.read_text(encoding="utf-8"))
    assert blob["mode"] == "shadow"
    assert {r["name"] for r in blob["thresholds"]} == {s.name for s in registry.THRESHOLD_SPECS}
    primed = registry.derived_snapshot()
    assert primed["primed"] is True
    assert primed["values"]["movie_monitor"] is not None
    assert primed["values"]["series_monitor"] is None    # gated out -> falls back

    # shadow mode still hands consumers their literal, primed values or not
    assert registry.get_threshold("movie_monitor", {}, 30) == 30
    # ... and flipping to derived now uses the committed number
    cfg = {"ml": {"thresholds": {"mode": "derived"}}}
    assert registry.get_threshold("movie_monitor", cfg, 30) != 30


def test_cold_install_reports_every_constant_unchanged(tmp_path):
    """THE fresh-install case: a store that exists but has nothing matured yet.
    Every row must show effective == current (bit-identically), no derived term,
    zero flips — and mode='derived' must still hand consumers their literal."""
    from scripts.managers.machine_learning.thresholds.test_derive import synthetic
    d = tmp_path / "ml" / "snapshots" / "radarr"
    d.mkdir(parents=True)
    synthetic(n=2000, seed=42, mature=False).to_parquet(
        d / "2026-07.parquet", index=False)

    rep = report.run({}, base_dir=tmp_path,
                     scores_by_service={"radarr": [10.0, 30.0, 40.0, 80.0]})
    assert rep is not None and rep["gates"]["radarr"]["n_pos"] == 0
    for row in rep["thresholds"]:
        spec = registry.SPEC_BY_NAME[row["name"]]
        assert row["derived"] is None and row["w"] is None, row["name"]
        assert row["confidence"] == "none"
        assert row["effective"] == float(spec.constant), row["name"]
        assert row["n_flip"] == 0
    assert rep["totals"]["n_flip_total"] == 0
    assert any("No calibrator could be fit" in w for w in rep["warnings"])

    # ...and the committed report cannot make mode='derived' move anything
    registry.clear()
    cfg = {"ml": {"thresholds": {"mode": "derived"}}}
    for spec in registry.THRESHOLD_SPECS:
        literal = int(spec.constant)
        assert registry.get_threshold(spec.name, cfg, literal,
                                      base_dir=tmp_path) is literal


def test_derived_mode_reads_the_shrunk_value_from_the_committed_report(tmp_path):
    """The JSON round-trip must hand over EFFECTIVE, not the raw derived cutoff:
    a household 20% of the way there must not get 100% of the calibrator."""
    _write_synthetic_cache(tmp_path, n=600, seed=7)      # ~300 positives -> w~2/3
    rep = report.run({}, base_dir=tmp_path, scores_by_service={})
    row = {r["name"]: r for r in rep["thresholds"]}["movie_monitor"]
    assert row["derived"] is not None
    assert 0.0 < row["w"] < 1.0
    assert row["effective"] != row["derived"]
    assert min(row["derived"], row["current"]) < row["effective"] \
        < max(row["derived"], row["current"])

    registry.clear()                                     # force the JSON reload
    cfg = {"ml": {"thresholds": {"mode": "derived"}}}
    got = registry.get_threshold("movie_monitor", cfg, 30.0, base_dir=tmp_path)
    assert got == pytest.approx(row["effective"])


def test_mode_off_skips_everything(tmp_path):
    _write_synthetic_cache(tmp_path)
    cfg = {"ml": {"thresholds": {"mode": "off"}}}
    assert report.run(cfg, base_dir=tmp_path, scores_by_service={}) is None
    assert registry.latest_report_path(tmp_path) is None      # nothing written
    assert registry.derived_snapshot()["primed"] is False


def test_report_is_none_without_a_snapshot_store(tmp_path):
    assert report.build_report({}, base_dir=tmp_path) is None
    assert report.run({}, base_dir=tmp_path, scores_by_service={}) is None


def test_report_never_raises_on_a_broken_store(tmp_path):
    d = tmp_path / "ml" / "snapshots" / "radarr"
    d.mkdir(parents=True)
    (d / "2026-06.parquet").write_bytes(b"not a parquet file")
    assert report.run({}, base_dir=tmp_path, scores_by_service={}) is None


def test_log_report_renders_a_table(tmp_path):
    _write_synthetic_cache(tmp_path)

    class _Log:
        def __init__(self):
            self.tables, self.debug = [], []

        def log_table(self, headers, rows, title="", caption="", descriptions=None):
            self.tables.append((headers, rows, title, caption))

        def log_debug(self, msg):
            self.debug.append(str(msg))

    log = _Log()
    rep = report.run({}, base_dir=tmp_path, logger=log,
                     scores_by_service={"radarr": [10.0, 40.0]})
    assert rep is not None and len(log.tables) == 1
    headers, rows, title, caption = log.tables[0]
    assert headers == shadow.RENDER_HEADERS
    assert len(rows) == len(registry.THRESHOLD_SPECS)
    assert "[shadow]" in title and "REPORT ONLY" in caption
    # unrouted rows are visibly marked
    assert any(r[0].endswith(" *") for r in rows)


def test_backfill_rows_feed_the_calibrator_by_default_and_are_reported_split(tmp_path):
    """The cold-start change: a fresh install's reconstructed labels COUNT, and
    the report says out loud how much of the evidence they are."""
    from scripts.managers.machine_learning.thresholds.test_derive import synthetic
    d = tmp_path / "ml" / "snapshots" / "radarr"
    d.mkdir(parents=True)
    pd.concat([synthetic(n=6000, seed=42, source="backfill"),
               synthetic(n=400, seed=7)], ignore_index=True).to_parquet(
        d / "2026-06.parquet", index=False)

    rep = report.build_report({}, base_dir=tmp_path)
    g = rep["gates"]["radarr"]
    assert rep["include_backfill"] is True
    assert g["n"] == 6400                                # both populations
    assert g["n_pos"] == g["n_pos_prospective"] + g["n_pos_backfill"]
    assert g["n_pos_backfill"] > g["n_pos_prospective"] > 0
    assert rep["n_pos_by_source"]["backfill"] == g["n_pos_backfill"]
    # ...and the caveat fires because backfill dominates the evidence
    assert any("BACKFILL reconstructions" in w for w in rep["warnings"])
    movie = {r["name"]: r for r in rep["thresholds"]}["movie_monitor"]
    assert movie["n_pos_backfill"] == g["n_pos_backfill"]
    assert movie["derived"] is not None and movie["effective"] is not None

    # opting OUT is still one config key away, and then only the 400 count
    rep = report.build_report(
        {"ml": {"thresholds": {"include_backfill": False}}}, base_dir=tmp_path)
    assert rep["gates"]["radarr"]["n"] == 400
    assert rep["gates"]["radarr"]["n_pos_backfill"] == 0
    assert any("EXCLUDED" in w for w in rep["warnings"])


class _FakeCache:
    def __init__(self, root):
        self.cache_root = root


def test_max_fit_days_bounds_the_label_join(tmp_path):
    from scripts.managers.machine_learning.thresholds.test_derive import synthetic
    d = tmp_path / "ml" / "snapshots" / "radarr"
    d.mkdir(parents=True)
    pd.concat([synthetic(n=6000, seed=42, dates=["2020-01-01"] * 6000),
               synthetic(n=400, seed=7, dates=["2026-06-01"] * 400)],
              ignore_index=True).to_parquet(d / "2026-06.parquet", index=False)
    rep = report.build_report({}, base_dir=tmp_path)
    assert rep["max_fit_days"] == registry.DEFAULT_MAX_FIT_DAYS
    assert rep["gates"]["radarr"]["n"] == 400            # the ancient rows dropped
    assert any("max_fit_days" in w for w in rep["warnings"])
    rep = report.build_report({"ml": {"thresholds": {"max_fit_days": 0}}},
                             base_dir=tmp_path)
    assert rep["gates"]["radarr"]["n"] == 6400           # 0 = unbounded


def test_plan_summary_calls_the_report_and_never_breaks(tmp_path):
    from scripts.managers.machine_learning.ledger.plan_summary import PlanSummary
    ps = PlanSummary(registry=None, logger=None,
                     config={"ml": {"thresholds": {"mode": "off"}}},
                     global_cache=_FakeCache(tmp_path))
    assert ps.log_thresholds() == {}
    assert ps.summarize() == ({}, [])
    assert ps._scores_by_service == {}

    # A config that explodes on .get degrades to the DEFAULTS (mode="shadow")
    # rather than taking the run down — and finds no store, so reports nothing.
    class _Boom:
        def get(self, *_a, **_k):
            raise RuntimeError("nope")

    assert PlanSummary(config=_Boom(),
                       global_cache=_FakeCache(tmp_path)).log_thresholds() == {}

    # With a real store it produces the table and still cannot raise.
    _write_synthetic_cache(tmp_path)
    out = PlanSummary(config={}, global_cache=_FakeCache(tmp_path)).log_thresholds()
    assert out and out["mode"] == "shadow"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
