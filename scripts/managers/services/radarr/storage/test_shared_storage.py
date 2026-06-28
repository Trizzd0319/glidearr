"""Tests for the shared-storage pre-flight probe — confirms a cross-instance MOVE only when the two
instances evidence ONE backing mount (common root-folder ancestor + equal disk capacity), and
FAILS CLOSED (degrade-to-log) on any disjoint path, mismatched capacity, or unreadable signal."""
from __future__ import annotations

from scripts.managers.services.radarr.storage.shared_storage import shared_storage_confirmed


class _Im:
    def __init__(self, totals): self._t = totals          # {inst: total_gb}
    def disk_total_gb(self, inst): return self._t[inst]


class _Gw:
    def __init__(self, roots, totals):
        self._roots = roots                               # {inst: [path, ...]}
        self.im = _Im(totals)
    def root_folders(self, inst):
        return [{"path": p} for p in self._roots.get(inst, [])]


def _gw(*, std_root="/data/media/movies/standard", uhd_root="/data/media/movies/4k",
        std_total=10000.0, uhd_total=10000.0):
    return _Gw({"standard": [std_root], "ultra": [uhd_root]},
               {"standard": std_total, "ultra": uhd_total})


def test_confirmed_on_common_ancestor_and_equal_capacity():
    ok, reason = shared_storage_confirmed(_gw(), "standard", "ultra")
    assert ok is True
    assert "shared mount confirmed" in reason


def test_not_confirmed_on_disjoint_roots():
    gw = _gw(std_root="/mnt/disk1/movies", uhd_root="/mnt/disk2/movies")
    ok, reason = shared_storage_confirmed(gw, "standard", "ultra")
    assert ok is False
    assert "common mount ancestor" in reason


def test_not_confirmed_on_mismatched_capacity():
    # same path tree but different total → two same-shaped but separate disks
    gw = _gw(std_total=10000.0, uhd_total=4000.0)
    ok, reason = shared_storage_confirmed(gw, "standard", "ultra")
    assert ok is False
    assert "capacity differs" in reason


def test_capacity_within_tolerance_still_confirms():
    # rounding noise (a few GB on a 10 TB mount) must not break confirmation
    gw = _gw(std_total=10000.0, uhd_total=10000.5)
    ok, _ = shared_storage_confirmed(gw, "standard", "ultra")
    assert ok is True


def test_fail_closed_on_missing_root():
    gw = _Gw({"standard": ["/data/media/movies/standard"], "ultra": []},
             {"standard": 10000.0, "ultra": 10000.0})
    ok, reason = shared_storage_confirmed(gw, "standard", "ultra")
    assert ok is False
    assert "no root folder" in reason


def test_fail_closed_on_unreadable_capacity():
    class _BadIm:
        def disk_total_gb(self, inst): raise RuntimeError("boom")
    gw = _gw()
    gw.im = _BadIm()
    ok, reason = shared_storage_confirmed(gw, "standard", "ultra")
    assert ok is False
    assert "unreadable" in reason


def test_fail_closed_on_infinite_capacity():
    # disk_free/total return inf on an unreadable mount in this codebase → must NOT confirm
    gw = _gw(std_total=float("inf"), uhd_total=float("inf"))
    ok, _ = shared_storage_confirmed(gw, "standard", "ultra")
    assert ok is False


def test_shallow_common_root_is_not_enough():
    # only "/" (or a single segment) in common → too weak to call a shared mount
    gw = _gw(std_root="/data1/movies", uhd_root="/data2/movies")
    ok, _ = shared_storage_confirmed(gw, "standard", "ultra")
    assert ok is False
