"""Tests for the pilot-search daemon (process_job + SonarrClient endpoint resolution + the
claim→process→complete loop). No network: the Sonarr client is faked.

process_job is the daemon's payload — it runs the SAME shared interactive-search core the
in-process worker uses, so these assert the daemon's wiring (config → client → ledger), not the
search logic itself (that's covered by test_pilot_interactive_worker.py).
"""
from __future__ import annotations

import threading

import scripts.support.daemons.pilot_search_daemon as psd


# ── Fakes ────────────────────────────────────────────────────────────────────────

def _rel(res, *, guid):
    return {"guid": guid, "indexerId": 1, "rejected": False, "seeders": 5,
            "size": 1000, "quality": {"quality": {"resolution": res}}}


class _FakeClient:
    """Mimics SonarrClient._make_request: interactive-search results per ep + series profile state."""
    def __init__(self, releases_by_ep):
        self._rel = releases_by_ep
        self._pid = {}
        self.searches: list = []
        self._lock = threading.Lock()

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        with self._lock:
            if endpoint.startswith("release?episodeId="):
                return list(self._rel.get(int(endpoint.split("=", 1)[1]), []))
            if endpoint == "command" and method == "POST" and (payload or {}).get("name") == "EpisodeSearch":
                self.searches += [int(e) for e in payload.get("episodeIds") or []]
                return {"id": len(self.searches)}
            if endpoint.startswith("series/") and method == "GET":
                sid = int(endpoint.split("/")[1])
                return {"id": sid, "qualityProfileId": self._pid.get(sid, 99), "title": f"S{sid}"}
            if endpoint.startswith("series/") and method == "PUT":
                self._pid[int(endpoint.split("/")[1])] = payload.get("qualityProfileId")
                return payload
            return fallback


class _Ledger:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def _job(instance="sonarr"):
    return {
        "version": 1, "mode": "interactive", "instance": instance,
        "items": [[1, 901], [2, 902]],
        "ladder": [[11, 480], [12, 720], [13, 1080], [14, 2160]],
        "meta": {"1": {"title": "Show One", "tvdb": 1001}, "2": {"title": "Show Two", "tvdb": 1002}},
        "current_indexers": [1, 2], "floor_res": 0, "recheck_days": 7,
    }


# ── process_job ────────────────────────────────────────────────────────────────────

def test_process_job_searches_and_flags(monkeypatch):
    # ep 901 has a 1080 release → searched at the 1080 tier; ep 902 empty → UNACQUIRABLE.
    fake = _FakeClient({901: [_rel(1080, guid="g1080")], 902: []})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()

    result = psd.process_job({}, _job(), ledger, dry_run=False)

    assert result["searched"] == [1]
    assert fake.searches == [901]
    assert fake._pid[1] == 13                         # lowest available tier (1080 → profile 13)
    flagged = ledger.get("sonarr/pilot/unacquirable/sonarr")
    assert "2" in flagged and flagged["2"]["title"] == "Show Two"
    assert "1" not in flagged


def test_process_job_records_reasons_and_ledger_reason(monkeypatch):
    # 901 available, 902 no results, 903 rejected for a PROFILE-DEPENDENT reason (rescuable → searched).
    rel_ok  = {"quality": {"quality": {"resolution": 1080}}, "rejected": False}
    rel_rej = {"quality": {"quality": {"resolution": 1080}}, "rejected": True,
               "rejections": ["WEBDL-1080p is not wanted in profile"]}
    fake = _FakeClient({901: [rel_ok], 902: [], 903: [rel_rej]})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()
    job = {"mode": "interactive", "instance": "sonarr",
           "items": [[1, 901], [2, 902], [3, 903]],
           "ladder": [[11, 480], [12, 720], [13, 1080]],
           "meta": {"2": {"title": "Two"}}, "current_indexers": [1], "floor_res": 0}

    psd.process_job({}, job, ledger, dry_run=False)

    # 901 + 903 get searched (903's quality rejection clears after the flip); 902 is flagged.
    assert sorted(fake.searches) == [901, 903]
    unacq = ledger.get("sonarr/pilot/unacquirable/sonarr") or {}
    assert unacq.get("2", {}).get("reason") == "no_results"      # reason recorded on the flag
    diag = ledger.get("sonarr/pilot/diagnostics/sonarr") or {}
    reasons = diag.get("reasons") or {}
    assert reasons.get("available") == 1 and reasons.get("no_results") == 1 and reasons.get("rejected") == 1
    assert any(m == "WEBDL-1080p is not wanted in profile" for m, _c in (diag.get("top_rejections") or []))


