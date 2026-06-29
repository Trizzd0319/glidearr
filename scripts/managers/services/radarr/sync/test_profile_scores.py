"""Cross-instance custom-format score sync: name-keyed reads, the diff planner, and the gated
round-trip apply. Proves it keys by NAME (not per-instance id), fills only unset scores by default,
never clobbers a tuned score without overwrite+consent, honours dry-run + the enable flag, syncs
missing definitions, preserves the rest of the profile on PUT, and is idempotent."""
from __future__ import annotations

import copy

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
        self.puts, self.posts, self.profile_posts = [], [], []

    def resolve_instance(self, i): return i

    def _make_request(self, inst, endpoint, method="GET", payload=None, fallback=None):
        if endpoint == "customformat" and method == "GET":
            return [dict(c) for c in self._cfs.get(inst, [])]
        if endpoint == "qualityprofile" and method == "GET":
            return [copy.deepcopy(p) for p in self._profiles.get(inst, [])]   # isolated, like real JSON
        if endpoint == "customformat" and method == "POST":
            self.posts.append((inst, payload))
            rec = {"id": 900 + len(self.posts), **payload}
            self._cfs.setdefault(inst, []).append(rec)
            return rec
        if endpoint == "qualityprofile" and method == "POST":
            self.profile_posts.append((inst, payload))
            rec = {"id": 800 + len(self.profile_posts), **payload}
            self._profiles.setdefault(inst, []).append(rec)
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


def test_automatic_on_2plus_instances_off_on_single():
    # no explicit enabled key -> AUTOMATIC by instance count
    cfg2 = _cfg(); cfg2["scoring"]["cf_sync"].pop("enabled", None)
    assert _build(cfg2)[0].enabled() is True                       # standard + ultra -> on
    cfg1 = _cfg(instances=("standard",)); cfg1["scoring"]["cf_sync"].pop("enabled", None)
    assert _build(cfg1)[0].enabled() is False                      # single instance -> no-op
    cfg_off = _cfg(); cfg_off["scoring"]["cf_sync"]["enabled"] = False
    assert _build(cfg_off)[0].enabled() is False                   # explicit opt-out wins


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


# ── tier-cap: profiles never grab above their named tier ─────────────────────────
def _cap_data():
    def _it(res, allowed=True):
        return {"allowed": allowed, "quality": {"name": f"{res}p", "resolution": res}}
    cfs = {"standard": [{"id": 1, "name": "x265"}], "ultra": [{"id": 10, "name": "x265"}]}
    profiles = {"standard": [
        {"id": 1, "name": "Remux + WEB 1080p", "items": [_it(720), _it(1080), _it(2160)], "formatItems": []},
        {"id": 2, "name": "Remux 2160p", "items": [_it(1080), _it(2160)], "formatItems": []},
        {"id": 3, "name": "HD-1080p grouped",
         "items": [{"allowed": True, "name": "WEB", "items": [_it(1080), _it(2160)]}], "formatItems": []},
        {"id": 4, "name": "Any", "items": [_it(2160)], "formatItems": []}],
        "ultra": []}
    return cfs, profiles


def test_cap_disallows_above_tier_only():
    ps, api = _build(_cfg(instances=("standard", "ultra")), *_cap_data())
    stats = ps.cap_profiles_to_tier()
    assert stats["capped"] == 2                              # the 1080p + grouped-1080p profiles
    put = {p["id"]: p for _, p in api.puts}
    r1 = {it["quality"]["resolution"]: it["allowed"] for it in put[1]["items"]}
    assert r1[2160] is False and r1[1080] is True and r1[720] is True   # only 2160p disallowed
    sub = {s["quality"]["resolution"]: s["allowed"] for s in put[3]["items"][0]["items"]}
    assert sub[2160] is False and sub[1080] is True          # grouped 2160p sub-item disallowed
    assert 2 not in put and 4 not in put                     # genuine 2160p + no-tier 'Any' untouched


def test_cap_dry_run_no_put():
    ps, api = _build(_cfg(instances=("standard", "ultra")), *_cap_data(), dry_run=True)
    ps.cap_profiles_to_tier()
    assert api.puts == []


