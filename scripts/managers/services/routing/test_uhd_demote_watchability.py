"""UhdReconcileManager._demote_overqualified_4k — the WATCHABILITY-driven 4K demote.

The pressure-independent companion to evict_uhd_first: a dual-version 4K BONUS whose saga-aware
watch-likelihood falls below the UHD threshold has its 4K FILE deleted + record unmonitored — but
ONLY while a 1080p baseline FILE survives on a standard-tier instance (never the last copy) and
never for a keep/universe pin. The 4K record is KEPT (fileless, ledgered); when the score climbs
back the companion is re-monitored + re-searched (recover). Gated by demote_4k_on_watchability +
4k_policy=='both' + deletions_consent + the backup gate; default OFF.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.services.backup import GATE_KEY
from scripts.managers.services.routing.uhd_reconcile import UhdReconcileManager

_LEDGER_KEY = "radarr/ultra/watch_demoted_4k"
_CLOCK_KEY = "radarr/ultra/watch_demote_clock"


def _profile(pid, name, res):
    return {"id": pid, "name": name, "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


# A ≤1080 standard-instance profile set so the 4K-only rehome can pick an HD baseline.
_STD_PROFILES = [_profile(3, "HD-1080", 1080), _profile(2, "HD-720", 720)]


class _Im:
    def __init__(self, libraries, *, tags=None, profiles=None, roots=None, free=9999.0, total=10000.0):
        self._lib = libraries
        self._tags = tags or {}
        self._profiles = profiles or {}
        self._roots = roots or {}
        self._free, self._total = free, total
        self.puts, self.deletes, self.commands, self.adds, self.gets = [], [], [], [], []

    def disk_free_gb(self, inst): return self._free
    def disk_total_gb(self, inst): return self._total
    def _get_apis(self): return {n: object() for n in self._lib}

    def _make_request(self, name, endpoint, method="GET", payload=None, fallback=None, **kw):
        if method == "POST" and endpoint == "movie":
            self.adds.append((name, payload)); return {"id": 9000}
        if method == "POST" and endpoint == "command":
            self.commands.append((name, payload)); return {"id": 1}
        if method == "PUT" and endpoint == "movie/editor":
            self.puts.append((name, payload)); return {"ok": True}
        if method == "DELETE":
            self.deletes.append((name, endpoint)); return {}
        self.gets.append((name, endpoint))
        table = {"movie": self._lib, "tag": self._tags,
                 "qualityprofile": self._profiles, "rootfolder": self._roots}
        return table.get(endpoint, {}).get(name, fallback if fallback is not None else [])


class _Mgr:
    def __init__(self, im): self.instance_manager = im


class _SP:
    def __init__(self, df): self._df = df
    def load_movie_files(self, inst): return self._df


class _Reg:
    def __init__(self, sp): self._sp = sp
    def get(self, kind, key): return self._sp if key == "RadarrSpacePressureManager" else None


class _Cache:
    def __init__(self, *, gate=None, ledger=None, clock=None):
        self._store = {}
        if gate is not None:
            self._store[GATE_KEY] = gate
        if ledger is not None:
            self._store[_LEDGER_KEY] = ledger
        if clock is not None:
            self._store[_CLOCK_KEY] = clock

    def get(self, k, *a, **kw): return self._store.get(k)
    def set(self, k, v): self._store[k] = v


def _u_file(tmdb, *, fid=None, res=2160, monitored=True, tags=None):
    return {"id": tmdb, "tmdbId": tmdb, "title": f"m{tmdb}", "hasFile": True, "monitored": monitored,
            "tags": tags or [], "movieFile": {"id": fid if fid is not None else 500 + tmdb,
                                              "quality": {"quality": {"resolution": res}}}}


def _u_shell(tmdb, *, monitored=False):
    return {"id": tmdb, "tmdbId": tmdb, "title": f"m{tmdb}", "hasFile": False, "monitored": monitored, "tags": []}


def _s_file(tmdb):
    return {"id": tmdb, "tmdbId": tmdb, "title": f"m{tmdb}", "hasFile": True,
            "movieFile": {"id": tmdb, "quality": {"quality": {"resolution": 1080}}}}


def _cfg(*, demote=True, consent=True, threshold=70, gap=10, dwell=0):
    c = {"routing": {"configured": True, "reorg_mode": "off",
                     "movies": {"4k_policy": "both", "4k_dual_min_score": threshold,
                                "demote_4k_on_watchability": demote,
                                "4k_demote_gap": gap, "4k_demote_dwell_days": dwell}, "tv": {}},
         "movieRootFolders": {"standard": "/data/media/movies/standard", "4k": "/data/media/movies/4k"},
         "radarr_instances": {"standard": {"url": "s"}, "ultra": {"url": "u"}, "default_instance": "standard"},
         "radarr_instances_categorized": {"4K": "ultra"}}
    if consent:
        c["deletions_consent"] = True
    return c


def _df(rows):
    return pd.DataFrame([{"tmdb_id": t, "is_watched": w, "watch_count": wc, "watchability_score": s}
                        for (t, w, wc, s) in rows])


def _run(standard, ultra, df, *, cfg=None, dry_run=False, gate=None, ledger=None, clock=None,
         tags=None, std_profiles=_STD_PROFILES, monkeypatch=None):
    if monkeypatch is not None:
        for v in ("RECOMMENDARR_DELETIONS_CONSENT", "GLIDEARR_DELETIONS_CONSENT"):
            monkeypatch.delenv(v, raising=False)
    im = _Im({"standard": standard, "ultra": ultra}, tags=tags or {},
             profiles={"standard": std_profiles} if std_profiles else {},
             roots={"standard": [{"path": "/data/media/movies/standard"}]})
    cache = _Cache(gate=gate, ledger=ledger, clock=clock)
    UhdReconcileManager(config=cfg or _cfg(), logger=None, radarr=_Mgr(im), dry_run=dry_run,
                        registry=_Reg(_SP(df)), global_cache=cache).run()
    return im, cache


# ── DEMOTE ────────────────────────────────────────────────────────────────────
def test_demotes_low_watchability_companion_with_surviving_baseline(monkeypatch):
    # tmdb 101: 2160p on ultra (file id 999), unwatched low score → lk < 70; a 1080p baseline FILE
    # survives on standard → demote: unmonitor the 4K record, delete the 4K file. Stale ledger pruned.
    im, cache = _run([_s_file(101)], [_u_file(101, fid=999)], _df([(101, False, 0, 10)]),
                     ledger={"999": "old"}, monkeypatch=monkeypatch)
    assert ("ultra", {"movieIds": [101], "monitored": False}) in im.puts   # unmonitor first
    assert ("ultra", "moviefile/999") in im.deletes                        # then delete the 4K FILE
    assert not any(ep.startswith("movie/") and not ep.startswith("moviefile") for _, ep in im.deletes)
    led = cache.get(_LEDGER_KEY)
    assert "101" in led and "999" not in led                              # ledgered; vanished entry pruned


def test_4k_only_rehomes_baseline_first_never_evicts_yet(monkeypatch):
    # A low-watchability 2160p on ultra with NO standard copy at all (4K-ONLY) → make-before-break:
    # grab a ≤1080 baseline on standard NOW; the 4K is UNTOUCHED (evicted only next run once it lands).
    im, _ = _run([], [_u_file(102)], _df([(102, False, 0, 10)]), monkeypatch=monkeypatch)
    add = [a for a in im.adds if a[0] == "standard"]
    assert len(add) == 1 and add[0][1]["tmdbId"] == 102            # baseline grabbed on standard
    assert add[0][1]["qualityProfileId"] == 3                      # the ≤1080 (HD-1080) profile
    assert add[0][1]["addOptions"] == {"searchForMovie": True}
    assert im.deletes == [] and im.puts == []                     # 4K file + record untouched this run


def test_4k_only_not_evicted_when_baseline_cannot_be_grabbed(monkeypatch):
    # NEVER the last copy: if standard has no ≤1080 profile to rehome into, the 4K stays put.
    im, _ = _run([], [_u_file(102)], _df([(102, False, 0, 10)]), std_profiles=[], monkeypatch=monkeypatch)
    assert im.adds == [] and im.deletes == [] and im.puts == []


def test_4k_only_baseline_pending_import_does_not_regrab(monkeypatch):
    # A 4K-only title already has a FILELESS standard record (rehome in flight) → don't re-grab; wait
    # for it to import (then a later run evicts the 4K). No add, no delete.
    pending = {"id": 102, "tmdbId": 102, "title": "m102", "hasFile": False}
    im, _ = _run([pending], [_u_file(102)], _df([(102, False, 0, 10)]), monkeypatch=monkeypatch)
    assert im.adds == [] and im.deletes == [] and im.puts == []


def test_keeps_companion_that_still_warrants_4k(monkeypatch):
    # tmdb 103 watched, high score → lk 74 >= 70 → warrants 4K → kept even with a baseline present.
    im, _ = _run([_s_file(103)], [_u_file(103)], _df([(103, True, 0, 90)]), monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []


def test_spares_keep_or_universe_pinned_title(monkeypatch):
    # tmdb 104 low watchability + a surviving baseline, BUT keep-movie-tagged → spared the demote.
    im, _ = _run([_s_file(104)], [_u_file(104, tags=[1])], _df([(104, False, 0, 10)]),
                 tags={"ultra": [{"id": 1, "label": "keep-movie"}]}, monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []


def test_never_demotes_unscored_title(monkeypatch):
    # No watchability row for tmdb 105 (lk None) → never demote on missing data.
    im, _ = _run([_s_file(105)], [_u_file(105)], _df([(999, False, 0, 10)]), monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []


# ── RECOVER ───────────────────────────────────────────────────────────────────
def test_recovers_demoted_shell_when_score_climbs_back(monkeypatch):
    # tmdb 101 is a fileless, unmonitored 4K shell in the ledger; its score recovered (lk 74 >= 70)
    # → re-monitor + search; ledger entry cleared. (file id keyed entry under tmdb, not file, here.)
    im, cache = _run([_s_file(101)], [_u_shell(101)], _df([(101, True, 0, 90)]),
                     ledger={"101": "old"}, monkeypatch=monkeypatch)
    assert ("ultra", {"movieIds": [101], "monitored": True}) in im.puts
    assert ("ultra", {"name": "MoviesSearch", "movieIds": [101]}) in im.commands
    assert "101" not in cache.get(_LEDGER_KEY)


def test_does_not_recover_unledgered_unmonitored_shell(monkeypatch):
    # An operator-unmonitored fileless 4K record NOT in the ledger must be left alone, even if its
    # score is high — we only re-acquire titles WE demoted.
    im, _ = _run([_s_file(101)], [_u_shell(101)], _df([(101, True, 0, 90)]),
                 ledger={}, monkeypatch=monkeypatch)
    assert im.puts == [] and im.commands == []


def test_does_not_recover_while_score_still_low(monkeypatch):
    im, cache = _run([_s_file(101)], [_u_shell(101)], _df([(101, False, 0, 10)]),
                     ledger={"101": "old"}, monkeypatch=monkeypatch)
    assert im.puts == [] and im.commands == []
    assert "101" in cache.get(_LEDGER_KEY)            # still demoted → entry retained


# ── gates ──────────────────────────────────────────────────────────────────────
def test_no_demote_when_flag_off(monkeypatch):
    im, _ = _run([_s_file(101)], [_u_file(101)], _df([(101, False, 0, 10)]),
                 cfg=_cfg(demote=False), monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []


def test_no_demote_without_deletion_consent(monkeypatch):
    im, _ = _run([_s_file(101)], [_u_file(101)], _df([(101, False, 0, 10)]),
                 cfg=_cfg(consent=False), monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []


def test_dry_run_writes_nothing(monkeypatch):
    im, _ = _run([_s_file(101)], [_u_file(101)], _df([(101, False, 0, 10)]),
                 dry_run=True, monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == [] and im.commands == []


def test_disarmed_backup_gate_writes_nothing(monkeypatch):
    im, _ = _run([_s_file(101)], [_u_file(101)], _df([(101, False, 0, 10)]),
                 gate={"armed": False}, monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []


# ── hysteresis band (the gap) ────────────────────────────────────────────────────
def test_band_keeps_title_between_demote_floor_and_threshold(monkeypatch):
    # watched score 90 → lk 74. threshold 80, gap 10 → demote_floor 70. 74 ∈ [70,80) is STICKY: the 4K
    # is kept even though 74 < the 80 promote threshold — no flap on a small sibling-adjacent dip.
    im, _ = _run([_s_file(101)], [_u_file(101)], _df([(101, True, 0, 90)]),
                 cfg=_cfg(threshold=80, gap=10), monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []


def test_demotes_only_below_the_demote_floor_not_the_threshold(monkeypatch):
    # Same lk 74, but a narrow gap of 4 → demote_floor 76 → 74 < 76 → demote. Proves the FLOOR
    # (threshold - gap), not the raw threshold, gates the demote.
    im, _ = _run([_s_file(101)], [_u_file(101, fid=999)], _df([(101, True, 0, 90)]),
                 cfg=_cfg(threshold=80, gap=4), monkeypatch=monkeypatch)
    assert ("ultra", "moviefile/999") in im.deletes


# ── dwell clock (optional, absorbs a transient large swing) ───────────────────────
def test_dwell_delays_demote_and_starts_clock(monkeypatch):
    # dwell 5d, no prior clock → fresh below-floor: NOT demoted yet; a clock is started.
    im, cache = _run([_s_file(101)], [_u_file(101, fid=999)], _df([(101, False, 0, 10)]),
                     cfg=_cfg(dwell=5), monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []
    assert "101" in (cache.get(_CLOCK_KEY) or {})


def test_dwell_satisfied_demotes(monkeypatch):
    # The clock shows the title has been below the floor 10 days >= the 5-day dwell → demote now.
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    im, _ = _run([_s_file(101)], [_u_file(101, fid=999)], _df([(101, False, 0, 10)]),
                 cfg=_cfg(dwell=5), clock={"101": old}, monkeypatch=monkeypatch)
    assert ("ultra", "moviefile/999") in im.deletes


def test_dwell_clock_resets_when_score_recovers_into_band(monkeypatch):
    # A clock was running, but the score recovered to/above the demote floor → the clock is dropped
    # (no demote), so a brief dip never accumulates toward eviction.
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    im, cache = _run([_s_file(101)], [_u_file(101)], _df([(101, True, 0, 90)]),    # lk 74 >= floor 60
                     cfg=_cfg(dwell=5), clock={"101": old}, monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []
    assert "101" not in (cache.get(_CLOCK_KEY) or {})


def test_dwell_clock_persists_under_dry_run(monkeypatch):
    # The dwell clock is non-destructive aging state → it advances + persists even in a preview run, so
    # a title's below-floor time survives a dry-run / backup-degraded run (no destructive write happens).
    im, cache = _run([_s_file(101)], [_u_file(101)], _df([(101, False, 0, 10)]),
                     cfg=_cfg(dwell=5), dry_run=True, monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == [] and im.commands == []
    assert "101" in (cache.get(_CLOCK_KEY) or {})


# ── shared-storage / same-physical-file guard (never orphan the survivor) ─────────
def test_same_path_guard_skips_demote_on_shared_file(monkeypatch):
    # The 4K file and the standard "survivor" are the SAME physical file (shared storage / symlink) →
    # deleting the 4K file would orphan the standard record. The demote must skip it (mirrors the
    # move path's same-path guard).
    shared = "/data/media/movies/Toy Story (1995)/ts.mkv"
    u = {"id": 101, "tmdbId": 101, "title": "m101", "hasFile": True, "monitored": True, "tags": [],
         "movieFile": {"id": 999, "path": shared, "quality": {"quality": {"resolution": 2160}}}}
    s = {"id": 101, "tmdbId": 101, "title": "m101", "hasFile": True,
         "movieFile": {"id": 101, "path": shared, "quality": {"quality": {"resolution": 1080}}}}
    im, _ = _run([s], [u], _df([(101, False, 0, 10)]), monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == []


# ── double-add prevention across the pressure + watchability legs ──────────────────
def test_no_double_add_when_pressure_and_watchability_both_fire(monkeypatch):
    # Both _downgrade_orphan_4k (pressure) and _demote_overqualified_4k (watchability) target the SAME
    # 4K-only title in one run. The gateway library cache wouldn't reflect the first add, so the shared
    # grabbed-set must ensure EXACTLY ONE baseline add (not a duplicate standard record).
    for v in ("RECOMMENDARR_DELETIONS_CONSENT", "GLIDEARR_DELETIONS_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    cfg = {"routing": {"configured": True, "reorg_mode": "off",
                       "movies": {"4k_policy": "both", "4k_dual_min_score": 70,
                                  "demote_4k_on_watchability": True, "evict_uhd_first": True,
                                  "4k_demote_gap": 10, "4k_demote_dwell_days": 0}, "tv": {}},
           "movieRootFolders": {"standard": "/data/media/movies/standard", "4k": "/data/media/movies/4k"},
           "radarr_instances": {"standard": {"url": "s"}, "ultra": {"url": "u"}, "default_instance": "standard"},
           "radarr_instances_categorized": {"4K": "ultra"},
           "deletions_consent": True, "free_space_limit": 1000.0, "space_coordinator_enabled": True}
    im = _Im({"standard": [], "ultra": [_u_file(102)]},
             profiles={"standard": _STD_PROFILES}, roots={"standard": [{"path": "/data/media/movies/standard"}]},
             free=500.0, total=10000.0)        # free < U → under pressure → the evict leg runs too
    UhdReconcileManager(config=cfg, logger=None, radarr=_Mgr(im), dry_run=False,
                        registry=_Reg(_SP(_df([(102, False, 0, 10)]))), global_cache=_Cache()).run()
    adds = [a for a in im.adds if a[0] == "standard" and a[1].get("tmdbId") == 102]
    assert len(adds) == 1, adds                 # exactly ONE baseline add despite both legs firing
    assert im.deletes == []                     # 4K untouched (baseline still importing)


# ── error isolation (a failed delete doesn't abort the sweep or half-ledger) ──────
def test_delete_failure_is_isolated_and_not_ledgered(monkeypatch):
    class _ImRaise(_Im):
        def _make_request(self, name, endpoint, method="GET", payload=None, fallback=None, **kw):
            if method == "DELETE":
                raise RuntimeError("boom")
            return super()._make_request(name, endpoint, method=method, payload=payload,
                                         fallback=fallback, **kw)
    for v in ("RECOMMENDARR_DELETIONS_CONSENT", "GLIDEARR_DELETIONS_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    im = _ImRaise({"standard": [_s_file(101)], "ultra": [_u_file(101, fid=999)]},
                  profiles={"standard": _STD_PROFILES}, roots={"standard": [{"path": "/x"}]})
    cache = _Cache()
    UhdReconcileManager(config=_cfg(), logger=None, radarr=_Mgr(im), dry_run=False,
                        registry=_Reg(_SP(_df([(101, False, 0, 10)]))), global_cache=cache).run()
    # The sweep COMPLETED (no exception escaped run()); the unmonitor was issued but the delete raised,
    # so the title is left UN-ledgered → it retries next run rather than recording a phantom demotion.
    assert ("ultra", {"movieIds": [101], "monitored": False}) in im.puts   # unmonitor was attempted
    assert "101" not in (cache.get(_LEDGER_KEY) or {})                     # not ledgered (delete failed)


# ── recover boundary is the THRESHOLD, not the demote floor (intended hysteresis) ─
def test_demoted_shell_does_not_recover_in_sticky_band(monkeypatch):
    # A demoted shell whose score recovers only INTO the band [demote_floor, threshold) is NOT
    # re-acquired — recovery requires crossing back to the full UHD threshold, so a title can't
    # demote-then-recover on a small swing. (threshold 80, gap 10 → floor 70; lk 74 ∈ band.)
    im, cache = _run([_s_file(101)], [_u_shell(101)], _df([(101, True, 0, 90)]),
                     cfg=_cfg(threshold=80, gap=10), ledger={"101": "old"}, monkeypatch=monkeypatch)
    assert im.puts == [] and im.commands == []
    assert "101" in (cache.get(_LEDGER_KEY) or {})       # stays demoted (ledgered) until >= threshold
