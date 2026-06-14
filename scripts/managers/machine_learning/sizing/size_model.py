"""
size_model.py — single source of truth for media file-size estimation.
================================================================================
RELOCATED HERE from ``scripts/support/utilities/size_model.py`` (ML-migration
Step 1). That old path is now a one-line re-export shim, so every existing
``from scripts.support.utilities.size_model import ...`` keeps working unchanged
while new code imports ``scripts.managers.machine_learning.sizing.size_model``.
This module owns the calibration-overlay state, so there is exactly ONE copy of
it regardless of which import path a caller used.

Every "how big will this be?" question in the app (acquisition ``~size``, JIT
upgrade space-reservation, active-watcher upgrade anticipation, quality
file-size comparisons, ML storage forecasting) funnels through here so the
numbers can never disagree again.

The unit is **MiB per minute** (MiB/min). A file's size is::

    size_GiB = (mb_per_min * runtime_minutes * n_items) / 1024

Resolution order for the MiB/min of a quality
----------------------------------------------
1. **Measured** — the library's own ``size_bytes / runtime`` average for that
   quality name, passed in by the caller (see :func:`measured_mb_per_min`).
   Always preferred when at least one real sample exists.
2. **Calibrated fallback** — :data:`CALIBRATED_MB_PER_MIN`, derived from this
   library's measured episode files for the low/mid tiers and anchored on the
   household's real 4K remux movies for the high tiers (see the table comment).
3. **Resolution default** — a coarse per-resolution number when the quality
   name is unrecognised.
4. **:data:`DEFAULT_MB_PER_MIN`** — last resort.

Whatever the source, the result is clamped to
``[MIN_MB_PER_MIN, MAX_MB_PER_MIN]`` so a bad upstream value (e.g. reading a
Radarr quality-definition ``maxSize`` ceiling of ~2000 MiB/min) can never again
produce a 187 GB estimate for a 96-minute movie.

This module is intentionally dependency-free (no logger, cache, or registry) so
it is safe to import from any layer — and so it satisfies the brain-layer rule
that nothing here makes an HTTP call.
"""

from __future__ import annotations

# ── Clamp + defaults ──────────────────────────────────────────────────────────
# Heaviest real file measured in this library is a 4K remux at ~540 MiB/min
# (p90 ~477 across 18 remuxes). A full UHD BR-DISK can reach ~850. Above that is
# not a real file — it is a units/field bug, so we clamp.
MIN_MB_PER_MIN: float = 0.5
MAX_MB_PER_MIN: float = 900.0
DEFAULT_MB_PER_MIN: float = 25.0