def test_cap_idempotent():
    ps, api = _build(_cfg(instances=("standard", "ultra")), *_cap_data())
    ps.cap_profiles_to_tier()
    api.puts.clear()
    ps.cap_profiles_to_tier()                                # already capped -> nothing to do
    assert api.puts == []


def test_designated_tier_from_name():
    f = RadarrSyncProfileScoresManager._designated_tier_res
    assert f("Remux + WEB 1080p (HEVC)") == 1080
    assert f("Remux 2160p (Combined)") == 2160
    assert f("UHD Bluray + WEB") == 2160
    assert f("Ultra-HD") == 2160
    assert f("HD-720p") == 720
    assert f("Any") is None                                  # no tier hint -> leave untouched


def test_cap_skips_profile_left_with_no_allowed_quality():
    # a profile NAMED 1080p whose ONLY allowed quality is 2160p — capping it would disable
    # everything → un-acquirable. The guard SKIPS the PUT rather than brick a live profile.
    cfs = {"standard": [{"id": 1, "name": "x265"}], "ultra": [{"id": 10, "name": "x265"}]}
    profiles = {"standard": [{"id": 1, "name": "Remux 1080p", "cutoff": 20, "formatItems": [],
                              "items": [{"allowed": True,
                                         "quality": {"id": 20, "name": "2160p", "resolution": 2160}}]}],
                "ultra": []}
    ps, api = _build(_cfg(instances=("standard", "ultra")), cfs, profiles)
    stats = ps.cap_profiles_to_tier()
    assert api.puts == [] and stats["capped"] == 0           # never PUT an un-acquirable profile


def test_cap_repoints_orphaned_cutoff_to_highest_allowed():
    # a 1080p-named profile that allows 2160p as a fallback with the cutoff ON that 2160p quality:
    # capping disables 2160p, so the now-orphaned cutoff is re-pointed to the highest still-allowed.
    cfs = {"standard": [{"id": 1, "name": "x265"}], "ultra": [{"id": 10, "name": "x265"}]}
    profiles = {"standard": [{"id": 1, "name": "WEB 1080p", "cutoff": 9, "formatItems": [], "items": [
        {"allowed": True, "quality": {"id": 7, "name": "1080p", "resolution": 1080}},
        {"allowed": True, "quality": {"id": 9, "name": "2160p", "resolution": 2160}}]}],
        "ultra": []}
    ps, api = _build(_cfg(instances=("standard", "ultra")), cfs, profiles)
    stats = ps.cap_profiles_to_tier()
    assert stats["capped"] == 1
    payload = api.puts[0][1]
    res_allowed = {it["quality"]["resolution"]: it["allowed"] for it in payload["items"]}
    assert res_allowed[2160] is False and res_allowed[1080] is True
    assert payload["cutoff"] == 7                            # re-pointed off the capped 2160p (id 9) to 1080p (id 7)


