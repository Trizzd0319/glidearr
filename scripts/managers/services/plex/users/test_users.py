"""Tests for Plex Home enum + identity crosswalk + token minting.

The crosswalk must fail CLOSED (never attribute one user's data to another) and the
PII-minimized roster must carry NO email / NO token. Token minting registers the
minted token with the logger scrubber and never caches it."""
from __future__ import annotations

from scripts.managers.services.plex.users import PlexUsersManager


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


# ── _parse_home_users (schema-tolerant) ───────────────────────────────────────
def test_parse_home_users_tolerates_missing_flags():
    resp = {"users": [
        {"uuid": "u1", "id": 11, "title": "Rob", "email": "rob@x.io", "admin": True},
        {"uuid": "u2", "id": 22, "username": "Kid", "restricted": True},
        {"id": 33},                     # no uuid? -> falls back to id as uuid
        {"no_uuid_no_id": True},        # fail-closed: skipped
    ]}
    out = PlexUsersManager._parse_home_users(resp)
    assert [u["uuid"] for u in out] == ["u1", "u2", "33"]
    assert out[0]["is_admin"] and out[0]["title"] == "Rob"
    assert out[1]["is_managed"] and out[1]["title"] == "Kid"


def test_parse_home_users_accepts_bare_list():
    assert PlexUsersManager._parse_home_users([{"uuid": "x", "title": "A"}])[0]["uuid"] == "x"
    assert PlexUsersManager._parse_home_users(None) == []


# ── build_identity_map (the crosswalk cascade) ────────────────────────────────
def test_crosswalk_matches_by_plex_id_then_email_then_title():
    home = [
        {"uuid": "ua", "id": 11, "title": "Rob",  "email": "rob@x.io"},
        {"uuid": "ub", "id": 99, "title": "Aiden", "email": "aiden@x.io"},   # id miss → email
        {"uuid": "uc", "id": None, "title": "Guest", "email": ""},           # title only
        {"uuid": "ud", "id": 0, "title": "Ghost", "email": ""},              # unmatched
    ]
    tau = [
        {"user_id": 11, "username": "robadams", "email": "rob@x.io"},
        {"user_id": 50, "username": "aiden", "email": "aiden@x.io"},
        {"user_id": 60, "username": "Guest", "email": "g@x.io"},
    ]
    m = PlexUsersManager.build_identity_map(home, tau, {"household": {}})
    assert m["ua"]["matched_via"] == "plex_id" and m["ua"]["tautulli_username"] == "robadams"
    assert m["ub"]["matched_via"] == "email" and m["ub"]["tautulli_username"] == "aiden"
    assert m["uc"]["matched_via"] == "title" and m["uc"]["tautulli_username"] == "Guest"
    assert m["ud"]["matched_via"] == "unmatched" and m["ud"]["tautulli_username"] is None
    # memberless household → everyone in the wildcard group
    assert all(v["rating_groups"] == ["household"] for v in m.values())


def test_crosswalk_respects_explicit_rating_groups_and_memberless_wildcard():
    home = [{"uuid": "ua", "id": 11, "title": "Rob", "email": ""},
            {"uuid": "ub", "id": 22, "title": "Kid", "email": ""}]
    tau = [{"user_id": 11, "username": "rob"}, {"user_id": 22, "username": "kid"}]
    groups = {"adults": {"members": ["rob"]}, "all": {}}   # 'all' is memberless = wildcard
    m = PlexUsersManager.build_identity_map(home, tau, groups)
    assert set(m["ua"]["rating_groups"]) == {"adults", "all"}
    assert m["ub"]["rating_groups"] == ["all"]            # kid not in adults, but in wildcard


def test_groups_for_defaults_to_household_when_no_match():
    # explicit member group, user matches none, no wildcard → fall back to household
    assert PlexUsersManager._groups_for("nobody", {"adults": {"members": ["rob"]}}) == ["household"]


# ── _pin_for (nested secret shape + flat) ─────────────────────────────────────
def test_pin_for_handles_nested_and_flat():
    assert PlexUsersManager._pin_for({"Kid": {"pin": "1234"}}, "uX", "Kid") == "1234"
    assert PlexUsersManager._pin_for({"uX": "9999"}, "uX", "Kid") == "9999"
    assert PlexUsersManager._pin_for({}, "uX", "Kid") is None


