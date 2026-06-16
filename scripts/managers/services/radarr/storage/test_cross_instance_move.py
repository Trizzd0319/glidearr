"""Unit tests for CrossInstanceMove — the Radarr-orchestrated 2160p file move between instances.
Drives relocate() with a recording fake gateway and asserts the exact API steps per phase:
MOVE-IN (un-monitor source → add dest search-off → DownloadedMoviesScan Move on dest from the
source folder), FINALIZE (retune source to the 1080p baseline → MoviesSearch), make-before-break
(the source is never touched until the destination has the file), and dry_run."""
from __future__ import annotations

from scripts.managers.services.radarr.storage.cross_instance_move import CrossInstanceMove


class _GW:
    def __init__(self):
        self.adds, self.commands, self.puts, self.deletes = [], [], [], []

    def add(self, inst, payload):
        self.adds.append((inst, payload)); return {"id": 500}

    def command(self, inst, payload):
        self.commands.append((inst, payload)); return {"id": 1}

    def put(self, inst, endpoint, payload):
        self.puts.append((inst, endpoint, payload)); return {"ok": True}

    def delete(self, inst, endpoint):
        self.deletes.append((inst, endpoint)); return {}


_MOVIE = {"id": 1, "title": "Toy Story", "tmdbId": 862, "year": 1995, "monitored": True,
          "qualityProfileId": 9, "rootFolderPath": "/data/media/movies/Kids",
          "movieFile": {"id": 50, "path": "/data/media/movies/Kids/Toy Story (1995)/ts.mkv",
                        "quality": {"quality": {"resolution": 2160}}}}
_FOLDER = "/data/media/movies/Kids/Toy Story (1995)"


def _mover(dry_run=False):
    gw = _GW()
    return CrossInstanceMove(gw, logger=None, dry_run=dry_run), gw


def _relocate(mover, movie=None, *, dest_present=False, dest_hasfile=False, hd=3):
    return mover.relocate(movie or _MOVIE, from_inst="standard", to_inst="ultra",
                          dest_root="/data/media/movies/4k", dest_profile_id=7,
                          hd_profile_id=hd, dest_present=dest_present, dest_hasfile=dest_hasfile)


# ── MOVE-IN ───────────────────────────────────────────────────────────────────
def test_move_in_unmonitors_source_adds_dest_and_scans():
    m, gw = _mover()
    res = _relocate(m, dest_present=False, dest_hasfile=False)
    assert res["status"] == "moved-in"
    # source un-monitored first (race guard) — source FILE untouched
    assert gw.puts == [("standard", "movie/editor", {"movieIds": [1], "monitored": False})]
    # added to dest, search OFF, 4k root + 2160p profile, source path stripped
    assert len(gw.adds) == 1
    inst, payload = gw.adds[0]
    assert inst == "ultra" and payload["qualityProfileId"] == 7
    assert payload["rootFolderPath"] == "/data/media/movies/4k"
    assert payload["addOptions"] == {"searchForMovie": False}
    assert "movieFile" not in payload and "path" not in payload
    # dest told to MOVE+import from the source folder
    assert gw.commands == [("ultra", {"name": "DownloadedMoviesScan",
                                      "path": _FOLDER, "importMode": "Move"})]


def test_move_in_skips_add_when_dest_present():
    m, gw = _mover()
    res = _relocate(m, dest_present=True, dest_hasfile=False)
    assert res["status"] == "moved-in"                     # first pass (still monitored)
    assert gw.adds == []                                    # already on dest → no re-add
    assert gw.commands and gw.commands[0][1]["importMode"] == "Move"


def test_pending_skips_unmonitor_when_already_unmonitored():
    m, gw = _mover()
    res = _relocate(m, dict(_MOVIE, monitored=False), dest_present=True, dest_hasfile=False)
    assert res["status"] == "pending-import"
    assert gw.puts == []                                    # nothing to un-monitor (in-flight already)
    assert gw.commands and gw.commands[0][1]["importMode"] == "Move"


# ── FINALIZE (delete stale record + re-add the 1080p baseline; never a physical delete) ──────
def test_finalize_deletes_record_and_readds_baseline_with_search():
    m, gw = _mover()
    res = _relocate(m, dict(_MOVIE, monitored=False), dest_present=True, dest_hasfile=True, hd=3)
    assert res["status"] == "finalized"
    assert gw.deletes == [("standard", "movie/1?deleteFiles=false")]   # record only, no physical file
    assert len(gw.adds) == 1
    inst, payload = gw.adds[0]
    assert inst == "standard" and payload["qualityProfileId"] == 3
    assert payload["rootFolderPath"] == "/data/media/movies/Kids"      # source folder preserved
    assert payload["addOptions"] == {"searchForMovie": True} and payload["monitored"] is True
    assert "movieFile" not in payload and "path" not in payload
    assert gw.puts == []                                    # no retune PUT


def test_finalize_only_when_source_in_flight_unmonitored():
    # a still-monitored title on the destination is steady (or a freshly re-added baseline) → noop
    m, gw = _mover()
    res = _relocate(m, dict(_MOVIE, monitored=True), dest_hasfile=True, hd=3)
    assert res["status"] == "noop"
    assert gw.deletes == [] and gw.adds == []


def test_finalize_skips_steady_1080_baseline_even_if_unmonitored():
    # an operator-un-monitored title that ALREADY holds a healthy ≤1080 baseline file is a steady
    # dual title, NOT one we are moving — it must never be DELETE+re-added.
    m, gw = _mover()
    steady = {"id": 1, "title": "X", "tmdbId": 862, "monitored": False, "rootFolderPath": "/r",
              "hasFile": True, "movieFile": {"quality": {"quality": {"resolution": 1080}}}}
    res = _relocate(m, steady, dest_present=True, dest_hasfile=True, hd=3)
    assert res["status"] == "noop"
    assert gw.deletes == [] and gw.adds == []


def test_finalize_noop_when_no_baseline_profile():
    m, gw = _mover()
    res = _relocate(m, dict(_MOVIE, monitored=False), dest_hasfile=True, hd=None)
    assert res["status"] == "noop"
    assert gw.deletes == [] and gw.adds == []              # never delete without a clean re-add


# ── make-before-break + dry_run ───────────────────────────────────────────────
def test_dry_run_writes_nothing():
    m, gw = _mover(dry_run=True)
    r1 = _relocate(m, dest_hasfile=False)
    r2 = _relocate(m, dict(_MOVIE, monitored=False), dest_hasfile=True)
    assert r1["status"] == "would-move-in" and r2["status"] == "would-finalize"
    assert gw.puts == [] and gw.commands == [] and gw.adds == [] and gw.deletes == []


def test_move_in_never_touches_source_file():
    # the only source write during MOVE-IN is the monitored flag — never a delete or file op
    m, gw = _mover()
    _relocate(m, dest_hasfile=False)
    assert all(p[1] == "movie/editor" and set(p[2]) <= {"movieIds", "monitored"} for p in gw.puts)


def test_skip_when_no_file_path():
    m, gw = _mover()
    res = _relocate(m, {"id": 1, "title": "X", "tmdbId": 5, "monitored": True}, dest_hasfile=False)
    assert res["status"] == "skip"
    assert gw.adds == [] and gw.commands == []
