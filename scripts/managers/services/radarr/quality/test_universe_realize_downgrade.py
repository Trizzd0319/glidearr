"""Universe downgrades REALIZE (verify → delete → guid-grab) — quality/universe.py
apply_quality_actions, live path.

*arr never downgrades an existing file (cutoff-met rejection), so the old live path —
flip qualityProfileId and hope — reclaimed NOTHING (universe downgrade space was
phantom). The live downgrade branch now follows the pattern space_pressure.run_downgrades
already ships (its ``_pick_stepdown_release`` picker is IMPORTED, not copied): one
interactive ``release?movieId=`` search first; a smaller release exists → DELETE the
moviefile then POST the picked guid (post-delete blind MoviesSearch fallback on grab
error — works once the file is gone); no smaller release → file KEPT, profile still
lowered, counted ``no_release``, re-probes next run. keep-universe titles get the SAME
treatment (a quality change, not a title loss — the availability check guarantees a copy
always exists). The dry-run branch is unchanged: stamps only, zero API calls."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager
from scripts.managers.services.radarr.quality.universe import (
    RadarrQualityUniverseManager as U,
    _pick_stepdown_release,
)

_GB = 1024 ** 3


class _Log:
    def __init__(self): self.infos = []; self.warns = []
    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): self.warns.append(m)
    def log_debug(self, *a, **k): pass
    def log_grid(self, *a, **k): pass
    def log_table(self, *a, **k): pass


class _FakeMfm:
    def __init__(self, df): self._df = df; self.saved = None
    def load(self, instance): return self._df.copy()
    def save(self, instance, df): self.saved = df


class _API:
    """Fake Radarr; records every call. ``grab_error=True`` fails the guid POST
    (exercising the post-delete blind-search fallback)."""
    def __init__(self, releases, grab_error=False):
        self.releases = releases
        self.grab_error = grab_error
        self.calls = []

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        self.calls.append((endpoint, method, payload))
        if endpoint.startswith("movie/") and method == "GET":
            return {"id": int(endpoint.split("/")[1]), "qualityProfileId": 5, "title": "x"}
        if endpoint.startswith("release?movieId="):
            return self.releases.get(int(endpoint.split("=")[1]), [])
        if endpoint == "release" and method == "POST" and self.grab_error:
            raise RuntimeError("indexer rejected the grab")
        if method == "DELETE":
            # BASE CONTRACT: a successful DELETE returns True. It used to return None,
            # indistinguishable from a swallowed failure returning the None fallback, so
            # no call site could check a delete result. universe.py now checks it - and a
            # double that answers {} reads as a FAILED delete, which skips the grab and
            # logs "file kept" on a file that is gone.
            return True
        if endpoint == "release" and method == "POST":
            return {"id": 1}          # Radarr returns a body on a successful grab
        return {}


def _rel(guid, res, size_gb):
    """A release row. ``guid`` stays the short label the assertions match on; ``title``
    has to look like a real release NAME.

    GLD-RAD-30 gates the pick on the release title naming the movie (plus year and
    alternate titles), so a bare ``"hd"`` is refused before size is even considered -
    which is the gate working: the pick precedes the DELETE, and grabbing a release
    that does not name the film is how you lose the file and replace it with something
    else. These titles name the movie the fake serves (``"x"``).
    """
    return {"guid": guid, "indexerId": 3, "title": f"x.2020.{res}p.{guid}-GRP",
            "size": int(size_gb * _GB),
            "quality": {"quality": {"resolution": res}}}


def _row(mid, fid, policy="universe", action="downgrade"):
    return dict(movie_id=mid, movie_file_id=fid, title=f"M{mid}", keep_policy=policy,
                universe_name="mcu", quality_action=action, quality_profile_id=5,
                quality_profile_name="Ultra-4K", quality_name="Bluray-2160p",
                resolution=2160, size_bytes=30 * _GB, runtime_minutes=120)


_TARGET = {"id": 2, "name": "HD-1080p", "cutoff": 7, "items": [
    {"allowed": True, "quality": {"id": 7, "name": "Bluray-1080p", "resolution": 1080}}]}
_RANKED = [
    {"id": 2, "items": [{"allowed": True, "quality": {"id": 7, "name": "Bluray-1080p", "resolution": 1080}}]},
    {"id": 5, "items": [{"allowed": True, "quality": {"id": 19, "name": "Bluray-2160p", "resolution": 2160}}]},
]
# 1080p 8 GB is the legitimate step-down; 2160p 25 GB is at current res (never a pick);
# 0.1 GB is a <300MB fake the shared picker must reject.
_GOOD_RELEASES = [_rel("hd", 1080, 8.0), _rel("same4k", 2160, 25.0), _rel("tiny", 1080, 0.1)]


def _mgr(rows, releases, *, dry_run=False, grab_error=False):
    m = object.__new__(U)                        # skip __init__/registry/base
    m.config = {}
    m.dry_run = dry_run
    m.logger = _Log()
    m.global_cache = None
    m.radarr_api = _API(releases, grab_error=grab_error)
    fake = _FakeMfm(pd.DataFrame(rows))
    m._get_movie_files_manager = lambda: fake                  # type: ignore[attr-defined]
    m._resolve_instance = lambda i: i                          # type: ignore[attr-defined]
    m._fetch_ranked_profiles = lambda inst: _RANKED            # type: ignore[attr-defined]
    m._downgrade_target = (                                    # type: ignore[attr-defined]
        lambda row, ranked, cur, min_rank=0, likelihood=None: _TARGET)
    m._get_target_profile = lambda *a, **k: _TARGET            # type: ignore[attr-defined]
    return m, fake


def _writes(api):
    return [c for c in api.calls if c[1] in ("POST", "PUT", "DELETE")]


def test_picker_is_imported_from_space_pressure_not_copied():
    assert _pick_stepdown_release is RadarrSpacePressureManager._pick_stepdown_release


def test_live_downgrade_verifies_then_deletes_then_grabs_picked_guid():
    m, fake = _mgr([_row(1, 11)], {1: list(_GOOD_RELEASES)})
    stats = m.apply_quality_actions("standard")
    calls = m.radarr_api.calls
    put_i    = calls.index(("movie/1", "PUT", {"id": 1, "qualityProfileId": 2, "title": "x"}))
    search_i = calls.index(("release?movieId=1", "GET", None))
    del_i    = calls.index(("moviefile/11", "DELETE", None))
    grab_i   = calls.index(("release", "POST", {"guid": "hd", "indexerId": 3, "movieId": 1}))
    assert put_i < search_i < del_i < grab_i                   # verify BEFORE delete, grab AFTER
    assert all(p != {"name": "MoviesSearch", "movieIds": [1]}  # guid grab worked → no blind search
               for _e, _m, p in calls if isinstance(p, dict))
    assert stats["downgraded"] == 1 and stats["no_release"] == 0 and stats["grab_fallback"] == 0
    out = fake.saved
    r = out[out.movie_id == 1].iloc[0]
    assert r["quality_profile_id"] == 2 and r["quality_profile_name"] == "HD-1080p"
    # pandas 3 stores None as NaN in a str column; every consumer reads this with
    # .notna(), so isna IS the "cleared" contract - `is None` only held on object dtype.
    assert pd.isna(r["quality_action"])                        # realized → cleared
    assert r["planned_action"] == "downgrade" and r["plan_reason"] == "universe downgrade"


def test_no_smaller_release_keeps_file_lowers_profile_and_reprobes():
    m, fake = _mgr([_row(1, 11)], {1: [_rel("same4k", 2160, 25.0), _rel("tiny", 1080, 0.1)]})
    stats = m.apply_quality_actions("standard")
    calls = m.radarr_api.calls
    assert all(c[1] != "DELETE" for c in calls)                # file NEVER deleted
    assert all(c[0] != "release" or c[1] != "POST" for c in calls)   # nothing grabbed
    assert ("movie/1", "PUT", {"id": 1, "qualityProfileId": 2, "title": "x"}) in calls
    assert stats["no_release"] == 1 and stats["downgraded"] == 0 and stats["failed"] == 0
    out = fake.saved
    r = out[out.movie_id == 1].iloc[0]
    assert r["quality_profile_id"] == 2                        # profile still lowered
    assert r["quality_action"] == "downgrade"                  # left set → re-probes next run
    assert r["planned_action"] == "downgrade"                  # plan persists (ledger/credit)


def test_grab_error_falls_back_to_blind_search_after_delete():
    m, fake = _mgr([_row(1, 11)], {1: list(_GOOD_RELEASES)}, grab_error=True)
    stats = m.apply_quality_actions("standard")
    calls = m.radarr_api.calls
    assert ("moviefile/11", "DELETE", None) in calls           # file already gone
    assert ("command", "POST", {"name": "MoviesSearch", "movieIds": [1]}) in calls
    assert stats["downgraded"] == 1 and stats["grab_fallback"] == 1
    r = fake.saved[fake.saved.movie_id == 1].iloc[0]
    assert pd.isna(r["quality_action"])                        # blind search owns it now


def test_keep_universe_rows_get_the_same_realize_treatment():
    """Per policy, keep-universe titles are quality-change-only — the verify → delete →
    guid-grab IS a quality change, and the availability check guarantees a copy exists
    (release verified BEFORE the delete), so the never-LOSE pin holds."""
    m, fake = _mgr([_row(1, 11, policy="keep_universe")], {1: list(_GOOD_RELEASES)})
    stats = m.apply_quality_actions("standard")
    calls = m.radarr_api.calls
    assert ("moviefile/11", "DELETE", None) in calls
    assert ("release", "POST", {"guid": "hd", "indexerId": 3, "movieId": 1}) in calls
    assert stats["downgraded"] == 1
    # …and with no release available, the keep-universe file is untouchable:
    m2, _ = _mgr([_row(2, 22, policy="keep_universe")], {2: []})
    stats2 = m2.apply_quality_actions("standard")
    assert stats2["no_release"] == 1
    assert all(c[1] != "DELETE" for c in m2.radarr_api.calls)


def test_dry_run_branch_unchanged_stamps_only_no_api_calls():
    m, fake = _mgr([_row(1, 11)], {1: list(_GOOD_RELEASES)}, dry_run=True)
    stats = m.apply_quality_actions("standard")
    assert m.radarr_api.calls == []                            # not even a movie GET
    assert stats["downgraded"] == 1 and stats["no_release"] == 0
    r = fake.saved[fake.saved.movie_id == 1].iloc[0]
    assert r["planned_action"] == "downgrade"                  # ledger stamp only
    assert r["quality_profile_id"] == 5                        # NO speculative profile write
    assert r["quality_action"] == "downgrade"                  # evaluate refreshes it next run


def test_live_upgrade_path_is_unchanged_no_search_no_delete():
    m, fake = _mgr([_row(1, 11, action="upgrade")], {1: list(_GOOD_RELEASES)})
    stats = m.apply_quality_actions("standard")
    calls = m.radarr_api.calls
    assert ("movie/1", "PUT", {"id": 1, "qualityProfileId": 2, "title": "x"}) in calls
    assert all("release" not in c[0] for c in calls)           # no interactive search
    assert all(c[1] != "DELETE" for c in calls)                # nothing deleted
    assert stats["upgraded"] == 1
    r = fake.saved[fake.saved.movie_id == 1].iloc[0]
    assert pd.isna(r["quality_action"]) and r["plan_reason"] == "universe upgrade"
