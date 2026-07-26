"""Realize-path tests for SonarrSpacePressureManager._realize_stepdown_files.

A profile flip alone reclaims nothing — Sonarr will not replace a file that already
exceeds the new cutoff ("cutoff met"), which is why the TV step-down used to free
zero bytes. These tests pin the replacement contract at episode-file granularity:

  * happy path      — ONE interactive search, then DELETE, then the guid grab, in
                      that order, with the reclaim counted from the deleted file.
  * no release      — nothing smaller exists → the file is KEPT (never traded for an
                      empty indexer result) and re-probes next run.
  * grab fallback   — the grab does not take, but the file is already gone, so the
                      episode joins the blind EpisodeSearch pool (effective now that
                      the cutoff-met blocker died with the file).
  * soft reject     — _make_request returning None (indexer down / release stale) is
                      a failed grab too, not a success.
  * inline cap      — interactive searches are slow, so files past the budget defer.
  * multi-episode   — a file backing several episodes is never single-grabbed.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.series.space_pressure import SonarrSpacePressureManager


class _Logger:
    def log_warning(self, *a, **k): pass
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _FakeApi:
    """Records every call; serves releases per episode id."""

    def __init__(self, releases_by_eid, grab_result="ok", raise_on_grab=False):
        self.calls: list = []
        self._releases = releases_by_eid
        self._grab_result = grab_result
        self._raise_on_grab = raise_on_grab

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        self.calls.append((method, endpoint))
        if endpoint.startswith("release?episodeId="):
            return self._releases.get(int(endpoint.split("=")[1]), [])
        if endpoint == "release" and method == "POST":
            if self._raise_on_grab:
                raise RuntimeError("indexer exploded")
            return self._grab_result
        if endpoint.startswith("episodefile/") and method == "DELETE":
            return None
        return fallback


class _FakeEpisodeFiles:
    """Episode-id resolver: (sid, season, episode) -> id."""

    def __init__(self, mapping=None):
        self._m = mapping or {}

    def _get_episode_id(self, instance, sid, sn, en):
        return self._m.get((int(sid), int(sn), int(en)))


def _rel(res, gb, guid="g1"):
    return {"guid": guid, "indexerId": 1, "title": guid, "size": gb * 1024 ** 3,
            "quality": {"quality": {"resolution": res}}}


def _df(rows):
    return pd.DataFrame(rows)


def _mk(api, ef_map=None):
    m = object.__new__(SonarrSpacePressureManager)
    m.config = {}
    m.logger = _Logger()
    m.sonarr_api = api
    # default: the single _ROW episode (series 1, S01E02) resolves to episode id 900
    m._ef = _FakeEpisodeFiles(ef_map if ef_map is not None else {(1, 1, 2): 900})
    return m


def _cand(sid=1, title="Show", indices=(0,)):
    # ``indices`` are the candidate series' episode rows in the df — the planner
    # supplies them and the realize walk iterates exactly those.
    return {"sid": sid, "title": title, "target_name": "HD-720p",
            "indices": list(indices)}


def _stats():
    return {"realized": 0, "realized_reclaim_gb": 0.0, "no_release": 0,
            "grab_fallback": 0, "deferred": 0, "skipped_multi_ep": 0, "failed": 0}


_ROW = {"series_id": 1, "episode_file_id": 55, "resolution": 2160,
        "size_bytes": 8 * 1024 ** 3, "season_number": 1, "episode_number": 2}


def test_happy_path_searches_then_deletes_then_grabs():
    api = _FakeApi({900: [_rel(720, 1.5)]})
    m, st, fb = _mk(api), _stats(), []
    m._realize_stepdown_files("standard", _df([_ROW]), m._ef, _cand(), 720,
                              {55: 1}, budget=5, fallback_eids=fb, stats=st)
    kinds = [(meth, ep.split("?")[0].split("/")[0]) for meth, ep in api.calls]
    assert kinds == [("GET", "release"), ("DELETE", "episodefile"), ("POST", "release")]
    assert st["realized"] == 1 and st["no_release"] == 0
    assert round(st["realized_reclaim_gb"], 2) == 8.0    # reclaim = the DELETED file
    assert fb == []


def test_no_smaller_release_keeps_the_file():
    # only a same-resolution release exists -> the ladder picker returns nothing
    api = _FakeApi({900: [_rel(2160, 9.0)]})
    m, st, fb = _mk(api), _stats(), []
    m._realize_stepdown_files("standard", _df([_ROW]), m._ef, _cand(), 720,
                              {55: 1}, budget=5, fallback_eids=fb, stats=st)
    assert st["no_release"] == 1 and st["realized"] == 0
    assert not any(meth == "DELETE" for meth, _ in api.calls)   # file untouched
    assert st["realized_reclaim_gb"] == 0.0


def test_grab_error_falls_back_to_blind_search_after_delete():
    api = _FakeApi({900: [_rel(720, 1.5)]}, raise_on_grab=True)
    m, st, fb = _mk(api), _stats(), []
    m._realize_stepdown_files("standard", _df([_ROW]), m._ef, _cand(), 720,
                              {55: 1}, budget=5, fallback_eids=fb, stats=st)
    assert any(meth == "DELETE" for meth, _ in api.calls)       # file IS gone
    assert st["realized"] == 1                                   # space freed regardless
    assert st["grab_fallback"] == 1 and fb == [900]


def test_soft_reject_none_counts_as_failed_grab():
    api = _FakeApi({900: [_rel(720, 1.5)]}, grab_result=None)
    m, st, fb = _mk(api), _stats(), []
    m._realize_stepdown_files("standard", _df([_ROW]), m._ef, _cand(), 720,
                              {55: 1}, budget=5, fallback_eids=fb, stats=st)
    assert st["grab_fallback"] == 1 and fb == [900]


def test_inline_cap_defers_the_remainder():
    rows = [dict(_ROW, episode_file_id=55, episode_number=2),
            dict(_ROW, episode_file_id=56, episode_number=3)]
    api = _FakeApi({900: [_rel(720, 1.5)], 901: [_rel(720, 1.5)]})
    m = _mk(api, {(1, 1, 2): 900, (1, 1, 3): 901})
    st, fb = _stats(), []
    left = m._realize_stepdown_files("standard", _df(rows), m._ef, _cand(indices=(0, 1)), 720,
                                     {55: 1, 56: 1}, budget=1, fallback_eids=fb, stats=st)
    assert st["realized"] == 1 and st["deferred"] == 1 and left == 0


def test_multi_episode_file_is_never_single_grabbed():
    api = _FakeApi({900: [_rel(720, 1.5)]})
    m, st, fb = _mk(api), _stats(), []
    m._realize_stepdown_files("standard", _df([_ROW]), m._ef, _cand(), 720,
                              {55: 3}, budget=5, fallback_eids=fb, stats=st)
    assert st["skipped_multi_ep"] == 1 and st["realized"] == 0
    assert api.calls == []      # not even searched


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} realize tests passed")
