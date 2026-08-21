"""sizing.quality_caps + measured_stats outlier rejection — the grab-time size
ceiling and the calibration ratchet it depends on.

The case both halves exist for: a 50.9 GiB release named `...720p.BluRay...`
parses as `Bluray-720p` because *arr grades on FILENAME, not bytes. Nothing
refuses it at grab time, and once imported it raises the very threshold meant to
catch the next one."""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing import quality_caps as qc
from scripts.managers.machine_learning.sizing.size_model import (
    MAX_MB_PER_MIN,
    MIN_MB_PER_MIN,
    measured_stats,
)

_CFG = qc.config_for({"quality_caps": {"enabled": True, "over_ratio": 3.0}})
_RATE_720P = 52.4        # measured, n=1170
_RUNTIME = 162           # The Godfather Part III, theatrical


# ── the ceiling ──────────────────────────────────────────────────────────────

def test_ships_disabled():
    """Writing *arr CONFIGURATION is a different class of change; opt-in only."""
    assert qc.DEFAULTS["enabled"] is False
    assert qc.config_for({})["enabled"] is False


def test_mib_to_mb_conversion_is_applied():
    """*arr stores decimal MB/min; this package measures binary MiB/min. Skipping
    the conversion under-states every cap by 4.9% — a silent over-tightening."""
    cap = qc.propose_max_mb_per_min(_RATE_720P, _CFG)
    assert abs(cap - _RATE_720P * 3.0 * qc.MIB_TO_MB) < 1e-6
    assert abs(cap - 164.8) < 0.1


def test_the_godfather_page_separates_correctly():
    """Real search results for that film against the derived cap: disc images and
    split archives refused, every genuine encode allowed."""
    cap_mib = qc.propose_max_mb_per_min(_RATE_720P, _CFG) / qc.MIB_TO_MB
    def rejected(gib):
        return (gib * 1024 / _RUNTIME) > cap_mib
    for gib in (50.9, 49.2, 28.0):          # CyTSuNee, BD50, PRoDJi .part098
        assert rejected(gib), f"{gib} GiB should be refused"
    for gib in (19.9, 18.2, 13.9, 12.2, 11.9, 9.9, 8.6, 5.9):
        assert not rejected(gib), f"{gib} GiB is a real encode and should pass"
    assert abs(qc.gib_at_runtime(qc.propose_max_mb_per_min(_RATE_720P, _CFG),
                                 _RUNTIME) - 24.9) < 0.2


def test_caps_only_ever_tighten():
    """A library polluted by bloat produces a HIGHER rate, which would produce a
    LOOSER cap, which admits more bloat. Never emit a widening proposal."""
    defs = [{"quality": {"name": "Bluray-720p"}, "maxSize": 165.0}]
    poisoned = {"Bluray-720p": {"mean": 321.7, "n": 1170}}
    props, skipped = qc.plan_caps(defs, poisoned, _CFG)
    assert props == []
    assert "already tighter" in skipped[0]["reason"]


def test_unlimited_is_not_zero():
    """P-C: *arr stores absent/null maxSize for tiers it never refuses. Reading
    unlimited as 0 would compare as 'tighter than anything' and suppress every
    cap this module exists to set."""
    assert qc.current_max({"maxSize": None}) is None
    assert qc.current_max({"maxSize": 0}) is None
    assert qc.current_max({}) is None
    props, _ = qc.plan_caps([{"quality": {"name": "Q"}, "maxSize": None}],
                            {"Q": {"mean": 50.0, "n": 100}}, _CFG)
    assert len(props) == 1 and props[0]["current_max"] is None


def test_thin_tiers_get_no_cap_and_the_check_comes_first():
    """Bluray-1080p carries n=18 here; one mislabelled file moves its mean ~21%.
    A cap from that is a cap from noise, and too LOW a cap starves a tier
    silently. The thin-sample check precedes the tighten check — you do not
    derive a number from noise just because a looser one exists."""
    props, skipped = qc.plan_caps(
        [{"quality": {"name": "T"}, "maxSize": 10.0}],
        {"T": {"mean": 50.0, "n": 3}}, _CFG)
    assert props == [] and "thin sample" in skipped[0]["reason"]