def test_no_resolution_searches_at_floor(monkeypatch):
    # releases exist but report no resolution → search at the FLOOR tier (not flagged).
    norel = {"quality": {"quality": {"resolution": 0}}, "rejected": False}
    fake = _FakeClient({901: [norel, norel]})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()
    job = {"mode": "interactive", "instance": "sonarr", "items": [[1, 901]],
           "ladder": [[11, 480], [12, 1080]], "meta": {}, "current_indexers": [1],
           "floor_res": 0, "search_no_resolution": True}
    psd.process_job({}, job, ledger, dry_run=False)
    assert fake.searches == [901] and fake._pid[1] == 11        # searched at floor profile 11
    assert "1" not in (ledger.get("sonarr/pilot/unacquirable/sonarr") or {})


def test_no_resolution_flagged_when_disabled(monkeypatch):
    norel = {"quality": {"quality": {"resolution": 0}}, "rejected": False}
    fake = _FakeClient({901: [norel]})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()
    job = {"mode": "interactive", "instance": "sonarr", "items": [[1, 901]],
           "ladder": [[11, 480]], "meta": {}, "current_indexers": [1],
           "floor_res": 0, "search_no_resolution": False}
    psd.process_job({}, job, ledger, dry_run=False)
    assert fake.searches == []
    assert (ledger.get("sonarr/pilot/unacquirable/sonarr") or {}).get("1", {}).get("reason") == "no_resolution"


def test_hard_reject_skipped_and_flagged(monkeypatch):
    # every release rejected for a profile-independent reason (size) → skip search + flag rejected_hard.
    hard = {"quality": {"quality": {"resolution": 1080}}, "rejected": True,
            "rejections": ["1.8 GB is larger than maximum allowed 910 MB"]}
    fake = _FakeClient({901: [hard]})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()
    job = {"mode": "interactive", "instance": "sonarr", "items": [[1, 901]],
           "ladder": [[11, 480], [12, 720], [13, 1080]], "meta": {}, "current_indexers": [1],
           "floor_res": 0, "skip_hard_rejects": True}
    psd.process_job({}, job, ledger, dry_run=False)
    assert fake.searches == []                                  # NOT searched (futile)
    entry = (ledger.get("sonarr/pilot/unacquirable/sonarr") or {}).get("1", {})
    assert entry.get("reason") == "rejected_hard" and entry.get("rejections")


def test_hard_reject_searched_when_disabled(monkeypatch):
    hard = {"quality": {"quality": {"resolution": 1080}}, "rejected": True,
            "rejections": ["larger than maximum allowed"]}
    fake = _FakeClient({901: [hard]})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()
    job = {"mode": "interactive", "instance": "sonarr", "items": [[1, 901]],
           "ladder": [[11, 480], [12, 720], [13, 1080]], "meta": {}, "current_indexers": [1],
           "floor_res": 0, "skip_hard_rejects": False}
    psd.process_job({}, job, ledger, dry_run=False)
    assert fake.searches == [901]                               # searched (skip disabled)


def test_transient_search_failure_is_deferred_not_flagged(monkeypatch):
    # A release search that returns None (timeout / rate-limit / error) must NOT flag UNACQUIRABLE —
    # it's deferred (re-probes next run) and recorded as 'search_failed', not 'no_results'.
    class _TimeoutClient:
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            if endpoint.startswith("release?episodeId="):
                return None                            # simulate a failed/timed-out interactive search
            return fallback
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: _TimeoutClient())
    ledger = _Ledger()
    job = {"mode": "interactive", "instance": "sonarr", "items": [[1, 901]],
           "ladder": [[11, 480], [12, 1080]], "meta": {}, "current_indexers": [1], "floor_res": 0}

    psd.process_job({}, job, ledger, dry_run=False)

    assert "1" not in (ledger.get("sonarr/pilot/unacquirable/sonarr") or {})   # NOT blacklisted
    diag = ledger.get("sonarr/pilot/diagnostics/sonarr") or {}
    assert (diag.get("reasons") or {}).get("search_failed") == 1               # recorded as transient


def test_progress_heartbeat_written(monkeypatch):
    from scripts.managers.services.sonarr.cache.pilot_interactive import progress_key
    fake = _FakeClient({901: [_rel(720, guid="g")], 902: []})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()
    job = {"mode": "interactive", "instance": "sonarr", "items": [[1, 901], [2, 902]],
           "ladder": [[11, 480], [12, 720]], "meta": {}, "current_indexers": [1], "floor_res": 0}
    psd.process_job({}, job, ledger, dry_run=False)
    prog = ledger.get(progress_key("sonarr")) or {}
    assert prog.get("total") == 2 and prog.get("done") == 2          # final heartbeat = all done
    assert isinstance(prog.get("reasons"), dict) and prog.get("updated_at")


