"""
size_calibration.py — keep size_model's MiB/min overlay in sync with the library.
================================================================================
The static ``size_model.CALIBRATED_MB_PER_MIN`` table is only a cold-start
fallback. This calibrator measures the **live** per-quality MiB/min from the
Sonarr/Radarr file caches (Parquet) that the run already built, and installs the
result as ``size_model``'s runtime overlay so every estimate (acquisition
``~size``, JIT / active-watcher space reservation) tracks reality with no manual
table edits.

Cheap by design — it reads the Parquet caches the cache managers already wrote
this run (no extra *arr API calls). The result is persisted in ``global_cache``
under ``size_model/calibration`` so:
  * ``load_into_model()`` warms the overlay at startup from the last run, and
  * ``refresh()`` recomputes at most once per ``MAX_AGE_DAYS`` (TTL-guarded),
    re-installing the cached table on the runs in between.

Best-effort throughout: any failure leaves the static table in place and never
breaks the run.

Wiring (main.py):
    cal = SizeCalibrator(registry=…, global_cache=…, logger=…, config=…)
    cal.load_into_model()      # start of run() — warm overlay from last run
    …run Sonarr + Radarr…       # caches/Parquets get refreshed
    cal.refresh()              # after Phase 2 — recompute if stale, persist
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.managers.machine_learning.sizing import size_model
from scripts.managers.machine_learning.sizing.size_calibration import (
    calibration_is_fresh,
    compute_calibration_table,
    fold_stats,
    movie_runtime_min,
)


class SizeCalibrator:
    CACHE_KEY    = "size_model/calibration"
    MIN_SAMPLES  = 20    # per-quality files required before it overrides the table
    MAX_AGE_DAYS = 7     # recompute cadence; cached table is reused in between

    def __init__(self, registry=None, global_cache=None, logger=None, config=None):
        self.registry = registry
        self.global_cache = global_cache
        self.logger = logger
        self.config = config

    # ── small helpers ─────────────────────────────────────────────────────────
    def _log(self, level: str, msg: str):
        fn = getattr(self.logger, level, None) if self.logger is not None else None
        if callable(fn):
            try:
                fn(msg)
            except Exception:
                pass

    def _cfg_get(self, key, default=None):
        cfg = self.config
        if cfg is None:
            return default
        getter = getattr(cfg, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                return default
        return default

    def _instance_names(self, service: str):
        insts = self._cfg_get(f"{service}_instances", {}) or {}
        return [k for k, v in insts.items()
                if k != "default_instance" and isinstance(v, dict)]

    def _mgr(self, name: str):
        if not self.registry:
            return None
        try:
            return self.registry.get("manager", name)
        except Exception:
            return None

    # ── gather measured stats across every instance / service ─────────────────
    def _add_stats(self, acc: dict, stats: dict) -> int:
        """Delegate the weighted fold to the pure brain function (sizing/)."""
        return fold_stats(acc, stats)

    def _accumulate_parquet(self, acc, service, mgr_name, rt_col, rt_unit) -> int:
        mgr = self._mgr(mgr_name)
        if mgr is None or not hasattr(mgr, "load"):
            return 0
        total = 0
        for inst in self._instance_names(service):
            try:
                df = mgr.load(inst)
            except Exception as e:
                self._log("log_debug", f"[SizeCal] {service}/{inst} load failed: {e}")
                continue
            total += self._add_stats(acc, size_model.measured_stats(
                df, runtime_col=rt_col, runtime_unit=rt_unit, codec_col="video_codec"))
        return total

    @staticmethod
    def _movie_runtime_min(m: dict):
        """Delegate to the pure brain function (sizing/size_calibration)."""
        return movie_runtime_min(m)

    def _accumulate_radarr_snapshot(self, acc) -> int:
        """Measure movies from the run's warm GET /movie snapshot already in
        global_cache (``radarr.movies.{instance}.full``) — no extra API call.
        Used when no Radarr movie Parquet is built, so movie tiers still
        calibrate automatically every run."""
        if not self.global_cache:
            return 0
        try:
            import pandas as pd
        except Exception:
            return 0
        total = 0
        for inst in self._instance_names("radarr"):
            try:
                movies = self.global_cache.get(f"radarr.movies.{inst}.full") or []
            except Exception:
                movies = []
            rows = []
            for m in movies:
                mf = m.get("movieFile") or {}
                size  = mf.get("size")
                qname = ((mf.get("quality") or {}).get("quality") or {}).get("name")
                vcodec = (mf.get("mediaInfo") or {}).get("videoCodec")
                rt    = self._movie_runtime_min(m)
                if size and qname and rt:
                    rows.append({"size_bytes": size, "runtime_minutes": rt,
                                 "quality_name": qname, "video_codec": vcodec})
            if rows:
                total += self._add_stats(acc, size_model.measured_stats(
                    pd.DataFrame(rows), runtime_col="runtime_minutes", runtime_unit="minutes",
                    codec_col="video_codec"))
        if total:
            self._log("log_debug", f"[SizeCal] radarr snapshot: {total} movie file(s) measured.")
        return total

    def _gather(self) -> dict:
        """Return ``{quality: {"wsum": Σ(mean·n), "n": Σn}}`` across all caches."""
        acc: dict = {}
        # Sonarr episodes — the Parquet cache (no cheap full-library alternative;
        # it's a pilot+watched SUBSET, so TV tiers carry a mild watch bias).
        self._accumulate_parquet(acc, "sonarr", "SonarrCacheEpisodeFilesManager",
                                 "runtime_seconds", "seconds")
        # Radarr movies — the warm full GET /movie snapshot already in global_cache
        # (FULL library, unbiased, no extra API call). Preferred over the
        # movie_files Parquet, which is only a franchise+watched SUBSET. The
        # Parquet is a fallback for when the snapshot is somehow unavailable.
        if self._accumulate_radarr_snapshot(acc) == 0:
            self._accumulate_parquet(acc, "radarr", "RadarrCacheMovieFilesManager",
                                     "runtime_minutes", "minutes")
        return acc

    # ── public API ────────────────────────────────────────────────────────────
    def load_into_model(self) -> bool:
        """Install the last persisted calibration into size_model (warm start)."""
        payload = None
        if self.global_cache:
            try:
                payload = self.global_cache.get(self.CACHE_KEY)
            except Exception:
                payload = None
        table = payload.get("table") if isinstance(payload, dict) else None
        if table:
            n = size_model.set_calibration(table)
            self._log("log_debug", f"[SizeCal] warm-loaded {n} calibrated tier(s) from cache.")
            return bool(n)
        return False

    def _is_fresh(self, payload) -> bool:
        return calibration_is_fresh(payload, self.MAX_AGE_DAYS)

    def refresh(self, force: bool = False) -> dict:
        """Recompute the overlay from the live caches if stale; else reuse cache.
        Always (re)installs an overlay when one is available. Returns the table."""
        existing = None
        if self.global_cache:
            try:
                existing = self.global_cache.get(self.CACHE_KEY)
            except Exception:
                existing = None

        if (not force and isinstance(existing, dict)
                and self._is_fresh(existing) and existing.get("table")):
            size_model.set_calibration(existing["table"])
            self._log("log_debug", "[SizeCal] calibration still fresh — reused cached table.")
            return existing["table"]

        acc = self._gather()
        table = compute_calibration_table(acc, self.MIN_SAMPLES)
        if not table:
            self._log("log_debug",
                      "[SizeCal] no measurable library data — keeping static table.")
            # Reuse a stale cache if present rather than nothing.
            if isinstance(existing, dict) and existing.get("table"):
                size_model.set_calibration(existing["table"])
                return existing["table"]
            return {}

        size_model.set_calibration(table)
        counts = {q: a["n"] for q, a in acc.items()}
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "table": table,
            "counts": counts,
        }
        if self.global_cache:
            try:
                self.global_cache.set_json(self.CACHE_KEY, payload)
            except Exception as e:
                self._log("log_warning", f"[SizeCal] persist failed: {e}")
        self._log("log_info",
                  f"[SizeCal] calibrated {len(table)} quality tier(s) from "
                  f"{sum(counts.values())} library file(s).")
        return table
