"""
quality_analytics/transcode_fingerprint.py — per-device transcode capability matrix (pure).
================================================================================
Extends the codec-pair ``device_codec_matrix`` (transcode.py) into a richer per-PLAYER
fingerprint so the playlist tier-selector can predict whether a given 4K file will
TRANSCODE on the device a user is likely to use (→ serve the HD-library copy) or
direct-play (→ keep 4K). Parallel to ``device_codec_matrix`` (left untouched); a strict
superset of it.

A fingerprint decomposes the transcode-relevant stream properties into INDEPENDENT axes
so each plausible CAUSE of a transcode lands in its own cell — a subtitle-burn transcode
no longer poisons the "is this codec safe?" read, and a WAN-bandwidth transcode no longer
poisons the LAN read:

    fingerprint = (video_codec, audio_codec, subtitle, res_hdr_tier, location)
    key         = (device, fingerprint)              # device = Tautulli ``platform``
    value       = {"direct": int, "transcode": int, "last_seen": int, "n": int}

History fields consumed (Tautulli watch history): ``platform``, ``transcode_decision``,
``stream_video_codec``, ``stream_audio_codec``, ``subtitle_decision``,
``stream_video_full_resolution``, ``location``, ``date``. A field that isn't cached yet
normalises to a coarse bucket ("unknown"/"none"/"sd"), so the matrix self-degrades to the
codec-only signal until the richer fields are added to the history projection.

PURE — no HTTP, no cache, no logging.
"""
from __future__ import annotations

# ── axis normalisers ──────────────────────────────────────────────────────────
# Coarse tiers so cells accumulate evidence across DIFFERENT titles (a raw codec/res
# string would explode the keyspace and never reach a trusted sample size).

_VIDEO_ALIASES = {"h265": "hevc", "x265": "hevc", "hevc": "hevc",
                  "h264": "h264", "x264": "h264", "avc": "h264", "avc1": "h264"}


def _norm_video(codec) -> str:
    c = str(codec or "").strip().lower() or "unknown"
    return _VIDEO_ALIASES.get(c, c)


def _norm_audio(codec) -> str:
    return str(codec or "").strip().lower() or "unknown"


def _norm_subtitle(decision) -> str:
    """Subtitle handling tier: ``none`` (no subs), ``burn`` (transcoded/burned-in — the
    expensive case), ``copy`` (passed through). Reads the playback ``subtitle_decision``."""
    d = str(decision or "").strip().lower()
    if not d or d in ("none", "0", "false"):
        return "none"
    if d in ("transcode", "burn"):
        return "burn"
    return "copy"


def _norm_res_hdr(full_resolution) -> str:
    """Bucket source resolution + HDR from Tautulli ``stream_video_full_resolution``
    (e.g. '4k', '1080', '720'); HDR/DoVi folds into the tier. Falls back to 'unknown'."""
    s = str(full_resolution or "").strip().lower()
    hdr = ("hdr" in s) or ("dovi" in s) or ("dolby" in s)
    if "4k" in s or "2160" in s or "uhd" in s:
        return "2160p_hdr" if hdr else "2160p_sdr"
    if "1080" in s:
        return "1080p_hdr" if hdr else "1080p_sdr"
    if "720" in s:
        return "720p"
    if any(t in s for t in ("480", "576", "sd")):
        return "sd"
    return s or "unknown"


def _norm_location(loc) -> str:
    location = str(loc or "").strip().lower()
    return location if location in ("lan", "wan") else "unknown"


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── fingerprints ──────────────────────────────────────────────────────────────

def row_fingerprint(entry) -> tuple:
    """The (video, audio, subtitle, res_hdr, location) fingerprint of a history row."""
    return (
        _norm_video(entry.get("stream_video_codec")),
        _norm_audio(entry.get("stream_audio_codec")),
        _norm_subtitle(entry.get("subtitle_decision")),
        _norm_res_hdr(entry.get("stream_video_full_resolution")),
        _norm_location(entry.get("location")),
    )


def _res_hdr_from_height(height, hdr) -> str:
    """Source res/HDR tier for a candidate file from Radarr mediainfo (height + hdr flag)."""
    h = _int(height, 0)
    is_hdr = bool(hdr) and str(hdr).strip().lower() not in ("", "0", "false", "none", "sdr")
    if h >= 2160:
        return "2160p_hdr" if is_hdr else "2160p_sdr"
    if h >= 1080:
        return "1080p_hdr" if is_hdr else "1080p_sdr"
    if h >= 720:
        return "720p"
    if h > 0:
        return "sd"
    return "unknown"