# ── persisted roster drops PII (no email, no token) ───────────────────────────
def test_persist_roster_is_pii_minimized():
    m = object.__new__(PlexUsersManager)
    m.logger = _Logger(); m.global_cache = _Cache()
    m.user_tokens = {"Rob": "tok-secret"}
    roster = [{"uuid": "ua", "id": 11, "title": "Rob", "email": "rob@x.io",
               "is_admin": True, "is_managed": False, "protected": False}]
    idmap = {"ua": {"tautulli_username": "rob", "tautulli_user_id": 11,
                    "rating_groups": ["household"], "matched_via": "plex_id"}}
    m._persist(roster, idmap)
    stored = m.global_cache.get("plex/users")
    assert stored == [{"uuid": "ua", "title": "Rob", "is_admin": True,
                       "is_managed": False, "protected": False, "token_scope_ok": True}]
    # no email, no token anywhere in the persisted blobs
    blob = repr(m.global_cache.d)
    assert "rob@x.io" not in blob and "tok-secret" not in blob


# ── token minting registers the token with the scrubber, never caches it ──────
def test_mint_tokens_in_memory_only_and_registered():
    from scripts.support.utilities.logger.logger import LoggerManager

    class _API:
        token = "ADMIN-TOKEN"
        def switch_home_user(self, uuid, pin=None, fallback=None):
            return {"authToken": f"minted-{uuid}"}

    m = object.__new__(PlexUsersManager)
    m.logger = _Logger(); m.global_cache = _Cache(); m.config = {"plex": {}}
    m.plex_api = _API(); m.user_tokens = {}
    roster = [
        {"uuid": "ua", "id": 1, "title": "Rob", "email": "", "is_admin": True,
         "is_managed": False, "protected": False},
        {"uuid": "ub", "id": 2, "title": "Kid", "email": "", "is_admin": False,
         "is_managed": True, "protected": False},
    ]
    skipped = m._mint_tokens(roster)
    assert skipped == 0
    assert m.user_tokens["Rob"] == "ADMIN-TOKEN"        # owner reuses account token
    assert m.user_tokens["Kid"] == "minted-ub"          # switched
    assert "minted-ub" in LoggerManager._scrub_values   # registered with the scrubber
    # tokens live only in the in-memory table — never in any cache key
    assert all("token" not in k for k in m.global_cache.d)


# ── server_write_token (per-server PMS write token for the playlist write-back) ──
def test_server_write_token_owner_reuses_account_token():
    class _API:
        token = "ADMIN-TOKEN"
    m = object.__new__(PlexUsersManager)
    m.logger = _Logger(); m.plex_api = _API()
    m.user_tokens = {"Rob": "ADMIN-TOKEN"}
    rob = {"safe_user": "Rob", "title": "Rob", "is_admin": True}
    assert m.server_write_token(rob) == "ADMIN-TOKEN"


def test_server_write_token_managed_derives_per_server_token_not_owner():
    from scripts.support.utilities.logger.logger import LoggerManager

    class _API:
        token = "ADMIN-TOKEN"
        def server_access_token(self, user_auth, fallback=None):
            assert user_auth == "minted-kid"        # the kid's account authToken, not the owner's
            return "PER-SERVER-KID"
    m = object.__new__(PlexUsersManager)
    m.logger = _Logger(); m.plex_api = _API()
    m.user_tokens = {"Kid": "minted-kid"}
    kid = {"safe_user": "Kid", "title": "Kid", "is_admin": False}
    tok = m.server_write_token(kid)
    assert tok == "PER-SERVER-KID" and tok != "ADMIN-TOKEN"   # NEVER the owner token
    assert "PER-SERVER-KID" in LoggerManager._scrub_values    # registered with the scrubber
    # cached per run — a second call does NOT re-derive (would assert again / blow up if it did)
    m.plex_api = None
    assert m.server_write_token(kid) == "PER-SERVER-KID"


# ── age-gate fail-open warning for managed profiles with no resolved tier ──
class _CapLogger:
    def __init__(self): self.warnings = []
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, msg, *a, **k): self.warnings.append(msg)


def _users_mgr(config):
    m = object.__new__(PlexUsersManager)
    m.logger = _CapLogger()
    m.config = config
    return m


