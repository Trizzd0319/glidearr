"""TautulliTranscodeManager.get_stream_decisions_cached — fetches per-row stream decisions for
TRANSCODED history rows via get_stream_data, incrementally, and caches them per row_id."""
from __future__ import annotations

from scripts.managers.services.tautulli.transcode import (
    STREAM_DECISIONS_KEY,
    TautulliTranscodeManager,
)


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


class _Api:
    def __init__(self): self.calls = []
    def get_stream_data(self, row_id):
        self.calls.append(row_id)
        return {"response": {"data": {"stream_video_decision": "transcode",
                                      "video_codec": "hevc", "stream_video_codec": "h264"}}}


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


def _mgr(cache, api):
    m = object.__new__(TautulliTranscodeManager)
    m.global_cache, m.tautulli_api, m.logger = cache, api, _Log()
    return m


def test_fetches_only_uncached_transcode_rows_and_persists():
    cache = _Cache({STREAM_DECISIONS_KEY: {"1": {"video_decision": "copy"}}})   # row 1 already cached
    api = _Api()
    hist = [
        {"transcode_decision": "transcode", "row_id": "1"},     # cached -> skip
        {"transcode_decision": "transcode", "row_id": "2"},     # fetch
        {"transcode_decision": "direct play", "row_id": "3"},   # not a transcode -> skip
    ]
    out = _mgr(cache, api).get_stream_decisions_cached(hist)
    assert api.calls == ["2"]                                   # only the uncached transcode row
    assert out["2"]["video_decision"] == "transcode" and out["2"]["stream_video_codec"] == "h264"
    assert "1" in out                                           # preserved
    assert cache.d[STREAM_DECISIONS_KEY]["2"]["video_codec"] == "hevc"   # persisted


def test_noop_without_api():
    m = _mgr(_Cache(), None)
    assert m.get_stream_decisions_cached([{"transcode_decision": "transcode", "row_id": "9"}]) == {}