# ── Calibrated fallback table (MiB/min per quality name) ──────────────────────
# This is the COLD-START fallback. At runtime it is overlaid by live measurements
# (see _CALIBRATION_OVERRIDE / set_calibration, refreshed each run from the
# library by machine_learning/size_calibration.py), so these values only matter
# for tiers the library has too few samples of.
#
# Seeded from a full measured run (`python scripts/support/tools/calibrate_sizes.py`,
# Radarr "standard" 1776 movie files + Sonarr "720" 5138 episode files = 6914).
# A tier's value is the MEAN MiB/min of its files. Cleanups vs raw measurement:
#   - DVD-R (n=3 measured ~55) pinned to DVD-class — tiny-n artifact, not a rate.
#   - CAM/TELESYNC/junk kept low (never a real grab target; tiny-n samples high).
#   - WEBRip-1080p (n=2) thin → conservative 40.
#   - Bluray-2160p (n=6 measured ~112) nudged to 135 to stay >= WEBDL-2160p.
#   - WEBRip-2160p/HDTV-2160p: no samples yet → sane defaults.
# Sonarr ("Bluray-2160p Remux") and Radarr ("Remux-2160p") spellings share a
# value so either service resolves without normalisation.
# Sample n: Bluray-720p 1170 · DVD 1176 · SDTV 1148 · Bluray-480p 1032 · WEBDL-480p
#   916 · WEBDL-720p 533 · WEBRip-480p 548 · WEBRip-720p 139 · Bluray-576p 87 ·
#   Remux-1080p 38 · HDTV-720p 31 · Bluray-1080p 18 · WEBDL-1080p 17 · HDTV-1080p 17
#   · Remux-2160p 18 · Bluray-2160p 6 · WEBDL-2160p 4.
CALIBRATED_MB_PER_MIN: dict[str, float] = {
    # Junk / pre-release (kept low — never a real grab target; tiny-n cam/telesync
    # samples measure high but are irrelevant to sizing)
    "Unknown":        1.0,
    "WORKPRINT":      2.0,
    "CAM":            3.0,
    "TELESYNC":       3.0,
    "TELECINE":       3.0,
    "REGIONAL":       4.0,
    "DVDSCR":         8.0,
    # SD / 480p — measured
    "SDTV":          10.8,
    "DVD":           13.3,
    "DVD-R":         12.0,   # n=3 measured ~55 (artifact) → pinned to DVD-class
    "WEBRip-480p":    9.1,
    "WEBDL-480p":    11.3,
    "Bluray-480p":    7.9,
    "Bluray-576p":   17.7,
    # 720p — measured (Bluray-720p n=1170, WEBDL-720p n=533)
    "Raw-HD":        45.0,
    "HDTV-720p":     16.5,
    "WEBRip-720p":   16.3,
    "WEBDL-720p":    24.7,
    "Bluray-720p":   52.4,
    # 1080p — measured
    "HDTV-1080p":    70.9,   # n=17 (was a single outlier before the TV scan)
    "WEBRip-1080p":  40.0,   # n=2, thin → conservative
    "WEBDL-1080p":   55.7,
    "Bluray-1080p":  65.3,
    "Remux-1080p":          235.2,
    "Bluray-1080p Remux":   235.2,
    # 2160p — measured high tiers; WEB/HDTV thin/absent
    "HDTV-2160p":    90.0,
    "WEBRip-2160p":  80.0,
    "WEBDL-2160p":  124.0,
    "Bluray-2160p": 135.0,   # n=6 measured ~112; nudged >= WEBDL-2160p for ordering
    "Remux-2160p":          389.4,
    "Bluray-2160p Remux":   389.4,
    # Full-disc images (rare; >= remux tier)
    "BR-DISK":      460.0,
}

# ── Runtime calibration overlay ───────────────────────────────────────────────
# Per-quality MiB/min measured from the LIVE library and refreshed each run by
# machine_learning/size_calibration.py (persisted in global_cache so it survives
# across runs). Wins over CALIBRATED_MB_PER_MIN, loses to an explicit per-call
# ``measured`` arg. Empty until the first calibration runs.
_CALIBRATION_OVERRIDE: dict[str, float] = {}


def set_calibration(mapping: "dict | None") -> int:
    """Replace the runtime overlay with clamped values from ``{quality: mib_per_min}``.
    Returns the number of tiers installed. Ignores non-positive / non-numeric values."""
    global _CALIBRATION_OVERRIDE
    if not mapping:
        return 0
    _CALIBRATION_OVERRIDE = {
        str(k): _clamp(v) for k, v in mapping.items()
        if isinstance(v, (int, float)) and v > 0
    }
    return len(_CALIBRATION_OVERRIDE)


def get_calibration() -> dict:
    return dict(_CALIBRATION_OVERRIDE)


def clear_calibration() -> None:
    global _CALIBRATION_OVERRIDE
    _CALIBRATION_OVERRIDE = {}

# Coarse per-resolution defaults when the quality NAME is unknown but a
# resolution (pixel height) is available.
_RESOLUTION_DEFAULT_MB_PER_MIN: dict[int, float] = {
    480:  12.0,
    540:  16.0,
    576:  18.0,
    720:  30.0,
    1080: 70.0,
    2160: 200.0,
}


# ── Codec normalisation (for codec-specific MiB/min keys) ─────────────────────
# HEVC/VP9/AV1 encodes are ~30-50% smaller than H.264 at the same resolution, so a
# codec-qualified rate ("Bluray-1080p@h265") sizes them far more accurately. mediaInfo
# spellings vary (h264/x264/AVC, hevc/h265/x265, ...) → fold to a canonical token.
_CODEC_ALIASES: dict[str, str] = {
    "h264": "h264", "x264": "h264", "avc": "h264", "avc1": "h264", "mpeg-4 avc": "h264",
    "h265": "h265", "x265": "h265", "hevc": "h265",
    "vp9": "vp9", "av1": "av1",
    "mpeg4": "mpeg4", "xvid": "mpeg4", "divx": "mpeg4",
    "mpeg2": "mpeg2", "vc1": "vc1", "vc-1": "vc1",
}


