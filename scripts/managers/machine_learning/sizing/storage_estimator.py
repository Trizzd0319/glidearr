"""
sizing/storage_estimator.py — episode-count / size forecasting.
================================================================================
RELOCATED HERE from ``scripts/managers/machine_learning/storage_estimator.py``
(ML-migration Step 1b). That old path is now a re-export shim. Pure forecasting
on top of the shared ``sizing.size_model`` — no HTTP, no cache writes (the
``cache`` arg is accepted for API compatibility but only read-through).

Live-action MiB/min comes from the shared, library-calibrated size_model; only
the flat animated rate is kept here.
"""
from __future__ import annotations

from scripts.managers.machine_learning.sizing import size_model
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class MLStorageForecaster:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger, cache):
        self.logger = logger
        self.cache = cache
        self.bitrate_profiles = self._load_bitrate_estimates()

    # Flat per-minute rate for animated content. Anime encodes (especially long
    # series) trend far smaller than live-action at the same quality label, so a
    # single conservative rate is used regardless of profile.
    # NOTE: revisit — a flat 5 MiB/min underestimates anime 4K/remux films.
    ANIMATED_MB_PER_MIN = 5.0

    @LoggerManager().log_function_entry
    @timeit("_load_bitrate_estimates")
    def _load_bitrate_estimates(self):
        """
        Live-action MiB/min now comes from the shared, library-calibrated
        size_model; only the animated override is kept here.
        """
        return {"animated": self.ANIMATED_MB_PER_MIN}

    def _bitrate(self, quality_profile, is_animated=False) -> float:
        """Resolve MiB/min for a quality label via the shared size_model
        (or the flat animated rate)."""
        if is_animated:
            return self.ANIMATED_MB_PER_MIN
        return size_model.mb_per_min(quality_profile)

    @LoggerManager().log_function_entry
    @timeit("estimate_episode_count")
    def estimate_episode_count(self, free_space_mb, quality_profile, avg_runtime_min=45):
        """
        Estimates how many episodes can be downloaded based on:
        - Free space
        - Profile bitrate (MiB/min, shared size_model)
        - Average episode runtime
        """
        bitrate = self._bitrate(quality_profile)
        est_size_mb = bitrate * avg_runtime_min

        episodes = free_space_mb // est_size_mb
        self.logger.log_info(
            f"📦 {episodes} episodes can fit in {free_space_mb} MB for profile {quality_profile} (using {bitrate} MB/min)")
        return int(episodes)

    @LoggerManager().log_function_entry
    @timeit("estimate_episode_size")
    def estimate_episode_size(self, quality_profile, avg_runtime_min=45, is_animated=False):
        """
        Returns estimated file size in MB for a given profile and runtime.
        """
        bitrate = self._bitrate(quality_profile, is_animated)
        est_size = bitrate * avg_runtime_min
        self.logger.log_info(f"📏 Estimated size: {est_size} MB for profile {quality_profile} @ {bitrate} MB/min")
        return est_size
