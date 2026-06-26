"""quality_analytics/profile_selector.py — codec-aware profile selection (pure).
==============================================================================
The decision brain for PER-VIEWER, CODEC-AWARE TRANSCODE REDUCTION: given the
resolution tier a title earned, the users likely to watch it, and the per-user
device→transcode capability matrix, pick the codec VARIANT of the quality profile
that minimises Plex transcoding across those viewers.

Objective (per title): choose the candidate profile whose fingerprint MINIMISES the
watch-share-weighted count of likely viewers who would transcode it, tie-broken by the
more space-efficient codec. For a single-viewer title this is "the codec that viewer
direct-plays"; for a shared title it is coverage-max / accept-minority (the low-share
viewers' transcode is a tolerated residual) — both fall straight out of the argmin.

PURE — no HTTP, no cache writes, no logging, no service imports. Consumes the
already-built per-user fingerprint matrix (transcode_fingerprint.py) + a candidate
profile list (the service FETCHes it) and emits a profile id + a reason dict that a
service adapter then APPLIES (PUT qualityProfileId).

Public API:
  * classify_profile_axes(profile, cf_id_to_name=None) -> dict
        {codec, res_tier, name, id} — what a quality profile actually targets. Codec is
        read from the profile NAME suffix ((H264)/(HEVC)/(AV1)/x264/x265) and reconciled
        with the custom-format scores (a codec banned at -10000 is excluded; the
        highest-scored non-banned codec wins when the name is silent). res_tier is the
        profile's max ALLOWED resolution (size_model.profile_max_quality).
  * candidate_fingerprint(axes, *, location="unknown") -> tuple
        the (video, audio, subtitle, res_hdr, location) fingerprint of a candidate profile,
        delegating to transcode_fingerprint.source_fingerprint so normalisation matches the
        live Stage-C path exactly.
  * viewer_transcode_cost(profile_fp, likely_viewers, per_user_matrix,
                          per_user_platform_weights, *, none_p=0.5, min_n=3) -> float
        Sum over likely viewers of watch_share x P(transcode), where P comes from
        predict_transcode against that viewer's matrix; an untrusted (None) read uses the
        neutral ``none_p`` prior so a cold viewer neither vetoes nor forces a codec.
  * choose_codec_profile(resolution_tier, likely_viewers, per_user_fingerprint_matrix,
                         candidate_profiles, *, per_user_platform_weights, size_hint=None,
                         min_n=3, none_p=0.5, min_coverage=0.0) -> (profile_id|None, reason)
        argmin of viewer_transcode_cost over the candidate profiles AT the earned resolution
        tier, tie-broken by space-efficiency (AV1 < HEVC < H264). Returns (None, reason) when
        no candidate sits at the tier (the caller keeps its resolution-only pick — byte-
        identical). ``min_coverage`` (default 0 = pure argmin) optionally prefers candidates
        that direct-play for at least that fraction of viewers before falling back to argmin.
"""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
    predict_transcode,
    source_fingerprint,
)
from scripts.managers.machine_learning.sizing.size_model import profile_max_quality

# Codec families, most→least space-efficient. The tokens match both profile-name suffixes
# ("(HEVC)", "x265") and custom-format names ("x265 (HD)", "AV1", "AVC"). Order is the
# size tie-break order: AV1 smallest, H.264 largest/most-compatible.
_CODEC_FAMILIES = [
    ("av1",  ("av1",)),
    ("hevc", ("hevc", "h265", "x265")),
    ("h264", ("h264", "x264", "avc")),
]
_CODEC_SIZE_RANK = {"av1": 0, "hevc": 1, "h264": 2, "unknown": 3}
# A custom format scoring at/below this is treated as a hard BAN of that codec (the user's
# live-action profiles ban x265/AV1 at -10000), so a banned codec never reads as the target.
_BAN_THRESHOLD = -1000.0


def _codec_from_name(name) -> "str | None":
    s = str(name or "").lower()
    for codec, tokens in _CODEC_FAMILIES:
        if any(t in s for t in tokens):
            return codec
    return None


def _profile_cf_scores(profile, cf_id_to_name=None) -> dict:
    """Normalise a profile's custom-format scores to ``{cf_name_lower: score}`` from either the
    LIVE shape (``formatItems=[{format: id, score}]`` resolved via ``cf_id_to_name``, or an inline
    ``name``) or the BLUEPRINT shape (``cf_scores={name: score}``)."""
    out: dict = {}
    cf_id_to_name = cf_id_to_name or {}
    for fi in (profile.get("formatItems") or []):
        nm = fi.get("name") or cf_id_to_name.get(fi.get("format")) or cf_id_to_name.get(str(fi.get("format")))
        if nm is None:
            continue
        try:
            out[str(nm).strip().lower()] = float(fi.get("score") or 0)
        except (TypeError, ValueError):
            pass
    cfs = profile.get("cf_scores")
    if isinstance(cfs, dict):
        for nm, sc in cfs.items():
            try:
                out[str(nm).strip().lower()] = float(sc or 0)
            except (TypeError, ValueError):
                pass
    return out


