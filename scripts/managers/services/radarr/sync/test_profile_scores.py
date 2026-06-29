"""Cross-instance custom-format score sync: name-keyed reads, the diff planner, and the gated
round-trip apply. Proves it keys by NAME (not per-instance id), fills only unset scores by default,
never clobbers a tuned score without overwrite+consent, honours dry-run + the enable flag, syncs
missing definitions, preserves the rest of the profile on PUT, and is idempotent."""
from __future__ import annotations

from scripts.managers.services.radarr.sync.custom_formats import RadarrSyncCustomFormatsManager
from scripts.managers.services.radarr.sync.profile_scores import (
    RadarrSyncProfileScoresManager, cf_sync_overwrite_consented,
)


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_grid(self, *a, **k): pass


class _Api:
    """Fake radarr_api serving customformat + qualityprofile per instance, recording PUT/POST."""
    def __init__(self, cfs, profiles):
        self._cfs = cfs
        self._profiles = profiles
        self.puts, self.posts = [], []

    def resolve_instance(self, i): return i

    def _make_request(self, inst, endpoint, method="GET", payload=None, fallback=None):
        if endpoint == "customformat" and method == "GET":
            return [dict(c) for c in self._cfs.get(inst, [])]
        if endpoint == "qualityprofile" and method == "GET":
            return [dict(p, formatItems=[dict(fi) for fi in p.get("formatItems", [])])
                    for p in self._profiles.get(inst, [])]
        if endpoint == "customformat" and method == "POST":
            self.posts.append((inst, payload))
            rec = {"id": 900 + len(self.posts), **payload}
            self._cfs.setdefault(inst, []).append(rec)
            return rec
        if endpoint.startswith("qualityprofile/") and method == "PUT":
            self.puts.append((inst, payload))
            lst = self._profiles.get(inst, [])           # persist so a re-read reflects the change
            for idx, p in enumerate(lst):
                if p.get("id") == payload.get("id"):
                    lst[idx] = payload
                    break
            return payload
        return fallback


def _data():
    # standard (source) and ultra (target) with DIFFERENT cf ids but matching names; ultra MISSING 'LQ'
    cfs = {"standard": [{"id": 1, "name": "x265"}, {"id": 2, "name": "BR-DISK"}, {"id": 3, "name": "LQ"}],
           "ultra":    [{"id": 10, "name": "x265"}, {"id": 11, "name": "BR-DISK"}]}
    profiles = {
        "standard": [{"id": 1, "name": "HD-1080p", "formatItems": [
            {"format": 1, "name": "x265", "score": 5}, {"format": 2, "name": "BR-DISK", "score": -50},
            {"format": 3, "name": "LQ", "score": -30}]}],
        "ultra": [{"id": 20, "name": "Remux-2160p", "cutoff": 1, "items": [{"x": 1}], "formatItems": [
            {"format": 10, "name": "x265", "score": 0}, {"format": 11, "name": "BR-DISK", "score": -50}]}],
    }
    return cfs, profiles


def _cfg(*, enabled=True, overwrite=False, consent=False, include_test=True, ref=None,
         instances=("standard", "ultra")):
    c = {"scoring": {"cf_sync": {"enabled": enabled, "source_instance": "standard",
                                 "reference_profile": ref, "overwrite_existing": overwrite,
                                 "include_test": include_test}},
         "radarr_instances": {"default_instance": {"name": "standard"}, **{i: {} for i in instances}}}
    if consent:
        c["cf_sync_overwrite_consent"] = True
    return c


def _build(cfg, cfs=None, profiles=None, dry_run=False):
    if cfs is None or profiles is None:
        cfs, profiles = _data()
    api, cache = _Api(cfs, profiles), _Cache()
    cf = RadarrSyncCustomFormatsManager.__new__(RadarrSyncCustomFormatsManager)
    cf.radarr_api, cf.global_cache, cf.instance_manager = api, cache, None
    cf.dry_run, cf.logger = dry_run, _Logger()
    ps = RadarrSyncProfileScoresManager.__new__(RadarrSyncProfileScoresManager)
    ps.config, ps.radarr_api, ps.global_cache, ps.instance_manager = cfg, api, cache, None
    ps.dry_run, ps.logger = dry_run, _Logger()
    ps._parent = type("_P", (), {"custom_formats": cf})()
    return ps, api


