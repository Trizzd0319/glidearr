"""Tests for the JIT background step-down worker (_spawn_jit_search_worker / _jit_search_worker)
— deliverable B: per-episode mixed-target tiers searched group-by-tier with NO over-grab.

The worker is driven entirely through self.sonarr_api._make_request + self._episodes_in_queue, so
it is fully stubbable via the object.__new__ pattern (same as test_episode_files_guards.py). The
load-bearing assertion: for every EpisodeSearch, the series quality profile flipped just before it
is at or BELOW every searched episode's target tier — a lower-target episode is never searched
while the profile sits at a higher tier (the over-grab the group-by-tier split prevents).
"""
from __future__ import annotations

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


# profile id → tier (max resolution). pid 5 = the series' pre-flip "original" profile.
_PID_TIER = {13: 2160, 12: 1080, 11: 720, 5: 1080}


class _FakeApi:
    """Records every (endpoint, method, payload); reflects series-profile PUTs so later GETs see
    the new profile; reports EpisodeSearch commands as instantly completed (no real sleep)."""
    def __init__(self, original_pid=5, fail_on_search_payload=None):
        self.calls = []
        self._pid = original_pid
        self._cmd = 0
        self._fail_on = fail_on_search_payload   # set of episodeIds → raise when searched

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        self.calls.append((endpoint, method, payload))
        if endpoint.startswith("series/") and method == "GET":
            return {"id": int(endpoint.split("/")[1]), "qualityProfileId": self._pid}
        if endpoint.startswith("series/") and method == "PUT":
            self._pid = payload.get("qualityProfileId")
            return payload
        if endpoint == "command" and method == "POST":
            if self._fail_on is not None and set(payload.get("episodeIds") or []) == self._fail_on:
                raise RuntimeError("injected EpisodeSearch failure")
            self._cmd += 1
            return {"id": self._cmd}
        if endpoint.startswith("command/") and method == "GET":
            return {"status": "completed"}
        return fallback


def _mgr(api, *, grab=True):
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.global_cache = None
    # Deterministic grab signal: every searched episode is "in queue" (grabbed at the first rung)
    # when grab=True, else nothing grabs (forces full step-down).
    m._episodes_in_queue = (
        (lambda instance, ep_ids, attempts=3, delay_s=2.0: {int(e) for e in ep_ids})
        if grab else
        (lambda instance, ep_ids, attempts=3, delay_s=2.0: set())
    )
    return m


def _episode_searches(calls):
    """Walk recorded calls, tracking the series profile set by the most recent PUT; yield
    (current_pid, set(episodeIds)) for each EpisodeSearch POST."""
    cur_pid = 5  # the original, before any flip
    out = []
    for endpoint, method, payload in calls:
        if endpoint.startswith("series/") and method == "PUT":
            cur_pid = payload.get("qualityProfileId")
        elif endpoint == "command" and method == "POST" and (payload or {}).get("name") == "EpisodeSearch":
            out.append((cur_pid, {int(e) for e in payload.get("episodeIds") or []}))
    return out


# ── the over-grab guarantee ───────────────────────────────────────────────────────
def test_mixed_targets_never_over_grab():
    # User's example: one series, 1 ep @2160 + 4 eps @1080. Built as TWO tier groups.
    target_tier = {100: 2160, 200: 1080, 201: 1080, 202: 1080, 203: 1080}
    items = [(
        1,
        [
            (2160, [(100, 1, 1)],                                    [13, 12, 11]),
            (1080, [(200, 1, 2), (201, 1, 3), (202, 1, 4), (203, 1, 5)], [12, 11]),
        ],
    )]
    api = _FakeApi(original_pid=5)
    _mgr(api)._jit_search_worker("inst", items)

    searches = _episode_searches(api.calls)
    assert searches, "expected at least one EpisodeSearch"
    for cur_pid, ep_ids in searches:
        qp_tier = _PID_TIER[cur_pid]
        for eid in ep_ids:
            # The flipped profile must never exceed the episode's target tier (no over-grab).
            assert qp_tier <= target_tier[eid], (
                f"OVER-GRAB: ep {eid} (target {target_tier[eid]}p) searched while QP at {qp_tier}p"
            )
    # The four 1080-target eps must NEVER appear in a search while the QP is flipped to 2160.
    for cur_pid, ep_ids in searches:
        if _PID_TIER[cur_pid] == 2160:
            assert ep_ids == {100}


def test_revert_restores_true_original_after_multiple_groups():
    items = [(
        1,
        [(2160, [(100, 1, 1)], [13]), (1080, [(200, 1, 2)], [12])],
    )]
    api = _FakeApi(original_pid=5)
    _mgr(api)._jit_search_worker("inst", items)
    # The LAST series PUT must restore the pre-flip profile (5), not an intermediate group's tier.
    puts = [p for ep, m, p in api.calls if ep == "series/1" and m == "PUT"]
    assert puts, "expected profile PUTs"
    assert puts[-1]["qualityProfileId"] == 5


def test_exception_mid_group_still_reverts_to_original():
    # Inject a failure when the 1080 group's EpisodeSearch fires; the series must still revert to 5.
    items = [(
        1,
        [(2160, [(100, 1, 1)], [13]), (1080, [(200, 1, 2)], [12])],
    )]
    api = _FakeApi(original_pid=5, fail_on_search_payload={200})
    _mgr(api)._jit_search_worker("inst", items)
    puts = [p for ep, m, p in api.calls if ep == "series/1" and m == "PUT"]
    assert puts[-1]["qualityProfileId"] == 5


# ── single-target parity (flag-OFF behavior, the migration oracle) ────────────────
def test_single_target_series_one_group_search_sequence():
    # A one-tier series (today's only case) → one group → exactly one PUT-up, one EpisodeSearch
    # over all eps, one revert. This is the byte-identical-by-construction baseline.
    items = [(1, [(1080, [(200, 1, 2), (201, 1, 3)], [12, 11])])]
    api = _FakeApi(original_pid=5)
    _mgr(api)._jit_search_worker("inst", items)

    searches = _episode_searches(api.calls)
    assert len(searches) == 1
    cur_pid, ep_ids = searches[0]
    assert cur_pid == 12 and ep_ids == {200, 201}     # single flip to the tier, all eps together
    puts = [p for ep, m, p in api.calls if ep == "series/1" and m == "PUT"]
    assert puts[0]["qualityProfileId"] == 12          # flip up
    assert puts[-1]["qualityProfileId"] == 5          # revert down to original


def test_step_down_within_group_searches_only_that_groups_eps():
    # Nothing grabs at the top rung → the group steps DOWN its own ladder, re-searching ONLY that
    # group's eps at each lower pid. Never touches another group's episodes.
    items = [(1, [(2160, [(100, 1, 1)], [13, 12, 11])])]
    api = _FakeApi(original_pid=5)
    _mgr(api, grab=False)._jit_search_worker("inst", items)
    searches = _episode_searches(api.calls)
    # one EpisodeSearch per ladder rung, each over exactly {100}
    assert len(searches) == 3
    assert {pid for pid, _ in searches} == {13, 12, 11}
    for _pid, ep_ids in searches:
        assert ep_ids == {100}