def _codec_from_cf_scores(scores) -> "str | None":
    """The codec a profile STEERS toward from its CF scores: the highest-scored codec family
    that isn't banned (<= _BAN_THRESHOLD). None when no codec CF is present."""
    best, best_score = None, None
    for codec, tokens in _CODEC_FAMILIES:
        vals = [sc for nm, sc in scores.items() if any(t in nm for t in tokens)]
        if not vals:
            continue
        s = max(vals)
        if s <= _BAN_THRESHOLD:
            continue
        if best_score is None or s > best_score:
            best, best_score = codec, s
    return best


def classify_profile_axes(profile, cf_id_to_name=None) -> dict:
    """{codec, res_tier, name, id} for a quality profile. Codec: name suffix first, then the
    CF-score reconciliation; 'unknown' when neither names a codec (e.g. a multi-codec 'Combined'
    profile). res_tier: the profile's max allowed resolution (0 when nothing allowed)."""
    name = str(profile.get("name") or "")
    res_tier, _q = profile_max_quality(profile)
    codec = _codec_from_name(name) or _codec_from_cf_scores(_profile_cf_scores(profile, cf_id_to_name)) or "unknown"
    return {
        "codec": codec,
        "res_tier": int(res_tier) if isinstance(res_tier, (int, float)) and res_tier > 0 else 0,
        "name": name,
        "id": profile.get("id"),
    }


def candidate_fingerprint(axes, *, location: str = "unknown") -> tuple:
    """The transcode fingerprint of a candidate profile — delegates to source_fingerprint so the
    normalisation is identical to the live capability matrix. Audio/HDR default to unknown/SDR at
    add time (the release codec is the only axis a profile reliably steers); the predictor's graded
    fallback coarsens the unknown axes away."""
    return source_fingerprint(
        video_codec=axes.get("codec"),
        audio_codec=axes.get("audio_codec"),
        subtitles=None,
        height=axes.get("res_tier"),
        hdr=axes.get("hdr"),
        location=location,
    )


def viewer_transcode_cost(profile_fp, likely_viewers, per_user_matrix,
                          per_user_platform_weights, *, none_p: float = 0.5, min_n: int = 3) -> float:
    """Σ over likely viewers of ``watch_share x P(transcode)`` for ``profile_fp``. P comes from
    predict_transcode against each viewer's own matrix + platform weights; an untrusted (None) read
    contributes the neutral ``none_p`` prior. A viewer with no weight is skipped. Lower is better."""
    total = 0.0
    for user, share in (likely_viewers or {}).items():
        try:
            w = float(share or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0.0:
            continue
        matrix = (per_user_matrix or {}).get(user) or {}
        weights = (per_user_platform_weights or {}).get(user) or {}
        p, _level = predict_transcode(matrix, profile_fp, weights, min_n=min_n)
        total += w * (none_p if p is None else float(p))
    return total


def choose_codec_profile(resolution_tier, likely_viewers, per_user_fingerprint_matrix,
                         candidate_profiles, *, per_user_platform_weights=None, size_hint=None,
                         min_n: int = 3, none_p: float = 0.5, min_coverage: float = 0.0):
    """Pick the codec variant AT ``resolution_tier`` that minimises transcoding for the likely
    viewers (coverage-max / accept-minority). Returns ``(profile_id, reason_dict)``, or
    ``(None, reason)`` when no candidate sits at the tier so the caller keeps its resolution-only
    pick (byte-identical when off / when only one variant exists).

    Tie-break: lower cost, then the more space-efficient codec (AV1 < HEVC < H264), so a cold
    household with no transcode signal defaults to the bandwidth-optimal codec rather than an
    arbitrary one. ``size_hint(profile)->float`` overrides the codec-rank tie-break when given.
    ``min_coverage`` (0 = off) first restricts to candidates whose cost <= 1-min_coverage (i.e.
    that fraction of viewers direct-play), falling back to the full set when none qualify."""
    cands = []
    tier = int(resolution_tier or 0)
    for prof in (candidate_profiles or []):
        axes = classify_profile_axes(prof)
        if axes["res_tier"] != tier:
            continue
        fp = candidate_fingerprint(axes)
        cost = viewer_transcode_cost(
            fp, likely_viewers, per_user_fingerprint_matrix, per_user_platform_weights or {},
            none_p=none_p, min_n=min_n,
        )
        size = float(size_hint(prof)) if callable(size_hint) else float(_CODEC_SIZE_RANK.get(axes["codec"], 3))
        cands.append({"id": prof.get("id"), "codec": axes["codec"], "cost": cost, "size": size})
    if not cands:
        return None, {"reason": "no_candidate_at_tier", "tier": tier}

    # Coverage floor (optional): prefer candidates that direct-play for >= min_coverage of viewers.
    floor = 1.0 - float(min_coverage or 0.0)
    meeting = [c for c in cands if c["cost"] <= floor + 1e-9]
    pool = meeting if meeting else cands
    pool.sort(key=lambda c: (round(c["cost"], 6), c["size"]))
    best = pool[0]
    return best["id"], {
        "reason": "coverage_max",
        "codec": best["codec"],
        "cost": round(best["cost"], 4),
        "tier": tier,
        "n_candidates": len(cands),
        "coverage_floor_met": bool(meeting),
    }