def source_fingerprint(*, video_codec=None, audio_codec=None, subtitles=None,
                       height=None, hdr=None, location="unknown") -> tuple:
    """Build the lookup fingerprint for a CANDIDATE owned file from Radarr mediainfo
    (``movie_files._extract_media_info`` columns). ``subtitles`` is the file's subtitle
    track string ('' → none, else 'copy' — an owned sub track is passed through; a burn
    only happens at playback, learned per-history, not here). ``location`` is the VIEWER's
    typical location, not the file's. The subtitle/location mismatch coarsens away in the
    predictor's graded fallback, so an imperfect match still resolves."""
    return (
        _norm_video(video_codec),
        _norm_audio(audio_codec),
        "copy" if (subtitles and str(subtitles).strip()) else "none",
        _res_hdr_from_height(height, hdr),
        _norm_location(location),
    )


# ── matrix builders ───────────────────────────────────────────────────────────

def transcode_fingerprint_matrix(history_entries: list) -> dict:
    """Build ``{(device, fingerprint): {direct, transcode, last_seen, n}}`` from history.
    Outcome rule mirrors ``device_codec_matrix``: ``transcode_decision == "transcode"`` →
    transcode; any OTHER non-empty decision (direct play / direct stream / copy) → direct;
    an empty decision is skipped (no signal). ``last_seen`` is the max ``date`` in the cell
    (Unix); ``n`` == direct + transcode (denormalised so the explore floor reads one field)."""
    out: dict = {}
    for entry in history_entries:
        decision = str(entry.get("transcode_decision") or "").strip().lower()
        if not decision:
            continue
        key = (entry.get("platform") or "unknown", row_fingerprint(entry))
        bucket = out.setdefault(key, {"direct": 0, "transcode": 0, "last_seen": 0, "n": 0})
        bucket["transcode" if decision == "transcode" else "direct"] += 1
        bucket["n"] += 1
        bucket["last_seen"] = max(bucket["last_seen"], _int(entry.get("date")))
    return out


def per_user_transcode_fingerprint_matrix(history_entries: list) -> dict:
    """``{user: {(device, fingerprint): bucket}}`` — the per-user variant the playlist
    tier-selector consumes. Groups history by ``user`` (falls back to ``user_id``)."""
    by_user: dict = {}
    for entry in history_entries:
        u = entry.get("user") or entry.get("user_id") or "unknown"
        by_user.setdefault(u, []).append(entry)
    return {u: transcode_fingerprint_matrix(rows) for u, rows in by_user.items()}


# ── JSON round-trip (the matrix is tuple-keyed; the cache is JSON) ─────────────
# The cache layer's make_json_safe stringifies dict keys with str(k) — IRREVERSIBLY for
# a (device, fingerprint) tuple key (it becomes a Python-repr string with no inverse). So
# the matrix can't be cached as-is and read back; it must be flattened to a record list
# that round-trips EXACTLY. serialize/deserialize are inverses (verified by unit test) and
# are the single source of truth for the on-disk shape.

def serialize_fingerprint_matrix(matrix) -> list:
    """Flatten ``{(device, fingerprint): bucket}`` to a JSON-safe list of records
    ``[{device, fingerprint: [v,a,sub,res,loc], direct, transcode, last_seen, n}, ...]``."""
    out = []
    for (device, fp), bucket in (matrix or {}).items():
        out.append({
            "device": device,
            "fingerprint": list(fp),
            "direct": _int(bucket.get("direct")),
            "transcode": _int(bucket.get("transcode")),
            "last_seen": _int(bucket.get("last_seen")),
            "n": _int(bucket.get("n")),
        })
    return out


def deserialize_fingerprint_matrix(records) -> dict:
    """Inverse of :func:`serialize_fingerprint_matrix`: rebuild the tuple-keyed matrix from
    the cached record list. Tolerant — a malformed record (missing device / non-list
    fingerprint) is skipped, never raised, so a partially-corrupt cache degrades gracefully."""
    out: dict = {}
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        device = rec.get("device")
        fp = rec.get("fingerprint")
        if device is None or not isinstance(fp, (list, tuple)):
            continue
        out[(device, tuple(fp))] = {
            "direct": _int(rec.get("direct")),
            "transcode": _int(rec.get("transcode")),
            "last_seen": _int(rec.get("last_seen")),
            "n": _int(rec.get("n")),
        }
    return out


# ── predictor (graded fallback) ───────────────────────────────────────────────

def _aggregate(matrix, predicate) -> tuple:
    """Sum ``(transcode, n)`` over matrix cells whose ``(device, fingerprint)`` key matches."""
    transcode = total = 0
    for (dev, fp), bucket in matrix.items():
        if predicate(dev, fp):
            transcode += bucket.get("transcode", 0)
            total += bucket.get("n", 0)
    return transcode, total