def test_every_definition_is_accounted_for():
    """A tier is never dropped on the floor: skipped is a reported outcome."""
    defs = [
        {"quality": {"name": "Bluray-720p"}, "maxSize": None},
        {"quality": {"name": "Bluray-1080p"}, "maxSize": 100.0},
        {"quality": {"name": "WEBDL-2160p"}, "maxSize": None},
        {"quality": {"name": "NoSamples"}, "maxSize": None},
        {"title": "FlatTitle", "maxSize": None},
        {"maxSize": None},
    ]
    measured = {
        "Bluray-720p": {"mean": 52.4, "n": 1170},
        "Bluray-1080p": {"mean": 65.3, "n": 500},
        "WEBDL-2160p": {"mean": 80.0, "n": 4},
        "FlatTitle": {"mean": 30.0, "n": 100},
    }
    props, skipped = qc.plan_caps(defs, measured, _CFG)
    assert len(props) + len(skipped) == len(defs)
    reasons = {s["quality"]: s["reason"] for s in skipped}
    assert "already tighter" in reasons["Bluray-1080p"]
    assert "thin sample" in reasons["WEBDL-2160p"]
    assert "no measured files" in reasons["NoSamples"]
    assert {p["quality"] for p in props} == {"Bluray-720p", "FlatTitle"}


def test_floor_ceiling_and_unusable_rates():
    assert qc.propose_max_mb_per_min(9999, _CFG) == qc.ABSOLUTE_MAX_MB_PER_MIN
    assert qc.propose_max_mb_per_min(0.01, _CFG) == _CFG["floor_mb_per_min"]
    for bad in (None, 0, -1, "x"):
        assert qc.propose_max_mb_per_min(bad, _CFG) is None


def test_cap_never_lands_under_the_tiers_own_mean():
    """min_headroom_ratio guards a misconfigured over_ratio below 1."""
    assert qc.propose_max_mb_per_min(52.4, dict(_CFG, over_ratio=0.5)) > 52.4


def test_apply_changes_only_maxsize():
    """An *arr PUT round-trips the whole object, so anything not preserved is
    silently rewritten from a value this module never computed."""
    out = qc.apply_to_definition(
        {"id": 9, "minSize": 1.0, "preferredSize": 50.0, "maxSize": None}, 164.8)
    assert out["minSize"] == 1.0 and out["preferredSize"] == 50.0
    assert out["maxSize"] == 164.8


# ── the ratchet ──────────────────────────────────────────────────────────────

_MIN = 60


def _files(rates, quality):
    return [{"size_bytes": int(r * _MIN * 1024 ** 2), "runtime_seconds": _MIN * 60,
             "quality_name": quality} for r in rates]


_HEALTHY = [63, 64, 65, 66, 67, 62, 68, 65, 64, 66, 65, 63, 67, 64, 66, 65, 64, 66]


def test_the_units_guard_does_not_catch_a_mislabelled_disc_image():
    """The premise: [0.5, 900] is a guard against CORRUPT RUNTIMES, per its own
    docstring — 321.7 MiB/min passes it comfortably."""
    assert MIN_MB_PER_MIN <= 321.7 <= MAX_MB_PER_MIN


def test_one_disc_image_ratchets_the_anomaly_threshold_open():
    """The defect, demonstrated: the file the detector would flag gets averaged
    in as a legitimate sample, raising the threshold meant to catch the next."""
    clean = measured_stats(pd.DataFrame(_files(_HEALTHY, "Bluray-1080p")))["Bluray-1080p"]
    poisoned = measured_stats(
        pd.DataFrame(_files(_HEALTHY + [321.7], "Bluray-1080p")))["Bluray-1080p"]
    assert poisoned["mean"] > clean["mean"] * 1.15      # ~+21% on this tier's n
    assert poisoned["mean"] * 3 > clean["mean"] * 3 + 35


def test_outlier_rejection_restores_the_clean_mean():
    clean = measured_stats(pd.DataFrame(_files(_HEALTHY, "Bluray-1080p")))["Bluray-1080p"]
    fixed = measured_stats(pd.DataFrame(_files(_HEALTHY + [321.7], "Bluray-1080p")),
                           outlier_ratio=3.0)["Bluray-1080p"]
    assert abs(fixed["mean"] - clean["mean"]) < 0.01
    assert fixed["n"] == len(_HEALTHY) and fixed["dropped"] == 1


def test_rejection_is_off_by_default():
    df = pd.DataFrame(_files(_HEALTHY + [321.7], "Bluray-1080p"))
    assert measured_stats(df) == measured_stats(df, outlier_ratio=None)
    assert "dropped" not in measured_stats(df)["Bluray-1080p"]


def test_a_tiny_sample_is_left_alone():
    """You cannot identify an outlier in a sample of two; the median IS the
    midpoint. Thin tiers are protected by min_samples on the cap, not here."""
    t = measured_stats(pd.DataFrame(_files([65, 321.7], "Thin")),
                       outlier_ratio=3.0)["Thin"]
    assert t["n"] == 2 and "dropped" not in t