def test_anime_sid_routes_onto_the_anime_ladder(monkeypatch):
    # sid 1 (anime) → anime-ladder profile; sid 2 (non-anime) → regular-ladder profile, same release.
    fake = _FakeClient({901: [_rel(1080, guid="g1")], 902: [_rel(1080, guid="g2")]})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    job = {"mode": "interactive", "instance": "sonarr", "items": [[1, 901], [2, 902]],
           "ladder": [[11, 480], [12, 1080]],            # regular: 1080 → profile 12
           "anime_ladder": [[111, 480], [112, 1080]],    # anime:   1080 → profile 112
           "anime_sids": [1], "meta": {}, "current_indexers": [1], "floor_res": 0}
    psd.process_job({}, job, _Ledger(), dry_run=False)
    assert fake._pid[1] == 112      # anime sid flipped onto the [Anime] profile
    assert fake._pid[2] == 12       # non-anime sid flipped onto the regular profile
    assert sorted(fake.searches) == [901, 902]


def test_resume_skips_checkpointed_stubs(monkeypatch):
    from scripts.managers.services.sonarr.cache.pilot_interactive import checkpoint_key
    fake = _FakeClient({901: [_rel(720, guid="g1")], 902: [_rel(720, guid="g2")]})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()
    ledger.set(checkpoint_key("sonarr"), {"job_id": 123, "done": [1]})   # sid 1 already done, same job
    job = {"mode": "interactive", "instance": "sonarr", "enqueued_at": 123,
           "items": [[1, 901], [2, 902]], "ladder": [[11, 480], [12, 720]],
           "meta": {}, "current_indexers": [1], "floor_res": 0}
    psd.process_job({}, job, ledger, dry_run=False)
    assert fake.searches == [902]                                  # sid 1 skipped on resume
    assert ledger.get(checkpoint_key("sonarr")) == {}             # checkpoint cleared on completion


def test_resume_ignores_checkpoint_for_a_different_job(monkeypatch):
    from scripts.managers.services.sonarr.cache.pilot_interactive import checkpoint_key
    fake = _FakeClient({901: [_rel(720, guid="g1")], 902: [_rel(720, guid="g2")]})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()
    ledger.set(checkpoint_key("sonarr"), {"job_id": 999, "done": [1]})   # a DIFFERENT job's checkpoint
    job = {"mode": "interactive", "instance": "sonarr", "enqueued_at": 123,
           "items": [[1, 901], [2, 902]], "ladder": [[11, 480], [12, 720]],
           "meta": {}, "current_indexers": [1], "floor_res": 0}
    psd.process_job({}, job, ledger, dry_run=False)
    assert sorted(fake.searches) == [901, 902]                     # stale checkpoint ignored — both searched


def test_process_job_dry_run_writes_nothing(monkeypatch):
    fake = _FakeClient({901: [_rel(1080, guid="g")], 902: []})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    ledger = _Ledger()

    result = psd.process_job({}, _job(), ledger, dry_run=True)

    assert result == {"searched": [], "flagged": {}}
    assert fake.searches == []                         # no search fired
    assert ledger.d == {}                              # ledger untouched


def test_process_job_drops_malformed(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: called.__setitem__("n", called["n"] + 1))
    bad = {"mode": "interactive", "instance": "sonarr", "items": [], "ladder": []}
    result = psd.process_job({}, bad, _Ledger(), dry_run=False)
    assert result == {"searched": [], "flagged": {}}
    assert called["n"] == 0                            # never built a client for an empty batch


def test_process_job_rejects_unsupported_mode(monkeypatch):
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: (_ for _ in ()).throw(AssertionError("built")))
    job = _job()
    job["mode"] = "climb"
    result = psd.process_job({}, job, _Ledger(), dry_run=False)
    assert result == {"searched": [], "flagged": {}}


class _BatchClient:
    """Records each EpisodeSearch command's episodeId LIST so we can assert chunking."""
    def __init__(self, eps):
        self._eps = eps                         # ep_ids that have a release
        self.commands: list = []                # one entry per EpisodeSearch command = its episodeIds
        self._lock = threading.Lock()

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        with self._lock:
            if endpoint.startswith("release?episodeId="):
                ep = int(endpoint.split("=", 1)[1])
                return [_rel(720, guid=f"g{ep}")] if ep in self._eps else []
            if endpoint == "command" and method == "POST" and (payload or {}).get("name") == "EpisodeSearch":
                self.commands.append(list(payload.get("episodeIds") or []))
                return {"id": len(self.commands)}
            if endpoint.startswith("series/") and method == "GET":
                return {"id": int(endpoint.split("/")[1]), "qualityProfileId": 99, "title": "S"}
            if endpoint.startswith("series/") and method == "PUT":
                return payload
            return fallback


