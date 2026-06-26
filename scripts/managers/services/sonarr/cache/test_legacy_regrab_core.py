"""run_legacy_regrab core — one interactive search + grab-by-guid per owned legacy file; the cooldown
ledger doubles as the resume checkpoint (written incrementally on live grabs/no-release, never in dry-run)."""
from __future__ import annotations

from scripts.managers.services.sonarr.cache.legacy_regrab import ledger_key, run_legacy_regrab


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _Api:
    def __init__(self, releases, eps):
        self.releases = releases
        self.eps = eps
        self.grabs = []

    def _make_request(self, instance, ep, method="GET", payload=None, fallback=None):
        if ep == "release" and method == "POST":
            self.grabs.append(payload)
            return {"id": 1}
        if ep.startswith("episode?seriesId="):
            return self.eps.get(int(ep.split("=")[1]), [])
        if ep.startswith("release?episodeId="):
            return self.releases.get(int(ep.split("=")[1]), [])
        return fallback


def _rel(title, res, guid="g"):
    return {"title": title, "quality": {"quality": {"resolution": res}},
            "rejected": False, "guid": guid, "indexerId": 7}


_ITEMS = [{"series_id": 1, "episode_file_id": 11, "resolution": 480, "series_title": "Show",
           "season_number": 1, "episode_number": 1, "video_codec": "XviD"}]
_EPS = {1: [{"id": 101, "episodeFileId": 11, "seasonNumber": 1, "episodeNumber": 1}]}


def test_core_grabs_and_writes_ledger():
    api = _Api({101: [_rel("Show.S01E01.480p.x264", 480, "gg")]}, _EPS)
    cache = _Cache()
    out = run_legacy_regrab(make_request=api._make_request, logger=_Log(), global_cache=cache,
                            instance="standard", items=_ITEMS, max_workers=1, dry_run=False)
    assert out["grabbed"] == 1 and api.grabs == [{"guid": "gg", "indexerId": 7}]
    assert cache.d[ledger_key("standard")]["11"]["result"] == "grabbed"


def test_core_dry_run_previews_without_writes():
    api = _Api({101: [_rel("Show.S01E01.480p.x264", 480, "gg")]}, _EPS)
    cache = _Cache()
    out = run_legacy_regrab(make_request=api._make_request, logger=_Log(), global_cache=cache,
                            instance="standard", items=_ITEMS, max_workers=1, dry_run=True)
    assert out["previewed"] == 1 and api.grabs == []
    assert ledger_key("standard") not in cache.d                 # cooldown not burned in dry-run
    assert out["preview"][0][0].startswith("Show S01E01")


def test_core_no_modern_records_no_release():
    api = _Api({101: [_rel("Show.S01E01.XviD", 480)]}, _EPS)      # only legacy available
    cache = _Cache()
    out = run_legacy_regrab(make_request=api._make_request, logger=_Log(), global_cache=cache,
                            instance="standard", items=_ITEMS, max_workers=1, dry_run=False)
    assert out["no_release"] == 1 and api.grabs == []
    assert cache.d[ledger_key("standard")]["11"]["result"] == "no_release"


def test_core_concurrent_grabs_all():
    items = [{"series_id": 1, "episode_file_id": i, "resolution": 480, "series_title": "Show",
              "season_number": 1, "episode_number": i, "video_codec": "XviD"} for i in (11, 12, 13)]
    eps = {1: [{"id": 100 + i, "episodeFileId": i, "seasonNumber": 1, "episodeNumber": i} for i in (11, 12, 13)]}
    rel = {100 + i: [_rel(f"Show.S01E{i}.480p.x264", 480, f"g{i}")] for i in (11, 12, 13)}
    api = _Api(rel, eps)
    cache = _Cache()
    out = run_legacy_regrab(make_request=api._make_request, logger=_Log(), global_cache=cache,
                            instance="standard", items=items, max_workers=3, dry_run=False)
    assert out["grabbed"] == 3 and out["checked"] == 3 and len(api.grabs) == 3
