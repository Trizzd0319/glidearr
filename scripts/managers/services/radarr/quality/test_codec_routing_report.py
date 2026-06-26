"""RadarrSpacePressureManager.report_codec_routing — read-only codec-routing preview that records
the would-change decisions in the end-of-run summary (changes nothing) and always logs what it
evaluated for transparency."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


class _RS:
    def __init__(self): self.calls = []
    def add_rows(self, service, concern, instance, headers, rows, order=None):
        self.calls.append((service, concern, instance, headers, rows, order))


class _GC:
    def __init__(self, rs, history, metadata=None):
        self.run_summary = rs; self._h = history; self._m = metadata or {}
    def get(self, k):
        if k == "tautulli/history/all": return self._h
        if k == "tautulli/metadata/index": return self._m
        return None


class _Api:
    def __init__(self, profs): self._p = profs
    def _make_request(self, instance, ep, method="GET", payload=None, fallback=None):
        return self._p if ep == "qualityprofile" else fallback


class _Log:
    def __init__(self): self.info = []; self.grids = []
    def log_info(self, msg="", *a, **k): self.info.append(str(msg))
    def log_grid(self, headers, rows, title="", cap=None): self.grids.append((title, headers, rows))
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


def _mgr(history, profs, config=None, metadata=None):
    m = object.__new__(RadarrSpacePressureManager)
    m.config = config or {}
    m._rs = _RS()
    m.global_cache = _GC(m._rs, history, metadata)
    m.radarr_api = _Api(profs)
    m.logger = _Log()
    m._resolve_instance = lambda i: i or "standard"
    return m


def _src_play(title, rk, user, platform, source_codec, decision, n):
    # The streamed codec for a TRANSCODE is Plex's target (h264), not the source; for a direct it IS
    # the source. The source-keyed matrix must recover the source codec from the metadata index.
    streamed = "h264" if decision == "transcode" else source_codec
    return [{"title": title, "rating_key": rk, "user": user, "platform": platform,
             "stream_video_codec": streamed, "transcode_decision": decision,
             "stream_video_full_resolution": "1080", "stream_audio_codec": "eac3",
             "subtitle_decision": "none", "location": "lan", "date": 0} for _ in range(n)]


def test_source_codec_join_recommends_a_directplay_codec():
    # A transcodes HEVC-source files but direct-plays h264-source files. 'Foo' is an HEVC file A
    # watched -> the SOURCE-keyed matrix (via the metadata index) recommends h264 over its HEVC, where
    # the codec-blind streamed matrix could not (this is the Phase-2 fix end to end).
    history = (_src_play("Foo", "10", "A", "TV", "hevc", "transcode", 5) +
               _src_play("Bar", "11", "A", "TV", "h264", "direct play", 5))
    metadata = {"10": {"video_codec": "hevc"}, "11": {"video_codec": "h264"}}
    df = pd.DataFrame([{"title": "Foo", "video_codec": "hevc", "resolution": 1080}])
    m = _mgr(history, _PROFS, metadata=metadata)
    rows = m.report_codec_routing("standard", df)
    assert len(rows) == 1
    r = rows[0]
    assert r["current_codec"] == "hevc" and r["recommended_codec"] == "h264"
    assert r["current_cost"] == 1.0 and r["recommended_cost"] == 0.0     # hevc transcodes, h264 direct
    assert r["change"] is True
    assert _has(m.logger.info, "1 would change")


def _has(log, *needles):
    return any(all(n in s for n in needles) for s in log)


def test_preview_flags_transcoding_title_records_summary_and_logs():
    # The owned file is AV1 (which A transcodes); the policy recommends HEVC (which A direct-plays).
    df = pd.DataFrame([{"title": "The Bear", "video_codec": "av1", "resolution": 1080}])
    m = _mgr(_HISTORY, _PROFS)
    rows = m.report_codec_routing("standard", df)
    assert len(rows) == 1
    r = rows[0]
    assert r["current_codec"] == "av1" and r["recommended_codec"] == "hevc" and r["change"] is True
    # recorded in the end-of-run summary
    svc, concern, inst, headers, table, order = m._rs.calls[0]
    assert (svc, concern, inst, order) == ("radarr", "Codec routing preview", "standard", 37)
    assert table[0][-1] == "YES"
    # table is ALSO logged directly (visible in the run log, not only the run-summary report)
    assert m.logger.grids and m.logger.grids[0][1][0] == "Title"
    # transparency line always logged
    assert _has(m.logger.info, "[CodecRoute]", "evaluated 1 watched", "1 would change")


def test_preview_logs_when_nothing_changes_and_still_shows_table():
    # Already on the direct-play codec (HEVC) -> a row with change=False; the table still shows and the
    # transparency line reports 0 changes.
    df = pd.DataFrame([{"title": "The Bear", "video_codec": "hevc", "resolution": 1080}])
    m = _mgr(_HISTORY, _PROFS)
    rows = m.report_codec_routing("standard", df)
    assert len(rows) == 1 and rows[0]["change"] is False
    assert m._rs.calls and m._rs.calls[0][4][0][-1] == "-"          # table shown, Change column "-"
    assert _has(m.logger.info, "evaluated 1 watched", "0 would change")


def test_transcode_causes_breakdown_logs_once():
    history = (
        [{"user": "A", "transcode_decision": "transcode", "video_decision": "transcode",
          "stream_video_codec": "h264", "rating_key": "1", "location": "lan"}] * 3 +     # video: codec x3
        [{"user": "A", "transcode_decision": "direct play"}] * 2 +
        [{"user": "B", "transcode_decision": "transcode", "audio_decision": "transcode",
          "stream_video_codec": "h264", "location": "lan"}]                              # audio
    )
    m = _mgr(history, _PROFS, metadata={"1": {"video_codec": "hevc"}})
    bd = m.report_transcode_causes()
    assert bd["A"]["causes"] == {"video: codec": 3} and bd["A"]["transcodes"] == 3
    assert bd["B"]["causes"] == {"audio": 1}
    assert m.logger.grids and m.logger.grids[0][1][0] == "Viewer"                        # grid logged
    assert _has(m.logger.info, "transcode causes household-wide", "video-codec")
    assert m.report_transcode_causes() == {}                                            # once per run


def test_preview_off_by_flag():
    df = pd.DataFrame([{"title": "The Bear", "video_codec": "av1", "resolution": 1080}])
    m = _mgr(_HISTORY, _PROFS, config={"scoring": {"codec_profiles": {"report": False}}})
    assert m.report_codec_routing("standard", df) == []
    assert m._rs.calls == [] and m.logger.info == []


def test_preview_logs_transparency_without_history():
    df = pd.DataFrame([{"title": "The Bear", "video_codec": "av1", "resolution": 1080}])
    m = _mgr([], _PROFS)
    assert m.report_codec_routing("standard", df) == []
    assert m._rs.calls == []
    assert _has(m.logger.info, "[CodecRoute]", "no Tautulli watch history")