# ── tier-cap with quality GROUPS (the TRaSH shape that defeated the first brick guard) ───────────
def test_cap_skips_grouped_profile_when_only_content_is_an_above_tier_group():
    # a 1080p-named profile whose ONLY allowed content is a 2160p quality GROUP: capping empties the
    # group, so the parent must be disabled too — else an allowed-but-empty group reads as resolution
    # 0 and the profile is PUT un-grabbable. Must SKIP, never PUT.
    cfs = {"standard": [{"id": 1, "name": "x265"}], "ultra": [{"id": 10, "name": "x265"}]}
    profiles = {"standard": [{"id": 1, "name": "WEB 1080p", "cutoff": 8, "formatItems": [], "items": [
        {"id": 8, "name": "WEB 2160p", "allowed": True, "items": [
            {"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}},
            {"allowed": True, "quality": {"id": 19, "name": "WEBRip-2160p", "resolution": 2160}}]}]}],
        "ultra": []}
    ps, api = _build(_cfg(instances=("standard", "ultra")), cfs, profiles)
    stats = ps.cap_profiles_to_tier()
    assert api.puts == [] and stats["capped"] == 0          # emptied group → un-grabbable → SKIP


def test_cap_disables_emptied_group_parent_but_keeps_top_level_tier():
    # 1080p-named profile: top-level Remux-1080p (kept) + an all-2160p group (emptied). The group
    # PARENT must end allowed=False (no phantom), the 1080p stays, and the profile is PUT clean.
    cfs = {"standard": [{"id": 1, "name": "x265"}], "ultra": [{"id": 10, "name": "x265"}]}
    profiles = {"standard": [{"id": 1, "name": "Remux + WEB 1080p", "cutoff": 5, "formatItems": [], "items": [
        {"allowed": True, "quality": {"id": 5, "name": "Remux-1080p", "resolution": 1080}},
        {"id": 8, "name": "WEB 2160p", "allowed": True, "items": [
            {"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}}]}]}],
        "ultra": []}
    ps, api = _build(_cfg(instances=("standard", "ultra")), cfs, profiles)
    stats = ps.cap_profiles_to_tier()
    assert stats["capped"] == 1
    payload = api.puts[0][1]
    group = next(it for it in payload["items"] if it.get("id") == 8)
    assert group["allowed"] is False                        # emptied group parent disabled (no phantom)
    assert group["items"][0]["allowed"] is False            # its 2160p sub disabled
    top = next(it for it in payload["items"] if (it.get("quality") or {}).get("id") == 5)
    assert top["allowed"] is True                           # top-level 1080p kept
    assert payload["cutoff"] == 5                           # cutoff already valid (on the kept 1080p)


def test_cap_repoints_cutoff_off_an_emptied_group():
    cfs = {"standard": [{"id": 1, "name": "x265"}], "ultra": [{"id": 10, "name": "x265"}]}
    profiles = {"standard": [{"id": 1, "name": "Remux + WEB 1080p", "cutoff": 8, "formatItems": [], "items": [
        {"allowed": True, "quality": {"id": 5, "name": "Remux-1080p", "resolution": 1080}},
        {"id": 8, "name": "WEB 2160p", "allowed": True, "items": [
            {"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}}]}]}],
        "ultra": []}
    ps, api = _build(_cfg(instances=("standard", "ultra")), cfs, profiles)
    ps.cap_profiles_to_tier()
    assert api.puts[0][1]["cutoff"] == 5                    # re-pointed off the now-dead group (8) to 1080p (5)


def test_cap_repoints_cutoff_off_a_nested_sub_quality():
    # cutoff names a quality NESTED in a group (as the repo's anime profiles do); capping that nested
    # quality must re-point the cutoff to a still-allowed entry, not leave it orphaned.
    cfs = {"standard": [{"id": 1, "name": "x265"}], "ultra": [{"id": 10, "name": "x265"}]}
    profiles = {"standard": [{"id": 1, "name": "WEB 1080p", "cutoff": 18, "formatItems": [], "items": [
        {"id": 7, "name": "WEB 1080p", "allowed": True, "items": [
            {"allowed": True, "quality": {"id": 17, "name": "WEBDL-1080p", "resolution": 1080}}]},
        {"id": 8, "name": "WEB 2160p", "allowed": True, "items": [
            {"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}}]}]}],
        "ultra": []}
    ps, api = _build(_cfg(instances=("standard", "ultra")), cfs, profiles)
    ps.cap_profiles_to_tier()
    cutoff = api.puts[0][1]["cutoff"]
    assert cutoff != 18 and cutoff in (7, 17)              # off the capped nested 2160p to a 1080p entry


# ── backup-gate: a real run with a DISARMED gate degrades every config write to dry-run ─────────
def _disarm(ps):
    from scripts.managers.services.backup import GATE_KEY
    ps.global_cache.set(GATE_KEY, {"armed": False})         # backup pre-flight failed → writes blocked


def test_cap_disarmed_backup_gate_no_put():
    ps, api = _build(_cfg(instances=("standard", "ultra")), *_cap_data())
    _disarm(ps)
    ps.cap_profiles_to_tier()
    assert api.puts == []                                   # real run + failed backup → degrade to dry-run


def test_sync_definitions_disarmed_backup_gate_no_post():
    ps, api = _build(_cfg())
    _disarm(ps)
    ps.sync_definitions()
    assert api.posts == []


def test_sync_uhd_profiles_disarmed_backup_gate_no_post():
    cfs, profiles = _uhd_data()
    ps, api = _build(_uhd_cfg(), cfs, profiles)
    _disarm(ps)
    ps.sync_uhd_profiles()
    assert api.profile_posts == []


def test_apply_score_disarmed_backup_gate_no_put():
    ps, api = _build(_cfg())
    _disarm(ps)
    ps.apply_score_sync()
    assert api.puts == []


# ── empty custom-format read must not poison the run or write degraded profiles ─────────────────
def test_get_custom_formats_does_not_cache_empty_read():
    # an empty/failed customformat read must NOT be cached — a cached [] poisons every later read
    # this run (the root of the silent definition-missing no-op CF sync).
    ps, api = _build(_cfg())
    cf = ps._cf()
    api._cfs["standard"] = []                                 # transient empty read
    assert cf.get_custom_formats("standard") == []
    assert ps.global_cache.get("radarr.custom_formats.standard", default="MISS") == "MISS"  # not cached
    api._cfs["standard"] = [{"id": 1, "name": "x265"}]
    assert cf.get_custom_formats("standard") == [{"id": 1, "name": "x265"}]   # re-read, now succeeds + caches


def test_sync_definitions_noops_and_warns_when_source_cf_read_empty():
    cfs, profiles = _data()
    cfs["standard"] = []                                     # source CF read empty
    ps, api = _build(_cfg(), cfs, profiles)
    stats = ps.sync_definitions()
    assert stats == {"created": 0, "present": 0} and api.posts == []   # vacuous no-op, nothing created


def test_sync_uhd_profiles_skips_when_4k_cf_read_empty():
    cfs, profiles = _uhd_data()
    cfs["ultra"] = []                                        # 4K instance CF read empty
    ps, api = _build(_uhd_cfg(), cfs, profiles)
    stats = ps.sync_uhd_profiles()
    assert stats["created"] == 0 and api.profile_posts == []   # never create CF-less 2160p profiles


def test_get_custom_formats_caches_non_empty_read():
    ps, _ = _build(_cfg())
    cf = ps._cf()
    cf.get_custom_formats("standard")                        # real data present in fixture
    assert ps.global_cache.get("radarr.custom_formats.standard", default="MISS") != "MISS"  # cached


# ── RadarrSyncManager.run() gating (defs then scores; inert when disabled) ──────
def test_sync_manager_run_gating_and_order():
    from scripts.managers.services.radarr.sync import RadarrSyncManager

    class _PS:
        def __init__(self, on): self._on, self.calls = on, []
        def enabled(self): return self._on
        def cap_profiles_to_tier(self): self.calls.append("cap")
        def sync_definitions(self): self.calls.append("def")
        def sync_uhd_profiles(self): self.calls.append("uhd")
        def apply_score_sync(self): self.calls.append("score")

    _two = {"radarr_instances": {"default_instance": {"name": "standard"}, "standard": {}, "ultra": {}}}

    off = RadarrSyncManager.__new__(RadarrSyncManager)
    off.logger, off.profile_scores, off.config = _Logger(), _PS(False), _two
    off.run()
    assert off.profile_scores.calls == []                 # disabled -> complete no-op

    on = RadarrSyncManager.__new__(RadarrSyncManager)
    on.logger, on.profile_scores, on.config = _Logger(), _PS(True), _two
    on.run()
    assert on.profile_scores.calls == ["cap", "def", "uhd", "score"]   # cap -> defs -> uhd -> scores


# ── 2160p/UHD profile routing to the 4K instance ────────────────────────────────
def _uhd_data():
    cfs = {"standard": [{"id": 1, "name": "x265"}, {"id": 2, "name": "BR-DISK"}],
           "ultra":    [{"id": 10, "name": "x265"}, {"id": 11, "name": "BR-DISK"}]}

    def _prof(pid, name, res, scores):
        return {"id": pid, "name": name, "cutoff": 1, "minFormatScore": 0,
                "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}],
                "formatItems": [{"format": cid, "name": n, "score": scores.get(n, 0)}
                                for cid, n in ((1, "x265"), (2, "BR-DISK"))]}
    profiles = {"standard": [_prof(1, "HD-1080p", 1080, {"x265": 5}),
                             _prof(2, "Ultra-HD", 2160, {"x265": 8, "BR-DISK": -50})],
                "ultra": []}                                   # 4K instance has no profiles yet
    return cfs, profiles


def _uhd_cfg(**kw):
    c = _cfg(instances=("standard", "ultra"), **kw)
    c["radarr_instances_categorized"] = {"4K": "ultra"}
    return c


def test_sync_uhd_profiles_copies_only_2160p_to_4k_instance():
    cfs, profiles = _uhd_data()
    ps, api = _build(_uhd_cfg(), cfs, profiles)
    stats = ps.sync_uhd_profiles()
    assert stats["created"] == 1                               # only the 2160p profile
    inst, payload = api.profile_posts[0]
    assert inst == "ultra" and payload["name"] == "Ultra-HD"
    assert "id" not in payload                                 # source id stripped
    # 1080p profile is NOT copied to the 4K instance
    assert all(p["name"] != "HD-1080p" for _, p in api.profile_posts)
    # CF scores carried by name, resolved to ultra's cf ids (10/11)
    fmt = {fi["name"]: (fi["format"], fi["score"]) for fi in payload["formatItems"]}
    assert fmt["x265"] == (10, 8) and fmt["BR-DISK"] == (11, -50)


def test_sync_uhd_profiles_excludes_lower_tier_named_profile():
    # a profile NAMED for 1080p that merely ALLOWS 2160p (TRaSH fallback) is NOT a 4K profile
    cfs, profiles = _uhd_data()
    profiles["standard"].append({
        "id": 9, "name": "Remux + WEB 1080p (HEVC)", "cutoff": 1, "minFormatScore": 0,
        "items": [{"allowed": True, "quality": {"name": "Remux-2160p", "resolution": 2160}}],
        "formatItems": [{"format": 1, "name": "x265", "score": 5}]})
    ps, api = _build(_uhd_cfg(), cfs, profiles)
    ps.sync_uhd_profiles()
    assert all(p["name"] != "Remux + WEB 1080p (HEVC)" for _, p in api.profile_posts)   # excluded
    assert any(p["name"] == "Ultra-HD" for _, p in api.profile_posts)                   # genuine 2160p copied


def test_sync_uhd_profiles_dry_run_no_post():
    cfs, profiles = _uhd_data()
    ps, api = _build(_uhd_cfg(), cfs, profiles, dry_run=True)
    ps.sync_uhd_profiles()
    assert api.profile_posts == []


def test_sync_uhd_profiles_skips_existing():
    cfs, profiles = _uhd_data()
    profiles["ultra"].append({"id": 50, "name": "Ultra-HD", "items": [], "formatItems": []})
    ps, api = _build(_uhd_cfg(), cfs, profiles)
    stats = ps.sync_uhd_profiles()
    assert stats["created"] == 0 and api.profile_posts == []   # already present -> additive no-op


def test_sync_uhd_profiles_noop_without_distinct_4k_instance():
    cfs, profiles = _uhd_data()
    cfg = _cfg(instances=("standard", "ultra"))                # no radarr_instances_categorized 4K
    ps, api = _build(cfg, cfs, profiles)
    assert ps.sync_uhd_profiles()["created"] == 0 and api.profile_posts == []


# ── consent reader ──────────────────────────────────────────────────────────────
def test_consent_default_false_and_env_override(monkeypatch):
    for v in ("RECOMMENDARR_CF_SYNC_OVERWRITE_CONSENT", "GLIDEARR_CF_SYNC_OVERWRITE_CONSENT"):
        monkeypatch.delenv(v, raising=False)
    assert cf_sync_overwrite_consented({}) is False
    assert cf_sync_overwrite_consented({"cf_sync_overwrite_consent": True}) is True
    monkeypatch.setenv("GLIDEARR_CF_SYNC_OVERWRITE_CONSENT", "false")
    assert cf_sync_overwrite_consented({"cf_sync_overwrite_consent": True}) is False   # env off wins
