"""SonarrSeriesQualityManager.run_monitor_by_watchability — the monitor-only Sonarr policy.

Unmonitors the persistently low-affinity tail (Sonarr stops grabbing it), re-monitors climbers, with
a sticky hysteresis band + optional dwell and keep-tag / household-watched hard guards. Never deletes.
Driven via object.__new__ + fakes (no network). The pure routing is covered by
test_series_monitor_policy.py; these assert the manager wiring (score read, guards, series/editor PUT,
dwell clock).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.services.sonarr.series.quality import SonarrSeriesQualityManager

_CLOCK_KEY = "sonarr/standard/monitor_demote_clock"


class _Logger:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_table(self, *a, **k): pass


class _EpMgr:
    def __init__(self, df): self._df = df
    def load(self, inst): return self._df


class _SeriesCache:
    def __init__(self, series): self._s = series
    def iter_all_series(self, inst): return list(self._s)


class _Reg:
    def __init__(self, ep): self._ep = ep
    def get(self, kind, name): return self._ep if name == "SonarrCacheEpisodeFilesManager" else None


class _Cache:
    def __init__(self, clock=None):
        self._d = {} if clock is None else {_CLOCK_KEY: dict(clock)}
    def get(self, k, default=None): return self._d.get(k, default)
    def set(self, k, v): self._d[k] = v


class _Api:
    def __init__(self, tags, tag_fails=False):
        self._tags = tags; self._tag_fails = tag_fails; self.puts = []
    def _make_request(self, inst, endpoint, method="GET", payload=None, fallback=None):
        if endpoint == "tag":
            # tag_fails simulates a transient fetch failure → the client returns the fallback
            # (None here); a genuine empty catalogue returns the real [] instead.
            return fallback if self._tag_fails else list(self._tags or [])
        if endpoint == "series/editor" and method == "PUT":
            self.puts.append(payload); return {"ok": True}
        return fallback


def _df(rows):   # rows = [(series_id, score, is_watched), ...] → one episode row per series
    return pd.DataFrame([{"series_id": s, "watchability_score": sc, "is_watched": w} for s, sc, w in rows])


def _series(sid, monitored, tags=None):
    return {"id": sid, "title": f"S{sid}", "monitored": monitored, "tags": tags or []}


def _mgr(df, series, *, tags=None, tag_fails=False, dry_run=False, clock=None, cfg_extra=None):
    m = object.__new__(SonarrSeriesQualityManager)
    m.logger = _Logger()
    m.dry_run = dry_run
    cfg = {"series_monitor_score_threshold": 35,
           "series_demote_score_threshold": 20, "series_demote_dwell_days": 0}
    if cfg_extra:
        cfg.update(cfg_extra)
    m.config = cfg
    m.instance_manager = type("IM", (), {"resolve_instance": staticmethod(lambda i: i or "standard")})()
    m.registry = _Reg(_EpMgr(df))
    m.sonarr_cache = type("SC", (), {"series": _SeriesCache(series)})()
    m.sonarr_api = _Api(tags or [], tag_fails=tag_fails)
    m.global_cache = _Cache(clock)
    return m


def test_runs_unconditionally_no_enable_switch():
    # There is no on/off config — the policy always runs and acts on the low-affinity tail.
    m = _mgr(_df([(1, 5, False)]), [_series(1, True)])
    stats = m.run_monitor_by_watchability("standard")
    assert stats["checked"] == 1
    assert stats["unmonitored"] == 1
    assert m.sonarr_api.puts == [{"seriesIds": [1], "monitored": False}]


def test_unmonitor_low_monitor_high_guard_keep_and_watched_defer_unscored():
    KEEP = 99
    df = _df([(1, 12, False), (2, 80, False), (3, 5, False), (4, 5, True), (5, 25, False)])
    series = [_series(1, True), _series(2, False), _series(3, True, [KEEP]),
              _series(4, True), _series(5, True), _series(6, True)]   # 6: no score row → defer
    m = _mgr(df, series, tags=[{"id": KEEP, "label": "keep_series"}])
    stats = m.run_monitor_by_watchability("standard")

    puts = {(tuple(p["seriesIds"]), p["monitored"]) for p in m.sonarr_api.puts}
    assert ((1,), False) in puts          # low-affinity monitored → unmonitor
    assert ((2,), True) in puts           # recovered unmonitored → monitor
    assert stats["checked"] == 6
    assert stats["unmonitored"] == 1 and stats["monitored"] == 1
    assert stats["guarded"] == 2          # series 3 (keep-tagged) + series 4 (watched) — spared
    assert stats["held"] == 1             # series 5 (in the sticky band)
    assert stats["deferred"] == 1         # series 6 (no score yet)


def test_dry_run_counts_but_does_not_mutate():
    m = _mgr(_df([(1, 12, False), (2, 80, False)]),
             [_series(1, True), _series(2, False)], dry_run=True)
    stats = m.run_monitor_by_watchability("standard")
    assert m.sonarr_api.puts == []
    assert stats["unmonitored"] == 1 and stats["monitored"] == 1


def test_dwell_delays_unmonitor_and_starts_clock():
    m = _mgr(_df([(1, 12, False)]), [_series(1, True)], cfg_extra={"series_demote_dwell_days": 7})
    stats = m.run_monitor_by_watchability("standard")
    assert stats["unmonitored"] == 0 and stats["aging"] == 1
    assert "1" in (m.global_cache.get(_CLOCK_KEY) or {})


def test_dwell_satisfied_unmonitors():
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    m = _mgr(_df([(1, 12, False)]), [_series(1, True)],
             cfg_extra={"series_demote_dwell_days": 7}, clock={"1": old})
    stats = m.run_monitor_by_watchability("standard")
    assert stats["unmonitored"] == 1


def test_clock_resets_when_score_recovers_into_band():
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    m = _mgr(_df([(1, 25, False)]), [_series(1, True)],     # 25 >= floor → sticky, clock dropped
             cfg_extra={"series_demote_dwell_days": 7}, clock={"1": old})
    stats = m.run_monitor_by_watchability("standard")
    assert stats["unmonitored"] == 0
    assert "1" not in (m.global_cache.get(_CLOCK_KEY) or {})


def test_failed_editor_call_counts_failed():
    class _FailApi(_Api):
        def _make_request(self, inst, endpoint, method="GET", payload=None, fallback=None):
            if endpoint == "series/editor":
                raise RuntimeError("boom")
            return super()._make_request(inst, endpoint, method=method, payload=payload, fallback=fallback)
    m = _mgr(_df([(1, 12, False)]), [_series(1, True)])
    m.sonarr_api = _FailApi([])
    stats = m.run_monitor_by_watchability("standard")
    assert stats["failed"] == 1 and stats["unmonitored"] == 0


# ── Adversarial-review fixes ──────────────────────────────────────────────────────────────────

def test_keep_guard_fail_safe_defers_unmonitor_when_tag_fetch_fails():
    # The keep-tag fetch FAILS (client returns the fallback=None). We can't prove a series isn't
    # pinned, so the unmonitor leg is deferred this pass — but climbers are still re-monitored.
    m = _mgr(_df([(1, 12, False), (2, 80, False)]),
             [_series(1, True), _series(2, False)], tag_fails=True)
    stats = m.run_monitor_by_watchability("standard")
    assert stats["unmonitored"] == 0
    assert stats["guard_deferred"] == 1
    assert stats["monitored"] == 1                       # climber recovery is still safe
    puts = {(tuple(p["seriesIds"]), p["monitored"]) for p in m.sonarr_api.puts}
    assert ((1,), False) not in puts                     # never unmonitored with the guard down
    assert ((2,), True) in puts


def test_genuinely_empty_tag_catalogue_still_unmonitors():
    # An empty [] catalogue (success, not a failure) means no keep tags exist → nothing is pinned →
    # unmonitoring the low-affinity tail is safe and proceeds.
    m = _mgr(_df([(1, 12, False)]), [_series(1, True)], tags=[])
    stats = m.run_monitor_by_watchability("standard")
    assert stats["unmonitored"] == 1 and stats["guard_deferred"] == 0


def test_unmonitor_ids_are_chunked():
    rows = [(i, 5, False) for i in range(1, 251)]        # 250 low-affinity monitored series
    m = _mgr(_df(rows), [_series(i, True) for i in range(1, 251)])
    stats = m.run_monitor_by_watchability("standard")
    assert stats["unmonitored"] == 250
    # 250 / 200-per-chunk → 2 PUTs, each <= 200, all monitored=False, covering every id once
    assert len(m.sonarr_api.puts) == 2
    assert all(len(p["seriesIds"]) <= 200 and p["monitored"] is False for p in m.sonarr_api.puts)
    seen = sorted(sid for p in m.sonarr_api.puts for sid in p["seriesIds"])
    assert seen == list(range(1, 251))


def test_dry_run_dwell_preview_is_idempotent():
    # dwell met (old clock) + dry_run: the candidate is NOT actually unmonitored, so its dwell clock
    # must be preserved — else the next preview resets age->0 and the series oscillates.
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    m = _mgr(_df([(1, 12, False)]), [_series(1, True)], dry_run=True,
             cfg_extra={"series_demote_dwell_days": 7}, clock={"1": old})
    s1 = m.run_monitor_by_watchability("standard")
    assert s1["unmonitored"] == 1
    assert "1" in (m.global_cache.get(_CLOCK_KEY) or {})  # clock kept across the preview
    s2 = m.run_monitor_by_watchability("standard")        # a second preview pass
    assert s2["unmonitored"] == 1                         # still surfaces — not reset to aging
