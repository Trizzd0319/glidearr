"""validate_series_count — the cache-vs-live drift check. The orchestrator already fetched the live
``/series`` list this run (run_series_retrieval after a live refresh), so it passes it in to avoid a
second full ~8k-series fetch. These pin: list-reuse (no second fetch), the fallback fetch when no
list is given, and that the drift comparison itself is unchanged.
"""
from __future__ import annotations

from scripts.managers.services.sonarr.series.retrieval.validate import (
    SonarrSeriesRetrievalValidationManager,
)


class _Logger:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


def _mgr(cached_n, api_returns):
    fetch_calls = []

    class _Api:
        def all_series(self):
            fetch_calls.append(1)
            return api_returns

    class _Apis:
        def __getitem__(self, k): return _Api()

    m = object.__new__(SonarrSeriesRetrievalValidationManager)
    m.logger = _Logger()
    m.instance_manager = type("IM", (), {"resolve_instance": staticmethod(lambda i: i)})()
    m.series_cache = type("SC", (), {"get_all_series_ids": staticmethod(lambda i: list(range(cached_n)))})()
    m.sonarr_api = type("API", (), {"get_all_sonarr_apis": staticmethod(lambda: _Apis())})()
    return m, fetch_calls


def test_reuses_passed_live_series_no_second_fetch():
    m, fetch_calls = _mgr(cached_n=100, api_returns=[{}] * 100)
    diff = m.validate_series_count("inst", live_series=[{}] * 100)
    assert diff == 0.0
    assert fetch_calls == []          # the live list was reused — all_series() never called


def test_fetches_live_when_no_list_passed():
    m, fetch_calls = _mgr(cached_n=100, api_returns=[{}] * 100)
    m.validate_series_count("inst")   # no live_series → falls back to its own fetch
    assert fetch_calls == [1]


def test_drift_comparison_unchanged_with_reused_list():
    # live 100 vs cached 90 → 10% drift, computed off the PASSED list (no fetch).
    m, fetch_calls = _mgr(cached_n=90, api_returns=None)
    diff = m.validate_series_count("inst", live_series=[{}] * 100)
    assert abs(diff - 0.10) < 1e-9
    assert fetch_calls == []
