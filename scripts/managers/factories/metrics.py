import time
from statistics import mean, stdev
from typing import Optional


class MetricsLogger:
    def __init__(self, logger, name: Optional[str] = "Global"):
        self.logger = logger
        self.name = name
        self.metrics = {
            "cache_hits": 0,
            "cache_misses": 0,
            "refresh_durations": [],
            "refresh_failures": 0,
            "background_tasks": 0,
            "trakt_405_suppressed": 0,
            "warnings": 0,
            "errors": 0,
            "successes": 0,
            "run_start_time": time.time()
        }

    # ────────────────────── CACHE METRICS ──────────────────────

    def log_hit(self):
        self.metrics["cache_hits"] += 1
        self.logger.log_debug(f"📊 Cache HIT → total: {self.metrics['cache_hits']}")

    def log_miss(self):
        self.metrics["cache_misses"] += 1
        self.logger.log_debug(f"📊 Cache MISS → total: {self.metrics['cache_misses']}")

    def log_refresh_duration(self, duration: float):
        self.metrics["refresh_durations"].append(duration)
        self.logger.log_debug(f"⏱️ Cache refresh took {duration:.2f}s")

    def log_refresh_failure(self):
        self.metrics["refresh_failures"] += 1
        self.logger.log_warning(f"❌ Cache refresh failed → total: {self.metrics['refresh_failures']}")

    # ────────────────────── TASK TRACKING ──────────────────────

    def log_background_task_start(self):
        self.metrics["background_tasks"] += 1
        self.logger.log_debug(f"🚀 Background tasks: {self.metrics['background_tasks']}")

    def log_background_task_end(self):
        self.metrics["background_tasks"] = max(0, self.metrics["background_tasks"] - 1)
        self.logger.log_debug(f"🛑 Background tasks: {self.metrics['background_tasks']}")

    # ────────────────────── OTHER METRICS ──────────────────────

    def log_trakt_405_suppressed(self):
        self.metrics["trakt_405_suppressed"] += 1
        self.logger.log_debug(f"🔕 Trakt 405s suppressed: {self.metrics['trakt_405_suppressed']}")

    def log_warning(self, note: Optional[str] = None):
        self.metrics["warnings"] += 1
        if note:
            self.logger.log_warning(f"⚠️ Warning metric triggered: {note}")

    def log_error(self, note: Optional[str] = None):
        self.metrics["errors"] += 1
        if note:
            self.logger.log_error(f"❌ Error metric triggered: {note}")

    def log_success(self):
        self.metrics["successes"] += 1

    # ────────────────────── SUMMARY ──────────────────────

    def summary(self) -> dict:
        durations = self.metrics["refresh_durations"]
        runtime = time.time() - self.metrics["run_start_time"]

        return {
            "name": self.name,
            "runtime_sec": round(runtime, 2),
            "cache_hits": self.metrics["cache_hits"],
            "cache_misses": self.metrics["cache_misses"],
            "refresh_avg_sec": round(mean(durations), 2) if durations else 0.0,
            "refresh_stdev_sec": round(stdev(durations), 2) if len(durations) > 1 else 0.0,
            "refresh_failures": self.metrics["refresh_failures"],
            "background_tasks": self.metrics["background_tasks"],
            "trakt_405_suppressed": self.metrics["trakt_405_suppressed"],
            "warnings": self.metrics["warnings"],
            "errors": self.metrics["errors"],
            "successes": self.metrics["successes"]
        }

    def log_summary(self):
        s = self.summary()
        self.logger.log_info(f"\n📈 {self.name} Metrics Summary:")
        self.logger.log_info(f"⏱️ Runtime: {s['runtime_sec']}s")
        self.logger.log_info(f"📊 Cache Hits/Misses: {s['cache_hits']} / {s['cache_misses']}")
        self.logger.log_info(f"⏲️ Refresh Avg: {s['refresh_avg_sec']}s ± {s['refresh_stdev_sec']}s")
        self.logger.log_info(f"❌ Failures: {s['refresh_failures']} | ⚠️ Warnings: {s['warnings']} | ✅ Successes: {s['successes']}")
        self.logger.log_info(f"🚦 Background Tasks: {s['background_tasks']} | 🔕 Trakt 405s: {s['trakt_405_suppressed']}")
