"""quality_analytics/legacy_codec.py — pure helpers for the legacy-codec re-grab lever.
=====================================================================================
Old DVD-era video codecs (XviD / DivX / MPEG-2 / MPEG-4 ASP / WMV / VC-1) cannot be
direct-played by most modern Plex clients, so EVERY play of them transcodes — regardless
of resolution or bandwidth. When an x264/x265 release of the same episode exists at the
same (or better) resolution, swapping to it removes the transcode at zero quality loss.

PURE: codec classification + 'which release would we swap to' + extracting the owned legacy
files from an episode-files dataframe. The Sonarr I/O (interactive search, grab) lives in the
service adapter (SonarrCacheEpisodeFilesManager.regrab_legacy_codecs) and the standalone tool
(support/tools/regrab_legacy_codecs.py); both import from here so the rule is defined once.
"""
from __future__ import annotations

import re

# Legacy video codecs modern Plex clients almost always transcode (MPEG-4 ASP / DivX / XviD,
# MPEG-2, WMV/VC-1). Sonarr mediaInfo.videoCodec values seen on real libraries: "XviD", "MPEG2".
LEGACY_CODECS = {"xvid", "divx", "div3", "dx50", "mp42", "mp43", "mpeg4", "msmpeg4", "msmpeg4v3",
                 "mpeg2", "mpeg2video", "mpeg1", "mpeg1video", "wmv", "wmv1", "wmv2", "wmv3",
                 "vc1", "vc-1", "wvc1"}
# Release-title tokens: MODERN = a release we'd swap TO; LEGACY = never swap to (it would re-transcode).
MODERN_RE = re.compile(r"(?i)\b([xh][\s._-]?26[45]|avc|hevc|av1|vp9)\b")
LEGACY_RE = re.compile(r"(?i)\b(xvid|divx|dx50|div3|wmv|vc-?1|mpeg-?[12]|msmpeg4)\b")


def normalize_codec(v) -> str:
    return str(v or "").strip().lower().replace(" ", "").replace(".", "")


def is_legacy_codec(codec) -> bool:
    c = normalize_codec(codec)
    return c in LEGACY_CODECS or c.startswith("msmpeg4")


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def release_resolution(rel) -> int:
    return _safe_int(((rel.get("quality") or {}).get("quality") or {}).get("resolution")) or 0


def best_modern_release(releases, cur_res):
    """The release to swap to: APPROVED, a modern codec in its title (and NOT a legacy one), at
    resolution >= the current file's. Prefer the SMALLEST qualifying resolution (a codec-only swap,
    not a resolution upgrade), then the highest custom-format score. Returns the release dict, or
    None when nothing qualifies (so a legacy file with no modern replacement is left untouched)."""
    cands = []
    for r in (releases or []):
        title = r.get("title") or ""
        if r.get("rejected"):
            continue
        if LEGACY_RE.search(title) or not MODERN_RE.search(title):
            continue
        res = release_resolution(r)
        if cur_res and res and res < cur_res:
            continue
        cands.append((res, -int(r.get("customFormatScore") or 0), r))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


def legacy_files_from_df(df) -> list:
    """Owned legacy-codec episode files from an episode_files dataframe, watched-first. Returns a list
    of ``{series_id, episode_file_id, series_title, season_number, episode_number, video_codec,
    resolution, watch_count}`` dicts. Empty when the required columns are absent. Pure."""
    need = {"series_id", "episode_file_id", "video_codec"}
    if df is None or not need <= set(getattr(df, "columns", [])):
        return []
    out = []
    for _idx, r in df.iterrows():
        if not is_legacy_codec(r.get("video_codec")):
            continue
        sid, fid = _safe_int(r.get("series_id")), _safe_int(r.get("episode_file_id"))
        if sid is None or fid is None:
            continue
        out.append({
            "series_id": sid, "episode_file_id": fid,
            "series_title": str(r.get("series_title") or "?"),
            "season_number": _safe_int(r.get("season_number")),
            "episode_number": _safe_int(r.get("episode_number")),
            "video_codec": str(r.get("video_codec") or "?"),
            "resolution": _safe_int(r.get("resolution")) or 0,
            "watch_count": _safe_int(r.get("watch_count")) or 0,
        })
    # Watched first — those are the files actually causing transcodes, so a small per-run budget
    # fixes the highest-impact ones before the long tail.
    out.sort(key=lambda d: -d["watch_count"])
    return out