def test_process_job_batches_episode_searches(monkeypatch):
    # 5 stubs, batch size 2 → 3 EpisodeSearch commands ([2,2,1]), NOT 5 individual ones.
    eps = {901, 902, 903, 904, 905}
    fake = _BatchClient(eps)
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    job = {
        "mode": "interactive", "instance": "sonarr",
        "items": [[i, 900 + i] for i in range(1, 6)],
        "ladder": [[11, 480], [12, 720], [13, 1080]], "meta": {},
        "current_indexers": [1], "floor_res": 0,
    }
    cfg = {"daemons": {"pilot_search": {"search_batch": 2}}}

    result = psd.process_job(cfg, job, _Ledger(), dry_run=False)

    assert len(fake.commands) == 3                       # 3 commands, not 5
    assert sorted(len(c) for c in fake.commands) == [1, 2, 2]
    assert sorted(e for c in fake.commands for e in c) == [901, 902, 903, 904, 905]
    assert sorted(result["searched"]) == [1, 2, 3, 4, 5]  # all marked searched


def test_process_job_failed_batch_not_marked_searched(monkeypatch):
    # A batch whose EpisodeSearch POST fails leaves those stubs UN-searched (re-probe next run).
    class _FailCmd(_BatchClient):
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            if endpoint == "command" and method == "POST":
                return None                                # POST fails
            return super()._make_request(instance, endpoint, method, payload, fallback)
    fake = _FailCmd({901, 902})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)
    job = {"mode": "interactive", "instance": "sonarr", "items": [[1, 901], [2, 902]],
           "ladder": [[11, 480], [12, 720]], "meta": {}, "current_indexers": [1], "floor_res": 0}
    result = psd.process_job({}, job, _Ledger(), dry_run=False)
    assert result["searched"] == []                      # nothing marked searched on a failed POST


# ── JIT step-down job mode ──────────────────────────────────────────────────────────

class _JitClient:
    """Per-series profile state; commands instantly 'completed'; queue/details returns nothing."""
    def __init__(self, original=5):
        self._orig = original
        self._pid = {}
        self._cmd = 0
        self._lock = threading.Lock()

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        with self._lock:
            if endpoint.startswith("series/") and method == "GET":
                sid = int(endpoint.split("/")[1])
                return {"id": sid, "qualityProfileId": self._pid.get(sid, self._orig), "title": f"S{sid}"}
            if endpoint.startswith("series/") and method == "PUT":
                sid = int(endpoint.split("/")[1])
                self._pid[sid] = payload.get("qualityProfileId")
                return payload
            if endpoint == "command" and method == "POST":
                self._cmd += 1
                return {"id": self._cmd}
            if endpoint.startswith("command/"):
                return {"status": "completed"}
            if endpoint.startswith("queue/details"):
                return []                                  # nothing grabbed → full step-down
            return fallback

    def pid(self, sid):
        with self._lock:
            return self._pid.get(sid, self._orig)


def test_process_jit_job_steps_down_and_reverts(monkeypatch):
    from scripts.managers.services.sonarr.cache.jit_search import (
        failed_upgrades_key, inflight_qp_key,
    )
    api = _JitClient(original=5)
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: api)
    monkeypatch.setattr(psd, "episodes_in_queue", lambda *a, **k: set())   # skip the real retry sleeps
    ledger = _Ledger()
    # items: [[sid, [[tier_res, [[ep_id, season, episode]], [step_pids]]]]]
    job = {"mode": "jit", "instance": "sonarr",
           "items": [[1, [[1080, [[200, 1, 2]], [12, 11]]]]]}

    result = psd.process_job({}, job, ledger, dry_run=False)

    assert api.pid(1) == 5                                      # reverted to the pre-flip profile
    assert len(result.get("failed", [])) == 1                  # ep 200 never grabbed
    assert ledger.get(failed_upgrades_key("sonarr"))           # persisted for retry
    assert ledger.get(inflight_qp_key("sonarr")) == {}         # inflight entry cleared after revert


def test_process_jit_job_dry_run_writes_nothing(monkeypatch):
    api = _JitClient(original=5)
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: api)
    ledger = _Ledger()
    job = {"mode": "jit", "instance": "sonarr", "items": [[1, [[1080, [[200, 1, 2]], [12, 11]]]]]}
    psd.process_job({}, job, ledger, dry_run=True)
    assert api.pid(1) == 5 and ledger.d == {}                  # untouched


