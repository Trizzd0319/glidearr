"""Unit tests for the download-based dual-version actuator (CrossInstanceMove).

The cross-instance file MOVE was removed (it needed a shared mount + durable filesystem moves that
aren't portable across households). What remains, driven by a recording fake gateway:
  * ``retune_baseline`` — retune an owned 2160p record DOWN to its ≤1080 baseline profile + re-monitor
    + RescanMovie + MoviesSearch (no file move, no delete; Radarr replaces the 2160p on the 1080p
    import — make-before-break, never file-less);
  * ``acquire`` — add the title to the 4K instance monitored + SEARCH ON so it downloads its own 2160p.
"""
from __future__ import annotations

from scripts.managers.services.radarr.storage.cross_instance_move import CrossInstanceMove


class _GW:
    def __init__(self):
        self.adds, self.commands, self.puts, self.gets = [], [], [], []
        self.manualimport = None                 # canned GET /manualimport response (None ⇒ sees nothing)
        # standard has two tags; ultra shares 'anime' (different id) and is MISSING 'keep-universe-mcu'
        self._tags = {"standard": [{"id": 7, "label": "keep-universe-mcu"}, {"id": 9, "label": "anime"}],
                      "ultra": [{"id": 2, "label": "anime"}]}
        self.created = []

    def add(self, inst, payload):
        self.adds.append((inst, payload)); return {"id": 500}

    def command(self, inst, payload):
        self.commands.append((inst, payload)); return {"id": 1}

    def put(self, inst, endpoint, payload):
        self.puts.append((inst, endpoint, payload)); return {"ok": True}

    def get(self, inst, endpoint, fallback=None):
        self.gets.append((inst, endpoint))
        if endpoint.startswith("manualimport"):
            return self.manualimport if self.manualimport is not None else fallback
        return fallback

    def tags(self, inst):
        return self._tags.get(inst, [])

    def ensure_tag(self, inst, label):
        existing = next((t for t in self._tags.get(inst, []) if t["label"].lower() == label.lower()), None)
        if existing:
            return existing["id"]
        nid = 100 + len(self.created)
        self._tags.setdefault(inst, []).append({"id": nid, "label": label})
        self.created.append((inst, label))
        return nid


_MOVIE = {"id": 1, "title": "Toy Story", "tmdbId": 862, "year": 1995, "monitored": True,
          "qualityProfileId": 9, "rootFolderPath": "/data/media/movies/Kids",
          "movieFile": {"id": 50, "path": "/data/media/movies/Kids/Toy Story (1995)/ts.mkv",
                        "quality": {"quality": {"resolution": 2160}}}}


def _mover(dry_run=False):
    gw = _GW()
    return CrossInstanceMove(gw, logger=None, dry_run=dry_run), gw


def _retune(mover, movie=None, *, hd=3):
    return mover.retune_baseline(movie or _MOVIE, inst="standard", hd_profile_id=hd)


# ── retune_baseline (retune the EXISTING record in place — no move, no delete) ────────────────────
def test_retune_to_1080_baseline_with_rescan_and_search():
    m, gw = _mover()
    res = _retune(m, _MOVIE, hd=3)                          # profile 9 != baseline 3, holds a 2160p
    assert res["status"] == "retuned"
    assert gw.adds == []                                    # no add, no delete → Radarr id/history kept
    assert gw.puts == [("standard", "movie/editor",
                        {"movieIds": [1], "qualityProfileId": 3, "monitored": True})]
    # rescan, then search → Radarr grabs the 1080p baseline and replaces the 2160p on import
    assert ("standard", {"name": "RescanMovie", "movieIds": [1]}) in gw.commands
    assert ("standard", {"name": "MoviesSearch", "movieIds": [1]}) in gw.commands


def test_retune_noop_when_already_at_baseline_profile():
    m, gw = _mover()
    res = _retune(m, dict(_MOVIE, qualityProfileId=3), hd=3)
    assert res["status"] == "noop"
    assert gw.puts == [] and gw.commands == []             # already the baseline — nothing to do


