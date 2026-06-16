"""Tests for UhdReconcileManager — the steady-state dual-version mirror (standard 2160p → 4K
instance). Verifies the gates (configured / 4k_policy=='both' / distinct 4K instance / reorg_mode
+ dry_run) and that a 2160p standard-instance movie with no 4K copy is mirrored onto the 4K
instance at its top profile + the 4k root, monitored, search ON. A fake instance-manager serves
movie/qualityprofile/rootfolder GETs per instance and records movie POSTs (adds)."""
from __future__ import annotations

from scripts.managers.services.routing.uhd_reconcile import UhdReconcileManager


def _profile(pid, name, res):
    return {"id": pid, "name": name, "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


_M4K = {"id": 1, "title": "Toy Story", "tmdbId": 862, "year": 1995, "rootFolderPath": "/m/std",
        "movieFile": {"id": 50, "quality": {"quality": {"name": "Bluray-2160p", "resolution": 2160}}}}
_M1080 = {"id": 2, "title": "Up", "tmdbId": 14160, "year": 2009, "rootFolderPath": "/m/std",
          "movieFile": {"id": 51, "quality": {"quality": {"name": "Bluray-1080p", "resolution": 1080}}}}
_UHD_PROFILES = [_profile(1, "HD-1080", 1080), _profile(7, "UHD-Bluray", 2160)]


class _Im:
    """Fake radarr instance-manager: serves movie/qualityprofile/rootfolder GETs per instance,
    records movie POSTs (adds)."""

    def __init__(self, movies, *, profiles=None, roots=None):
        self._movies = movies                                  # {inst: [movie, ...]}
        self._profiles = profiles or {}
        self._roots = roots or {}
        self.adds, self.gets = [], []

    def _get_apis(self):
        return {n: object() for n in self._movies}

    def _make_request(self, name, endpoint, method="GET", payload=None, fallback=None, **kw):
        if method == "POST" and endpoint == "movie":
            self.adds.append((name, payload))
            return {"id": 999}
        self.gets.append((name, endpoint))
        table = {"movie": self._movies, "qualityprofile": self._profiles, "rootfolder": self._roots}
        return table.get(endpoint, {}).get(name, fallback if fallback is not None else [])


class _Mgr:
    def __init__(self, im): self.instance_manager = im


def _cfg(*, reorg="same_instance", policy="both", configured=True, with_4k=True):
    routing = {"reorg_mode": reorg, "movies": {"4k_policy": policy}, "tv": {}}
    if configured:
        routing["configured"] = True
    insts = {"standard": {"url": "s"}, "default_instance": "standard"}
    cat = {}
    if with_4k:
        insts["uhd"] = {"url": "u"}
        cat = {"4K": "uhd"}
    return {"routing": routing, "movieRootFolders": {"standard": "/m/std", "4k": "/m/4k"},
            "radarr_instances": insts, "radarr_instances_categorized": cat}


def _run(cfg, movies, *, dry_run=False, profiles=None):
    im = _Im(movies, profiles=profiles or {"uhd": _UHD_PROFILES}, roots={"uhd": [{"path": "/m/4k"}]})
    UhdReconcileManager(config=cfg, logger=None, radarr=_Mgr(im), dry_run=dry_run).run()
    return im


# ── mirror-up actuation ───────────────────────────────────────────────────────
def test_mirrors_2160p_standard_movie_to_4k_instance():
    im = _run(_cfg(), {"standard": [_M4K, _M1080], "uhd": []})
    assert len(im.adds) == 1
    inst, payload = im.adds[0]
    assert inst == "uhd"
    assert payload["tmdbId"] == 862
    assert payload["qualityProfileId"] == 7                    # the instance's top (2160p) profile
    assert payload["rootFolderPath"] == "/m/4k"
    assert payload["monitored"] is True
    assert payload["addOptions"] == {"searchForMovie": True}   # search ON
    assert "id" not in payload and "movieFile" not in payload  # standard-instance keys stripped


def test_strips_standard_path_so_4k_root_wins():
    # the owned object carries an absolute standard-instance path; Radarr would honour it over
    # rootFolderPath, so it must be stripped (else the 4K copy lands in the standard folder).
    owned = dict(_M4K, path="/m/std/Toy Story (1995)", folderName="Toy Story (1995)")
    im = _run(_cfg(), {"standard": [owned], "uhd": []})
    assert len(im.adds) == 1
    _, payload = im.adds[0]
    assert payload["rootFolderPath"] == "/m/4k"
    assert "path" not in payload and "folderName" not in payload


def test_does_not_mirror_1080p_movie():
    im = _run(_cfg(), {"standard": [_M1080], "uhd": []})
    assert im.adds == []


def test_skips_when_already_in_4k_library():
    im = _run(_cfg(), {"standard": [_M4K], "uhd": [dict(_M4K, id=99)]})
    assert im.adds == []                                       # 4K copy already exists


# ── gates ─────────────────────────────────────────────────────────────────────
def test_log_only_logs_but_does_not_add():
    im = _run(_cfg(reorg="log_only"), {"standard": [_M4K], "uhd": []})
    assert ("standard", "movie") in im.gets                    # it fetched + planned
    assert im.adds == []                                       # but added nothing


def test_dry_run_does_not_add():
    im = _run(_cfg(reorg="same_instance"), {"standard": [_M4K], "uhd": []}, dry_run=True)
    assert im.adds == []


def test_off_mode_does_nothing():
    im = _run(_cfg(reorg="off"), {"standard": [_M4K], "uhd": []})
    assert im.adds == [] and im.gets == []


def test_noop_when_highest_only():
    im = _run(_cfg(policy="highest_only"), {"standard": [_M4K], "uhd": []})
    assert im.adds == [] and im.gets == []


def test_noop_when_not_configured():
    im = _run(_cfg(configured=False), {"standard": [_M4K], "uhd": []})
    assert im.adds == [] and im.gets == []


def test_noop_without_distinct_4k_instance():
    im = _run(_cfg(with_4k=False), {"standard": [_M4K]})
    assert im.adds == []                                       # nowhere to mirror to