def _device_levels(device, fp):
    """Ordered ``(label, predicate)`` graded-fallback levels for one device + target
    fingerprint ``(video, audio, subtitle, res_hdr, location)``. Each step drops a less
    causally-important axis so evidence aggregates until a trusted sample is reached."""
    v, a, sub, res, loc = fp
    return [
        ("exact",       lambda d, f: d == device and f == fp),
        ("drop_sub",    lambda d, f: d == device and (f[0], f[1], f[3], f[4]) == (v, a, res, loc)),
        ("drop_audio",  lambda d, f: d == device and (f[0], f[3], f[4]) == (v, res, loc)),
        ("codec_res",   lambda d, f: d == device and (f[0], f[3]) == (v, res)),
        ("codec_only",  lambda d, f: d == device and f[0] == v),
        ("device_only", lambda d, f: d == device),
    ]


def predict_transcode(matrix, fingerprint, platform_weights, *, min_n: int = 3):
    """``P(transcode)`` in [0,1] for a candidate ``fingerprint`` across a user's likely
    devices, or ``None`` when no fallback level for any weighted device reaches ``min_n``
    samples (→ the caller may EXPLORE). ``platform_weights`` = ``{platform: share}`` (shares
    need not sum to 1; they are renormalised over devices that produce a read).

    For each device, walk the graded fallback (exact → drop subtitle → drop audio →
    codec+res → codec → device-any) and take the FIRST level with ``n >= min_n``; that
    level's ``transcode/n`` is the device's P. The result is the share-weighted mean over
    devices that produced a read; if none did, fall back to the HOUSEHOLD (all-device)
    codec+res then codec aggregate. Returns ``(p | None, level_label)``."""
    weights = {str(k): float(v) for k, v in (platform_weights or {}).items() if v}
    fp = tuple(fingerprint)
    contribs = []          # (weight, p)
    coarsest = None
    for device, w in weights.items():
        for label, pred in _device_levels(device, fp):
            transcode, n = _aggregate(matrix, pred)
            if n >= min_n:
                contribs.append((w, transcode / n))
                coarsest = label
                break
    if contribs:
        wsum = sum(w for w, _ in contribs) or 1.0
        return (sum(w * p for w, p in contribs) / wsum, coarsest or "device")
    # Household fallback — any device, coarsening on the fingerprint dims.
    v, _a, _sub, res, _loc = fp
    for label, pred in (
        ("hh_codec_res", lambda d, f: (f[0], f[3]) == (v, res)),
        ("hh_codec",     lambda d, f: f[0] == v),
    ):
        transcode, n = _aggregate(matrix, pred)
        if n >= min_n:
            return (transcode / n, label)
    return (None, "no_data")


def choose_tier(matrix, fingerprint, platform_weights, *, min_n: int = 3,
                explore_cap: int = 2, transcode_thresh: float = 0.34) -> tuple:
    """Pick the delivery tier (``"hd"`` | ``"4k"``) for a dual-version title, with a reason.

    EXPLOIT when there is a trusted read: ``P(transcode) >= transcode_thresh`` → HD (the
    device can't direct-play this 4K profile), else → 4K. With no trusted read, EXPLORE —
    serve 4K and learn from the next run's history — but only until the exact cell has been
    observed ``explore_cap`` times; past that, exploit on the thin evidence. A wrong 4K send
    is the harmful direction, so exploration is bounded per (device, fingerprint) cell and
    amortised across every title that shares the fingerprint."""
    fp = tuple(fingerprint)
    p, level = predict_transcode(matrix, fp, platform_weights, min_n=min_n)
    if p is not None:
        if p >= transcode_thresh:
            return ("hd", f"exploit: P(transcode)={p:.2f} [{level}]")
        return ("4k", f"exploit: direct-play likely P={p:.2f} [{level}]")
    # No trusted read → look at the exact-cell thin evidence across the user's devices.
    weights = {str(k): float(v) for k, v in (platform_weights or {}).items() if v}
    transcode = n = 0
    for device in weights:
        bucket = matrix.get((device, fp))
        if bucket:
            transcode += bucket.get("transcode", 0)
            n += bucket.get("n", 0)
    if n < explore_cap:
        return ("4k", f"explore: serve 4K to learn (n={n})")
    ratio = transcode / n if n else 0.0
    if ratio >= transcode_thresh:
        return ("hd", f"exploit(thin): {transcode}/{n} transcoded")
    return ("4k", f"exploit(thin): {transcode}/{n} direct")