def _norm_codec(codec: "str | None") -> "str | None":
    """Canonical codec token (h264/h265/vp9/av1/...) or None for blank input."""
    if not codec:
        return None
    c = str(codec).strip().lower()
    return _CODEC_ALIASES.get(c, c) or None


def _clamp(v: float) -> float:
    return max(MIN_MB_PER_MIN, min(MAX_MB_PER_MIN, float(v)))


def mb_per_min(
    quality_name: "str | None",
    measured: "dict | None" = None,
    *,
    resolution: "int | float | None" = None,
    codec: "str | None" = None,
) -> float:
    """
    Resolve the MiB/min for a quality, clamped to a sane range.

    ``measured`` is an optional ``{quality_name: mib_per_min}`` map (typically
    from :func:`measured_mb_per_min`); a present, positive entry wins. Falls
    back to the calibrated table, then a resolution default, then the global
    default.

    ``codec`` (optional) prefers a codec-qualified rate ``"{quality}@{codec}"``
    when one exists in ``measured`` / the overlay / the table, falling back to the
    plain quality otherwise. With ``codec=None`` (the default) the result is
    byte-identical to the legacy behaviour.
    """
    name = (quality_name or "").strip()

    nc = _norm_codec(codec)
    if nc:
        key = f"{name}@{nc}"
        if measured:
            m = measured.get(key)
            if m and m > 0:
                return _clamp(m)
        if key in _CALIBRATION_OVERRIDE:
            return _clamp(_CALIBRATION_OVERRIDE[key])
        if key in CALIBRATED_MB_PER_MIN:
            return _clamp(CALIBRATED_MB_PER_MIN[key])

    if measured:
        m = measured.get(name)
        if m and m > 0:
            return _clamp(m)

    if name in _CALIBRATION_OVERRIDE:
        return _clamp(_CALIBRATION_OVERRIDE[name])

    if name in CALIBRATED_MB_PER_MIN:
        return _clamp(CALIBRATED_MB_PER_MIN[name])

    if resolution is not None:
        try:
            res = int(resolution)
        except (TypeError, ValueError):
            res = 0
        if res:
            # nearest defined resolution tier at or below the given height
            tiers = sorted(_RESOLUTION_DEFAULT_MB_PER_MIN)
            chosen = tiers[0]
            for t in tiers:
                if res >= t:
                    chosen = t
            return _clamp(_RESOLUTION_DEFAULT_MB_PER_MIN[chosen])

    return _clamp(DEFAULT_MB_PER_MIN)


def estimate_gb(
    quality_name: "str | None",
    runtime_min: "float | int | None",
    n_items: int = 1,
    measured: "dict | None" = None,
    *,
    resolution: "int | float | None" = None,
    codec: "str | None" = None,
) -> float:
    """
    Estimated size in **GiB** for ``n_items`` items of ``runtime_min`` minutes
    each at ``quality_name``. Returns 0.0 when runtime/count are missing.
    ``codec`` (optional) selects a codec-qualified rate; ``None`` = legacy behaviour.
    """
    try:
        rt = max(0.0, float(runtime_min or 0))
        n = max(0, int(n_items or 0))
    except (TypeError, ValueError):
        return 0.0
    if rt <= 0 or n <= 0:
        return 0.0
    rate = mb_per_min(quality_name, measured, resolution=resolution, codec=codec)
    return (rate * rt * n) / 1024.0


def profile_max_quality(profile: dict) -> "tuple[int, str | None]":
    """
    Return ``(max_resolution, quality_name)`` of the highest-resolution *allowed*
    quality in a Sonarr/Radarr quality profile. Walks both top-level items and
    grouped sub-items. ``(-1, None)`` when nothing is allowed.

    Use the returned quality_name to size what the profile will actually grab —
    NOT the profile's ``cutoff`` (which is the "good enough, stop upgrading"
    floor, not the ceiling).
    """
    best_res, best_name = -1, None
    for item in (profile.get("items") or []):
        if not item.get("allowed"):
            continue
        q = item.get("quality") or {}
        res = q.get("resolution", 0)
        if isinstance(res, (int, float)) and int(res) > best_res:
            best_res, best_name = int(res), q.get("name")
        for sub in (item.get("items") or []):
            if not sub.get("allowed"):
                continue
            sq = sub.get("quality") or {}
            sr = sq.get("resolution", 0)
            if isinstance(sr, (int, float)) and int(sr) > best_res:
                best_res, best_name = int(sr), sq.get("name")
    return best_res, best_name


