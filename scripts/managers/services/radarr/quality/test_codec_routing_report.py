"""RadarrSpacePressureManager.report_codec_routing — read-only codec-routing preview that records
the would-change decisions in the end-of-run summary (changes nothing)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


class _RS:
    def __init__(self): self.calls = []
    def add_rows(self, service, concern, instance, headers, rows, order=None):
        self.calls.append((service, concern, instance, headers, rows, order))


class _GC:
    def __init__(self, rs, history): self.run_summary = rs; self._h = history
    def get(self, k): return self._h if k == "tautulli/history/all" else None


class _Api:
    def __init__(self, profs): self._p = profs
    def _make_request(self, instance, ep, method="GET", payload=None, fallback=None):
        return self._p if ep == "qualityprofile" else fallback


class _Log:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


def _prof(pid, name, res):
    return {"id": pid, "name": name,
            "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}


_PROFS = [_prof(16, "WEB-1080p (HEVC)", 1080), _prof(15, "WEB-1080p (H264)", 1080),
          _prof(17, "WEB-1080p (AV1)", 1080)]


def _plays(title, user, platform, codec, decision, n):
    return [{"title": title, "user": user, "platform": platform, "stream_video_codec": codec,
             "transcode_decision": decision, "stream_video_full_resolution": "1080",
             "stream_audio_codec": "ac3", "subtitle_decision": "none", "location": "lan", "date": 0}
            for _ in range(n)]


# User A direct-plays HEVC and transcodes AV1 (both watched on a PS5).
_HISTORY = _plays("The Bear", "A", "PS5", "hevc", "direct play", 10) + \
           _plays("The Bear", "A", "PS5", "av1", "transcode", 10)


def _mgr(history, profs, config=None):
    m = object.__new__(RadarrSpacePressureManager)
    m.config = config or {}
    m._rs = _RS()
    m.global_cache = _GC(m._rs, history)
    m.radarr_api = _Api(profs)
    m.logger = _Log()
    m._resolve_instance = lambda i: i or "standard"
    return m


def test_preview_flags_transcoding_title_and_records_summary():
    # The owned file is AV1 (which A transcodes); the policy recommends HEVC (which A direct-plays).
    df = pd.DataFrame([{"title": "The Bear", "video_codec": "av1", "resolution": 1080}])
    m = _mgr(_HISTORY, _PROFS)
    rows = m.report_codec_routing("standard", df)
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "The Bear" and r["current_codec"] == "av1"
    assert r["recommended_codec"] == "hevc" and r["change"] is True
    # recorded in the end-of-run summary
    assert m._rs.calls, "should record a run-summary table"
    svc, concern, inst, headers, table, order = m._rs.calls[0]
    assert (svc, concern, inst, order) == ("radarr", "Codec routing preview", "standard", 37)
    assert headers[0] == "Title" and "Change" in headers
    assert table[0][-1] == "YES"


def test_preview_off_by_flag():
    df = pd.DataFrame([{"title": "The Bear", "video_codec": "av1", "resolution": 1080}])
    m = _mgr(_HISTORY, _PROFS, config={"scoring": {"codec_profiles": {"report": False}}})
    assert m.report_codec_routing("standard", df) == []
    assert m._rs.calls == []


def test_preview_noop_without_history():
    df = pd.DataFrame([{"title": "The Bear", "video_codec": "av1", "resolution": 1080}])
    m = _mgr([], _PROFS)
    assert m.report_codec_routing("standard", df) == []
    assert m._rs.calls == []
