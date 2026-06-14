"""
quality_analytics/transcode.py — transcode-usage signal (pure).
================================================================================
Relocated from ``services/tautulli/transcode.get_transcode_stats`` (ML Step 3b-2).
Counts the stream codec pairs (video/audio) that have ACTUALLY caused a transcode
in the pre-fetched history. Scoring Group-D rewards a codec the household has
never had to transcode (direct play), so this map is the "what transcodes here"
evidence. PURE — no HTTP, no global_cache, no logging; the transcode manager keeps
the raw history FETCH + its summary log and delegates here.

Public API:
  * transcode_stats(history_entries) -> {"<video>/<audio>": count}
  * device_codec_matrix(history_entries) -> {device: {"<video>/<audio>": {direct, transcode}}}
        per-DEVICE codec play vs transcode counts — the evidence for "which room can
        direct-play which codec", the basis for per-device profile selection.
  * codec_direct_play_rate(matrix, device, video_codec, audio_codec=None) -> float | None
        direct/(direct+transcode) for a codec on a device (None when never tried).

(Distinct from ``transcode_analyzer.py``, which is the decision/analysis core; these
are usage derivations.)
"""
from __future__ import annotations


def transcode_stats(history_entries: list) -> dict:
    """Tally ``"<stream_video_codec>/<stream_audio_codec>": count`` over history
    entries whose ``transcode_decision == "transcode"``. Missing codecs default to
    ``"unknown"``."""
    format_map: dict = {}
    for entry in history_entries:
        if entry.get("transcode_decision") != "transcode":
            continue
        video = entry.get("stream_video_codec", "unknown")
        audio = entry.get("stream_audio_codec", "unknown")
        key = f"{video}/{audio}"
        format_map[key] = format_map.get(key, 0) + 1
    return format_map


def device_codec_matrix(history_entries: list) -> dict:
    """Per-device codec play/transcode counts from history::

        {device: {"<video>/<audio>": {"direct": n, "transcode": m}}}

    ``device`` is the entry's ``platform``. A ``transcode_decision == "transcode"``
    counts as a transcode; any OTHER non-empty decision (direct play / direct stream
    / copy) counts as a direct play. Entries with no decision are skipped (no signal).
    Missing platform/codecs default to ``"unknown"``. This disambiguates the
    event-only ``transcode_stats`` (which can't tell "0 transcodes = safe" from
    "= never tried") with an explicit direct-play count per device."""
    out: dict = {}
    for entry in history_entries:
        decision = (entry.get("transcode_decision") or "").strip().lower()
        if not decision:
            continue
        device = entry.get("platform") or "unknown"
        video = entry.get("stream_video_codec") or "unknown"
        audio = entry.get("stream_audio_codec") or "unknown"
        bucket = out.setdefault(device, {}).setdefault(
            f"{video}/{audio}", {"direct": 0, "transcode": 0}
        )
        bucket["transcode" if decision == "transcode" else "direct"] += 1
    return out


def codec_direct_play_rate(matrix: dict, device, video_codec, audio_codec=None):
    """Direct-play rate (0..1) for a codec on a device: ``direct / (direct +
    transcode)``, aggregated over audio codecs when ``audio_codec`` is omitted.
    Returns ``None`` when the device has never streamed that codec (no sample) so a
    caller can distinguish "always direct-played" from "never tried"."""
    dev = matrix.get(device)
    if not dev:
        return None
    direct = transcode = 0
    for key, counts in dev.items():
        v, _, a = key.partition("/")
        if v != video_codec:
            continue
        if audio_codec is not None and a != audio_codec:
            continue
        direct += counts.get("direct", 0)
        transcode += counts.get("transcode", 0)
    total = direct + transcode
    return (direct / total) if total else None
