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
