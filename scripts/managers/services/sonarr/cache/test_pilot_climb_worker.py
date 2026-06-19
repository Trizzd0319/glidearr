"""Tests for the within-run floor-first pilot climb worker (_pilot_climb_worker).

The worker is the MIRROR of the JIT step-down worker: per stub pilot it flips the series profile UP
an ascending floor→widest ladder, searches S01E01 at each tier, and STOPS at the first tier that
yields a grab — leaving the series at that low tier (NOT reverting). When no tier grabs it reverts
to the pre-climb profile. The load-bearing guarantees: it grabs at the LOWEST available resolution,
never searches above the grab tier, leaves the series at the grab tier, and reverts on a dry climb.

Driven entirely through self.sonarr_api._make_request + self._episodes_in_queue (object.__new__
pattern), so no network. The fake reports an episode "in queue" only while the series profile sits
at that series' designated grab tier — so the climb's stop point is exactly testable.
"""
from __future__ import annotations

import threading

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


class _CapLogger:
    def __init__(self): self.infos: list[str] = []
    def log_info(self, msg, *a, **k): self.infos.append(str(msg))
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


# Ascending floor→widest ladder: 720p (pid 11) → 1080p (pid 12) → 2160p (pid 13).
LADDER = [(11, 720), (12, 1080), (13, 2160)]


class _FakeApi:
    """Per-series profile state + thread-safe call log; reflects PUTs so later GETs see the new
    profile; reports EpisodeSearch commands as instantly completed (no real sleep)."""
    def __init__(self, originals):                 # originals: {sid: pid}
        self._pid = dict(originals)
        self._lock = threading.Lock()
        self.calls: list = []
        self._cmd = 0

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        with self._lock:
            self.calls.append((endpoint, method, payload))
            if endpoint.startswith("series/") and method == "GET":
                sid = int(endpoint.split("/")[1])
                return {"id": sid, "qualityProfileId": self._pid.get(sid),
                        "title": f"Show {sid}", "tvdbId": 1000 + sid}
            if endpoint.startswith("series/") and method == "PUT":
                sid = int(endpoint.split("/")[1])
                self._pid[sid] = payload.get("qualityProfileId")
                return payload
            if endpoint == "command" and method == "POST":
                self._cmd += 1
                return {"id": self._cmd}
            if endpoint.startswith("command/") and method == "GET":
                return {"status": "completed"}
            return fallback

    def final_pid(self, sid):
        with self._lock:
            return self._pid[sid]


def _mk(api, items, grab_at):
    """grab_at: {sid: pid_that_grabs}; a sid absent → never grabs (forces a full climb + revert)."""
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    ep_to_sid = {int(ep): int(sid) for sid, ep in items}

    def _in_queue(instance, ep_ids, attempts=3, delay_s=2.0):
        out = set()
        for e in ep_ids:
            sid = ep_to_sid[int(e)]
            tgt = grab_at.get(sid)
            if tgt is not None and api.final_pid(sid) == tgt:   # grabs only while AT its grab tier
                out.add(int(e))
        return out
    m._episodes_in_queue = _in_queue
    return m


def _searches(calls, start_pid):
    """(profile_at_search, {episodeIds}) for each EpisodeSearch, tracking the profile set by the most
    recent PUT. Valid only for SINGLE-series runs (multi-series PUTs interleave on the shared log)."""
    cur = start_pid
    out = []
    for endpoint, method, payload in calls:
        if endpoint.startswith("series/") and method == "PUT":
            cur = payload.get("qualityProfileId")
        elif endpoint == "command" and method == "POST" and (payload or {}).get("name") == "EpisodeSearch":
            out.append((cur, {int(e) for e in payload.get("episodeIds") or []}))
    return out


# ── the lowest-available guarantee ────────────────────────────────────────────────
def test_climb_stops_at_lowest_available_and_leaves_profile():
    # Release exists only at 1080p (pid 12): climb floor(11) → 1080(12), grab, STOP — never 2160.
    items = [(1, 901)]
    api = _FakeApi({1: 99})
    _mk(api, items, grab_at={1: 12})._pilot_climb_worker("inst", items, LADDER)

    searches = _searches(api.calls, start_pid=99)
    assert [pid for pid, _ in searches] == [11, 12]      # climbed floor→1080 and stopped
    assert all(eps == {901} for _, eps in searches)      # only S01E01 ever searched
    assert api.final_pid(1) == 12                         # LEFT at the grab tier (NOT reverted)


