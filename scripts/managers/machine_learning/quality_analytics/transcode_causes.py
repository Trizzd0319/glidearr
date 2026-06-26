"""quality_analytics/transcode_causes.py — why each viewer's plays transcode (pure).
==============================================================================
Codec routing only ever fixes the VIDEO-CODEC slice of transcoding. This decomposes each
viewer's TRANSCODED plays by primary cause so the operator can see the real lever — whether a
viewer transcodes because of the video codec (codec routing helps), the video bitrate/resolution,
the audio track (Atmos/DTS), a burned-in subtitle, the container, or remote bandwidth (none of
which codec routing fixes). It's the diagnostic that answers "is codec routing worth it for us?".

Ground truth: Tautulli's ``get_history`` carries only the OVERALL ``transcode_decision`` — the
per-stream decisions live in ``get_stream_data(row_id)`` (``video_decision`` / ``audio_decision`` /
``stream_subtitle_decision`` / ``stream_container_decision`` + source-vs-stream codec). The service
fetches + caches those per row_id (immutable) and passes them here as ``stream_decisions``; this stays
pure. A transcode row with no stream-decision record falls back to a coarse heuristic and is flagged
``ground_truth=False``.

PURE — no HTTP, no cache, no logging.

Public API:
  * extract_stream_decision(get_stream_data_response) -> dict   (parse one get_stream_data payload)
  * transcode_cause_breakdown(history_entries, stream_decisions=None, metadata_index=None)
        -> {user: {transcodes, directs, rate, causes: {cause: count}, ground_truth: bool}}
"""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
    _meta_video_codec,
    _norm_video,
)

_STREAM_DECISION_FIELDS = ("video_decision", "audio_decision", "subtitle_decision",
                           "container_decision", "video_codec", "stream_video_codec")


def extract_stream_decision(get_stream_data_response) -> dict:
    """Pull the per-stream transcode decisions + codecs from a Tautulli ``get_stream_data`` response
    into ``{video_decision, audio_decision, subtitle_decision, container_decision, video_codec,
    stream_video_codec}`` (the FILE's source ``video_codec`` vs the delivered ``stream_video_codec``).
    Prefers the ``stream_*`` decision fields, falling back to the bare names. Pure."""
    d = ((get_stream_data_response or {}).get("response") or {}).get("data") or {}
    if not isinstance(d, dict):
        return {}
    return {
        "video_decision":     d.get("stream_video_decision") or d.get("video_decision"),
        "audio_decision":     d.get("stream_audio_decision") or d.get("audio_decision"),
        "subtitle_decision":  d.get("stream_subtitle_decision") or d.get("subtitle_decision"),
        "container_decision": d.get("stream_container_decision"),
        "video_codec":        d.get("video_codec"),          # source
        "stream_video_codec": d.get("stream_video_codec"),   # delivered
    }


def _classify(vdec, adec, sdec, cdec, src, streamed, location, has_decisions) -> tuple:
    """(cause, used_ground_truth) for one transcoded play from its per-stream decisions (+ source vs
    stream codec). Priority: subtitle burn → video (codec vs bitrate) → audio → container → (heuristic)."""
    sub = str(sdec or "").strip().lower()
    vd = str(vdec or "").strip().lower()
    ad = str(adec or "").strip().lower()
    cd = str(cdec or "").strip().lower()
    loc = str(location or "").strip().lower()
    src_n, str_n = _norm_video(src), _norm_video(streamed)

    if sub in ("burn", "transcode"):
        return "subtitle", True
    if vd == "transcode":
        codec_changed = src_n not in ("", "unknown") and str_n not in ("", "unknown") and src_n != str_n
        return ("video: codec" if codec_changed else "video: bitrate/res"), True
    if ad == "transcode":
        return "audio", True
    if cd == "transcode":
        return "container", True
    if has_decisions:                          # decisions present, none flagged transcode (rare)
        return ("remote (bandwidth)" if loc == "wan" else "other"), True
    # Heuristic (no per-stream decisions for this row).
    if src_n not in ("", "unknown") and str_n not in ("", "unknown") and src_n != str_n:
        return "video: codec", False
    if loc == "wan":
        return "remote (bandwidth)", False
    if src_n and src_n != "unknown" and src_n == str_n:
        return "audio/other", False
    return "other", False


def transcode_cause_breakdown(history_entries, stream_decisions=None, metadata_index=None) -> dict:
    """``{user: {transcodes, directs, rate, causes, ground_truth}}`` — per viewer, their transcode +
    direct counts, transcode rate, and the breakdown of transcodes by primary cause (most-common
    first). ``stream_decisions`` is ``{row_id(str): extract_stream_decision(...)}`` from the service;
    a row's decisions are taken from there (ground truth), else from the entry itself, else a source-vs-
    stream-codec heuristic. ``ground_truth`` is True when EVERY one of that viewer's transcodes was
    attributed via per-stream decisions. Plays with no ``transcode_decision`` are skipped. Pure."""
    stream_decisions = stream_decisions or {}
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
        rid = str(entry.get("row_id") or entry.get("reference_id") or entry.get("id") or "")
        sd = stream_decisions.get(rid) or {}
        vdec = sd.get("video_decision") or entry.get("video_decision")
        adec = sd.get("audio_decision") or entry.get("audio_decision")
        sdec = sd.get("subtitle_decision") or entry.get("subtitle_decision")
        cdec = sd.get("container_decision")
        src = sd.get("video_codec") or _meta_video_codec(metadata_index or {}, entry.get("rating_key"))
        streamed = sd.get("stream_video_codec") or entry.get("stream_video_codec")
        has_decisions = any(x for x in (vdec, adec, sdec, cdec))
        cause, gt = _classify(vdec, adec, sdec, cdec, src, streamed, entry.get("location"), has_decisions)
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
