"""quality_analytics/transcode_causes.py — why each viewer's plays transcode (pure).
==============================================================================
Codec routing only ever fixes the VIDEO-CODEC slice of transcoding. This decomposes each
viewer's TRANSCODED plays by primary cause so the operator can see the real lever — whether a
viewer transcodes because of the video codec (codec routing helps), the video bitrate/resolution,
the audio track (Atmos/DTS), a burned-in subtitle, or remote bandwidth (codec routing does NOT
help those). It's the diagnostic that answers "is codec routing worth it for us?".

Ground truth vs heuristic: Plex reports a per-stream decision (``video_decision`` / ``audio_decision``
/ ``subtitle_decision`` = directplay/copy/transcode). When present those attribute the cause exactly.
On older cached rows that predate the projection adding them, it falls back to comparing the SOURCE
codec (metadata index) to the STREAMED codec; the per-user ``ground_truth`` flag reports which.

PURE — no HTTP, no cache, no logging.

Public API:
  * transcode_cause_breakdown(history_entries, metadata_index=None)
        -> {user: {transcodes, directs, rate, causes: {cause: count}, ground_truth: bool}}
"""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
    _meta_video_codec,
    _norm_video,
)


def _classify_cause(entry, metadata_index) -> tuple:
    """(primary_cause, used_ground_truth) for one TRANSCODED play. Priority: subtitle burn, then the
    per-stream video/audio decision (ground truth), else a source-vs-streamed-codec heuristic."""
    sub = str(entry.get("subtitle_decision") or "").strip().lower()
    vdec = str(entry.get("video_decision") or "").strip().lower()
    adec = str(entry.get("audio_decision") or "").strip().lower()
    loc = str(entry.get("location") or "").strip().lower()
    src = _norm_video(_meta_video_codec(metadata_index or {}, entry.get("rating_key")))
    streamed = _norm_video(entry.get("stream_video_codec"))

    if sub in ("burn", "transcode"):
        return "subtitle", True
    if vdec == "transcode":
        codec_changed = src not in ("", "unknown") and src != streamed
        return ("video: codec" if codec_changed else "video: bitrate/res"), True
    if adec == "transcode":
        return "audio", True
    if vdec or adec:                                       # decisions present, neither video nor audio
        return ("remote (bandwidth)" if loc == "wan" else "container/other"), True
    # Heuristic (row predates the per-stream-decision fields).
    if src not in ("", "unknown") and streamed not in ("", "unknown") and src != streamed:
        return "video: codec", False
    if loc == "wan":
        return "remote (bandwidth)", False
    if src and src != "unknown" and src == streamed:       # video copied -> not a video-codec transcode
        return "audio/other", False
    return "other", False


def transcode_cause_breakdown(history_entries, metadata_index=None) -> dict:
    """``{user: {transcodes, directs, rate, causes, ground_truth}}`` — per viewer, their transcode
    count + direct count + transcode rate, and the breakdown of transcodes by primary cause (sorted
    most-common first). ``ground_truth`` is True when EVERY one of that viewer's transcodes was
    attributed via Plex's per-stream decisions (vs. the source-codec heuristic). Plays with no
    ``transcode_decision`` are skipped. Pure."""
    acc: dict = {}
    for entry in (history_entries or []):
        decision = str(entry.get("transcode_decision") or "").strip().lower()
        if not decision:
            continue
        user = entry.get("user") or entry.get("user_id") or "unknown"
        rec = acc.setdefault(user, {"transcodes": 0, "directs": 0, "causes": {}, "gt": 0})
        if decision != "transcode":
            rec["directs"] += 1
            continue
        rec["transcodes"] += 1
        cause, gt = _classify_cause(entry, metadata_index)
        rec["causes"][cause] = rec["causes"].get(cause, 0) + 1
        if gt:
            rec["gt"] += 1

    out: dict = {}
    for user, rec in acc.items():
        t, d = rec["transcodes"], rec["directs"]
        n = t + d
        out[user] = {
            "transcodes": t,
            "directs": d,
            "rate": round(t / n, 3) if n else 0.0,
            "causes": dict(sorted(rec["causes"].items(), key=lambda kv: -kv[1])),
            "ground_truth": (t > 0 and rec["gt"] == t),
        }
    return out