def test_retune_skips_steady_1080_baseline():
    # a title already holding a healthy ≤1080 file is a steady dual title, never re-touched
    m, gw = _mover()
    steady = {"id": 1, "title": "X", "tmdbId": 862, "monitored": False, "qualityProfileId": 9,
              "hasFile": True, "movieFile": {"quality": {"quality": {"resolution": 1080}}}}
    res = _retune(m, steady, hd=3)
    assert res["status"] == "noop"
    assert gw.puts == [] and gw.commands == []


def test_retune_noop_when_no_baseline_profile():
    m, gw = _mover()
    res = _retune(m, _MOVIE, hd=None)
    assert res["status"] == "noop"
    assert gw.puts == [] and gw.commands == []


def test_retune_dry_run_writes_nothing():
    m, gw = _mover(dry_run=True)
    res = _retune(m, _MOVIE, hd=3)
    assert res["status"] == "would-retune"
    assert gw.puts == [] and gw.commands == [] and gw.adds == []


def test_retune_never_deletes_a_file():
    # the only write is the in-place profile/monitored edit — never a delete or a file op
    m, gw = _mover()
    _retune(m, _MOVIE, hd=3)
    assert all(p[1] == "movie/editor" and set(p[2]) <= {"movieIds", "qualityProfileId", "monitored"}
               for p in gw.puts)
    assert not any("delete" in str(c).lower() or c[1].get("importMode") for c in gw.commands)


# ── acquire (the 4K instance downloads its OWN 2160p — source untouched) ──────────────────────────
def test_acquire_adds_4k_with_search_on():
    m, gw = _mover()
    res = m.acquire(_MOVIE, to_inst="ultra", dest_root="/data/media/movies/4k", dest_profile_id=7)
    assert res["status"] == "acquired"
    assert len(gw.adds) == 1
    inst, payload = gw.adds[0]
    assert inst == "ultra" and payload["qualityProfileId"] == 7
    assert payload["rootFolderPath"] == "/data/media/movies/4k"
    assert payload["addOptions"] == {"searchForMovie": True} and payload["monitored"] is True
    assert "movieFile" not in payload and "path" not in payload
    assert gw.puts == [] and gw.commands == []             # source untouched, no scan/move


def test_acquire_dry_run_no_add():
    m, gw = _mover(dry_run=True)
    res = m.acquire(_MOVIE, to_inst="ultra", dest_root="/r", dest_profile_id=7)
    assert res["status"] == "would-acquire" and gw.adds == []


# ── ensure_acquiring (drive an EXISTING 4K record) + unmonitor (freeze standard) ──────────────────
def test_ensure_acquiring_drives_existing_record():
    m, gw = _mover()
    res = m.ensure_acquiring(55, inst="ultra", profile_id=7)
    assert res["status"] == "acquiring"
    assert gw.puts == [("ultra", "movie/editor",
                        {"movieIds": [55], "qualityProfileId": 7, "monitored": True})]
    assert gw.commands == [("ultra", {"name": "MoviesSearch", "movieIds": [55]})]
    assert gw.adds == []                                   # drives the existing record, never re-adds


def test_ensure_acquiring_dry_run():
    m, gw = _mover(dry_run=True)
    res = m.ensure_acquiring(55, inst="ultra", profile_id=7)
    assert res["status"] == "would-acquire"
    assert gw.puts == [] and gw.commands == []


def test_ensure_acquiring_noop_without_id_or_profile():
    m, gw = _mover()
    assert m.ensure_acquiring(None, inst="ultra", profile_id=7)["status"] == "noop"
    assert m.ensure_acquiring(55, inst="ultra", profile_id=None)["status"] == "noop"
    assert gw.puts == [] and gw.commands == []


