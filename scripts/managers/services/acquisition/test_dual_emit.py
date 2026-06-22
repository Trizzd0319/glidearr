"""Integration test for the dual-version EMIT path in AcquisitionManager.run().

Drives the real loop with fake collaborators (candidate gatherer, scorer, gateway, adder) but
the REAL Resolver, so apply_hd_baseline + plan_uhd_companion are exercised end-to-end. Asserts
that one movie under routing.movies.4k_policy=='both' with a distinct 4K instance and ample
space produces TWO adds — a <=1080 baseline on the standard instance THEN a 2160p copy on the
4K instance — both monitored and searching, in make-before-break order. With highest_only it
produces exactly ONE add (byte-for-byte today's behaviour).
"""
from __future__ import annotations

import pytest

import scripts.managers.services.acquisition as acq_mod
from scripts.managers.services.acquisition import AcquisitionManager


def _profile(name, res):
    return {"id": res, "name": name, "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


_STD_WITH_4K = [_profile("HD-720", 720), _profile("HD-1080", 1080), _profile("UHD-std", 2160)]
_UHD = [_profile("HD-1080", 1080), _profile("UHD-2160", 2160)]
_LOOKUP = {"tmdbId": 862, "genres": ["Drama"], "runtime": 81, "title": "Toy Story"}


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_table(self, *a, **k): pass


class _IM:
    def disk_free_gb(self, inst): return 9999.0
    def disk_total_gb(self, inst): return 10000.0


class _FakeGW:
    def __init__(self, service, im, config, logger):
        self.service = service
        self.im = _IM()
        self.available = True
        self._cat = {"4K": "uhd"} if service == "radarr" else {}
        self._profiles = {"standard": _STD_WITH_4K, "uhd": _UHD}

    def default_instance(self): return "standard"
    def categorized_instance(self, label="1080p"): return self._cat.get(label, "standard")
    def lookup(self, inst, term): return [dict(_LOOKUP)]
    def in_library(self, inst, id_field, value): return False
    def quality_profiles(self, inst): return self._profiles.get(inst, [])
    def root_folders(self, inst): return [{"path": "/fallback"}]
    def add(self, inst, payload): return {"id": 1}


class _FakeGatherer:
    def __init__(self, *a, **k): pass
    def gather(self): return [{"type": "movie", "ids": {"tmdb": 862}, "title": "Toy Story"}]


class _FakeScorer:
    def __init__(self, *a, **k): pass
    def score(self, enriched): return {"total": 87, "matrix": {}, "evidence": {}}
    def reason(self, matrix, **k): return ""
    def taste_profile(self, k=5): return {"genres": [], "directors": [], "actors": []}


class _RecAdder:
    instance = None

    def __init__(self, gateways, logger, *, dry_run, monitored=True, search=False):
        self.dry_run = dry_run
        self.monitored = monitored
        self.search = search
        self.calls = []
        self.fail_first = False
        _RecAdder.instance = self

    def add(self, enriched, *, search=None):
        eff = self.search if search is None else bool(search)
        self.calls.append({
            "instance": enriched.get("instance"),
            "profile": (enriched.get("quality_profile") or {}).get("name"),
            "max_res": (enriched.get("quality_profile") or {}).get("max_res"),
            "search": eff,
            "uhd": bool(enriched.get("is_uhd_companion")),
        })
        if self.fail_first and len(self.calls) == 1:
            return {"action": "add-failed", "ok": False}
        return {"action": "would-add" if self.dry_run else "added", "ok": True,
                "result": {"id": len(self.calls)}}


def _patch(monkeypatch, *, fail_first=False):
    monkeypatch.setattr(acq_mod, "ArrGateway", _FakeGW)
    monkeypatch.setattr(acq_mod, "CandidateGatherer", _FakeGatherer)
    monkeypatch.setattr(acq_mod, "AcquisitionScorer", _FakeScorer)
    monkeypatch.setattr(acq_mod, "Adder", _RecAdder)
    _RecAdder.instance = None
    # the recorder is constructed inside run(); flag it after construction via a closure
    orig_init = _RecAdder.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        self.fail_first = fail_first
    monkeypatch.setattr(_RecAdder, "__init__", _init)


def _mgr(config, *, dry_run=False):
    m = object.__new__(AcquisitionManager)
    m.config = config
    m.logger = _Logger()
    m.dry_run = dry_run
    m.global_cache = None
    m.trakt = m.mal = m.plex = m.sonarr = m.radarr = None
    return m


def _cfg(policy="both", *, min_score=0):
    return {
        "acquisition": {"enabled": True, "monitored": True, "search_on_add": False},
        "free_space_limit": 1000,
        "movieRootFolders": {"standard": "/m/std", "kids": "/m/kids", "anime": "/m/anime", "4k": "/m/4k"},
        "rootFolders": {},
        "radarr_instances_categorized": {"4K": "uhd"},
        "routing": {"configured": True, "tv": {},
                    "movies": {"4k_policy": policy, "4k_dual_min_score": min_score,
                               "kids_bucket_enabled": False}},
    }


class _CapLogger(_Logger):
    def __init__(self):
        self.tables = []
        self.infos = []
    def log_info(self, m="", *a, **k): self.infos.append(str(m))
    def log_table(self, headers, rows, **k): self.tables.append((headers, rows))


class _DictCache:
    def __init__(self, d): self.d = dict(d)
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def test_decision_table_attributes_saga_and_profile(monkeypatch):
    """A recommendation add that is ALSO a saga member gets a 'saga' column (short key) in the
    decision table and a friendly-name + profile-reasoning stanza in the elevation breakdown."""
    _patch(monkeypatch)
    src = {"universes": {"mcu": {"timeline": True, "items": [{"media": "movie", "tmdb": 862}]}}}
    m = _mgr(_cfg("highest_only"))
    m.logger = _CapLogger()
    m.global_cache = _DictCache({"plex/playlists/universe_source": src})
    m.run()

    headers, rows = m.logger.tables[-1]
    assert "saga" in headers
    assert rows[0][headers.index("saga")] == "Marvel Cinematic Universe"   # full name in the table
    text = "\n".join(m.logger.infos)
    assert "saga: part of Marvel Cinematic Universe" in text       # friendly name in the breakdown
    assert "profile: UHD-std  (score 87 picks up to the 2160p tier) -> standard" in text


def test_both_emits_baseline_then_4k(monkeypatch):
    _patch(monkeypatch)
    _mgr(_cfg("both")).run()
    calls = _RecAdder.instance.calls
    assert len(calls) == 2
    base, uhd = calls
    assert base["instance"] == "standard" and base["max_res"] == 1080 and base["uhd"] is False
    assert base["search"] is True                         # dual baseline searches ON
    assert uhd["instance"] == "uhd" and uhd["max_res"] == 2160 and uhd["uhd"] is True
    assert uhd["search"] is True                          # 4K bonus searches ON


def test_highest_only_emits_single_add(monkeypatch):
    _patch(monkeypatch)
    _mgr(_cfg("highest_only")).run()
    calls = _RecAdder.instance.calls
    assert len(calls) == 1
    assert calls[0]["uhd"] is False
    assert calls[0]["search"] is False                    # search_on_add default, untouched


def test_dry_run_emits_two_would_adds_no_post(monkeypatch):
    _patch(monkeypatch)
    _mgr(_cfg("both"), dry_run=True).run()
    calls = _RecAdder.instance.calls
    assert len(calls) == 2                                # both planned, neither POSTed (adder dry_run)
    assert {c["instance"] for c in calls} == {"standard", "uhd"}


def test_companion_skipped_when_baseline_fails(monkeypatch):
    _patch(monkeypatch, fail_first=True)
    _mgr(_cfg("both")).run()
    calls = _RecAdder.instance.calls
    assert len(calls) == 1                                # baseline add-failed -> no 4K companion
    assert calls[0]["instance"] == "standard"


def test_companion_skipped_below_threshold(monkeypatch):
    _patch(monkeypatch)
    _mgr(_cfg("both", min_score=95)).run()                # score 87 < 95 -> no 4K bonus
    calls = _RecAdder.instance.calls
    assert len(calls) == 1
    assert calls[0]["instance"] == "standard" and calls[0]["max_res"] == 1080
