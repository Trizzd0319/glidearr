"""
sizing/size_calibration.py — the PURE calibration math (decision half).
================================================================================
ML-migration Step 1c. The arithmetic that turns measured per-quality
``{mean, n}`` stats (from ``size_model.measured_stats`` over the library's file
caches) into a weighted MiB/min calibration table — with NO I/O. The service-io
orchestrator (``machine_learning/size_calibration.SizeCalibrator``: reads the
registry's cache managers, persists to global_cache, calls
``size_model.set_calibration``) is the thin BRIDGE that calls these functions.

Pure + unit-testable with hand-built dicts; no registry, no global_cache, no
HTTP, no ``set_calibration`` side effect.
"""
from __future__ import annotations

from datetime import datetime, timezone


def fold_stats(acc: dict, stats: dict) -> int:
    """Fold per-quality ``{quality: {"mean": m, "n": k}}`` into the weighted
    accumulator ``acc`` (``{quality: {"wsum": Σ(m·k), "n": Σk}}``) in place.
    Returns the Σn added so callers can combine several instances/services."""
    added = 0
    for q, s in stats.items():
        a = acc.setdefault(q, {"wsum": 0.0, "n": 0})
        a["wsum"] += s["mean"] * s["n"]
        a["n"]    += s["n"]
        added     += s["n"]
    return added


def compute_calibration_table(acc: dict, min_samples: int) -> dict:
    """Reduce the weighted accumulator to ``{quality: mib_per_min}`` (mean,
    1-dp), keeping only tiers with at least ``min_samples`` real files."""
    return {
        q: round(a["wsum"] / a["n"], 1)
        for q, a in acc.items()
        if a["n"] >= min_samples and a["n"] > 0
    }


def movie_runtime_min(m: dict) -> "float | None":
    """Runtime (minutes) for a Radarr movie dict: prefer the file's mediaInfo
    runTime (seconds or 'H:MM:SS'), else the movie's TMDB runtime (minutes)."""
    raw = ((m.get("movieFile") or {}).get("mediaInfo") or {}).get("runTime")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw) / 60.0
    if isinstance(raw, str) and ":" in raw:
        parts = raw.split(":")
        try:
            if len(parts) == 3:
                return (float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])) / 60.0
            if len(parts) == 2:
                return (float(parts[0]) * 60 + float(parts[1])) / 60.0
        except (ValueError, TypeError):
            pass
    rt = m.get("runtime")
    try:
        return float(rt) if rt and float(rt) > 0 else None
    except (TypeError, ValueError):
        return None


def calibration_is_fresh(payload, max_age_days: int) -> bool:
    """True if a persisted calibration payload's ``generated_at`` is within
    ``max_age_days``. Tolerant of missing/naive timestamps."""
    try:
        gen = (payload or {}).get("generated_at")
        if not gen:
            return False
        dt = datetime.fromisoformat(gen)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - dt).total_seconds()
        return 0 <= age_s < max_age_days * 86_400
    except Exception:
        return False