# ── read helpers (real custom_formats methods, name-keyed) ──────────────────────
def test_read_profile_scores_by_name_is_name_keyed():
    ps, _ = _build(_cfg())
    cf = ps._cf()
    std = cf.read_profile_scores_by_name("standard")
    assert std["HD-1080p"] == {"x265": 5, "BR-DISK": -50, "LQ": -30}
    # ids differ on ultra (10/11) but names resolve identically
    assert cf.cf_name_to_id("ultra") == {"x265": 10, "br-disk": 11}


# ── planner ─────────────────────────────────────────────────────────────────────
def test_plan_fill_noop_and_definition_missing():
    ps, _ = _build(_cfg())
    rows = {(r["cf"], r["action"]) for r in ps.plan_score_sync() if r["instance"] == "ultra"}
    assert ("x265", "fill") in rows           # ultra x265 unset (0) -> fill to 5
    assert ("BR-DISK", "noop") in rows        # already -50 == canonical
    assert ("LQ", "definition-missing") in rows   # CF absent on ultra


def test_plan_conflict_when_score_differs_fill_only():
    cfs, profiles = _data()
    profiles["ultra"][0]["formatItems"][0]["score"] = 3   # ultra x265 already 3 (!= canonical 5)
    ps, _ = _build(_cfg(), cfs, profiles)
    r = next(r for r in ps.plan_score_sync() if r["cf"] == "x265")
    assert r["action"] == "skip-conflict"                 # fill-only never clobbers a tuned score


def test_plan_conflict_becomes_overwrite_with_flag_and_consent():
    cfs, profiles = _data()
    profiles["ultra"][0]["formatItems"][0]["score"] = 3
    ps, _ = _build(_cfg(overwrite=True, consent=True), cfs, profiles)
    r = next(r for r in ps.plan_score_sync() if r["cf"] == "x265")
    assert r["action"] == "overwrite"


# ── apply ───────────────────────────────────────────────────────────────────────
def test_apply_fill_only_sets_unset_and_preserves_rest():
    ps, api = _build(_cfg())
    stats = ps.apply_score_sync()
    assert stats["filled"] == 1 and stats["profiles_put"] == 1
    inst, payload = api.puts[0]
    assert inst == "ultra" and payload["id"] == 20
    fmt = {fi["name"]: fi["score"] for fi in payload["formatItems"]}
    assert fmt["x265"] == 5 and fmt["BR-DISK"] == -50      # filled x265, BR-DISK untouched
    assert payload["cutoff"] == 1 and payload["items"] == [{"x": 1}]   # round-trip preserved the rest


def test_apply_resolves_formatitem_without_name_via_id():
    # a target formatItem carrying only the cf id (no 'name') must still be matched + filled,
    # for parity with the planner which resolves id->name (hardening nit).
    cfs, profiles = _data()
    profiles["ultra"][0]["formatItems"][0] = {"format": 10, "score": 0}   # x265, id-only
    ps, api = _build(_cfg(), cfs, profiles)
    stats = ps.apply_score_sync()
    assert stats["filled"] == 1
    fmt = {(fi.get("name") or fi.get("format")): fi["score"] for fi in api.puts[0][1]["formatItems"]}
    assert fmt[10] == 5                                    # resolved by id, filled to canonical 5


def test_apply_dry_run_writes_nothing():
    ps, api = _build(_cfg(), dry_run=True)
    ps.apply_score_sync()
    assert api.puts == []


def test_disabled_is_total_noop():
    ps, api = _build(_cfg(enabled=False))
    stats = ps.apply_score_sync()
    assert api.puts == [] and stats["filled"] == 0