def test_process_job_dispatches_jit_mode(monkeypatch):
    # process_job must route mode=jit to the jit handler (not the interactive path).
    called = {"jit": 0}
    monkeypatch.setattr(psd, "_process_jit_job", lambda cfg, job, ledger, dry_run: called.__setitem__("jit", 1))
    psd.process_job({}, {"mode": "jit", "instance": "sonarr", "items": []}, _Ledger(), dry_run=False)
    assert called["jit"] == 1


# ── SonarrClient endpoint resolution (no network) ───────────────────────────────────

def test_client_resolves_base_url_and_api():
    cfg = {"sonarr_instances": {"main": {"base_url": "http://host:8989/", "api": "KEY"}}}
    c = psd.SonarrClient(cfg)
    assert c._endpoint("main") == ("http://host:8989", "KEY")


def test_client_builds_scheme_and_port_from_url_form():
    cfg = {"sonarr_instances": {"main": {"url": "192.168.1.5", "port": 8989, "ssl": False, "api": "K"}}}
    c = psd.SonarrClient(cfg)
    base, api = c._endpoint("main")
    assert base == "http://192.168.1.5:8989" and api == "K"


def test_client_unconfigured_instance_returns_fallback():
    c = psd.SonarrClient({"sonarr_instances": {}})
    assert c._make_request("missing", "series", fallback=["fb"]) == ["fb"]


# ── --status report (read-only) ─────────────────────────────────────────────────────

def test_status_reports_queue_and_processing(monkeypatch, tmp_path, capsys):
    from scripts.managers.factories.daemons import pilot_jobs
    q, p = tmp_path / "queue", tmp_path / "processing"
    for mod, name in ((pilot_jobs, "PILOT_QUEUE_DIR"), (psd, "PILOT_QUEUE_DIR")):
        monkeypatch.setattr(mod, name, q)
    for mod, name in ((pilot_jobs, "PILOT_PROCESSING_DIR"), (psd, "PILOT_PROCESSING_DIR")):
        monkeypatch.setattr(mod, name, p)
    monkeypatch.setattr(psd, "PILOT_PID_PATH", tmp_path / "no.pid")
    monkeypatch.setattr(psd, "PILOT_STOP_SENTINEL", tmp_path / "no.stop")
    monkeypatch.setattr(psd, "PILOT_LOG_PATH", tmp_path / "pilot.log")

    pilot_jobs.enqueue("alpha", _job("alpha"))     # 2 stubs, queued
    pilot_jobs.claim_next()                         # → moves to processing/ (in-flight)
    pilot_jobs.enqueue("beta", _job("beta"))        # 2 stubs, queued

    psd.print_status()
    out = capsys.readouterr().out

    assert "daemon       not running" in out        # no live pid file
    assert "alpha" in out and "beta" in out
    assert "queued (1 job(s))" in out               # beta still queued
    assert "in-flight (processing)" in out          # alpha claimed
    assert "TOTAL pending stubs: 4" in out          # 2 queued + 2 in-flight


def test_status_empty_queue(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(psd, "PILOT_QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(psd, "PILOT_PROCESSING_DIR", tmp_path / "processing")
    monkeypatch.setattr(psd, "PILOT_PID_PATH", tmp_path / "no.pid")
    monkeypatch.setattr(psd, "PILOT_STOP_SENTINEL", tmp_path / "no.stop")
    monkeypatch.setattr(psd, "PILOT_LOG_PATH", tmp_path / "pilot.log")
    psd.print_status()
    out = capsys.readouterr().out
    assert "TOTAL pending stubs: 0" in out
    assert out.count("(none)") == 2                 # both queue + processing empty


# ── claim → process → complete loop (queue redirected to tmp) ───────────────────────

def test_claim_process_complete_drains_queue(monkeypatch, tmp_path):
    from scripts.managers.factories.daemons import pilot_jobs
    monkeypatch.setattr(pilot_jobs, "PILOT_QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(pilot_jobs, "PILOT_PROCESSING_DIR", tmp_path / "processing")

    fake = _FakeClient({901: [_rel(720, guid="g")], 902: []})
    monkeypatch.setattr(psd, "SonarrClient", lambda cfg: fake)

    pilot_jobs.enqueue("sonarr", _job())
    claimed = pilot_jobs.claim_next()
    assert claimed is not None
    proc_path, job = claimed
    psd.process_job({}, job, _Ledger(), dry_run=False)
    pilot_jobs.complete(proc_path)

    assert fake.searches == [901]
    assert not proc_path.exists()
    assert pilot_jobs.claim_next() is None
