"""
sizing/anomaly.py — "wildly out of size profile" detector (pure, no I/O).
================================================================================
Flags media files whose ACTUAL bitrate is wildly inconsistent with their GRADED
quality — e.g. a 45 GiB movie graded ``Bluray-720p`` (≈300 MiB/min, which is
4K-remux territory, not 720p). This is the signal that absolute large-file checks
miss: 45 GiB is fine for a real 2160p remux but absurd for 720p.

The yardstick is **MiB/min** (size ÷ runtime), compared against the expected rate
for the file's own quality name:

  * well-sampled tiers use the LIBRARY's own measured mean (self-calibrating —
    one bloated outlier barely moves a mean over hundreds of files);
  * thin tiers fall back to :mod:`sizing.size_model`'s calibrated table (which is
    outlier-free), then a per-resolution default.

A file is ``oversized`` when its rate ≥ ``over_ratio`` × expected (default 3×) and
``undersized`` when ≤ ``under_ratio`` × expected (default 0.3× — a fake/corrupt
file far too small to be its claimed quality). Everything between is normal.

Pure/optional-pandas and dependency-light (only the brain-layer ``size_model``), so
it is safe to import anywhere and makes no HTTP call. The service loads the
media-files DataFrame and calls :func:`find_size_anomalies`.
"""
from __future__ import annotations

from scripts.managers.machine_learning.sizing import size_model

# A file must have at least this many same-quality siblings before we trust the
# library's measured mean as the baseline; below it we use the calibrated table so
# a thin tier's own outliers can't define "normal".
DEFAULT_MIN_SAMPLES = 8
DEFAULT_OVER_RATIO = 3.0       # ≥ this × expected → oversized (wildly out)
DEFAULT_UNDER_RATIO = 0.3      # ≤ this × expected → undersized (fake/corrupt)
DEFAULT_REPORT_LIMIT = 25      # rows surfaced in the run-summary table (rest counted)

_CONFIG_DEFAULTS = {
    "enabled": True,            # run the read-only detector + report
    "remediate": False,         # opt-in: ACT on findings (rescan mis-graded, re-grab bloated)
    "over_ratio": DEFAULT_OVER_RATIO,
    "under_ratio": DEFAULT_UNDER_RATIO,
    "min_samples": DEFAULT_MIN_SAMPLES,
    "report_limit": DEFAULT_REPORT_LIMIT,
}


def config_for(config) -> dict:
    """Merge the ``size_anomaly`` config block over the defaults (pure dict parsing — the
    service passes its config). Unknown keys and ``None`` values are ignored, so a partial
    block (e.g. just ``{"over_ratio": 2.5}``) keeps every other default."""
    raw = {}
    try:
        raw = (config.get("size_anomaly", {}) or {}) if hasattr(config, "get") else {}
    except Exception:
        raw = {}
    out = dict(_CONFIG_DEFAULTS)
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        if k in out and v is not None:
            out[k] = v
    return out

# Real, gradeable tiers for the "looks like" diagnostic — junk/pre-release spellings
# are excluded so a bloated file is described as the real tier its bitrate implies.
_REAL_TIERS = {
    k: v for k, v in size_model.CALIBRATED_MB_PER_MIN.items()
    if k not in {"Unknown", "WORKPRINT", "CAM", "TELESYNC", "TELECINE", "REGIONAL", "DVDSCR"}
}

# Junk / SD grades: a HUGE file carrying one of these is almost always MIS-GRADED (the file is
# really HD), so the fix is a metadata RESCAN, not a re-grab. A bloated file graded as a real
# HD/UHD tier is genuinely over-bitrated → RE-GRAB a properly-sized release at its profile.
_JUNK_OR_SD_GRADES = {
    "Unknown", "WORKPRINT", "CAM", "TELESYNC", "TELECINE", "REGIONAL", "DVDSCR",
    "SDTV", "DVD", "DVD-R", "WEBRip-480p", "WEBDL-480p", "Bluray-480p", "Bluray-576p",
}


def recommend_action(verdict: str, quality_name: "str | None") -> str:
    """The remediation a service should take: 'rescan' (re-read mediainfo to fix a wrong grade —
    non-destructive) or 'regrab' (replace a genuinely-bloated file at its profile target —
    destructive). Undersized files rescan (verify the suspiciously-small file)."""
    if verdict == "undersized":
        return "rescan"
    if verdict == "oversized":
        return "rescan" if (quality_name in _JUNK_OR_SD_GRADES) else "regrab"
    return ""