def test_unmonitor_freezes_record():
    m, gw = _mover()
    res = m.unmonitor(1, inst="standard")
    assert res["status"] == "frozen"
    assert gw.puts == [("standard", "movie/editor", {"movieIds": [1], "monitored": False})]
    assert gw.commands == []                               # freeze only — no search, no delete


def test_unmonitor_dry_run():
    m, gw = _mover(dry_run=True)
    res = m.unmonitor(1, inst="standard")
    assert res["status"] == "would-freeze"
    assert gw.puts == []


# ── relocate (SHARED STORAGE: import the existing 2160p, copy → hardlink, no re-download) ─────────
_CAND = [{"path": "/data/media/movies/Kids/Toy Story (1995)/ts.mkv", "size": 50_000_000_000,
          "quality": {"quality": {"resolution": 2160, "name": "Remux-2160p"}},
          "languages": [{"id": 1, "name": "English"}], "releaseGroup": "GRP", "movie": {"id": 500}}]


def _seeing(cand, dry_run=False):
    m, gw = _mover(dry_run=dry_run)
    gw.manualimport = cand
    return m, gw


def _mi_cmd(gw):
    return next((c[1] for c in gw.commands if c[1].get("name") == "ManualImport"), None)


def test_relocate_imports_existing_into_a_new_record():
    m, gw = _seeing(_CAND)
    res = m.relocate(_MOVIE, to_inst="ultra", dest_root="/data/media/movies/4k",
                     dest_profile_id=7, from_inst="standard")
    assert res["status"] == "relocating"
    # PROBED manualimport for the source folder (derived from the movieFile path) BEFORE any write
    assert any("manualimport" in e for _, e in gw.gets)
    # added a 4K record with SEARCH OFF (import, not download), 4k root + 2160p profile
    assert len(gw.adds) == 1 and gw.adds[0][0] == "ultra"
    assert gw.adds[0][1]["addOptions"] == {"searchForMovie": False}
    assert gw.adds[0][1]["qualityProfileId"] == 7
    # ManualImport COPY of the existing file, bound to the new record id (500)
    cmd = _mi_cmd(gw)
    assert cmd and cmd["importMode"] == "copy"
    assert cmd["files"][0]["movieId"] == 500
    assert cmd["files"][0]["path"] == "/data/media/movies/Kids/Toy Story (1995)/ts.mkv"
    # NEVER a Move or a delete — the source file is untouched (make-before-break)
    assert not any(c[1].get("importMode") == "move" or "delete" in str(c).lower() for c in gw.commands)


def test_relocate_drives_existing_record_without_readd():
    m, gw = _seeing(_CAND)
    res = m.relocate(_MOVIE, to_inst="ultra", dest_root="/data/media/movies/4k",
                     dest_profile_id=7, from_inst="standard", dest_id=42)
    assert res["status"] == "relocating"
    assert gw.adds == []                                    # existing record → no re-add
    assert ("ultra", "movie/editor",
            {"movieIds": [42], "qualityProfileId": 7, "monitored": True}) in gw.puts
    assert _mi_cmd(gw)["files"][0]["movieId"] == 42


def test_relocate_not_visible_adds_nothing():
    # the 4K instance sees NO importable file (not shared storage) → adds nothing, no import; the
    # caller falls back to download. Adding here would duplicate the 4K record.
    m, gw = _mover()                                        # gw.manualimport stays None
    res = m.relocate(_MOVIE, to_inst="ultra", dest_root="/data/media/movies/4k",
                     dest_profile_id=7, from_inst="standard")
    assert res["status"] == "not-visible"
    assert gw.adds == [] and gw.puts == [] and _mi_cmd(gw) is None


def test_relocate_dry_run_writes_nothing():
    m, gw = _seeing(_CAND, dry_run=True)
    res = m.relocate(_MOVIE, to_inst="ultra", dest_root="/data/media/movies/4k",
                     dest_profile_id=7, from_inst="standard")
    assert res["status"] == "would-relocate"
    assert gw.adds == [] and gw.commands == [] and gw.puts == []