def test_climb_grabs_at_floor_immediately():
    # Release at the floor (720, pid 11): exactly one search, left at the floor.
    items = [(1, 901)]
    api = _FakeApi({1: 99})
    _mk(api, items, grab_at={1: 11})._pilot_climb_worker("inst", items, LADDER)

    searches = _searches(api.calls, 99)
    assert [pid for pid, _ in searches] == [11]
    assert api.final_pid(1) == 11


def test_climb_never_searches_above_grab_tier():
    # The four-eyes check for over-grab: the profile is NEVER flipped above the tier that grabs.
    items = [(1, 901)]
    api = _FakeApi({1: 99})
    _mk(api, items, grab_at={1: 11})._pilot_climb_worker("inst", items, LADDER)
    searches = _searches(api.calls, 99)
    assert all(pid <= 11 for pid, _ in searches)


def test_climb_exhausts_all_tiers_then_reverts():
    # Nothing grabs at any tier → climb the full ladder, then revert to the pre-climb profile.
    items = [(1, 901)]
    api = _FakeApi({1: 99})
    _mk(api, items, grab_at={})._pilot_climb_worker("inst", items, LADDER)

    searches = _searches(api.calls, 99)
    assert [pid for pid, _ in searches] == [11, 12, 13]   # tried every tier, floor → widest
    assert api.final_pid(1) == 99                          # reverted to original (never abandoned high)


# ── concurrent multi-pilot path ───────────────────────────────────────────────────
def test_multiple_pilots_climb_concurrently_each_left_independently():
    # Three pilots, three different originals + grab tiers. Each must end at its OWN grab tier (or
    # revert) despite the climbs overlapping — no cross-contamination from parallel profile flips.
    items = [(1, 901), (2, 902), (3, 903)]
    api = _FakeApi({1: 99, 2: 98, 3: 97})
    grab_at = {1: 12, 2: 11}                  # series 3 absent → never grabs → reverts
    _mk(api, items, grab_at)._pilot_climb_worker("inst", items, LADDER)

    assert api.final_pid(1) == 12             # left at its lowest-available tier (1080)
    assert api.final_pid(2) == 11             # grabbed at the floor, left there
    assert api.final_pid(3) == 97             # no release anywhere → reverted to original


# ── readable label (no raw `series <id>` leak) ────────────────────────────────────
def test_climb_logs_use_readable_series_label():
    items = [(17208, 901)]
    api = _FakeApi({17208: 99})
    m = _mk(api, items, grab_at={})           # dry climb → step + ∅ + revert lines all log the label
    m.logger = cap = _CapLogger()
    m._pilot_climb_worker("inst", items, LADDER)

    blob = "\n".join(cap.infos)
    assert "sonarr/inst 'Show 17208' (tvdb-18208)" in blob
    assert "series 17208" not in blob          # raw id no longer leaks into climb/∅ lines


def test_single_pilot_grab_leaves_no_revert_call():
    # A successful grab must NOT issue a revert PUT — the series stays at the grab tier.
    items = [(1, 901)]
    api = _FakeApi({1: 99})
    _mk(api, items, grab_at={1: 12})._pilot_climb_worker("inst", items, LADDER)
    puts = [p.get("qualityProfileId") for ep, m, p in api.calls if ep == "series/1" and m == "PUT"]
    assert puts[-1] == 12                       # last write is the grab tier, not a revert to 99
    assert 99 not in puts                        # original never re-applied on a successful grab


# ── failed profile flip must abort (never search at the un-set, possibly higher, tier) ──
class _FailPutApi(_FakeApi):
    """A series PUT silently fails (returns the fallback, profile unchanged) — the real failure
    mode of a write that doesn't land. The climb must NOT then search at the un-flipped profile."""
    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        if endpoint.startswith("series/") and method == "PUT":
            with self._lock:
                self.calls.append((endpoint, method, payload))   # record the attempt
            return None                                          # ...but the write did NOT land
        return super()._make_request(instance, endpoint, method, payload, fallback)


def test_failed_profile_flip_aborts_climb_no_search_at_wrong_tier():
    # The floor flip PUT fails → _set_profile returns False → the climb aborts BEFORE any
    # EpisodeSearch, so it never searches at the series' original (high) profile and grabs high.
    items = [(1, 901)]
    api = _FailPutApi({1: 99})
    _mk(api, items, grab_at={1: 11})._pilot_climb_worker("inst", items, LADDER)
    commands = [c for c in api.calls if c[0] == "command" and c[1] == "POST"]
    assert commands == []                       # no EpisodeSearch fired at the un-set profile
    assert api.final_pid(1) == 99               # profile genuinely never changed
