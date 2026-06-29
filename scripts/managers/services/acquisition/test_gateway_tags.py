"""ArrGateway tag access — read tag definitions and ensure-by-label (create if missing). Tag ids are
per-instance, so this is the primitive the cross-instance move uses to carry tags across by label."""
from __future__ import annotations

from scripts.managers.services.acquisition.gateway import ArrGateway


class _Im:
    def __init__(self):
        self._tags = {"ultra": [{"id": 2, "label": "anime"}]}
        self.posts = []

    def resolve_instance(self, inst):
        return inst

    def _make_request(self, inst, endpoint, method="GET", payload=None, fallback=None):
        if endpoint == "tag" and method == "GET":
            return list(self._tags.get(inst, []))
        if endpoint == "tag" and method == "POST":
            rec = {"id": 50 + len(self.posts), "label": payload["label"]}
            self._tags.setdefault(inst, []).append(rec)
            self.posts.append((inst, payload["label"]))
            return rec
        return fallback


def _gw(im=None):
    return ArrGateway("radarr", im or _Im(), {}, None)


def test_ensure_tag_returns_existing_id_case_insensitive():
    gw = _gw()
    assert gw.ensure_tag("ultra", "anime") == 2
    assert gw.ensure_tag("ultra", "ANIME") == 2          # label match is case-insensitive


def test_ensure_tag_creates_missing_then_caches():
    im = _Im()
    gw = _gw(im)
    tid = gw.ensure_tag("ultra", "keep-universe-mcu")
    assert tid == 50 and ("ultra", "keep-universe-mcu") in im.posts
    # a second ensure for the same label finds the just-created tag — no duplicate POST
    assert gw.ensure_tag("ultra", "keep-universe-mcu") == 50
    assert len(im.posts) == 1


def test_tags_lists_definitions():
    gw = _gw()
    assert {t["label"] for t in gw.tags("ultra")} == {"anime"}
