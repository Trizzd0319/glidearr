"""Exhaustive step-down policy — "deletion is the TRUE last resort" (movies).

``space_exhaustive_downgrade`` (DEFAULT ON) makes the movie step-down pass shrink
EVERYTHING that can still be shrunk before anything may be deleted:

  * the planner drops the watchability ceiling and plans every title down to the 720p
    floor (covered in machine_learning/space/test_downgrade_planner.py);
  * ``_pick_stepdown_release`` may fall BELOW 720 — but ONLY for a title with no >=720
    release at all, and only when the caller opts in;
  * the pass stops at the band top U measured against free space NET of the smaller
    replacements it queued this run (a realized step-down deletes NOW and imports LATER,
    so the raw free-space figure spikes — deciding against that spike would downgrade the
    library against phantom headroom);
  * ``space_downgrade_max_regrabs_per_run`` bounds the re-grab storm; deferred titles keep
    their files (and, still being above the floor, stay undeletable).

Stub manager via ``object.__new__`` — the heavy __init__/registry is bypassed.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager

_GIB = 1024 ** 3
_pick = RadarrSpacePressureManager._pick_stepdown_release


def _rel(guid, res, size_gib):
    return {"guid": guid, "indexerId": 3, "title": guid, "size": int(size_gib * _GIB),
            "quality": {"quality": {"resolution": res}}}


# ══ 1. sub-720 fallback in the shared release picker ═════════════════════════════
def test_sub720_never_chosen_while_a_720_rung_exists():
    rels = [_rel("hd720", 720, 4.0), _rel("sd480", 480, 1.0)]
    assert _pick(rels, current_res=1080, allow_below_floor=True)["guid"] == "hd720"
    # …and the 1080 rung is preferred over nothing, still never the SD one
    rels2 = [_rel("hd1080", 1080, 8.0), _rel("sd480", 480, 1.0)]
    assert _pick(rels2, current_res=2160, allow_below_floor=True)["guid"] == "hd1080"


def test_sub720_chosen_only_when_no_ge720_release_exists():
    rels = [_rel("sd480", 480, 1.0), _rel("sd576", 576, 1.5)]
    got = _pick(rels, current_res=1080, allow_below_floor=True)
    assert got["guid"] == "sd576"                     # BEST available sub-720 rung
    assert got["stepped_below_floor"] is True         # marker so the caller can say why


def test_sub720_requires_the_opt_in_flag_default_is_the_hard_720_floor():
    rels = [_rel("sd480", 480, 1.0)]
    assert _pick(rels, current_res=1080) is None                       # default: unchanged
    assert _pick(rels, current_res=1080, allow_below_floor=False) is None


def test_sub720_still_applies_the_fake_undersized_size_floor():
    # A 100 MiB "movie" is a fake — rejected even on the last-resort sub-720 path.
    rels = [_rel("fake", 480, 0.1)]
    assert _pick(rels, current_res=1080, allow_below_floor=True) is None
    assert _pick(rels, current_res=1080, allow_below_floor=True,
                 min_size_bytes=50 * 1024 * 1024)["guid"] == "fake"    # episode-sized floor


def test_ge720_path_returns_the_untouched_release_object():
    # The normal ladder must stay byte-identical: same object, no marker key added.
    r = _rel("hd720", 720, 4.0)
    got = _pick([r], current_res=1080, allow_below_floor=True)
    assert got is r and "stepped_below_floor" not in r


# ══ 2. the pass: in-flight accounting, stop-at-U, re-grab cap ════════════════════
class _Log:
    def __init__(self): self.infos = []
    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): self.infos.append(m)
    def log_debug(self, *a, **k): pass
    def log_table(self, *a, **k): pass
    def log_grid(self, *a, **k): pass


class _Mfm:
    def __init__(self, df): self._df = df; self.saved = None
    def load(self, instance): return self._df.copy()
    def save(self, instance, df): self.saved = df


class _Api:
    """Fake Radarr: every movie's interactive search returns ONE 1080p release of
    ``pick_gib`` so the replacement size is deterministic."""
    def __init__(self, pick_gib): self.pick_gib = pick_gib; self.calls = []

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        self.calls.append((endpoint, method))
        if endpoint.startswith("movie/") and method == "GET":
            return {"id": int(endpoint.split("/")[1]), "qualityProfileId": 13}
        if endpoint.startswith("release?movieId="):
            return [_rel(f"r{endpoint.split('=')[1]}", 1080, self.pick_gib)]
        return {}


_PROFILES = [
    {"id": 11, "name": "HD-720p", "items": [{"allowed": True, "quality": {"resolution": 720, "name": "q720"}}]},
    {"id": 12, "name": "HD-1080p", "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "q1080"}}]},
    {"id": 13, "name": "UHD", "items": [{"allowed": True, "quality": {"resolution": 2160, "name": "q2160"}}]},
]


def _lib(n, size_gib=50.0):
    return pd.DataFrame([
        dict(movie_id=i, movie_file_id=100 + i, title=f"M{i}", resolution=2160,
             size_bytes=int(size_gib * _GIB), runtime_minutes=100.0, keep_policy=None,
             is_watched=False, last_watched_at=None, collection_name=None,
             universe_credit=0.0, quality_profile_id=13, quality_profile_name="UHD",
             quality_action=None)
        for i in range(1, n + 1)
    ])


def _mgr(df, *, free, U, pick_gib, cap=0, exhaustive=True, dry_run=False):
    m = object.__new__(RadarrSpacePressureManager)
    m.config = {"space_exhaustive_downgrade": exhaustive}
    if cap:
        m.config["space_downgrade_max_regrabs_per_run"] = cap
    m.dry_run = dry_run
    m.logger = _Log()
    m.global_cache = None
    m.radarr_api = _Api(pick_gib)
    mfm = _Mfm(df)
    m._get_movie_files_manager = lambda: mfm                       # type: ignore[attr-defined]
    m._fetch_ranked_profiles = lambda inst: _PROFILES              # type: ignore[attr-defined]
    m._fetch_hd720p_profile = lambda inst: _PROFILES[0]            # type: ignore[attr-defined]
    m._build_active_collection_set = lambda d: set()               # type: ignore[attr-defined]
    m._build_score_map = lambda d, inst: {i: i for i in d.index}   # type: ignore[attr-defined]
    m._space_targets = lambda inst=None: (U / 1.1, U)              # type: ignore[attr-defined]
    return m, mfm


def test_inflight_accounting_prevents_a_second_wave_on_phantom_free_space():
    # 4 x 50 GiB 4K titles, each replaced by a 45 GiB 1080p grab → the REAL net gain is only
    # 5 GiB apiece, but the raw free-space figure jumps 50 GiB the instant each file is
    # deleted. Booking the queued replacement (45 GiB) against free space is what stops the
    # pass believing it already hit U after two deletions.
    m, _ = _mgr(_lib(4), free=0.0, U=100.0, pick_gib=45.0)
    st = m.run_downgrades("standard", 0.0)
    assert st["downgraded"] == 4 and st["stopped_at_target"] == 0
    assert round(st["freed_now_gb"]) == 200 and round(st["inflight_regrab_gb"]) == 180
    # Naive (freed-only) accounting would have read 100 GiB free after 2 and stopped:
    assert st["freed_now_gb"] - st["inflight_regrab_gb"] < 100.0


def test_pass_stops_at_U_instead_of_downgrading_the_whole_library():
    # Same library, but the replacements are tiny (5 GiB) → net +45 GiB each. free+net
    # reaches U=100 before the 4th candidate, which is left untouched at its current quality.
    m, _ = _mgr(_lib(4), free=0.0, U=100.0, pick_gib=5.0)
    st = m.run_downgrades("standard", 0.0)
    assert st["downgraded"] == 3 and st["stopped_at_target"] == 1
    assert st["candidates_found"] == 4                    # the planner still PLANNED all four


def test_regrab_cap_defers_the_remainder_and_keeps_their_files():
    m, mfm = _mgr(_lib(4), free=0.0, U=10_000.0, pick_gib=5.0, cap=2)
    st = m.run_downgrades("standard", 0.0)
    assert st["downgraded"] == 2 and st["deferred_cap"] == 2
    # exactly two files deleted — the deferred pair keeps its copies (and, still above the
    # 720p floor, stays out of every delete pool)
    assert sum(1 for e, meth in m.radarr_api.calls if meth == "DELETE") == 2


def test_below_floor_pick_is_counted_and_logged():
    m, _ = _mgr(_lib(1), free=0.0, U=10_000.0, pick_gib=5.0)
    m.radarr_api._make_request = (                                  # only an SD release exists
        lambda inst, ep, method="GET", payload=None, fallback=None:
        ({"id": 1, "qualityProfileId": 13} if ep.startswith("movie/") and method == "GET"
         else [_rel("sd", 480, 2.0)] if ep.startswith("release?movieId=") else {}))
    st = m.run_downgrades("standard", 0.0)
    assert st["downgraded"] == 1 and st["below_floor_picks"] == 1
    assert any("BELOW 720" in msg for msg in m.logger.infos)


def test_legacy_mode_applies_neither_the_cap_nor_the_stop_at_U():
    # space_exhaustive_downgrade=false: the pass behaves exactly as before — the planner's
    # need_gb spread alone governs; the per-run cap and the stop-at-U never fire even with
    # cap=1 and free far below U. (The in-flight figures are still MEASURED, purely for
    # observability — they change no decision on this path.)
    m, _ = _mgr(_lib(4), free=0.0, U=100.0, pick_gib=45.0, cap=1, exhaustive=False)
    st = m.run_downgrades("standard", 0.0)
    assert st["stopped_at_target"] == 0 and st["deferred_cap"] == 0
    assert st["downgraded"] == st["candidates_found"] > 0
    assert st["below_floor_picks"] == 0


def test_dry_run_models_the_same_stop_at_U():
    # The preview must stop where a live run would, so the plan the operator reviews is the
    # plan that would execute. No pick to size → the replacement is the planner's estimate.
    m, mfm = _mgr(_lib(4), free=0.0, U=100.0, pick_gib=5.0, dry_run=True)
    st = m.run_downgrades("standard", 0.0)
    assert st["downgraded"] + st["stopped_at_target"] == st["candidates_found"] == 4
    assert st["stopped_at_target"] >= 1                    # did NOT plan the whole library
    assert m.radarr_api.calls == []                        # dry_run issues no Radarr writes
    stamped = mfm.saved["planned_action"].tolist().count("downgrade")
    assert stamped == st["downgraded"]                     # only the applied slice is stamped
