"""Tests for UhdReconcileManager — the steady-state dual-version cross-instance MOVE driver.
Verifies the gates (configured / 4k_policy=='both' / distinct 4K instance / reorg_mode +
relocation_consent + dry_run) and that the reconcile drives CrossInstanceMove with the right
state read from BOTH libraries: a 2160p standard movie with no 4K copy → MOVE-IN (un-monitor
source, add dest search-off, DownloadedMoviesScan Move); once the 4K instance has the file →
FINALIZE (retune source to the 1080p baseline + search). A fake instance-manager records the
movie/command/editor calls."""
from __future__ import annotations


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
_U_HASFILE = {"id": 5, "tmdbId": 862, "hasFile": True}
_U_NOFILE = {"id": 5, "tmdbId": 862, "hasFile": False}


class _Im:
    def __init__(self, movies, profiles=None, roots=None):
        self._movies = movies
        self._profiles = profiles or {}
        self._roots = roots or {}
        self.commands, self.puts, self.adds, self.deletes, self.gets = [], [], [], [], []

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
        table = {"movie": self._movies, "qualityprofile": self._profiles, "rootfolder": self._roots}
        return table.get(endpoint, {}).get(name, fallback if fallback is not None else [])


class _Mgr:
    def __init__(self, im): self.instance_manager = im


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


# ── MOVE-IN actuation ─────────────────────────────────────────────────────────
def test_move_in_2160p_standard_movie(monkeypatch):
    im = _run(_cfg(), [_M4K, _M1080], [], monkeypatch=monkeypatch)
    # source un-monitored (race guard), added to ultra search-off, dest told to Move-import
    assert ("standard", {"movieIds": [1], "monitored": False}) in im.puts
    assert len(im.adds) == 1 and im.adds[0][0] == "ultra"
    assert im.adds[0][1]["addOptions"] == {"searchForMovie": False}
    assert im.adds[0][1]["qualityProfileId"] == 7 and im.adds[0][1]["rootFolderPath"] == "/data/media/movies/4k"
    scan = [c for c in im.commands if c[1].get("name") == "DownloadedMoviesScan"]
    assert scan and scan[0][1]["importMode"] == "Move"
    assert scan[0][1]["path"] == "/data/media/movies/Kids/Toy Story (1995)"


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


def test_pending_import_rescans_without_touching_source(monkeypatch):
    im = _run(_cfg(), [dict(_M4K, monitored=False)], [_U_NOFILE], monkeypatch=monkeypatch)
    # on dest but no file yet → just re-scan; source already un-monitored, so no editor PUT
    assert im.adds == []
    assert any(c[1].get("name") == "DownloadedMoviesScan" for c in im.commands)
    assert im.puts == []


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
