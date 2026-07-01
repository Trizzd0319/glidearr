"""Tests for UhdReconcileManager — the steady-state DOWNLOAD-BASED dual-version driver.
Verifies the gates (configured / 4k_policy=='both' / distinct 4K instance / reorg_mode +
consent + dry_run) and that the reconcile drives the right state read from BOTH libraries: a 2160p
standard movie that warrants 4K → the 4K instance ACQUIRES its own 2160p (add, SEARCH ON) and the
standard record is retuned to its ≤1080 baseline + search (Radarr replaces the 2160p on import). No
cross-instance file move. A fake instance-manager records the movie/command/editor calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.managers.services.routing.uhd_reconcile import UhdReconcileManager


def _profile(pid, name, res):
    return {"id": pid, "name": name, "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


_STD_PROFILES = [_profile(3, "HD-1080", 1080), _profile(9, "UHD-std", 2160)]
_UHD_PROFILES = [_profile(7, "UHD-Bluray", 2160)]
_M4K = {"id": 1, "title": "Toy Story", "tmdbId": 862, "year": 1995, "monitored": True,
        "qualityProfileId": 9, "rootFolderPath": "/data/media/movies/Kids",
        "movieFile": {"id": 50, "path": "/data/media/movies/Kids/Toy Story (1995)/ts.mkv",
                      "quality": {"quality": {"resolution": 2160}}}}
_M1080 = {"id": 2, "title": "Up", "tmdbId": 14160, "monitored": True, "qualityProfileId": 3,
          "movieFile": {"id": 51, "path": "/x/Up/up.mkv", "quality": {"quality": {"resolution": 1080}}}}
# a 4K-instance record that HAS a 2160p file (the legit finalize precondition — ultra holds the 4K)
_U_HASFILE = {"id": 5, "tmdbId": 862, "hasFile": True,
              "movieFile": {"id": 55, "quality": {"quality": {"resolution": 2160}}}}
_U_NOFILE = {"id": 5, "tmdbId": 862, "hasFile": False}


class _Im:
    def __init__(self, movies, profiles=None, roots=None, free=9999.0, total=10000.0, manual=None):
        self._movies = movies
        self._profiles = profiles or {}
        self._roots = roots or {}
        self._free, self._total = free, total
        self._manual = manual                 # canned GET /manualimport response (shared-storage relocate)
        self.commands, self.puts, self.adds, self.deletes, self.gets = [], [], [], [], []

    def disk_free_gb(self, inst): return self._free
    def disk_total_gb(self, inst): return self._total

    def _get_apis(self):
        return {n: object() for n in self._movies}

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
        if endpoint.startswith("manualimport"):
            return self._manual if self._manual is not None else (fallback if fallback is not None else [])
        table = {"movie": self._movies, "qualityprofile": self._profiles, "rootfolder": self._roots}
        return table.get(endpoint, {}).get(name, fallback if fallback is not None else [])


class _Mgr:
    def __init__(self, im): self.instance_manager = im


class _SP:
    def __init__(self, df): self._df = df
    def load_movie_files(self, inst): return self._df


class _Reg:
    def __init__(self, sp): self._sp = sp
    def get(self, kind, key): return self._sp if key == "RadarrSpacePressureManager" else None


class _CapLog:
    def __init__(self): self.info = []
    def log_info(self, m): self.info.append(m)
    def log_warning(self, m): pass
    def log_success(self, m): pass


def _cfg(*, reorg="same_instance", policy="both", configured=True, with_4k=True, consent=True):
    routing = {"reorg_mode": reorg, "movies": {"4k_policy": policy}, "tv": {}}
    if configured:
        routing["configured"] = True
    insts = {"standard": {"url": "s"}, "default_instance": "standard"}
    cat = {}
    if with_4k:
        insts["ultra"] = {"url": "u"}
        cat = {"4K": "ultra"}
    c = {"routing": routing,
         "movieRootFolders": {"standard": "/data/media/movies/standard", "4k": "/data/media/movies/4k"},
         "radarr_instances": insts, "radarr_instances_categorized": cat}
    if consent:
        c["relocation_consent"] = True
    return c


def _run(cfg, std, ultra, *, dry_run=False, monkeypatch=None):
    if monkeypatch is not None:
        for v in ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT"):
            monkeypatch.delenv(v, raising=False)
    im = _Im({"standard": std, "ultra": ultra},
             profiles={"standard": _STD_PROFILES, "ultra": _UHD_PROFILES},
             roots={"ultra": [{"path": "/data/media/movies/4k"}]})
    UhdReconcileManager(config=cfg, logger=None, radarr=_Mgr(im), dry_run=dry_run).run()
    return im


# ── first contact: acquire 4K, HOLD standard's 2160p until the 4K copy is confirmed ───────────────
def test_2160p_standard_acquires_4k_and_freezes_standard(monkeypatch):
    im = _run(_cfg(), [_M4K, _M1080], [], monkeypatch=monkeypatch)
    # the 4K instance acquires its OWN 2160p: add, SEARCH ON, 4k root + 2160p profile
    assert len(im.adds) == 1 and im.adds[0][0] == "ultra"
    assert im.adds[0][1]["addOptions"] == {"searchForMovie": True}
    assert im.adds[0][1]["qualityProfileId"] == 7 and im.adds[0][1]["rootFolderPath"] == "/data/media/movies/4k"
    # MAKE-BEFORE-BREAK: standard's 2160p is FROZEN (un-monitored), kept on disk, NOT retuned-down yet.
    assert ("standard", {"movieIds": [1], "monitored": False}) in im.puts
    assert not any(p[0] == "standard" and "qualityProfileId" in p[1] for p in im.puts)   # no retune yet
    assert not any(c[1].get("name") in ("RescanMovie", "DownloadedMoviesScan") for c in im.commands)
    # _M1080 is already a 1080p baseline → never touched
    assert not any(p[0] == "standard" and p[1].get("movieIds") == [2] for p in im.puts)


def test_does_not_move_1080p(monkeypatch):
    im = _run(_cfg(), [_M1080], [], monkeypatch=monkeypatch)
    assert im.adds == [] and im.puts == [] and im.commands == []


# ── FINALIZE (retune the EXISTING record in place — no delete, no re-add) ─────
def test_finalize_retunes_standard_in_place(monkeypatch):
    im = _run(_cfg(), [_M4K], [_U_HASFILE], monkeypatch=monkeypatch)   # _M4K profile 9 != baseline 3
    assert im.deletes == []                                # NEVER deletes the Radarr record
    assert im.adds == []                                   # never re-adds (id/history preserved)
    assert ("standard", {"movieIds": [1], "qualityProfileId": 3, "monitored": True}) in im.puts
    assert ("standard", {"name": "RescanMovie", "movieIds": [1]}) in im.commands
    assert ("standard", {"name": "MoviesSearch", "movieIds": [1]}) in im.commands


def test_lower_res_4k_is_driven_to_2160_and_standard_frozen(monkeypatch):
    # standard holds 2160p but the 4K instance only has 1080p → DRIVE the 4K record to its own 2160p
    # (4K profile + monitor + search) and FREEZE standard's 2160p; never downgrade the only high-res
    # copy until the 4K side genuinely holds its own 2160p.
    u_1080 = {"id": 5, "tmdbId": 862, "hasFile": True,
              "movieFile": {"id": 55, "quality": {"quality": {"resolution": 1080}}}}
    im = _run(_cfg(), [_M4K], [u_1080], monkeypatch=monkeypatch)
    assert im.adds == []                                            # ultra record exists → driven, not re-added
    assert ("ultra", {"movieIds": [5], "qualityProfileId": 7, "monitored": True}) in im.puts   # drive 4K
    assert ("ultra", {"name": "MoviesSearch", "movieIds": [5]}) in im.commands
    assert ("standard", {"movieIds": [1], "monitored": False}) in im.puts                       # freeze std
    assert not any(p[0] == "standard" and "qualityProfileId" in p[1] for p in im.puts)           # no retune
    assert im.deletes == []                                         # standard 2160p never deleted


def test_steady_baseline_title_is_noop(monkeypatch):
    # standard already at the baseline profile with a 1080p file + also on ultra = steady dual → skip
    steady = dict(_M4K, qualityProfileId=3, hasFile=True,
                  movieFile={"quality": {"quality": {"resolution": 1080}}})
    im = _run(_cfg(), [steady], [_U_HASFILE], monkeypatch=monkeypatch)
    assert im.deletes == [] and im.puts == [] and im.commands == [] and im.adds == []


def test_unmonitored_steady_1080_baseline_not_finalized(monkeypatch):
    # an operator-un-monitored steady 1080p dual title that's also on ultra → must NOT be touched
    steady = dict(_M4K, monitored=False, qualityProfileId=9, hasFile=True,
                  movieFile={"quality": {"quality": {"resolution": 1080}}})
    im = _run(_cfg(), [steady], [_U_HASFILE], monkeypatch=monkeypatch)
    assert im.deletes == [] and im.adds == [] and im.commands == [] and im.puts == []


def test_4k_record_no_file_is_driven_to_acquire_no_readd(monkeypatch):
    im = _run(_cfg(), [dict(_M4K, monitored=False)], [_U_NOFILE], monkeypatch=monkeypatch)
    # ultra has a record but NO 2160p file → DRIVE it (4K profile + monitor + search), don't re-add.
    assert im.adds == []
    assert ("ultra", {"movieIds": [5], "qualityProfileId": 7, "monitored": True}) in im.puts
    assert ("ultra", {"name": "MoviesSearch", "movieIds": [5]}) in im.commands
    # standard is already un-monitored → no standard write; never retuned/deleted while 4K not confirmed
    assert not any(p[0] == "standard" for p in im.puts) and im.deletes == []


def test_finalize_retunes_once_4k_confirmed(monkeypatch):
    # ONCE the 4K instance holds its own 2160p (dest_hasfile) → standard is safely retuned to 1080p.
    im = _run(_cfg(), [_M4K], [_U_HASFILE], monkeypatch=monkeypatch)
    assert im.adds == []                                   # ultra already has it → no acquire
    assert ("standard", {"movieIds": [1], "qualityProfileId": 3, "monitored": True}) in im.puts
    assert ("standard", {"name": "MoviesSearch", "movieIds": [1]}) in im.commands


# ── gates ─────────────────────────────────────────────────────────────────────
def test_log_only_writes_nothing(monkeypatch):
    im = _run(_cfg(reorg="log_only"), [_M4K], [], monkeypatch=monkeypatch)
    assert ("standard", "movie") in im.gets                # it fetched + planned
    assert im.adds == [] and im.puts == [] and im.commands == []


def test_no_consent_writes_nothing(monkeypatch):
    im = _run(_cfg(consent=False), [_M4K], [], monkeypatch=monkeypatch)
    assert im.adds == [] and im.puts == [] and im.commands == []


def test_dry_run_writes_nothing(monkeypatch):
    im = _run(_cfg(), [_M4K], [], dry_run=True, monkeypatch=monkeypatch)
    assert im.adds == [] and im.puts == [] and im.commands == []


def test_off_mode_does_nothing(monkeypatch):
    im = _run(_cfg(reorg="off"), [_M4K], [], monkeypatch=monkeypatch)
    assert im.adds == [] and im.gets == []


def test_noop_when_highest_only(monkeypatch):
    im = _run(_cfg(policy="highest_only"), [_M4K], [], monkeypatch=monkeypatch)
    assert im.adds == [] and im.gets == []


def test_noop_when_not_configured(monkeypatch):
    im = _run(_cfg(configured=False), [_M4K], [], monkeypatch=monkeypatch)
    assert im.adds == [] and im.gets == []


def test_noop_without_distinct_4k_instance(monkeypatch):
    im = _run(_cfg(with_4k=False), [_M4K], [], monkeypatch=monkeypatch)
    assert im.adds == [] and im.puts == [] and im.commands == []


# ── watchability score threading (read-only) ──────────────────────────────────
def test_likelihood_map_reads_watchability_from_cache():
    import pandas as pd
    df = pd.DataFrame([{"tmdb_id": 862, "is_watched": True, "watchability_score": 80},
                       {"tmdb_id": 14160, "is_watched": False, "watchability_score": 10}])
    m = UhdReconcileManager(config={}, logger=None, registry=_Reg(_SP(df)))
    lk = m._likelihood_map("standard")
    assert lk.get(862) == 74.0                         # watched + score 80 → likelihood 74
    assert 14160 in lk


def test_likelihood_map_empty_without_registry():
    assert UhdReconcileManager(config={}, logger=None)._likelihood_map("standard") == {}


def test_move_log_includes_watch_score(monkeypatch):
    import pandas as pd
    for v in ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    im = _Im({"standard": [_M4K], "ultra": []},
             profiles={"standard": _STD_PROFILES, "ultra": _UHD_PROFILES},
             roots={"ultra": [{"path": "/data/media/movies/4k"}]})
    df = pd.DataFrame([{"tmdb_id": 862, "is_watched": True, "watchability_score": 80}])
    log = _CapLog()
    UhdReconcileManager(config=_cfg(), logger=log, radarr=_Mgr(im), dry_run=False,
                        registry=_Reg(_SP(df))).run()
    assert any("watch 74" in s for s in log.info)      # the per-title move log shows the score


# ── proactive 4K acquire (owned 1080p movie warranting 4K gets a 4K copy on the 4K instance) ──
class _GateCache:
    """Minimal global_cache stub serving the two keys the Stage-C remote-play gate reads."""
    def __init__(self, records, weights):
        self._d = {"tautulli/transcode_fingerprint": records or [],
                   "tautulli/platforms": weights or {}}

    def get(self, key, *a, **k):
        return self._d.get(key)


def _hevc_cell(*, transcode, direct, device="Chromecast"):
    # a 2160p HEVC capability cell matching the gate's source fingerprint at the drop_audio level
    return [{"device": device, "fingerprint": ["hevc", "eac3", "none", "2160p_sdr", "unknown"],
             "transcode": transcode, "direct": direct, "last_seen": 0, "n": transcode + direct}]


def _proactive_run(monkeypatch, std, *, score=90, proactive=True, free=9999.0,
                   gate=False, fp_records=None, weights=None):
    import pandas as pd
    for v in ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    im = _Im({"standard": std, "ultra": []},
             profiles={"standard": _STD_PROFILES, "ultra": _UHD_PROFILES},
             roots={"ultra": [{"path": "/data/media/movies/4k"}]}, free=free)
    cfg = _cfg()                                            # both + same_instance + consent
    cfg["routing"]["movies"]["proactive_4k"] = proactive
    cfg["routing"]["movies"]["4k_dual_min_score"] = 70
    if gate:
        cfg["routing"]["movies"]["transcode_gate"] = True
    gc = _GateCache(fp_records, weights) if gate else None
    df = pd.DataFrame([{"tmdb_id": mv["tmdbId"], "is_watched": True, "watchability_score": score}
                       for mv in std])
    UhdReconcileManager(config=cfg, logger=None, radarr=_Mgr(im), dry_run=False,
                        registry=_Reg(_SP(df)), global_cache=gc).run()
    return im


def test_proactive_acquires_4k_for_high_watchability_1080p(monkeypatch):
    im = _proactive_run(monkeypatch, [_M1080], score=90)   # likelihood 74 >= 70
    acq = [a for a in im.adds if a[0] == "ultra"]
    assert len(acq) == 1
    assert acq[0][1]["tmdbId"] == 14160
    assert acq[0][1]["addOptions"] == {"searchForMovie": True}
    assert acq[0][1]["qualityProfileId"] == 7              # the 4K instance's top profile
    assert im.puts == [] and im.commands == []             # source 1080p untouched


def test_no_proactive_acquire_when_flag_off(monkeypatch):
    im = _proactive_run(monkeypatch, [_M1080], score=90, proactive=False)
    assert im.adds == []                                   # default off → nothing


def test_no_proactive_acquire_below_threshold(monkeypatch):
    im = _proactive_run(monkeypatch, [_M1080], score=10)   # likelihood low → < 70
    assert im.adds == []


def test_no_proactive_acquire_when_space_tight(monkeypatch):
    im = _proactive_run(monkeypatch, [_M1080], score=90, free=1.0)   # below the 4K-instance band
    assert im.adds == []                                   # space-gated


# ── Stage-C remote-play gate on the proactive acquire (same authority as add-time) ──
def test_proactive_suppressed_when_remote_play_false(monkeypatch):
    # gate ON + the household always transcodes 2160p HEVC → suppress the proactive 4K acquire
    im = _proactive_run(monkeypatch, [_M1080], score=90, gate=True,
                        fp_records=_hevc_cell(transcode=4, direct=0), weights={"Chromecast": 1})
    assert im.adds == []


def test_proactive_acquires_when_remote_play_true(monkeypatch):
    # gate ON but the household direct-plays 2160p HEVC → still acquire
    im = _proactive_run(monkeypatch, [_M1080], score=90, gate=True,
                        fp_records=_hevc_cell(transcode=0, direct=4), weights={"Chromecast": 1})
    acq = [a for a in im.adds if a[0] == "ultra"]
    assert len(acq) == 1 and acq[0][1]["tmdbId"] == 14160


def test_proactive_acquires_on_no_data_even_with_gate_on(monkeypatch):
    # gate ON but no transcode history yet → explore (acquire and learn), never deny a fresh household
    im = _proactive_run(monkeypatch, [_M1080], score=90, gate=True, fp_records=[], weights={})
    assert len([a for a in im.adds if a[0] == "ultra"]) == 1


# ── downgrade low-watchability 4K-only titles to a 1080p baseline under pressure ──
_ORPHAN_4K = {"id": 5, "title": "Orphan", "tmdbId": 777, "monitored": True, "qualityProfileId": 7,
              "movieFile": {"quality": {"quality": {"resolution": 2160}}},
              "rootFolderPath": "/data/media/movies/4k"}


def _evict_cfg(*, evict=True, reorg="off"):
    return {"routing": {"configured": True, "reorg_mode": reorg,
                        "movies": {"4k_policy": "both", "evict_uhd_first": evict}, "tv": {}},
            "movieRootFolders": {"standard": "/data/media/movies/standard", "4k": "/data/media/movies/4k"},
            "radarr_instances": {"standard": {"url": "s"}, "ultra": {"url": "u"}, "default_instance": "standard"},
            "radarr_instances_categorized": {"4K": "ultra"},
            "space_coordinator_enabled": True, "deletions_consent": True, "free_space_limit": 1000.0}


def _downgrade_run(monkeypatch, *, std, ultra, score=5, free=500.0, evict=True, wc=0):
    import pandas as pd
    for v in ("RECOMMENDARR_DELETIONS_CONSENT", "GLIDEARR_DELETIONS_CONSENT",
              "RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    im = _Im({"standard": std, "ultra": ultra},
             profiles={"standard": _STD_PROFILES, "ultra": _UHD_PROFILES},
             roots={"ultra": [{"path": "/data/media/movies/4k"}],
                    "standard": [{"path": "/data/media/movies/standard"}]},
             free=free, total=10000.0)
    df = pd.DataFrame([{"tmdb_id": 777, "is_watched": wc > 0, "watch_count": wc,
                        "watchability_score": score}])
    UhdReconcileManager(config=_evict_cfg(evict=evict), logger=None, radarr=_Mgr(im), dry_run=False,
                        registry=_Reg(_SP(df))).run()
    return im


def test_downgrades_low_watchability_orphan_4k_under_pressure(monkeypatch):
    im = _downgrade_run(monkeypatch, std=[], ultra=[_ORPHAN_4K], score=5, free=500.0)
    add = [a for a in im.adds if a[0] == "standard"]
    assert len(add) == 1                                   # a 1080p baseline grabbed on standard
    assert add[0][1]["tmdbId"] == 777
    assert add[0][1]["qualityProfileId"] == 3              # the ≤1080 baseline profile
    assert add[0][1]["addOptions"] == {"searchForMovie": True}
    assert add[0][1]["rootFolderPath"] == "/data/media/movies/standard"
    assert not any(a[0] == "ultra" for a in im.adds)       # the 4K copy is untouched (make-before-break)


def test_no_downgrade_when_not_under_pressure(monkeypatch):
    im = _downgrade_run(monkeypatch, std=[], ultra=[_ORPHAN_4K], score=5, free=9999.0)
    assert im.adds == []                                   # free above the band → nothing


def test_no_downgrade_for_rewatched_orphan(monkeypatch):
    # REWATCHED (engagement) → likelihood >= the 75 4K gate → genuinely warrants 4K → kept in 4K.
    im = _downgrade_run(monkeypatch, std=[], ultra=[_ORPHAN_4K], score=50, free=500.0, wc=4)
    assert im.adds == []                                   # warrants 4K by rewatch → kept in 4K


def test_unwatched_high_affinity_orphan_is_downgraded(monkeypatch):
    # UNWATCHED but high watchability_score: affinity caps at 74 (< the 75 4K gate) → taste alone no
    # longer holds 4K, so under pressure it gets a 1080p baseline (the recalibrated 'taste never 4K').
    im = _downgrade_run(monkeypatch, std=[], ultra=[_ORPHAN_4K], score=95, free=500.0, wc=0)
    add = [a for a in im.adds if a[0] == "standard"]
    assert len(add) == 1 and add[0][1]["tmdbId"] == 777    # demoted to a 1080p baseline


def test_no_downgrade_when_baseline_already_present(monkeypatch):
    # the title already has a record on standard (pending or filed) → no repeat grab
    on_std = {"id": 1, "title": "Orphan", "tmdbId": 777, "qualityProfileId": 3}
    im = _downgrade_run(monkeypatch, std=[on_std], ultra=[_ORPHAN_4K], score=5, free=500.0)
    assert im.adds == []


def test_no_downgrade_when_evict_gate_off(monkeypatch):
    im = _downgrade_run(monkeypatch, std=[], ultra=[_ORPHAN_4K], score=5, free=500.0, evict=False)
    assert im.adds == []


def test_only_scans_hd_tier_sources_not_a_real_4k_library(monkeypatch):
    # standard = source; "ultra" = a real 4K library (uncategorized) that must be LEFT ALONE;
    # "testing-ultra" = the configured 4K target. The sweep must not drag ultra's 2160p into it.
    for v in ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    real_ultra_movie = dict(_M4K, id=77, tmdbId=999, title="RealUltra4K")
    im = _Im({"standard": [_M4K], "ultra": [real_ultra_movie], "testing-ultra": []},
             profiles={"standard": _STD_PROFILES, "testing-ultra": _UHD_PROFILES},
             roots={"testing-ultra": [{"path": "/data/media/movies/4k"}]})
    cfg = {"routing": {"reorg_mode": "same_instance", "movies": {"4k_policy": "both"}, "tv": {},
                       "configured": True},
           "movieRootFolders": {"standard": "/data/media/movies/standard", "4k": "/data/media/movies/4k"},
           "radarr_instances": {"standard": {"url": "s"}, "ultra": {"url": "u"},
                                "testing-ultra": {"url": "t"}, "default_instance": "standard"},
           "radarr_instances_categorized": {"720p": "standard", "1080p": "standard", "4K": "testing-ultra"},
           "relocation_consent": True}
    UhdReconcileManager(config=cfg, logger=None, radarr=_Mgr(im), dry_run=False).run()
    adds_to_target = [a for a in im.adds if a[0] == "testing-ultra"]
    assert any(a[1].get("tmdbId") == 862 for a in adds_to_target)        # standard's title moved in
    assert not any(a[1].get("tmdbId") == 999 for a in adds_to_target)    # real ultra library untouched
    assert not any(p[0] == "ultra" for p in im.puts)                     # ultra never written to


# ══ cross_instance mode: hardened move (shared-storage probe + backup gate) + DEDUP ══
from scripts.managers.services.backup import GATE_KEY                    # noqa: E402

_SHARED_ROOTS = {"standard": [{"path": "/data/media/movies/standard"}],
                 "ultra": [{"path": "/data/media/movies/4k"}]}
# a redundant 2160p copy of _M4K (tmdb 862) on ultra, DISTINCT path → a true cross-instance duplicate
_U_DUP_2160 = {"id": 5, "tmdbId": 862, "hasFile": True,
               "movieFile": {"id": 71, "path": "/data/media/movies/4k/Toy Story (1995)/ts.mkv",
                             "quality": {"quality": {"resolution": 2160}}}}
# a FILED 2160p record on standard (hasFile set, as real Radarr reports) — the dedup loser
_STD_DUP_2160 = {"id": 1, "tmdbId": 862, "title": "Toy Story", "monitored": True, "qualityProfileId": 9,
                 "hasFile": True, "rootFolderPath": "/data/media/movies/standard",
                 "movieFile": {"id": 50, "path": "/data/media/movies/standard/Toy Story (1995)/ts.mkv",
                               "quality": {"quality": {"resolution": 2160}}}}
# the intended dual-version end state: a 1080p baseline on standard for tmdb 862
_STD_1080_862 = {"id": 1, "tmdbId": 862, "title": "Toy Story", "monitored": True, "qualityProfileId": 3,
                 "hasFile": True, "movieFile": {"id": 50, "quality": {"quality": {"resolution": 1080}}}}


class _XCache:
    """global_cache stub: backup gate key + arbitrary seeded keys, records set() writes."""
    def __init__(self, gate=None, seed=None):
        self._d = dict(seed or {})
        if gate is not None:
            self._d[GATE_KEY] = gate
        self.writes = []
    def get(self, k, *a, **kw): return self._d.get(k)
    def set(self, k, v, *a, **kw): self._d[k] = v; self.writes.append((k, v))


def _xcfg(*, move=True, dedup=True, mode="cross_instance"):
    c = {"routing": {"configured": True, "reorg_mode": mode,
                     "movies": {"4k_policy": "both"}, "tv": {}},
         "movieRootFolders": {"standard": "/data/media/movies/standard", "4k": "/data/media/movies/4k"},
         "radarr_instances": {"standard": {"url": "s"}, "ultra": {"url": "u"}, "default_instance": "standard"},
         "radarr_instances_categorized": {"720p": "standard", "1080p": "standard", "4K": "ultra"}}
    if move:
        c["cross_instance_move_consent"] = True
    if dedup:
        c["cross_instance_dedup_consent"] = True
    return c


def _clear_x_env(monkeypatch):
    for v in ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT",
              "RECOMMENDARR_CROSS_INSTANCE_MOVE_CONSENT", "GLIDEARR_CROSS_INSTANCE_MOVE_CONSENT",
              "RECOMMENDARR_CROSS_INSTANCE_DEDUP_CONSENT", "GLIDEARR_CROSS_INSTANCE_DEDUP_CONSENT"):
        monkeypatch.delenv(v, raising=False)


def _xrun(std, ultra, monkeypatch, *, cfg=None, dry_run=False, gate=None, roots=None, manual=None,
          seed=None, cache=None):
    _clear_x_env(monkeypatch)
    im = _Im({"standard": std, "ultra": ultra},
             profiles={"standard": _STD_PROFILES, "ultra": _UHD_PROFILES},
             roots=roots or _SHARED_ROOTS, manual=manual)
    gc = cache or _XCache(gate, seed)
    UhdReconcileManager(config=cfg or _xcfg(), logger=None, radarr=_Mgr(im), dry_run=dry_run,
                        global_cache=gc).run()
    im.cache = gc
    return im


# ── dual-version: shared storage → RELOCATE (hardlink) ; else → DOWNLOAD ───────────────────────
def test_shared_storage_relocates_instead_of_downloading(monkeypatch):
    # standard + ultra share a filesystem (common root ancestor + equal total) AND the 4K instance
    # can SEE standard's 2160p → RELOCATE it (ManualImport copy → hardlink), NOT re-download.
    cand = [{"path": "/data/media/movies/standard/Toy Story (1995)/ts.mkv", "size": 50_000_000_000,
             "quality": {"quality": {"resolution": 2160}}, "languages": [], "releaseGroup": ""}]
    im = _xrun([_M4K], [], monkeypatch, manual=cand)
    mi = [c for c in im.commands if c[1].get("name") == "ManualImport"]
    assert len(mi) == 1 and mi[0][1]["importMode"] == "copy"        # import the existing file (copy)
    assert mi[0][1]["files"][0]["movieId"] == 9000                  # bound to the added 4K record
    assert len(im.adds) == 1 and im.adds[0][1]["addOptions"] == {"searchForMovie": False}  # no search
    assert not any(a[1].get("addOptions") == {"searchForMovie": True} for a in im.adds)     # NO download
    # standard's 2160p FROZEN until the 4K copy is confirmed (make-before-break), never retuned yet
    assert ("standard", {"movieIds": [1], "monitored": False}) in im.puts
    assert not any(p[0] == "standard" and "qualityProfileId" in p[1] for p in im.puts)


def test_shared_but_file_not_visible_falls_back_to_download(monkeypatch):
    # shared totals/roots pass the coarse probe, but the 4K instance sees NO file there (manual=None)
    # → NO relocate/import; DOWNLOAD instead (one add, SEARCH ON), and standard is frozen.
    im = _xrun([_M4K], [], monkeypatch)                            # manual defaults to None
    assert not any(c[1].get("name") == "ManualImport" for c in im.commands)
    assert len(im.adds) == 1 and im.adds[0][1]["addOptions"] == {"searchForMovie": True}
    assert ("standard", {"movieIds": [1], "monitored": False}) in im.puts   # standard frozen, not retuned
    assert not any(p[0] == "standard" and "qualityProfileId" in p[1] for p in im.puts)


def test_disjoint_mounts_download_no_relocate(monkeypatch):
    # separate storage (no common root ancestor) → probe says NOT shared → download, never relocate.
    im = _xrun([_M4K], [], monkeypatch, manual=[{"path": "/x.mkv", "size": 1,
               "quality": {"quality": {"resolution": 2160}}}],
               roots={"standard": [{"path": "/mnt/a/movies"}], "ultra": [{"path": "/mnt/b/movies"}]})
    assert not any(c[1].get("name") == "ManualImport" for c in im.commands)   # never relocates
    assert len(im.adds) == 1 and im.adds[0][1]["addOptions"] == {"searchForMovie": True}  # downloads
    assert ("standard", {"movieIds": [1], "monitored": False}) in im.puts


_RELO_CAND = [{"path": "/data/media/movies/standard/Toy Story (1995)/ts.mkv", "size": 50_000_000_000,
               "quality": {"quality": {"resolution": 2160}}, "languages": [], "releaseGroup": ""}]


def test_relocate_marks_pending_ledger(monkeypatch):
    im = _xrun([_M4K], [], monkeypatch, manual=_RELO_CAND)                    # no prior marker
    assert any(c[1].get("name") == "ManualImport" for c in im.commands)       # relocates
    assert "862" in im.cache.get("radarr/ultra/relocate_pending", {})         # and records it pending


def test_relocate_pending_marker_holds_off_reissue(monkeypatch):
    # a prior sweep's relocate copy is still in flight (fresh marker) → do NOT re-issue and do NOT
    # download; just hold standard frozen and carry the marker forward.
    fresh = datetime.now(timezone.utc).isoformat()
    im = _xrun([_M4K], [], monkeypatch, manual=_RELO_CAND,
               seed={"radarr/ultra/relocate_pending": {"862": fresh}})
    assert not any(c[1].get("name") == "ManualImport" for c in im.commands)   # not re-issued
    assert im.adds == []                                                       # not downloaded either
    assert ("standard", {"movieIds": [1], "monitored": False}) in im.puts      # standard held (frozen)
    assert im.cache.get("radarr/ultra/relocate_pending") == {"862": fresh}     # marker carried forward


def test_relocate_stale_marker_reissues(monkeypatch):
    # a marker aged past the grace ⇒ the copy is stuck, not in flight → re-issue (re-marked fresh),
    # so a permanently-failing import is rate-limited, not looped every run.
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    im = _xrun([_M4K], [], monkeypatch, manual=_RELO_CAND,
               seed={"radarr/ultra/relocate_pending": {"862": stale}})
    assert any(c[1].get("name") == "ManualImport" for c in im.commands)       # re-issued
    assert im.cache.get("radarr/ultra/relocate_pending", {}).get("862") != stale   # re-marked fresh


def test_cross_instance_move_blocked_by_disarmed_backup(monkeypatch):
    im = _xrun([_M4K], [], monkeypatch, gate={"armed": False})
    assert im.adds == [] and im.commands == [] and im.puts == []


def test_skips_when_4k_instance_has_no_2160p_profile(monkeypatch):
    # the 4K instance only offers a 1080p profile → never land a sub-4K copy there
    _clear_x_env(monkeypatch)
    im = _Im({"standard": [_M4K], "ultra": []},
             profiles={"standard": _STD_PROFILES, "ultra": [_profile(3, "HD-1080", 1080)]},
             roots=_SHARED_ROOTS)
    UhdReconcileManager(config=_xcfg(), logger=None, radarr=_Mgr(im), dry_run=False,
                        global_cache=_XCache()).run()
    assert im.adds == [] and im.commands == []


# ── dedup under cross_instance mode ───────────────────────────────────────────────
def test_cross_instance_dedup_reclaims_redundant_copy(monkeypatch):
    im = _xrun([_STD_DUP_2160], [_U_DUP_2160], monkeypatch)
    assert ("standard", "moviefile/50") in im.deletes          # standard's redundant 2160p file
    assert not any(ep.startswith("movie/") and not ep.startswith("moviefile")
                   for _, ep in im.deletes)                    # record never deleted


def test_cross_instance_intended_dual_version_left_alone(monkeypatch):
    im = _xrun([_STD_1080_862], [_U_DUP_2160], monkeypatch)
    assert im.adds == [] and im.puts == [] and im.commands == [] and im.deletes == []


def test_cross_instance_same_path_never_written(monkeypatch):
    shared = "/data/media/movies/standard/Toy Story (1995)/ts.mkv"
    std = dict(_M4K, movieFile={"id": 50, "path": shared, "quality": {"quality": {"resolution": 2160}}})
    uhd = {"id": 5, "tmdbId": 862, "hasFile": True,
           "movieFile": {"id": 71, "path": shared, "quality": {"quality": {"resolution": 2160}}}}
    im = _xrun([std], [uhd], monkeypatch)
    # both the dedup AND the move same-path guard must leave the shared file untouched
    assert im.deletes == [] and im.adds == [] and im.commands == [] and im.puts == []


def test_cross_instance_dedup_blocked_by_disarmed_backup(monkeypatch):
    im = _xrun([_STD_DUP_2160], [_U_DUP_2160], monkeypatch, gate={"armed": False})
    assert im.deletes == []                                    # dedup not actuated (gate down)


def test_proactive_acquire_actuates_under_cross_instance(monkeypatch):
    # proactive 4K acquire must FIRE under cross_instance mode (not just same_instance): an owned
    # 1080p film warranting 4K with no 4K anywhere → add a 4K copy on ultra, search ON.
    import pandas as pd
    _clear_x_env(monkeypatch)
    im = _Im({"standard": [_M1080], "ultra": []},
             profiles={"standard": _STD_PROFILES, "ultra": _UHD_PROFILES},
             roots=_SHARED_ROOTS, free=9999.0)
    cfg = _xcfg()
    cfg["routing"]["movies"]["proactive_4k"] = True
    cfg["routing"]["movies"]["4k_dual_min_score"] = 70
    df = pd.DataFrame([{"tmdb_id": 14160, "is_watched": True, "watchability_score": 90}])
    UhdReconcileManager(config=cfg, logger=None, radarr=_Mgr(im), dry_run=False,
                        registry=_Reg(_SP(df)), global_cache=_XCache()).run()
    acq = [a for a in im.adds if a[0] == "ultra"]
    assert len(acq) == 1 and acq[0][1]["tmdbId"] == 14160
    assert acq[0][1]["addOptions"] == {"searchForMovie": True}   # actuates (not would-acquire)


def test_cross_instance_dedup_off_dual_version_on(monkeypatch):
    other = {"id": 2, "tmdbId": 999, "title": "Other", "monitored": True, "qualityProfileId": 9,
             "hasFile": True,
             "movieFile": {"id": 60, "path": "/data/media/movies/standard/Other (2020)/o.mkv",
                           "quality": {"quality": {"resolution": 2160}}}}
    im = _xrun([_STD_DUP_2160, other], [_U_DUP_2160], monkeypatch, cfg=_xcfg(dedup=False))
    assert im.deletes == []                                    # dedup logged-only (no consent)
    # 999 (2160p on standard, not on ultra) → the 4K instance acquires its own 2160p
    assert any(a[0] == "ultra" and a[1].get("tmdbId") == 999 for a in im.adds)