def estimate_gb_for_profile(
    profile: dict,
    runtime_min: "float | int | None",
    n_items: int = 1,
    measured: "dict | None" = None,
    *,
    codec: "str | None" = None,
) -> float:
    """
    Estimated GiB to grab ``n_items`` at the profile's top *allowed* quality.
    Convenience wrapper over :func:`profile_max_quality` + :func:`estimate_gb`.
    ``codec`` (optional) sizes the grab at a codec-qualified rate (e.g. the
    household's modal codec for that tier); ``None`` = legacy behaviour.
    """
    _res, q_name = profile_max_quality(profile) if profile else (-1, None)
    return estimate_gb(q_name, runtime_min, n_items, measured,
                       resolution=_res if _res > 0 else None, codec=codec)


def measured_stats(
    df,
    *,
    size_col: str = "size_bytes",
    runtime_col: str = "runtime_seconds",
    runtime_unit: str = "seconds",
    quality_col: str = "quality_name",
    codec_col: "str | None" = None,
) -> dict:
    """
    Per-quality ``{quality_name: {"mean": mib_per_min, "n": sample_count}}``
    measured from a media-files DataFrame (``size_bytes`` / runtime). Rows whose
    implied rate falls outside ``[MIN_MB_PER_MIN, MAX_MB_PER_MIN]`` (corrupt
    runtimes) are dropped before averaging. The ``n`` lets callers combine
    measurements from several instances/services weighted by sample count.

    ``runtime_unit`` is ``"seconds"`` for Sonarr's ``runtime_seconds`` or
    ``"minutes"`` for Radarr's ``runtime_minutes``. Pure/optional pandas:
    returns ``{}`` if pandas is unavailable or the columns are missing.

    ``codec_col`` (optional) ADDITIONALLY emits codec-qualified keys
    ``"{quality}@{codec}"`` (codec normalised) alongside the plain quality keys, so
    callers can size HEVC/VP9 files at their real rate. The plain keys are unchanged
    (computed over all rows), so passing ``codec_col=None`` is byte-identical and a
    missing column is silently skipped.
    """
    try:
        import pandas as pd
    except Exception:
        return {}

    out: dict = {}
    try:
        needed = {size_col, runtime_col, quality_col}
        if df is None or df.empty or not needed.issubset(df.columns):
            return out
        has_codec = bool(codec_col) and codec_col in df.columns
        cols = [size_col, runtime_col, quality_col] + ([codec_col] if has_codec else [])
        sub = df[cols].copy()
        sub[size_col] = pd.to_numeric(sub[size_col], errors="coerce")
        sub[runtime_col] = pd.to_numeric(sub[runtime_col], errors="coerce")
        sub = sub[(sub[size_col] > 0) & (sub[runtime_col] > 0) & sub[quality_col].notna()]
        if sub.empty:
            return out
        runtime_min = sub[runtime_col] / (60.0 if runtime_unit == "seconds" else 1.0)
        sub["__mbpm"] = (sub[size_col] / (1024 ** 2)) / runtime_min
        sub = sub[(sub["__mbpm"] >= MIN_MB_PER_MIN) & (sub["__mbpm"] <= MAX_MB_PER_MIN)]
        if sub.empty:
            return out
        # Plain per-quality keys (unchanged — over ALL surviving rows).
        g = sub.groupby(quality_col)["__mbpm"]
        means, counts = g.mean(), g.size()
        out = {str(q): {"mean": float(means[q]), "n": int(counts[q])} for q in means.index}
        # Additive codec-qualified keys ("quality@codec"); rows with a blank codec drop out.
        if has_codec:
            sub["__codec"] = sub[codec_col].map(_norm_codec)
            cs = sub[sub["__codec"].notna()]
            if not cs.empty:
                gc = cs.groupby([quality_col, "__codec"])["__mbpm"]
                cmeans, ccounts = gc.mean(), gc.size()
                for (q, c) in cmeans.index:
                    out[f"{q}@{c}"] = {"mean": float(cmeans[(q, c)]), "n": int(ccounts[(q, c)])}
    except Exception:
        pass
    return out


def measured_mb_per_min(df, **kwargs) -> dict:
    """Mean measured MiB/min per quality name — thin wrapper over
    :func:`measured_stats` returning just ``{quality_name: mean}``."""
    return {q: s["mean"] for q, s in measured_stats(df, **kwargs).items()}
