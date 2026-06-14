"""Regression test: the Sonarr stay-ahead prefetch (_do_acquire_next_episodes) gates on
the space band TOP U (= free_space_limit + headroom, or 25% of the total drive when unset)
— NOT the floor T and NOT the old hardcoded MIN_FREE_SPACE_GB=50. Prefetch is a
'consume space' op, so it pauses across the whole pressure band [T, U): once free dips
below U, the space-pressure downgrade/delete passes own that band and prefetch waits until
free recovers above U.

Drives the REAL _do_acquire_next_episodes via a stub manager (object.__new__). A pending
episode that's gated leaves stats checked == pending, triggered == 0.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


def _mk(config, free_gb, total_gb):
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = config
    m.logger = _Logger()
    m._get_free_space_gb = lambda instance: free_gb         # shadow heavy methods
    m._get_total_space_gb = lambda instance: total_gb
    return m


def _pending_df(n=2):
    return pd.DataFrame({
        "next_episode": [True] * n,
        "episode_file_id": [np.nan] * n,   # not yet downloaded
    })


def test_skips_acquire_in_pressure_band_gates_at_U_not_T():
    # free_space_limit=2500 -> T=2500, U=2750. free 2600 is in the band [T, U): under the
    # OLD floor(T) gate it would acquire; gating at the band top U it must SKIP.
    m = _mk({"free_space_limit": 2500}, free_gb=2600.0, total_gb=8000.0)
    stats = m._do_acquire_next_episodes("standard", _pending_df(2))
    assert stats["checked"] == 2
    assert stats["triggered"] == 0


def test_skips_acquire_below_25pct_of_total_when_limit_unset():
    # free 80 GB, total 8000 GB -> band top 2000 GB (no headroom on the fallback) -> SKIP
    # (old 50 GB floor would proceed).
    m = _mk({}, free_gb=80.0, total_gb=8000.0)
    stats = m._do_acquire_next_episodes("standard", _pending_df(2))
    assert stats["checked"] == 2
    assert stats["triggered"] == 0


def test_last_resort_constant_when_total_unreadable():
    # No free_space_limit AND total 0 (helper returns 0.0 on failure) -> last-resort
    # MIN_FREE_SPACE_GB. free 40 GB < 50 -> skip; free 60 GB >= 50 would proceed.
    m = _mk({}, free_gb=40.0, total_gb=0.0)
    stats = m._do_acquire_next_episodes("standard", _pending_df(3))
    assert stats["checked"] == 3
    assert stats["triggered"] == 0