def test_warn_ungated_managed_users_fires_for_managed_without_tier_or_override():
    m = _users_mgr({"plex": {"playlists": {"profile_ages": {"Wyatt": "little_kid"}}}})
    roster = [
        {"uuid": "a", "title": "Trizzd", "is_admin": True, "is_managed": False, "restriction_profile": None},
        {"uuid": "b", "title": "Wyatt", "is_managed": True, "restriction_profile": None},        # has override
        {"uuid": "c", "title": "Kidd", "is_managed": True, "restriction_profile": "little_kid"}, # Plex tier known
        {"uuid": "d", "title": "Aiden", "is_managed": True, "restriction_profile": None},        # UNGATED
        {"uuid": "e", "title": "Mom", "is_managed": True, "restriction_profile": None},          # UNGATED
    ]
    m._warn_ungated_managed_users(roster)
    assert len(m.logger.warnings) == 1
    w = m.logger.warnings[0]
    # DE-IDENTIFIED: full names are PII and must NOT appear; the two ungated profiles show as
    # '{initial} - unknown {n}', sorted (Aiden, Mom) and numbered.
    assert "Aiden" not in w and "Mom" not in w
    assert "A - unknown 1" in w and "M - unknown 2" in w
    assert "2 managed profile(s)" in w
    assert "Wyatt" not in w and "Kidd" not in w and "Trizzd" not in w


def test_warn_ungated_managed_users_silent_when_all_gated():
    m = _users_mgr({"plex": {"playlists": {"profile_ages": {"Aiden": "older_kid"}}}})
    roster = [
        {"uuid": "d", "title": "Aiden", "is_managed": True, "restriction_profile": None},   # override
        {"uuid": "c", "title": "Kidd", "is_managed": True, "restriction_profile": "teen"},  # Plex tier
        {"uuid": "a", "title": "Trizzd", "is_admin": True, "is_managed": False, "restriction_profile": None},
    ]
    m._warn_ungated_managed_users(roster)
    assert m.logger.warnings == []


def test_server_write_token_managed_no_token_returns_none_never_owner():
    class _API:
        token = "ADMIN-TOKEN"
        def server_access_token(self, user_auth, fallback=None):
            return None                              # our server not shared to this user
    m = object.__new__(PlexUsersManager)
    m.logger = _Logger(); m.plex_api = _API()
    m.user_tokens = {"Kid": "minted-kid"}
    assert m.server_write_token({"safe_user": "Kid", "is_admin": False}) is None


def test_extract_token_accepts_all_known_keys():
    f = PlexUsersManager._extract_token
    assert f({"authenticationToken": "X"}) == "X"     # the documented field (python-plexapi)
    assert f({"authToken": "Y"}) == "Y"               # v2-JSON variant
    assert f({"authentication_token": "Z"}) == "Z"
    assert f({"user": {"authToken": "N"}}) == "N"      # nested envelope
    assert f({"nope": 1}) is None


def test_safe_map_disambiguates_sanitisation_collisions():
    # Two distinct Home users whose titles sanitize identically must get DISTINCT keys,
    # or one user's token/cache silently overwrites the other's (fail-OPEN attribution).
    m = object.__new__(PlexUsersManager)
    m.logger = _Logger()
    roster = [
        {"uuid": "u1111aaaa", "title": "Rob/Kids"},   # -> 'Rob_Kids'
        {"uuid": "u2222bbbb", "title": "Rob_Kids"},   # -> 'Rob_Kids' (collision)
        {"uuid": "u3333cccc", "title": "Rob:Kids"},   # -> 'Rob_Kids' (collision)
    ]
    smap = m._build_safe_map(roster)
    assert len(set(smap.values())) == 3                # all distinct → no cross-attribution
    assert smap["u1111aaaa"] == "Rob_Kids"             # first keeps the clean base
    assert smap["u2222bbbb"] != smap["u3333cccc"]      # collisions uuid-disambiguated


def test_mint_skips_and_counts_pin_protected_without_pin():
    class _API:
        token = "ADMIN"
        def switch_home_user(self, *a, **k): return {"authToken": "x"}
    m = object.__new__(PlexUsersManager)
    m.logger = _Logger(); m.global_cache = _Cache(); m.config = {"plex": {}}
    m.plex_api = _API(); m.user_tokens = {}
    roster = [{"uuid": "ub", "id": 2, "title": "Kid", "email": "", "is_admin": False,
               "is_managed": True, "protected": True}]
    assert m._mint_tokens(roster) == 1                  # counted
    assert "Kid" not in m.user_tokens                   # skipped


# ── allowed_sections: the per-user library allowlist (fail-CLOSED) ────────────
def _sections_mgr(*, sections=None, grants=None, config=None, api=None):
    m = object.__new__(PlexUsersManager)
    m.logger = _Logger()
    c = _Cache()
    c.d["plex/sections"] = sections if sections is not None else {
        "1": {"type": "movie"}, "2": {"type": "show"}, "3": {"type": "movie"}}
    m.global_cache = c
    m.config = config or {"plex": {"playlists": {"this_week_in_history": {"enabled": True}}}}
    m.plex_api = api
    m._section_grants = grants or {}
    m._user_sections = {}
    return m


