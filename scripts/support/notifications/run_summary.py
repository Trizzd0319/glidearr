"""
RunSummaryCollector
===================
Thread-safe accumulator for per-run statistics across all Glidearr
service managers.  Passed into each manager at run time; they call
``record(section, key, value)`` to contribute their stats.

At the end of ``Main.run()`` the summary is serialised and handed to
``DiscordNotifier.send_run_summary()``.

Schema
------
{
    "run_duration_s":  float,
    "dry_run":         bool,
    "errors":          [str, ...],

    "radarr": {
        "movies_upgraded":    int,
        "movies_downgraded":  int,
        "movies_searched":    int,
        "movies_unmonitored": int,
        "queue_cancelled":    int,
        "space_freed_gb":     float,
        "errors":             int,
    },

    "sonarr": {
        "episodes_acquired":       int,
        "episodes_jit_upgraded":   int,
        "episodes_jit_restored":   int,
        "series_upgraded":         int,
        "queue_cancelled":         int,
        "errors":                  int,
    },

    "tautulli": {
        "history_entries":  int,
        "metadata_indexed": int,
        "users_tracked":    int,
    },

    "trakt": {
        "ratings_added": int,
    },

    "plex": {
        "users":           int,
        "watchlist_items": int,
        "scope_ok":        bool,
        "pin_skipped":     int,
        "calls":           int,
    },
}
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from scripts.support.utilities.logger.logger import LoggerManager

# Strip network topology that must never egress off-network: full URLs and
# bare host:port / IP[:port] addresses lurking in raw error strings.
_URL_RE       = re.compile(r'https?://\S+')
_HOST_PORT_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?')


def _scrub_error(message: str) -> str:
    """Scrub credentials and internal network topology from an error string."""
    s = str(message)
    try:
        s = LoggerManager()._scrub(s)
    except Exception:
        pass
    s = _URL_RE.sub("<redacted-url>", s)
    s = _HOST_PORT_RE.sub("<redacted-host>", s)
    return s


class RunSummaryCollector:
    """Accumulates run statistics from all service managers."""

    _SECTIONS = ("radarr", "sonarr", "tautulli", "trakt", "plex")

    def __init__(self, dry_run: bool = False):
        self._lock       = threading.Lock()
        self._start      = time.monotonic()
        self.dry_run     = dry_run
        self.errors:  list[str] = []
        self._data: dict[str, dict[str, Any]] = {s: {} for s in self._SECTIONS}

    # ── Write API ────────────────────────────────────────────────────────────

    def record(self, section: str, key: str, value: Any):
        """
        Set or increment a counter in *section*.

        If *value* is numeric and the key already exists, the values are
        summed.  Otherwise the key is overwritten.
        """
        if section not in self._data:
            return
        with self._lock:
            existing = self._data[section].get(key)
            if existing is not None and _is_numeric(existing) and _is_numeric(value):
                self._data[section][key] = existing + value
            else:
                self._data[section][key] = value

    def add_error(self, message: str):
        # Scrub credentials and internal host:port/URL topology before storing,
        # so run errors that egress to the Discord webhook cannot leak network
        # internals or raw HTTP error bodies off-network.
        scrubbed = _scrub_error(message)[:300]
        with self._lock:
            self.errors.append(scrubbed)

    def merge(self, section: str, stats: dict):
        """
        Bulk-merge a stats dict (e.g. the return value of a manager method)
        into *section*.  Numeric values are summed.
        """
        for key, val in (stats or {}).items():
            self.record(section, key, val)

    # ── Convenience wrappers ─────────────────────────────────────────────────

    # Radarr
    def radarr_upgraded(self,    n: int = 1): self.record("radarr", "movies_upgraded",    n)
    def radarr_downgraded(self,  n: int = 1): self.record("radarr", "movies_downgraded",  n)
    def radarr_searched(self,    n: int = 1): self.record("radarr", "movies_searched",    n)
    def radarr_unmonitored(self, n: int = 1): self.record("radarr", "movies_unmonitored", n)
    def radarr_queue_cancelled(self, n: int = 1): self.record("radarr", "queue_cancelled", n)
    def radarr_space_freed(self, gb: float):  self.record("radarr", "space_freed_gb",     gb)
    def radarr_error(self,       n: int = 1): self.record("radarr", "errors",             n)

    # Sonarr
    def sonarr_acquired(self,      n: int = 1): self.record("sonarr", "episodes_acquired",      n)
    def sonarr_jit_upgraded(self,  n: int = 1): self.record("sonarr", "episodes_jit_upgraded",  n)
    def sonarr_jit_restored(self,  n: int = 1): self.record("sonarr", "episodes_jit_restored",  n)
    def sonarr_series_upgraded(self, n: int=1): self.record("sonarr", "series_upgraded",        n)
    def sonarr_queue_cancelled(self, n: int=1): self.record("sonarr", "queue_cancelled",        n)
    def sonarr_error(self,         n: int = 1): self.record("sonarr", "errors",                 n)

    # Tautulli
    def tautulli_history(self,   n: int): self.record("tautulli", "history_entries",  n)
    def tautulli_metadata(self,  n: int): self.record("tautulli", "metadata_indexed", n)
    def tautulli_users(self,     n: int): self.record("tautulli", "users_tracked",    n)

    # Trakt
    def trakt_ratings(self,      n: int): self.record("trakt", "ratings_added", n)

    # ── Read API ─────────────────────────────────────────────────────────────

    def build(self) -> dict:
        """Return the final summary dict, ready for DiscordNotifier."""
        with self._lock:
            return {
                "run_duration_s": round(time.monotonic() - self._start, 1),
                "dry_run":        self.dry_run,
                "errors":         list(self.errors),
                **{s: dict(self._data[s]) for s in self._SECTIONS},
            }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
