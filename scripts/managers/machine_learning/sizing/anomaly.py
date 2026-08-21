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
    # Dry-run: how many bloated candidates get a REAL interactive release search
    # (movie GET + indexer roundtrip, ~2s blocked wall each) just to name the
    # would-grab release in the preview. 0 (default) defers all checks — the
    # size-anomaly grid already lists every candidate.
    "dry_run_search_budget": 0,
    # Re-grab attempt ledger (see `should_attempt`). A bloat re-grab is a SEARCH, and
    # Sonarr only grabs on UPGRADE — a smaller replacement is never an upgrade, so a
    # genuinely-mis-sized file can be searched every run forever with no effect and no
    # detector. These bound that: stop after `max_regrab_attempts` fruitless attempts,
    # and wait `regrab_retry_days` between them. A file whose size CHANGES resets both
    # (something worked; it is no longer the same file).
    "max_regrab_attempts": 3,
    "regrab_retry_days": 7,
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

# Grades at which a HUGE file means MIS-GRADED, not genuinely bloated — so the fix is a
# metadata RESCAN, not a re-grab.
#
# Two families qualify:
#   * junk / SD / pre-release spellings — a multi-GB file simply is not one of these;
#   * BROADCAST captures (HDTV-720p / HDTV-1080p) — broadcast bitrate is capped well below
#     disc bitrate, so an HDTV-graded file at several times its expected rate is a Blu-ray
#     source Sonarr labelled wrong. Sending those down the re-grab path cannot help: the
#     search asks for an UPGRADE, a smaller file is not one, and the same file is re-searched
#     every run (measured: the same 38 Dragon Ball Z episodes searched on two consecutive
#     runs, 496 anomalies unchanged). A rescan re-reads mediainfo and can actually fix it.
#
# HDTV-2160p is deliberately NOT here: UHD broadcast is genuinely high-bitrate and there is
# no higher HDTV tier for it to be mistaken for.
#
# A bloated file graded at a real disc tier (Bluray-*/Remux-*) IS over-bitrated for what it
# claims → RE-GRAB a properly-sized release at its profile.
_MISGRADE_WHEN_BLOATED = {
    "Unknown", "WORKPRINT", "CAM", "TELESYNC", "TELECINE", "REGIONAL", "DVDSCR",
    "SDTV", "DVD", "DVD-R", "WEBRip-480p", "WEBDL-480p", "Bluray-480p", "Bluray-576p",
    "HDTV-720p", "HDTV-1080p",
}

# Back-compat alias — the old name described the set before the broadcast tiers joined it.
_JUNK_OR_SD_GRADES = _MISGRADE_WHEN_BLOATED


def recommend_action(verdict: str, quality_name: "str | None") -> str:
    """The remediation a service should take: 'rescan' (re-read mediainfo to fix a wrong grade —
    non-destructive) or 'regrab' (replace a genuinely-bloated file at its profile target).
    Undersized files rescan (verify the suspiciously-small file)."""
    if verdict == "undersized":
        return "rescan"
    if verdict == "oversized":
        return "rescan" if (quality_name in _MISGRADE_WHEN_BLOATED) else "regrab"
    return ""


# ── Re-grab attempt ledger (pure) ───────────────────────────────────────────
#
# A bloat re-grab fires an EpisodeSearch and keeps the file. Sonarr grabs only on UPGRADE,
# and a SMALLER replacement is by definition not an upgrade — so for a file that is merely
# mis-sized (rather than mis-graded) the search is accepted, does nothing, and re-fires on
# every subsequent run. There was no detector for that: 496 anomalies and the same 38 episode
# searches on two consecutive runs. These helpers bound the retry loop.
#
# State shape, per instance: {str(file_id): {"attempts": int, "last_at": epoch, "size_bytes": int}}
# The service owns the read/write; everything below is pure so it can be tested without a cache.

def attempts_key(instance: str) -> str:
    """Global-cache key holding one instance's re-grab attempt ledger."""
    return f"sonarr/size_anomaly/regrab_attempts/{instance}"


def _as_int(v):
    """``int(v)`` or None. A value that will not parse is UNKNOWN, never 0 — a corrupt ledger
    entry must not read as "size changed" (which would reset the budget and restore the churn)
    nor as "size 0" (which would look like a change every time)."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def should_attempt(entry, size_bytes, now_ts, *, max_attempts, retry_days) -> tuple:
    """``(ok, reason)`` — may this file be searched again now?

    Reasons: ``first`` (never tried), ``changed`` (the file was replaced since the last
    attempt, so the loop is not stuck — start over), ``retry`` (cooldown elapsed, budget
    left), ``cooling`` (too soon), ``abandoned`` (budget spent with no change).

    An ABSENT entry and a zero-attempt entry mean the same thing here — never tried — so
    they deliberately share the ``first``/``retry`` path. Stating that explicitly is the
    P-C discipline: the two are only safe to conflate because the answer is genuinely the
    same, unlike ``size_bytes`` below where absent must NOT read as "unchanged".
    """
    if not entry:
        return True, "first"
    prev_size = _as_int(entry.get("size_bytes"))
    cur_size = _as_int(size_bytes)
    # Only a KNOWN-and-different size proves a replacement landed. An absent or unparseable
    # size is unknown, not "same" and not "changed" — treating it as changed would reset the
    # budget forever and restore the exact churn this ledger exists to stop.
    if prev_size is not None and cur_size is not None and prev_size != cur_size:
        return True, "changed"
    try:
        attempts = int(entry.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if max_attempts and attempts >= int(max_attempts):
        return False, "abandoned"
    try:
        last = float(entry.get("last_at") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if retry_days and last and (float(now_ts) - last) < float(retry_days) * 86400.0:
        return False, "cooling"
    return True, "retry"


def record_attempt(entry, size_bytes, now_ts) -> dict:
    """The ledger entry after an attempt. A size change resets the counter to 1 — the file
    that was failing to move has moved, so its history no longer describes this file."""
    prev = dict(entry or {})
    prev_size = _as_int(prev.get("size_bytes"))
    cur_size = _as_int(size_bytes)
    changed = prev_size is not None and cur_size is not None and prev_size != cur_size
    try:
        attempts = 0 if (changed or not prev) else int(prev.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return {
        "attempts": attempts + 1,
        "last_at": float(now_ts),
        "size_bytes": cur_size,
    }


def prune_attempts(store, live_file_ids) -> dict:
    """Drop ledger entries for files that are no longer anomalous — remediation worked, or
    the file is gone. Keeps the ledger from growing without bound and lets a file that
    re-develops a size problem start with a clean budget."""
    live = {str(f) for f in (live_file_ids or [])}
    return {k: v for k, v in (store or {}).items() if str(k) in live}


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
            "size_bytes": int(size),
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