def implied_tier(actual_mb_per_min: float) -> str:
    """The real quality tier whose calibrated rate is closest to ``actual_mb_per_min`` —
    the diagnostic that makes an anomaly self-evident ('graded 720p, bitrate is Remux-2160p
    class'). Returns the nearest tier name, or '' for a non-positive rate."""
    if not actual_mb_per_min or actual_mb_per_min <= 0:
        return ""
    return min(_REAL_TIERS, key=lambda k: abs(_REAL_TIERS[k] - actual_mb_per_min))


def find_size_anomalies(
    df,
    *,
    size_col: str = "size_bytes",
    runtime_col: str = "runtime_minutes",
    runtime_unit: str = "minutes",
    quality_col: str = "quality_name",
    resolution_col: str = "resolution",
    id_cols: "tuple[str, ...]" = ("title",),
    over_ratio: float = DEFAULT_OVER_RATIO,
    under_ratio: float = DEFAULT_UNDER_RATIO,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> list[dict]:
    """Return the rows of ``df`` whose size is wildly out of profile for their quality.

    Each result carries the echoed ``id_cols`` plus: ``quality_name``, ``runtime_min``,
    ``size_gb``, ``expected_gb``, ``ratio`` (actual ÷ expected size), ``actual_mb_per_min``,
    ``expected_mb_per_min``, ``looks_like`` (the tier the bitrate implies), ``verdict``
    ('oversized' | 'undersized'), and ``reclaim_gb`` (size − expected, ≥0; the space an
    in-profile re-grab would free). Oversized first, each group sorted by ``reclaim_gb`` desc.

    Pure: returns ``[]`` if pandas is unavailable or the required columns are missing.
    """
    try:
        import pandas as pd
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []
    needed = {size_col, runtime_col, quality_col}
    if not needed.issubset(getattr(df, "columns", [])):
        return []

    # Library baseline per quality (mean MiB/min + sample count), outlier-trimmed by size_model.
    measured = size_model.measured_stats(
        df, size_col=size_col, runtime_col=runtime_col,
        runtime_unit=runtime_unit, quality_col=quality_col,
    )
    div = 60.0 if runtime_unit == "seconds" else 1.0
    has_res = resolution_col in df.columns

    oversized: list[dict] = []
    undersized: list[dict] = []
    for row in df.itertuples(index=False):
        d = row._asdict()
        try:
            size = float(d.get(size_col) or 0)
            rt_min = float(d.get(runtime_col) or 0) / div
        except (TypeError, ValueError):
            continue
        if size <= 0 or rt_min <= 0:
            continue
        actual_mbpm = (size / (1024 ** 2)) / rt_min
        # Out-of-physics rates (corrupt runtime) can't be judged against a tier.
        if not (size_model.MIN_MB_PER_MIN <= actual_mbpm <= size_model.MAX_MB_PER_MIN):
            continue

        quality = d.get(quality_col)
        qn = str(quality) if quality is not None and str(quality) else None
        stat = measured.get(qn) if qn else None
        if stat and stat.get("n", 0) >= min_samples:
            expected_mbpm = float(stat["mean"])             # trusted library mean
        else:
            res = d.get(resolution_col) if has_res else None
            expected_mbpm = size_model.mb_per_min(qn, resolution=res)   # calibrated/res fallback
        if expected_mbpm <= 0:
            continue

        ratio = actual_mbpm / expected_mbpm
        if ratio >= over_ratio:
            verdict = "oversized"
        elif ratio <= under_ratio:
            verdict = "undersized"
        else:
            continue

        size_gb = size / (1024 ** 3)
        expected_gb = (expected_mbpm * rt_min) / 1024.0
        rec = {col: d.get(col) for col in id_cols}
        rec.update({
            "quality_name": qn or "",
            "runtime_min": round(rt_min, 1),
            "size_gb": round(size_gb, 2),
            "expected_gb": round(expected_gb, 2),
            "ratio": round(ratio, 1),
            "actual_mb_per_min": round(actual_mbpm, 1),
            "expected_mb_per_min": round(expected_mbpm, 1),
            "looks_like": implied_tier(actual_mbpm),
            "verdict": verdict,
            "reclaim_gb": round(max(0.0, size_gb - expected_gb), 2),
            "action": recommend_action(verdict, qn),
        })
        (oversized if verdict == "oversized" else undersized).append(rec)

    oversized.sort(key=lambda r: r["reclaim_gb"], reverse=True)
    undersized.sort(key=lambda r: r["size_gb"])
    return oversized + undersized