def test_allowed_sections_admin_gets_all_and_persists_keys_only():
    m = _sections_mgr()
    assert m.allowed_sections({"safe_user": "rob", "is_admin": True}) == {"1", "2", "3"}
    assert m.global_cache.d["plex/user_sections"]["rob"] == ["1", "2", "3"]   # section keys only


def test_allowed_sections_unresolved_managed_fails_closed():
    m = _sections_mgr()                                  # no grant for kid, trust off
    assert m.allowed_sections({"safe_user": "kid", "is_admin": False}) == set()
    assert m.global_cache.d["plex/user_sections"]["kid"] == []


def test_allowed_sections_trust_home_managed_opens_to_all():
    m = _sections_mgr(config={"plex": {"playlists": {"this_week_in_history": {
        "enabled": True, "trust_home_managed": True}}}})
    assert m.allowed_sections({"safe_user": "kid", "is_admin": False}) == {"1", "2", "3"}


def test_allowed_sections_explicit_and_all_grants():
    assert _sections_mgr(grants={"kid": {"1"}}).allowed_sections(
        {"safe_user": "kid", "is_admin": False}) == {"1"}
    assert _sections_mgr(grants={"kid": "ALL"}).allowed_sections(
        {"safe_user": "kid", "is_admin": False}) == {"1", "2", "3"}


def test_allowed_sections_empty_when_section_index_cold():
    m = _sections_mgr(sections={})                       # libraries not warm yet → no allowlist
    assert m.allowed_sections({"safe_user": "rob", "is_admin": True}) == set()


# ── _resolve_section_grants: parse plex.tv shared_servers ─────────────────────
class _ShareAPI:
    def __init__(self, payload, mid="MID"): self._p = payload; self._mid = mid
    def get_machine_id(self): return self._mid
    def get_shared_servers(self, fallback=None): return self._p


def test_resolve_grants_matches_email_and_parses_alllibraries():
    m = _sections_mgr(api=_ShareAPI([{"machineIdentifier": "MID", "email": "kid@x.com",
                                      "allLibraries": True}]))
    m._safe_by_uuid = {"u2": "kid"}
    roster = [{"uuid": "u2", "title": "Kid", "email": "kid@x.com", "is_admin": False}]
    assert m._resolve_section_grants(roster) == {"kid": "ALL"}


def test_resolve_grants_specific_sections_and_other_server_ignored():
    payload = {"sharedServers": [
        {"machineIdentifier": "OTHER", "email": "kid@x.com", "allLibraries": True},   # other server
        {"machineIdentifier": "MID", "email": "kid@x.com",
         "Section": [{"key": "1", "shared": True}, {"key": "9", "shared": False}]}]}
    m = _sections_mgr(api=_ShareAPI(payload))
    m._safe_by_uuid = {"u2": "kid"}
    roster = [{"uuid": "u2", "title": "Kid", "email": "kid@x.com", "is_admin": False}]
    assert m._resolve_section_grants(roster) == {"kid": {"1"}}    # unshared/other-server dropped


def test_resolve_grants_off_when_feature_disabled():
    m = _sections_mgr(config={"plex": {"playlists": {}}},
                      api=_ShareAPI([{"allLibraries": True}]))
    assert m._resolve_section_grants([{"uuid": "u", "title": "x", "is_admin": False}]) == {}


def test_resolve_grants_ignores_share_record_id_collision():
    # A share ROW id (3) must NEVER match a Home user's ACCOUNT id (3) — namespacing prevents the
    # cross-match that would otherwise hand the user a different account's 'allLibraries' grant.
    payload = [{"machineIdentifier": "MID", "id": 3, "email": "friend@x.com", "allLibraries": True}]
    m = _sections_mgr(api=_ShareAPI(payload))
    m._safe_by_uuid = {"u9": "sam"}
    roster = [{"uuid": "u9", "id": 3, "title": "Sam", "email": "", "is_admin": False}]
    assert m._resolve_section_grants(roster) == {}      # no spurious grant → allowed_sections fails closed


def test_resolve_grants_fails_closed_without_machine_id():
    # If the local machine id can't be determined, we can't confirm which server a share targets →
    # take NO grants rather than apply another server's share locally.
    m = _sections_mgr(api=_ShareAPI([{"email": "kid@x.com", "allLibraries": True}], mid=None))
    m._safe_by_uuid = {"u2": "kid"}
    roster = [{"uuid": "u2", "title": "Kid", "email": "kid@x.com", "is_admin": False}]
    assert m._resolve_section_grants(roster) == {}
