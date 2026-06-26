"""SonarrCacheEpisodeFilesManager.report_codec_routing — read-only TV codec-routing preview, the
twin of the Radarr movie preview. It aggregates per-episode-file rows to one (series, resolution)
row at the dominant codec, recommends a direct-play codec for the actual viewers, records the
would-change decisions in the end-of-run summary, and always logs what it evaluated."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


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
    """Sonarr profiles endpoint is camelCase ``qualityProfile`` (Radarr's is lowercase)."""
    def __init__(self, profs): self._p = profs
    def _make_request(self, instance, ep, method="GET", payload=None, fallback=None):
        return self._p if ep == "qualityProfile" else fallback


class _Log:
    def __init__(self): self.info = []; self.grids = []
    def log_info(self, msg="", *a, **k): self.info.append(str(msg))
    def log_grid(self, headers, rows, title="", cap=None): self.grids.append((title, headers, rows))
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


def _prof(pid, name, res):
    return {"id": pid, "name": name,
            "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}


# Three codec-variant profiles at the 1080 tier (the only tier with >=2 codec variants).
_PROFS = [_prof(16, "WEB-1080p (HEVC)", 1080), _prof(15, "WEB-1080p (H264)", 1080),
          _prof(17, "WEB-1080p (AV1)", 1080)]


def _src_play(series_title, rk, user, source_codec, decision, n):
    # Episode history rows: Tautulli keys the SERIES title under 'grandparent_title'. The streamed
    # codec on a TRANSCODE is Plex's target (h264); the source-keyed matrix recovers it from metadata.
    streamed = "h264" if decision == "transcode" else source_codec
    return [{"grandparent_title": series_title, "rating_key": rk, "user": user, "platform": "TV",
             "stream_video_codec": streamed, "transcode_decision": decision,
             "stream_video_full_resolution": "1080", "stream_audio_codec": "eac3",
             "subtitle_decision": "none", "location": "lan", "date": 0} for _ in range(n)]


def _mgr(history, profs, config=None, metadata=None):
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = config or {}
    m._rs = _RS()
    m.global_cache = _GC(m._rs, history, metadata)
    m.sonarr_api = _Api(profs)
    m.logger = _Log()
    m._resolve_instance = lambda i: i or "standard"
    return m


def _edf(rows):
    """rows: list of (series_title, codec, resolution) -> one episode-FILE row each."""
    return pd.DataFrame([{"series_title": t, "video_codec": c, "resolution": r} for t, c, r in rows])


def _has(log, *needles):
    return any(all(n in s for n in needles) for s in log)


def test_tv_preview_aggregates_episode_files_and_flags_change():
    # Viewer A transcodes HEVC-source files but direct-plays h264. 'Foo' is owned as HEVC across FOUR
    # episode files -> aggregated to ONE series row -> recommends h264 (the direct-play codec).
    history = (_src_play("Foo", "10", "A", "hevc", "transcode", 5) +
               _src_play("Bar", "11", "A", "h264", "direct play", 5))
    metadata = {"10": {"video_codec": "hevc"}, "11": {"video_codec": "h264"}}
    df = _edf([("Foo", "hevc", 1080)] * 4)
    m = _mgr(history, _PROFS, metadata=metadata)
    rows = m.report_codec_routing("standard", df)
    assert len(rows) == 1                           # 4 episode rows aggregated to ONE series row
    r = rows[0]
    assert r["current_codec"] == "hevc" and r["recommended_codec"] == "h264" and r["change"] is True
    svc, concern, inst, headers, table, order = m._rs.calls[0]
    assert (svc, concern, inst, order) == ("sonarr", "Codec routing preview", "standard", 37)
    assert headers[0] == "Series" and table[0][-1] == "YES"
    assert m.logger.grids and "TV" in m.logger.grids[0][0]
    assert _has(m.logger.info, "[CodecRoute]", "evaluated 1 watched series", "1 would change")


def test_tv_preview_picks_the_dominant_codec_per_tier():
    # 'Foo' owned as 3x hevc + 1x av1 at 1080 -> the dominant (hevc) is what the row evaluates.
    history = _src_play("Foo", "10", "A", "hevc", "transcode", 5)
    metadata = {"10": {"video_codec": "hevc"}}
    df = _edf([("Foo", "hevc", 1080), ("Foo", "hevc", 1080), ("Foo", "hevc", 1080), ("Foo", "av1", 1080)])
    m = _mgr(history, _PROFS, metadata=metadata)
    rows = m.report_codec_routing("standard", df)
    assert len(rows) == 1 and rows[0]["current_codec"] == "hevc"


def test_tv_preview_no_row_when_tier_has_no_codec_variants():
    # A 480p series has no >=2 codec-variant profile at that tier -> nothing to choose -> no row (but
    # the transparency line still fires). This is why the XviD@480 lever is surfaced by the re-grab
    # tool, not this profile-driven preview.
    history = _src_play("OldShow", "20", "A", "mpeg4", "transcode", 5)
    metadata = {"20": {"video_codec": "mpeg4"}}
    df = _edf([("OldShow", "XviD", 480)] * 3)
    m = _mgr(history, _PROFS, metadata=metadata)
    rows = m.report_codec_routing("standard", df)
    assert rows == []
    assert _has(m.logger.info, "[CodecRoute]", "0 would change")


def test_tv_preview_logs_when_nothing_changes():
    # Already on the direct-play codec (h264) -> a row with change=False; the table still shows.
    history = _src_play("Baz", "12", "A", "h264", "direct play", 6)
    metadata = {"12": {"video_codec": "h264"}}
    df = _edf([("Baz", "h264", 1080)] * 3)
    m = _mgr(history, _PROFS, metadata=metadata)
    rows = m.report_codec_routing("standard", df)
    assert len(rows) == 1 and rows[0]["change"] is False
    assert m._rs.calls and m._rs.calls[0][4][0][-1] == "-"
    assert _has(m.logger.info, "evaluated 1 watched series", "0 would change")


def test_tv_preview_off_by_flag():
    df = _edf([("Foo", "hevc", 1080)] * 3)
    m = _mgr(_src_play("Foo", "10", "A", "hevc", "transcode", 5), _PROFS,
             config={"scoring": {"codec_profiles": {"report": False}}})
    assert m.report_codec_routing("standard", df) == []
    assert m._rs.calls == [] and m.logger.info == []


def test_tv_preview_transparency_without_history():
    df = _edf([("Foo", "hevc", 1080)] * 3)
    m = _mgr([], _PROFS)
    assert m.report_codec_routing("standard", df) == []
    assert m._rs.calls == []
    assert _has(m.logger.info, "[CodecRoute]", "no Tautulli watch history")