def test_relocate_picks_the_largest_2160p_file():
    two = [dict(_CAND[0], path="/a/small.mkv", size=1), dict(_CAND[0], path="/a/big.mkv", size=99)]
    m, gw = _seeing(two)
    m.relocate(_MOVIE, to_inst="ultra", dest_root="/data/media/movies/4k",
               dest_profile_id=7, from_inst="standard", dest_id=1)
    assert _mi_cmd(gw)["files"][0]["path"] == "/a/big.mkv"


def test_relocate_ignores_sub_2160_and_rejected_files():
    # a BIGGER 1080p companion + a rejected 2160p + a genuine 2160p → import ONLY the real 2160p, so
    # the caller's uhd_has_2160 gate converges (a sub-4K import would re-fire forever) and no sample lands.
    files = [dict(_CAND[0], path="/a/big-1080.mkv", size=99, quality={"quality": {"resolution": 1080}}),
             dict(_CAND[0], path="/a/rej-2160.mkv", size=80, rejections=[{"reason": "Sample"}]),
             dict(_CAND[0], path="/a/good-2160.mkv", size=50)]
    m, gw = _seeing(files)
    m.relocate(_MOVIE, to_inst="ultra", dest_root="/data/media/movies/4k",
               dest_profile_id=7, from_inst="standard", dest_id=1)
    assert _mi_cmd(gw)["files"][0]["path"] == "/a/good-2160.mkv"


def test_relocate_not_visible_when_only_sub_2160p():
    m, gw = _seeing([dict(_CAND[0], quality={"quality": {"resolution": 1080}})])
    res = m.relocate(_MOVIE, to_inst="ultra", dest_root="/data/media/movies/4k",
                     dest_profile_id=7, from_inst="standard")
    assert res["status"] == "not-visible"                  # no genuine 2160p → caller downloads
    assert gw.adds == [] and _mi_cmd(gw) is None


def test_relocate_skips_without_a_source_folder():
    m, gw = _seeing(_CAND)
    res = m.relocate({"tmdbId": 862, "title": "X"}, to_inst="ultra",
                     dest_root="/data/media/movies/4k", dest_profile_id=7, from_inst="standard")
    assert res["status"] == "skip"
    assert gw.adds == [] and gw.gets == [] and _mi_cmd(gw) is None


# ── tags carried across instances by LABEL (per-instance ids must NOT be copied raw) ──
def test_acquire_carries_tags_by_label():
    m, gw = _mover()
    tagged = dict(_MOVIE, tags=[9])                        # anime only (standard id 9)
    res = m.acquire(tagged, to_inst="ultra", dest_root="/r", dest_profile_id=7, from_inst="standard")
    assert res["status"] == "acquired"
    _, payload = gw.adds[0]
    assert payload["tags"] == [2]                          # anime -> ultra's id 2, nothing created
    assert gw.created == []


def test_acquire_creates_missing_label_on_dest():
    m, gw = _mover()
    tagged = dict(_MOVIE, tags=[7, 9])                     # keep-universe-mcu(7) missing on ultra, anime(9)
    res = m.acquire(tagged, to_inst="ultra", dest_root="/r", dest_profile_id=7, from_inst="standard")
    assert res["status"] == "acquired"
    _, payload = gw.adds[0]
    assert 2 in payload["tags"] and 7 not in payload["tags"] and 9 not in payload["tags"]
    assert ("ultra", "keep-universe-mcu") in gw.created
    assert len(payload["tags"]) == 2


def test_no_source_instance_copies_no_tags():
    # without a source instance we can't resolve labels → carry NO tags (never the raw source ids)
    m, gw = _mover()
    res = m.acquire(dict(_MOVIE, tags=[7, 9]), to_inst="ultra", dest_root="/r", dest_profile_id=7)
    assert res["status"] == "acquired"
    assert gw.adds[0][1]["tags"] == []
