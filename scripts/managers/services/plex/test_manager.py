"""Integration smoke for the real PlexManager: construct → prepare → run, exercising
split_components, init_args threading, the scope gate, and run_stats aggregation with
a fake PlexAPI. Also covers the self-disable (no token) and scope-fail (owner-only)
degrade paths — Plex is NON-critical and must never abort the run."""
from __future__ import annotations

import pytest

import scripts.managers.services.plex as plex_pkg
from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.registry import RegistryManager
from scripts.managers.services.plex import PlexManager
from scripts.support.utilities.logger.logger import LoggerManager


# ── fakes ──────────────────────────────────────────────────────────────────────
class _FakeConfig:
    def __init__(self, data): self.raw_data = dict(data); self.version = "test"
    def get(self, key, default=None): return self.raw_data.get(key, default)
    def set(self, key, val): self.raw_data[key] = val


class _FakeCache:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v
    def delete(self, k): self.d.pop(k, None); return True
    def json_exists(self, k): return k in self.d


class _FakePlexAPI:
    """Drop-in for PlexAPI with canned responses. ``scope_ok`` toggles the gate."""
    def __init__(self, logger=None, instance_config=None, client_identifier=None, **kw):
        cfg = instance_config or {}
        self.token = cfg.get("plex_token") or ""
        self.base_url = "http://plex.test:32400"
        self.client_identifier = client_identifier or "cid"
        self.calls_made = 0
        self.scope_ok = True
        self.watchlists = {
            "ADMIN": [_wl_item("Dune", tmdb=438631)],
            "minted-ub": [_wl_item("Dune", tmdb=438631), _wl_item("Bluey", tvdb=355554, typ="show")],
        }
    @property
    def configured(self): return bool(self.token)
    def get_identity(self, fallback=None):
        self.calls_made += 1
        return {"MediaContainer": {"version": "1.40.0"}}
    def get_account(self, fallback=None):
        self.calls_made += 1
        return {"username": "owner"} if self.scope_ok else None
    def get_home_users(self, fallback=None):
        return {"users": [
            {"uuid": "ua", "id": 11, "title": "Rob", "email": "rob@x.io", "admin": True},
            {"uuid": "ub", "id": 22, "title": "Kid", "restricted": True},
        ]}
    def switch_home_user(self, uuid, pin=None, fallback=None):
        return {"authToken": f"minted-{uuid}"}
    def get_watchlist(self, token, start=0, size=100, fallback=None):
        if start > 0:
            return {"MediaContainer": {"totalSize": 0, "Metadata": []}}
        items = self.watchlists.get(token, [])
        return {"MediaContainer": {"totalSize": len(items), "Metadata": items}}
    def get_sections(self, fallback=None): return {"MediaContainer": {"Directory": []}}
    def get_sessions(self, fallback=None): return {"MediaContainer": {"size": 0}}


def _wl_item(title, tmdb=None, tvdb=None, typ="movie"):
    guids = []
    if tmdb: guids.append({"id": f"tmdb://{tmdb}"})
    if tvdb: guids.append({"id": f"tvdb://{tvdb}"})
    return {"ratingKey": title, "title": title, "year": 2021, "type": typ,
            "guid": "plex://x", "Guid": guids}


class _FakeTautulli:
    class users:
        @staticmethod
        def get_all_users():
            return [{"user_id": 11, "username": "rob", "email": "rob@x.io"},
                    {"user_id": 22, "username": "kid"}]


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch):
    # PlexManager + submanagers are (cls, None) singletons — save/restore the global
    # registries so each test constructs fresh ones without polluting the suite.
    saved_i = dict(BaseManager._instances)
    saved_s = dict(BaseManager._singleton_instances)
    BaseManager._instances.clear()
    BaseManager._singleton_instances.clear()
    monkeypatch.setattr(plex_pkg, "PlexAPI", _FakePlexAPI)
    yield
    BaseManager._instances.clear(); BaseManager._instances.update(saved_i)
    BaseManager._singleton_instances.clear(); BaseManager._singleton_instances.update(saved_s)


def _build(plex_cfg):
    reg = RegistryManager()
    reg.register("manager", "TautulliManager", _FakeTautulli())
    cfg = _FakeConfig({"plex": plex_cfg, "rating_groups": {"household": {}},
                       "radarr_instances": {}, "sonarr_instances": {}})
    cache = _FakeCache()
    mgr = PlexManager(logger=LoggerManager(), config=cfg, global_cache=cache,
                      validator=None, registry=reg)
    mgr.prepare()
    return mgr, cache


# ── full inventory pass ────────────────────────────────────────────────────────
def test_run_full_inventory_writes_all_caches():
    mgr, cache = _build({"plex_token": "ADMIN", "url": "plex.test", "port": 32400})
    assert mgr.enabled is True
    mgr.run()

    stats = cache.get("plex/run_stats")
    assert stats["enabled"] and stats["scope_ok"]
    assert stats["pms_version"] == "1.40.0"
    assert stats["users_tracked"] == 2
    assert stats["watchlist_items"] == 2          # Dune (shared) + Bluey

    # identity crosswalk persisted, PII-minimized
    roster = cache.get("plex/users")
    assert {u["title"] for u in roster} == {"Rob", "Kid"}
    assert "rob@x.io" not in repr(cache.d)        # email never persisted
    idmap = cache.get("plex/identity_map")
    assert idmap["ua"]["tautulli_username"] == "rob"

    # the flagship union, with attribution + resolved ids
    union = cache.get("plex/watchlist/union")
    dune = next(u for u in union if u["title"] == "Dune")
    assert dune["ids"]["tmdb"] == 438631
    assert sorted(dune["watchlisted_by"]) == ["Kid", "Rob"]

    # acquisition reads it warm
    assert mgr.watchlist.acquisition_candidates() == union


def test_client_identifier_persisted_when_absent():
    mgr, cache = _build({"plex_token": "ADMIN"})       # no client_identifier
    assert mgr.client_identifier
    assert mgr.config.get("plex")["client_identifier"] == mgr.client_identifier


# ── self-disable (no token) ─────────────────────────────────────────────────────
def test_disabled_without_token_never_aborts():
    mgr, cache = _build({})                              # no plex_token
    assert mgr.enabled is False
    mgr.run()                                            # must not raise
    stats = cache.get("plex/run_stats")
    assert stats["enabled"] is False and stats["watchlist_items"] == 0
    assert cache.get("plex/watchlist/union") is None


# ── scope-fail degrades to owner-only, watchlist gated off ──────────────────────
def test_scope_fail_degrades_owner_only():
    mgr, cache = _build({"plex_token": "ADMIN"})
    mgr.plex_api.scope_ok = False                        # account probe 401s
    mgr.run()
    stats = cache.get("plex/run_stats")
    assert stats["enabled"] and stats["scope_ok"] is False
    assert mgr.account_scope_ok is False                 # watchlist pass was gated off
    # degrade wrote an empty public roster + cleared identity map (no stale artifacts),
    # not user data under the wrong scope
    assert cache.get("plex/users") == []
    assert cache.get("plex/identity_map") == {}
