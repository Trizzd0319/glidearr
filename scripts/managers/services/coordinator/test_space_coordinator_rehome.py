"""FORK-D unit tests: the watchability->standard-profile resolver (_rehome_target_profile)
and _instance_profiles. Proves the hard sub-4K cap (a rehome can never re-grab 2160p),
validation against the instance's REAL profiles, the walk-down to the best present tier,
the HD-720p floor, and the fail-safe (no resolvable profile -> None -> rehome skipped)."""
from __future__ import annotations

from scripts.managers.services.coordinator.space_coordinator import SpaceCoordinatorManager as C


class _Api:
    def __init__(self, profiles): self._p = profiles            # list of {id, name}
    def _make_request(self, inst, endpoint, fallback=None, **k):
        return self._p if endpoint == "qualityprofile" else fallback


class _RadarrSP:
    def __init__(self, profiles): self.radarr_api = _Api(profiles)


def _coord(profiles, *, floor="HD-720p"):
    c = C.__new__(C)
    c.config = {"routing": {"movies": {"rehome_floor_profile": floor}}}
    return c, _RadarrSP(profiles)


# Default Radarr ladder ids: 3 HD-720p, 4 HD-1080p(WEB), 7 HD Bluray+WEB, 8 Remux+WEB-1080p;
# 5/10/9 are the 4K trio a rehome must NEVER select.
_FULL = [{"id": 3, "name": "HD-720p"}, {"id": 4, "name": "HD-1080p"},
         {"id": 7, "name": "HD Bluray+WEB"}, {"id": 8, "name": "Remux+WEB-1080p"},
         {"id": 5, "name": "Ultra-HD"}, {"id": 9, "name": "Remux-2160p"}]

# watch_count 4 + watched → engagement floor ~90 → would EARN a 4K tier (id 9) without the cap.
_HIGH = {"watch_count": 4, "is_watched": True, "completion_pct": 100, "watchability_score": 40}
# untouched + zero affinity → sticky cold 720p floor.
_LOW = {"watch_count": 0, "is_watched": False, "completion_pct": 0, "watchability_score": 0}


def test_high_likelihood_capped_below_4k():
    c, sp = _coord(_FULL)
    pid = c._rehome_target_profile(_HIGH, sp, "standard")
    assert pid not in (5, 10, 9)        # NEVER a 4K profile (INV-5)
    assert pid == 8                     # capped to the 1080p ceiling (present on the instance)


def test_earned_absent_walks_down_to_present():
    # Only 720p + 1080p present; the capped earner (id8) is absent → walk down to id4.
    c, sp = _coord([{"id": 3, "name": "HD-720p"}, {"id": 4, "name": "HD-1080p"}])
    assert c._rehome_target_profile(_HIGH, sp, "standard") == 4


def test_low_likelihood_lands_at_720p():
    c, sp = _coord(_FULL)
    assert c._rehome_target_profile(_LOW, sp, "standard") == 3


def test_floor_lifts_cold_film():
    # A higher floor (HD-1080p) lifts a cold film that would otherwise land at 720p.
    c, sp = _coord(_FULL, floor="HD-1080p")
    assert c._rehome_target_profile(_LOW, sp, "standard") == 4


def test_no_profiles_returns_none():
    c, sp = _coord([])
    assert c._rehome_target_profile(_HIGH, sp, "standard") is None


def test_misconfigured_4k_floor_never_leaks_4k():
    # rehome_floor_profile names a 4K-tier profile ('Ultra-HD'). The hard cap must treat it as
    # 'no valid floor' and still pick a SUB-4K tier — never a 2160p id (INV-5, fallback path).
    c, sp = _coord(_FULL, floor="Ultra-HD")
    pid = c._rehome_target_profile(_HIGH, sp, "standard")
    assert pid not in (5, 9, 10)
    assert pid == 8                       # capped to the 1080p ceiling, floor ignored


def test_only_4k_profiles_returns_none():
    # The instance offers ONLY 4K-tier profiles → no valid sub-4K target → None (keep the 4K).
    c, sp = _coord([{"id": 5, "name": "Ultra-HD"}, {"id": 9, "name": "Remux-2160p"}])
    assert c._rehome_target_profile(_HIGH, sp, "standard") is None
