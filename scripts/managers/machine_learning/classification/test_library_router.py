"""Tests for library_router — the pure routing-pref application + owned-library move planner
shared by the add-time resolver and the in-run re-organizer."""
from __future__ import annotations

from scripts.managers.machine_learning.classification.library_router import (
    route_category, target_folder, plan_moves,
)

_ROUTING = {
    "movies": {"kids_bucket_enabled": True, "anime_policy": "dedicated"},
    "tv": {"anime_policy": "series_type_plus_folder", "kids_bucket_enabled": True},
}
_MRF = {"standard": "/m/std", "kids": "/m/kids", "anime": "/m/anime"}
_RF = {"series": "/t/series", "anime": "/t/anime", "kids": "/t/kids"}


# ── route_category ────────────────────────────────────────────────────────────
def test_route_category_identity_when_buckets_on():
    assert route_category("kids", False, _ROUTING) == "kids"
    assert route_category("anime", False, _ROUTING) == "anime"
    assert route_category("anime", True, _ROUTING) == "anime"
    assert route_category("kids", True, _ROUTING) == "kids"


def test_route_category_redirects_when_off():
    r = {"movies": {"kids_bucket_enabled": False, "anime_policy": "standard_only"},
         "tv": {"anime_policy": "series_type", "kids_bucket_enabled": False}}
    assert route_category("kids", False, r) == "standard"
    assert route_category("anime", False, r) == "standard"
    assert route_category("anime", True, r) == "series"
    assert route_category("kids", True, r) == "series"
    assert route_category("reality", True, r) == "reality"      # untouched bucket


# ── target_folder ─────────────────────────────────────────────────────────────
def test_target_folder_category_then_default():
    assert target_folder("kids", False, _RF, _MRF) == "/m/kids"
    assert target_folder("4k", False, _RF, _MRF) == "/m/std"            # no 4k folder → standard
    assert target_folder("anime", True, _RF, _MRF) == "/t/anime"
    assert target_folder("documentary", True, _RF, _MRF) == "/t/series"  # no doc folder → series


# ── plan_moves ────────────────────────────────────────────────────────────────
def test_plan_moves_emits_move_when_misplaced():
    items = [{"id": 1, "title": "KidFlick", "rootFolderPath": "/m/std"},    # kids title sitting in standard
             {"id": 2, "title": "Already", "rootFolderPath": "/m/kids"}]     # already correct
    plans = plan_moves(items, is_show=False, routing=_ROUTING, root_folders=_RF,
                       movie_root_folders=_MRF, classify=lambda it: "kids")
    assert len(plans) == 1
    assert plans[0]["id"] == 1 and plans[0]["target_root"] == "/m/kids" and plans[0]["current_root"] == "/m/std"


def test_plan_moves_honours_routing_redirect():
    # kids bucket OFF → a kids title's target is standard → already there → no move
    r = {"movies": {"kids_bucket_enabled": False}, "tv": {}}
    items = [{"id": 1, "title": "X", "rootFolderPath": "/m/std"}]
    plans = plan_moves(items, is_show=False, routing=r, root_folders=_RF,
                       movie_root_folders=_MRF, classify=lambda it: "kids")
    assert plans == []


def test_plan_moves_show_seriestype_fix_without_move():
    items = [{"id": 5, "title": "Naruto", "rootFolderPath": "/t/anime", "seriesType": "standard"}]
    plans = plan_moves(items, is_show=True, routing=_ROUTING, root_folders=_RF,
                       movie_root_folders=_MRF, classify=lambda it: "anime",
                       anime_media=lambda it: True)
    assert len(plans) == 1
    assert plans[0]["target_root"] is None and plans[0]["new_series_type"] == "anime"


def test_plan_moves_show_move_and_type():
    items = [{"id": 6, "title": "Bleach", "rootFolderPath": "/t/series", "seriesType": "standard"}]
    plans = plan_moves(items, is_show=True, routing=_ROUTING, root_folders=_RF,
                       movie_root_folders=_MRF, classify=lambda it: "anime",
                       anime_media=lambda it: True)
    assert plans[0]["target_root"] == "/t/anime" and plans[0]["new_series_type"] == "anime"


def test_plan_moves_normalises_trailing_slash_and_case():
    items = [{"id": 7, "title": "Y", "rootFolderPath": "/M/Kids/"}]       # same folder, different case/slash
    plans = plan_moves(items, is_show=False, routing=_ROUTING, root_folders=_RF,
                       movie_root_folders={"standard": "/m/std", "kids": "/m/kids"},
                       classify=lambda it: "kids")
    assert plans == []                                                    # treated as already in place