def test_overwrite_needs_flag_and_consent():
    # differing score + overwrite flag but NO consent -> conflict, not written
    cfs, profiles = _data()
    profiles["ultra"][0]["formatItems"][0]["score"] = 3
    ps, api = _build(_cfg(overwrite=True, consent=False), cfs, profiles)
    ps.apply_score_sync()
    assert api.puts == []                                  # consent missing -> no overwrite


def test_overwrite_applies_with_flag_and_consent():
    cfs, profiles = _data()
    profiles["ultra"][0]["formatItems"][0]["score"] = 3
    ps, api = _build(_cfg(overwrite=True, consent=True), cfs, profiles)
    stats = ps.apply_score_sync()
    assert stats["overwritten"] == 1
    fmt = {fi["name"]: fi["score"] for fi in api.puts[0][1]["formatItems"]}
    assert fmt["x265"] == 5                                # tuned 3 -> standard's 5


def test_apply_idempotent_second_run_no_put():
    ps, api = _build(_cfg())
    ps.apply_score_sync()                                  # fills x265 -> 5 (mutates api profile dict)
    api.puts.clear()
    ps.apply_score_sync()                                  # now all-equal -> nothing to do
    assert api.puts == []


def test_include_test_false_excludes_test_instance():
    cfs, profiles = _data()
    cfs["test"] = [{"id": 30, "name": "x265"}]
    profiles["test"] = [{"id": 40, "name": "4K-test", "formatItems": [{"format": 30, "name": "x265", "score": 0}]}]
    ps, api = _build(_cfg(include_test=False, instances=("standard", "ultra", "test")), cfs, profiles)
    ps.apply_score_sync()
    assert all(inst != "test" for inst, _ in api.puts)


# ── definition sync ─────────────────────────────────────────────────────────────
def test_sync_definitions_creates_missing_on_target():
    ps, api = _build(_cfg())
    stats = ps.sync_definitions()
    assert stats["created"] == 1                           # 'LQ' missing on ultra
    assert api.posts and api.posts[0][0] == "ultra" and api.posts[0][1]["name"] == "LQ"
    assert "id" not in api.posts[0][1]                     # source id stripped


def test_sync_definitions_dry_run_no_post():
    ps, api = _build(_cfg(), dry_run=True)
    ps.sync_definitions()
    assert api.posts == []


# ── RadarrSyncManager.run() gating (defs then scores; inert when disabled) ──────
def test_sync_manager_run_gating_and_order():
    from scripts.managers.services.radarr.sync import RadarrSyncManager

    class _PS:
        def __init__(self, on): self._on, self.calls = on, []
        def enabled(self): return self._on
        def sync_definitions(self): self.calls.append("def")
        def apply_score_sync(self): self.calls.append("score")

    off = RadarrSyncManager.__new__(RadarrSyncManager)
    off.logger, off.profile_scores = _Logger(), _PS(False)
    off.run()
    assert off.profile_scores.calls == []                 # disabled -> complete no-op

    on = RadarrSyncManager.__new__(RadarrSyncManager)
    on.logger, on.profile_scores = _Logger(), _PS(True)
    on.run()
    assert on.profile_scores.calls == ["def", "score"]    # definitions BEFORE scores


# ── consent reader ──────────────────────────────────────────────────────────────
def test_consent_default_false_and_env_override(monkeypatch):
    for v in ("RECOMMENDARR_CF_SYNC_OVERWRITE_CONSENT", "GLIDEARR_CF_SYNC_OVERWRITE_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    assert cf_sync_overwrite_consented({}) is False
    assert cf_sync_overwrite_consented({"cf_sync_overwrite_consent": True}) is True
    monkeypatch.setenv("GLIDEARR_CF_SYNC_OVERWRITE_CONSENT", "false")
    assert cf_sync_overwrite_consented({"cf_sync_overwrite_consent": True}) is False   # env off wins
