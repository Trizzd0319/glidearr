"""Adversarial tests for the cross-instance dedup ACTUATOR. Proves: same-path plans are never
actioned; the keeper's file is re-confirmed before any delete; only the LOSER's file is deleted
(record preserved, never a movie/{id} delete); the loser is un-monitored first; and dry-run writes
nothing."""
from __future__ import annotations

from scripts.managers.services.radarr.storage.cross_instance_dedup_apply import CrossInstanceDedup


class _GW:
    def __init__(self, libs):
        self._libs = libs                                   # {inst: [movies]}
        self.deletes, self.puts = [], []
    def library_items(self, inst):
        return self._libs.get(inst, [])
    def put(self, inst, endpoint, payload):
        self.puts.append((inst, endpoint, payload)); return {"ok": True}
    def delete(self, inst, endpoint):
        self.deletes.append((inst, endpoint)); return {"ok": True}


def _gw(*, keeper_has_file=True):
    return _GW({"ultra": [{"tmdbId": 7, "hasFile": keeper_has_file}],
                "standard": [{"tmdbId": 7, "hasFile": True}]})


_RECLAIM = {"tmdb": 7, "title": "Iron Man", "is_same_path": False, "action": "reclaim_loser_file",
            "keeper_inst": "ultra", "keeper_movie_id": 71, "keeper_file_id": 710, "keeper_res": 2160,
            "loser_inst": "standard", "loser_movie_id": 70, "loser_file_id": 700, "loser_res": 2160,
            "reason": "keep 2160p on ultra, reclaim 2160p on standard"}

_SAME_PATH = {"tmdb": 9, "title": "Quantumania", "is_same_path": True, "action": "flag_only",
              "path": "/data/media/movies/standard/Q/file.mkv"}


def test_happy_path_deletes_only_loser_file_record_kept():
    gw = _gw()
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_RECLAIM)
    assert res["status"] == "deduped"
    # exactly one DELETE, of the LOSER's moviefile, on the loser instance
    assert gw.deletes == [("standard", "moviefile/700")]
    # loser un-monitored first; never a movie/{id} record delete
    assert ("standard", "movie/editor", {"movieIds": [70], "monitored": False}) in gw.puts
    assert all("moviefile" in ep for _, ep in gw.deletes)
    assert all("movie/" != ep[:6] or ep.startswith("moviefile") for _, ep in gw.deletes)


def test_keeper_unconfirmed_blocks_delete():
    gw = _gw(keeper_has_file=False)
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_RECLAIM)
    assert res["status"] == "keeper-unconfirmed"
    assert gw.deletes == [] and gw.puts == []               # nothing touched


def test_same_path_never_actioned():
    gw = _gw()
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_SAME_PATH)
    assert res["status"] == "flag-same-path"
    assert gw.deletes == [] and gw.puts == []


def test_dry_run_writes_nothing():
    gw = _gw()
    d = CrossInstanceDedup(gw, dry_run=True)
    res = d.apply(_RECLAIM)
    assert res["status"] == "would-dedup"
    assert gw.deletes == [] and gw.puts == []


def test_incomplete_plan_skipped():
    gw = _gw()
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply({"tmdb": 7, "action": "reclaim_loser_file", "keeper_inst": "ultra",
                   "loser_inst": "standard", "loser_file_id": None})
    assert res["status"] == "skip"
    assert gw.deletes == []


def test_delete_failure_returns_dedup_failed():
    # a transient Radarr error on the file delete fails THIS title only, never raises out
    gw = _gw()
    def _boom(inst, ep): raise RuntimeError("500")
    gw.delete = _boom
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_RECLAIM)
    assert res["status"] == "dedup-failed"


class _ImFresh:
    """instance-manager stub that answers a fresh GET movie/{id} (uncached keeper re-confirm)."""
    def __init__(self, recs): self._recs = recs           # {movie_id: {"hasFile": bool}}
    def _make_request(self, inst, endpoint, method="GET", payload=None, fallback=None):
        if endpoint.startswith("movie/"):
            try:
                return self._recs.get(int(endpoint.split("/")[1]), fallback)
            except ValueError:
                return fallback
        return fallback


class _GWim(_GW):
    def __init__(self, libs, fresh):
        super().__init__(libs)
        self.im = _ImFresh(fresh)
    def resolve(self, inst): return inst


def test_fresh_keeper_read_overrides_stale_cache():
    # the run-cached library says the keeper still has a file, but a FRESH GET movie/71 says it does
    # NOT → the destructive delete is skipped (never leave the title copy-less).
    gw = _GWim({"ultra": [{"tmdbId": 7, "hasFile": True}], "standard": [{"tmdbId": 7, "hasFile": True}]},
               fresh={71: {"hasFile": False}})
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_RECLAIM)
    assert res["status"] == "keeper-unconfirmed"
    assert gw.deletes == []


def test_fresh_keeper_confirm_allows_delete():
    gw = _GWim({"ultra": [{"tmdbId": 7, "hasFile": False}],   # stale cache says no file...
                "standard": [{"tmdbId": 7, "hasFile": True}]},
               fresh={71: {"hasFile": True}})                  # ...but the fresh read confirms it
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_RECLAIM)
    assert res["status"] == "deduped"
    assert gw.deletes == [("standard", "moviefile/700")]


def test_stale_loser_file_id_skips_delete_no_404():
    # the plan's loser_file_id (700) is stale: a concurrent retune re-imported the loser under a NEW id
    # (999). Deleting 700 would 404 + reclaim nothing (and falsely log success) → skip cleanly instead.
    gw = _GWim({"ultra": [{"tmdbId": 7, "hasFile": True}], "standard": [{"tmdbId": 7, "hasFile": True}]},
               fresh={71: {"hasFile": True}, 70: {"hasFile": True, "movieFile": {"id": 999}}})
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_RECLAIM)
    assert res["status"] == "already-reclaimed"
    assert gw.deletes == []                                  # no stale-id delete → no 404 spam


def test_loser_already_fileless_skips_delete():
    # the loser's 2160p was already replaced (retune import) → fresh read shows no file → skip, not delete.
    gw = _GWim({"ultra": [{"tmdbId": 7, "hasFile": True}], "standard": [{"tmdbId": 7, "hasFile": True}]},
               fresh={71: {"hasFile": True}, 70: {"hasFile": False}})
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_RECLAIM)
    assert res["status"] == "already-reclaimed"
    assert gw.deletes == []


def test_current_loser_file_id_still_deletes():
    # fresh read confirms the loser STILL holds the SAME planned file (700) → the delete proceeds.
    gw = _GWim({"ultra": [{"tmdbId": 7, "hasFile": True}], "standard": [{"tmdbId": 7, "hasFile": True}]},
               fresh={71: {"hasFile": True}, 70: {"hasFile": True, "movieFile": {"id": 700}}})
    d = CrossInstanceDedup(gw, dry_run=False)
    res = d.apply(_RECLAIM)
    assert res["status"] == "deduped"
    assert gw.deletes == [("standard", "moviefile/700")]