def test_a_legitimately_heavy_tier_is_untouched():
    """Remux runs ~235 MiB/min with a tight spread — median-anchored rejection
    must not gut it just because the absolute rate is high."""
    remux = pd.DataFrame(_files([230, 235, 240, 238, 232, 236, 234, 239, 233, 237],
                                "Remux-1080p"))
    assert measured_stats(remux) == measured_stats(remux, outlier_ratio=3.0)


def test_only_the_poisoned_tier_changes():
    multi = pd.DataFrame(_files(_HEALTHY + [321.7], "Bluray-1080p")
                         + _files([52] * 40, "Bluray-720p"))
    out = measured_stats(multi, outlier_ratio=3.0)
    assert out["Bluray-720p"]["n"] == 40 and "dropped" not in out["Bluray-720p"]
    assert out["Bluray-1080p"]["dropped"] == 1


# ── the shared push path (Radarr AND Sonarr) ─────────────────────────────

class _Log:
    def __init__(self):
        self.tables, self.warns = [], []
    def log_table(self, headers, data, title="", descriptions=None, caption=""):
        self.tables.append({"headers": headers, "rows": data, "title": title,
                            "descs": descriptions or []})
    def log_warning(self, m):
        self.warns.append(str(m))
    def log_info(self, m):
        pass


_DEFS = [
    {"id": 1, "quality": {"name": "Bluray-720p"}, "maxSize": None, "minSize": 1.0},
    {"id": 2, "quality": {"name": "Thin"}, "maxSize": None},
]
_MEAS = {"Bluray-720p": {"mean": 52.4, "n": 1170}, "Thin": {"mean": 90.0, "n": 3}}


def test_dry_run_issues_no_writes():
    """This writes *arr CONFIGURATION, which outlives the run and governs every
    future grab -- gated harder than a grab, and withheld writes are REPORTED."""
    puts, log = [], _Log()
    stats = qc.push_caps(definitions=_DEFS, measured=_MEAS, cfg=_CFG,
                         runtime_minutes=162, put=lambda d: puts.append(d) or True,
                         logger=log, dry_run=True, label="radarr/standard")
    assert puts == []
    assert stats == {"proposed": 1, "skipped": 1, "applied": 0, "would": 1, "failed": 0}
    assert "[dry_run]" in log.tables[0]["title"]


def test_armed_write_round_trips_the_whole_definition():
    puts, log = [], _Log()
    stats = qc.push_caps(definitions=_DEFS, measured=_MEAS, cfg=_CFG,
                         runtime_minutes=162, put=lambda d: (puts.append(d), True)[1],
                         logger=log, dry_run=False, label="radarr/standard")
    assert stats["applied"] == 1 and stats["failed"] == 0
    assert puts[0]["maxSize"] == 164.8
    assert puts[0]["minSize"] == 1.0 and puts[0]["id"] == 1


def test_a_rejected_write_is_a_failure_not_a_success():
    """The GLD-SON-20 lesson: `_make_request` LOGS and returns a falsy fallback
    rather than raising, so an unchecked result turns rejections into successes."""
    log = _Log()
    stats = qc.push_caps(definitions=_DEFS, measured=_MEAS, cfg=_CFG,
                         runtime_minutes=162, put=lambda d: None,
                         logger=log, dry_run=False, label="sonarr/standard")
    assert stats["applied"] == 0 and stats["failed"] == 1
    assert any("REJECTED" in w for w in log.warns)


def test_one_renderer_serves_both_services():
    """P-E: Radarr and Sonarr already keep two of everything around quality
    definitions and the pairs have drifted. Only runtime_minutes may differ."""
    plan = qc.plan_caps(_DEFS, _MEAS, _CFG)
    h_movie, _, _ = qc.render_rows(*plan, 162)
    h_episode, _, _ = qc.render_rows(*plan, 42)
    assert h_movie[:5] == h_episode[:5]
    assert "@162m" in h_movie[5] and "@42m" in h_episode[5]


def test_every_definition_reaches_the_table_with_its_reason():
    _, rows, descs = qc.render_rows(*qc.plan_caps(_DEFS, _MEAS, _CFG), 162)
    assert len(rows) == len(_DEFS)
    assert any("thin sample" in d for d in descs)
    for row in rows:                      # cp1252 console cannot encode fancy glyphs
        for cell in row:
            str(cell).encode("ascii")


def test_nothing_to_report_logs_no_table():
    log = _Log()
    qc.push_caps(definitions=[], measured={}, cfg=_CFG, runtime_minutes=162,
                 put=lambda d: True, logger=log, dry_run=True, label="x")
    assert log.tables == []
