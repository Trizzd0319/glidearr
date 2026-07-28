"""Tests for labels/first_run.py — the one-shot fresh-install backfill trigger.

The trigger's whole job is deciding WHEN to replay (the replay itself is covered
end-to-end by scripts/support/tools/test_ml_backfill_snapshots.py), so these
tests inject a fake ``main(argv)`` and assert the decision, the bound, the
one-shot marker and the fault isolation:

  * fires exactly once on an empty store with Tautulli history present;
  * is a no-op on a populated store (matured prospective rows, or backfill rows
    already there) — and records that permanently so the check stops costing;
  * never fires twice: marker, then process guard;
  * the CLI it builds is bounded (grid days x max points) and clamped to the
    first real event;
  * a failing/raising tool is a logged no-op, never an exception.

    python -m pytest scripts/managers/machine_learning/labels/test_first_run.py -q
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pandas as pd
import pytest

from scripts.managers.machine_learning.labels import first_run

UTC = timezone.utc
NOW = datetime(2026, 7, 26, tzinfo=UTC)


class _Log:
    def __init__(self):
        self.lines: list = []

    def __getattr__(self, name):
        if not name.startswith("log_"):
            raise AttributeError(name)
        return lambda msg: self.lines.append(f"{name[4:]}: {msg}")

    def text(self) -> str:
        return "\n".join(self.lines)


class _Runner:
    """Stand-in for ml_backfill_snapshots.main(argv)."""

    def __init__(self, rc: int = 0, raises: bool = False, out: str = ""):
        self.rc, self.raises, self.out = rc, raises, out
        self.calls: list = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.raises:
            raise RuntimeError("boom")
        if self.out:
            print(self.out)
        return self.rc


@pytest.fixture(autouse=True)
def _reset_guard():
    first_run.reset_process_guard()
    yield
    first_run.reset_process_guard()


def _fresh_cache(tmp_path: Path, *, events: int = 40,
                 first_event: datetime = datetime(2026, 1, 5, tzinfo=UTC),
                 instance: str = "standard") -> Path:
    """A first-boot cache: Tautulli history + movie_files, NO snapshots yet."""
    hist = tmp_path / "tautulli" / "history"
    hist.mkdir(parents=True)
    step = timedelta(days=3)
    hist.joinpath("all.json").write_text(json.dumps([
        {"date": int((first_event + step * i).timestamp()), "media_type": "movie",
         "rating_key": f"r{i}", "title": f"M{i}", "percent_complete": 100}
        for i in range(events)]), encoding="utf-8")
    mf = tmp_path / "radarr" / instance
    mf.mkdir(parents=True)
    pd.DataFrame({"tmdb_id": [1, 2], "title": ["A", "B"]}).to_parquet(
        mf / "movie_files.parquet", index=False)
    return tmp_path


def _snapshot_rows(tmp_path: Path, rows: list) -> None:
    d = tmp_path / "ml" / "snapshots" / "radarr"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(d / "2026-07.parquet", index=False)


def _row(ts: datetime, source: str = "prospective", entity: str = "1") -> dict:
    return {"snapshot_ts": ts.isoformat(), "snapshot_date": f"{ts:%Y-%m-%d}",
            "service": "radarr", "instance": "standard", "entity_id": entity,
            "watchability_score": 40.0, "source": source}


# ── it fires, once ────────────────────────────────────────────────────────────

def test_fires_once_on_an_empty_store(tmp_path):
    base = _fresh_cache(tmp_path)
    runner = _Runner(out="appended 1200 new row(s)\nlabels @ horizon 14d: n_pos=31")
    log = _Log()

    out = first_run.maybe_backfill_on_first_run(
        {}, base, logger=log, now=NOW, runner=runner)
    assert out["status"] == "ok" and len(runner.calls) == 1
    assert "fresh install" in log.text() and "backfill ok" in log.text()
    assert "appended 1200 new row(s)" in out["summary"]

    # the marker is the one-shot guard: it exists, and it names the outcome
    marker = first_run.marker_path(base)
    assert marker.exists()
    blob = json.loads(marker.read_text(encoding="utf-8"))
    assert blob["status"] == "ok" and blob["rc"] == 0 and blob["argv"]

    # a second call in the same process does nothing (process guard) …
    assert first_run.maybe_backfill_on_first_run(
        {}, base, now=NOW, runner=runner)["status"] == "skipped"
    assert len(runner.calls) == 1
    # … and neither does a fresh process, because the marker survives
    first_run.reset_process_guard()
    out = first_run.maybe_backfill_on_first_run({}, base, now=NOW, runner=runner)
    assert out["status"] == "skipped" and "already run once" in out["reason"]
    assert len(runner.calls) == 1


def test_todays_prospective_rows_do_not_count_as_evidence(tmp_path):
    """The trigger runs at the END of a run, by which time the run has already
    appended today's prospective snapshot — which matures a horizon from now and
    is exactly why the reconstruction is needed."""
    base = _fresh_cache(tmp_path)
    _snapshot_rows(base, [_row(NOW), _row(NOW - timedelta(days=1), entity="2")])
    runner = _Runner()
    out = first_run.maybe_backfill_on_first_run({}, base, now=NOW, runner=runner)
    assert out["status"] == "ok" and len(runner.calls) == 1


# ── it is a no-op on a populated store ────────────────────────────────────────

@pytest.mark.parametrize("rows,why", [
    ([_row(NOW - timedelta(days=30))], "a matured prospective row"),
    ([_row(NOW, source="backfill")], "an existing backfill row"),
])
def test_no_op_on_a_populated_store(tmp_path, rows, why):
    base = _fresh_cache(tmp_path)
    _snapshot_rows(base, rows)
    runner = _Runner()
    out = first_run.maybe_backfill_on_first_run({}, base, now=NOW, runner=runner)
    assert out["status"] == "skipped", why
    assert "already carries" in out["reason"]
    assert runner.calls == []
    # a permanent "no" is recorded, so the store is never re-read on later runs
    blob = json.loads(first_run.marker_path(base).read_text(encoding="utf-8"))
    assert blob["status"] == "not_needed"


def test_transient_misses_leave_no_marker(tmp_path):
    """No history / no movie_files yet is a "try again next run", not a "never"."""
    runner = _Runner()
    out = first_run.maybe_backfill_on_first_run({}, tmp_path, now=NOW, runner=runner)
    assert out["status"] == "skipped" and "no Tautulli history" in out["reason"]
    assert not first_run.marker_path(tmp_path).exists()

    hist = tmp_path / "tautulli" / "history"
    hist.mkdir(parents=True)
    hist.joinpath("all.json").write_text(json.dumps(
        [{"date": int(datetime(2026, 3, 1, tzinfo=UTC).timestamp()),
          "media_type": "movie"}]), encoding="utf-8")
    out = first_run.maybe_backfill_on_first_run({}, tmp_path, now=NOW, runner=runner)
    assert out["status"] == "skipped" and "movie_files.parquet" in out["reason"]
    assert not first_run.marker_path(tmp_path).exists()
    assert runner.calls == []


def test_config_gates(tmp_path):
    base = _fresh_cache(tmp_path)
    runner = _Runner()
    for cfg in ({"ml": {"snapshots": {"backfill_on_first_run": False}}},
                {"ml": {"snapshots": {"enabled": False}}}):
        out = first_run.maybe_backfill_on_first_run(cfg, base, now=NOW, runner=runner)
        assert out["status"] == "skipped" and "disabled" in out["reason"]
    assert runner.calls == []
    assert not first_run.marker_path(base).exists()
    assert first_run.enabled({}) is True                 # DEFAULT TRUE
    assert first_run.enabled(None) is True


# ── the invocation is bounded ─────────────────────────────────────────────────

def test_argv_is_bounded_by_the_grid_cap(tmp_path):
    base = _fresh_cache(tmp_path, first_event=datetime(2020, 1, 1, tzinfo=UTC))
    runner = _Runner()
    first_run.maybe_backfill_on_first_run({}, base, now=NOW, runner=runner)
    argv = dict(zip(runner.calls[0][::2], runner.calls[0][1::2]))
    assert argv["--cache-base"] == str(base)
    assert argv["--instance"] == "standard"
    assert argv["--grid-days"] == "7"
    assert argv["--horizon-days"] == "14"
    # end cap is now-horizon; 26 weekly points back from it, NOT 2020
    start = datetime.strptime(argv["--start"], "%Y-%m-%d").replace(tzinfo=UTC)
    span_days = ((NOW - timedelta(days=14)) - start).days
    assert span_days == 7 * (first_run.DEFAULT_MAX_GRID_POINTS - 1) == 175


def test_start_is_clamped_to_the_first_real_event(tmp_path):
    """A young household must not pay to rescore the library over grid dates
    that predate its own history (all-zero features, pure cost)."""
    base = _fresh_cache(tmp_path, first_event=datetime(2026, 6, 20, tzinfo=UTC))
    runner = _Runner()
    first_run.maybe_backfill_on_first_run({}, base, now=NOW, runner=runner)
    argv = dict(zip(runner.calls[0][::2], runner.calls[0][1::2]))
    assert argv["--start"] == "2026-06-20"


def test_grid_knobs_are_configurable_and_unbounded_is_possible(tmp_path):
    base = _fresh_cache(tmp_path, first_event=datetime(2020, 1, 1, tzinfo=UTC))
    cfg = {"ml": {"snapshots": {"backfill_grid_days": 14,
                                "backfill_max_grid_points": 4}}}
    runner = _Runner()
    first_run.maybe_backfill_on_first_run(cfg, base, now=NOW, runner=runner)
    argv = dict(zip(runner.calls[0][::2], runner.calls[0][1::2]))
    assert argv["--grid-days"] == "14"
    start = datetime.strptime(argv["--start"], "%Y-%m-%d").replace(tzinfo=UTC)
    assert ((NOW - timedelta(days=14)) - start).days == 14 * 3

    # 0 points = unbounded: fall back to the first event, like the CLI by hand
    first_run.reset_process_guard()
    first_run.marker_path(base).unlink()
    runner = _Runner()
    first_run.maybe_backfill_on_first_run(
        {"ml": {"snapshots": {"backfill_max_grid_points": 0}}}, base,
        now=NOW, runner=runner)
    argv = dict(zip(runner.calls[0][::2], runner.calls[0][1::2]))
    assert argv["--start"] == "2020-01-01"


def test_instance_comes_from_config(tmp_path):
    base = _fresh_cache(tmp_path, instance="movies4k")
    cfg = {"radarr_instances": {"default_instance": "movies4k",
                                "movies4k": {"url": "x"}, "other": {}}}
    assert first_run.resolve_instance(cfg) == "movies4k"
    assert first_run.resolve_instance({}) == "standard"
    assert first_run.resolve_instance({"radarr_instances": {"solo": {}}}) == "solo"
    runner = _Runner()
    out = first_run.maybe_backfill_on_first_run(cfg, base, now=NOW, runner=runner)
    assert out["status"] == "ok"
    assert "movies4k" in runner.calls[0]


# ── fault isolation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("runner", [_Runner(rc=1), _Runner(raises=True)])
def test_a_failing_tool_is_a_logged_no_op(tmp_path, runner):
    base = _fresh_cache(tmp_path)
    log = _Log()
    out = first_run.maybe_backfill_on_first_run(
        {}, base, logger=log, now=NOW, runner=runner)
    assert out["status"] == "failed" and out["rc"] == 1
    assert "backfill failed" in log.text()
    # still one-shot: a broken cache must not retry every run forever
    blob = json.loads(first_run.marker_path(base).read_text(encoding="utf-8"))
    assert blob["status"] == "failed"
    first_run.reset_process_guard()
    assert first_run.maybe_backfill_on_first_run(
        {}, base, now=NOW, runner=runner)["status"] == "skipped"


def test_a_broken_snapshot_store_never_raises(tmp_path):
    base = _fresh_cache(tmp_path)
    d = base / "ml" / "snapshots" / "radarr"
    d.mkdir(parents=True)
    (d / "2026-07.parquet").write_bytes(b"not a parquet file")
    assert first_run.store_has_evidence(base) is False
    runner = _Runner()
    assert first_run.maybe_backfill_on_first_run(
        {}, base, now=NOW, runner=runner)["status"] == "ok"


def test_the_real_tool_accepts_the_argv_we_build(tmp_path):
    """Contract test against the REAL CLI: every flag this module emits must be
    one argparse knows, or the first (and only) attempt dies in the parser. An
    empty cache makes the tool return 1 immediately — that is the tool's own
    "nothing to replay" exit, reached only if parsing succeeded."""
    import contextlib
    import io

    mod = first_run._load_tool()
    assert callable(mod.main)
    argv = first_run.build_argv({}, tmp_path, now=NOW,
                                first_event=datetime(2026, 1, 1, tzinfo=UTC),
                                config_path=tmp_path / "config.json")
    assert {"--cache-base", "--instance", "--grid-days", "--horizon-days",
            "--start", "--config"} == {a for a in argv if a.startswith("--")}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(list(argv) + ["--dry-run"])   # SystemExit here = bad flag
    assert rc == 1 and "nothing to replay" in buf.getvalue()


# ── the plan_summary seam ─────────────────────────────────────────────────────

def test_plan_summary_calls_the_trigger_and_never_breaks(tmp_path, monkeypatch):
    from scripts.managers.machine_learning.ledger.plan_summary import PlanSummary

    class _Cache:
        cache_root = tmp_path

    seen: list = []
    monkeypatch.setattr(first_run, "maybe_backfill_on_first_run",
                        lambda *a, **k: seen.append(k) or {"status": "skipped"})
    ps = PlanSummary(config={"ml": {"thresholds": {"mode": "off"}}},
                     global_cache=_Cache())
    assert ps.log_thresholds() == {}
    assert len(seen) == 1 and seen[0]["horizon_days"] == 14

    def _boom(*_a, **_k):
        raise RuntimeError("nope")

    monkeypatch.setattr(first_run, "maybe_backfill_on_first_run", _boom)
    assert ps.first_run_backfill() == {}          # wrapped, never fatal
    assert ps.log_thresholds() == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
