"""Tests for the per-user playlist WRITE path (PlaylistWritebackManager).

Every test drives the tested core ``_writeback`` (or its sub-steps) with a FAKE PlexAPI
that CAPTURES each write call, so the safety rails are asserted on the actual call-log:
  • dry-run / disabled ⇒ ZERO writes (byte-identical default-off);
  • a managed user is NEVER written with the owner token;
  • find-or-create resolves via the cached anchor (no create when it still resolves);
  • a non-anchor title-match is never delete_playlist'd;
  • an orphan is LEFT ALONE on a PIN-skip (still in the live roster);
  • steady-state (current == desired) is a no-op.
"""
from __future__ import annotations

from scripts.managers.services.plex.playlists.writeback import (
    _ANCHOR_KEY,
    PlaylistWritebackManager,
)


# ── fakes ──────────────────────────────────────────────────────────────────────
class _Log:
    def __init__(self):
        self.infos: list = []
        self.warns: list = []
        self.errors: list = []
        self.audits: list = []
        self.files: dict = {}                  # category -> [lines]  (the dedicated-file sink)

    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): self.warns.append(m)
    def log_error(self, m): self.errors.append(m)
    def log_debug(self, m): pass
    def log_audit(self, m): self.audits.append(m)

    def log_to_file(self, category, message, *, reset=False):
        bucket = self.files.setdefault(category, [])
        if reset:
            bucket.clear()
        bucket.append(message)


class _Cache:
    def __init__(self, d=None):
        self.d: dict = dict(d or {})

    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


class _FakeAPI:
    """Captures every write verb; reads (get_playlist_items/get_playlists) come from a script."""
    def __init__(self, token="OWNER", items_by_rk=None, playlists=None):
        self.token = token
        self.writes: list = []                # (verb, rk, extra, token)
        self._items_by_rk = items_by_rk or {}  # rk -> [{ratingKey, playlistItemID}]
        self._playlists = playlists or []      # [{ratingKey, title, playlistType}]
        self._next_rk = 9000

    # reads (token-scoped, matching the real api — captured so a test can assert per-user scope)
    def get_playlist_items(self, rating_key, token=None, fallback=None):
        self.reads = getattr(self, "reads", [])
        self.reads.append(("items", rating_key, token))
        if rating_key not in self._items_by_rk:
            return None                        # 404 → anchor no longer resolves
        return {"MediaContainer": {"Metadata": self._items_by_rk[rating_key]}}

    def get_playlists(self, token=None, fallback=None):
        self.reads = getattr(self, "reads", [])
        self.reads.append(("list", None, token))
        return {"MediaContainer": {"Metadata": self._playlists}}

    # writes (captured)
    def create_playlist(self, title, rating_keys, token=None, fallback=None):
        self.writes.append(("create", None, {"title": title, "items": list(rating_keys)}, token))
        rk = str(self._next_rk); self._next_rk += 1
        self._playlists.append({"ratingKey": rk, "title": title, "playlistType": "video"})
        self._items_by_rk[rk] = [{"ratingKey": k, "playlistItemID": f"pi-{k}"} for k in rating_keys]
        return None

    def add_playlist_items(self, playlist_rk, rating_keys, token=None, fallback=None):
        self.writes.append(("add", playlist_rk, list(rating_keys), token))

    def remove_playlist_item(self, playlist_rk, playlist_item_id, token=None, fallback=None):
        self.writes.append(("remove", playlist_rk, playlist_item_id, token))

    def move_playlist_item(self, playlist_rk, playlist_item_id, after_id=None, token=None, fallback=None):
        self.writes.append(("move", playlist_rk, (playlist_item_id, after_id), token))

    def delete_playlist(self, playlist_rk, token=None, fallback=None):
        self.writes.append(("delete", playlist_rk, None, token))


class _Users:
    """Stand-in PlexUsersManager.server_write_token — returns a per-user token map; owner
    reuses the owner token, a managed user gets a DISTINCT per-server token (never the owner's)."""
    def __init__(self, tokens):
        self.tracked_users: list = []
        self._tokens = tokens
        self._safe_by_uuid: dict = {}

    def server_write_token(self, user):
        return self._tokens.get(user.get("safe_user"))


def _mgr(cache, api, config=None, dry_run=False):
    m = PlaylistWritebackManager.__new__(PlaylistWritebackManager)
    m.global_cache = cache
    m.logger = _Log()
    m.config = config if config is not None else {"plex": {"playlists": {"writeback": {"enabled": True}}}}
    m.registry = None
    m.plex_api = api
    m.dry_run = dry_run
    return m


def _plan(*rks):
    return {"family": "up_next", "items": [{"rating_key": rk, "ordinal": i} for i, rk in enumerate(rks)]}


_OWNER_USER = {"safe_user": "rob", "title": "Rob", "is_admin": True}
_KID_USER = {"safe_user": "kid", "title": "Kid", "is_admin": False}

# Fresh inventory resolving ratingKeys a,b,c (TV) so re-resolution keeps them.
_TV_INV = {"100:1:1": {"rating_key": "a"}, "100:1:2": {"rating_key": "b"},
           "200:1:1": {"rating_key": "c"}}


# ── P0 #1: default-off / dry-run = ZERO writes ─────────────────────────────────
def test_disabled_performs_zero_writes():
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI()
    m = _mgr(cache, api, config={"plex": {"playlists": {"writeback": {"enabled": False}}}})
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m.registry = _Reg(users)
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert api.writes == []                          # byte-identical: NOTHING written
    assert stats["armed"] is False
    assert stats["created"] == 1                     # but the preview still counts a would-create


def test_dry_run_true_performs_zero_writes_even_if_enabled():
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI()
    m = _mgr(cache, api, dry_run=True)               # enabled in config but dry_run wins
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    assert m.writeback_armed() is False
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert api.writes == []


def test_disarmed_previews_route_to_dedicated_file_not_main_log():
    # The user's request: dry-run per-playlist previews belong in support/logs/playlists.log
    # (the dedicated file sink), keeping the main run log to the one-line summary banner.
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI()
    m = _mgr(cache, api, dry_run=True)                # disarmed
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    files = m.logger.files.get("playlists", [])
    assert any("would be CREATED" in ln for ln in files)             # preview → dedicated file
    assert not any("would be CREATED" in i for i in m.logger.infos)  # ...not the main run log
    assert any("disarmed" in i and "support/logs/playlists.log" in i  # banner still summarizes
               for i in m.logger.infos)


def test_run_log_lines_are_de_identified():
    # Privacy: profile names must NOT reach the shareable run log (log_info/warn/error). An
    # EXCLUDED user emits a run-log line — it must carry the de-identified handle, not 'Kid'.
    cfg = {"plex": {"playlists": {"writeback": {"enabled": True},
                                  "exclude_users": ["kid"],
                                  "profile_ages": {"Kid": "older_kid"}}}}
    m = _mgr(_Cache(), _FakeAPI(), config=cfg)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    runlog = " ".join(m.logger.infos + m.logger.warns + m.logger.errors)
    assert "excluded" in runlog                     # the line fired
    assert "'Kid'" not in runlog                     # ...without the real name
    assert "K - older_kid 1" in runlog               # ...using the de-identified handle


def test_armed_requires_enabled_and_not_dry_run():
    assert _mgr(_Cache(), _FakeAPI()).writeback_armed() is True
    assert _mgr(_Cache(), _FakeAPI(), config={"plex": {}}).writeback_armed() is False
    assert _mgr(_Cache(), _FakeAPI(), dry_run=True).writeback_armed() is False


# ── per-user READS are token-scoped (regression) ──────────────────────────────
def test_per_user_reads_use_member_token_not_owner():
    # A managed user's playlist is private to THEIR account; reading it with the owner token
    # 404s and churns a duplicate every run. Every read (adopt scan, create re-GET, anchor
    # check, item read) must carry the member's per-server token, never the owner's.
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)                              # armed
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    read_tokens = {tok for _kind, _rk, tok in getattr(api, "reads", [])}
    assert read_tokens == {"KIDTOK"}                  # all reads scoped to the member, never OWNER


# ── P0 #2: a managed user is NEVER written with the owner token ────────────────
def test_managed_user_never_written_with_owner_token():
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    # server_write_token hands back the OWNER token for the kid (a bug we must catch).
    users = _Users({"kid": "OWNER"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert api.writes == []                          # refused — no write on the owner account
    assert stats["skipped"] == 1
    assert any("OWNER token" in e for e in m.logger.errors)


def test_managed_user_no_token_skips_and_counts():
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI()
    m = _mgr(cache, api)
    users = _Users({"kid": None}); users.tracked_users = [_KID_USER]   # token exchange yielded nothing
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert api.writes == []
    assert stats["skipped"] == 1


def test_managed_user_with_own_token_creates_on_their_account():
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    create = [w for w in api.writes if w[0] == "create"]
    assert len(create) == 1 and create[0][3] == "KIDTOK"            # scoped to the kid, NOT owner
    assert create[0][2]["items"] == ["a", "b"]
    assert stats["created"] == 1
    assert m.logger.audits                                         # managed write audited


# ── P0 #3: find-or-create via the anchor; never delete a non-anchor title-match ─
def test_find_or_create_resolves_via_cached_anchor_no_create():
    # The cached anchor still resolves (steady state) → no create, no delete.
    cache = _Cache({
        "plex/playlists/tv_plan/rob": _plan("a", "b"),
        f"{_ANCHOR_KEY}/rob": "555",
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [
        {"ratingKey": "a", "playlistItemID": "p1"}, {"ratingKey": "b", "playlistItemID": "p2"}]})
    m = _mgr(cache, api)
    users = _Users({"rob": "OWNER"}); users.tracked_users = [_OWNER_USER]
    m._writeback([_OWNER_USER], [{"uuid": "u-rob"}], users, _TV_INV, {})
    assert api.writes == []                          # resolved by anchor + steady state → no-op


def test_never_deletes_a_non_anchor_title_match():
    # No cached anchor; a title-matching playlist exists but is adopted (NOT deleted) and
    # then diffed in place. The delete verb must never fire on a title-match.
    cache = _Cache({"plex/playlists/tv_plan/rob": _plan("a", "b")})
    api = _FakeAPI(token="OWNER",
                   items_by_rk={"777": [{"ratingKey": "a", "playlistItemID": "p1"}]},
                   playlists=[{"ratingKey": "777", "title": "Rob Up Next", "playlistType": "video"}])
    m = _mgr(cache, api)
    users = _Users({"rob": "OWNER"}); users.tracked_users = [_OWNER_USER]
    m._writeback([_OWNER_USER], [{"uuid": "u-rob"}], users, _TV_INV, {})
    assert not any(w[0] == "delete" for w in api.writes)           # adopted, never deleted
    assert cache.get(f"{_ANCHOR_KEY}/rob") == "777"               # adopted as the anchor
    assert any(w[0] == "add" for w in api.writes)                 # 'b' added in place


# ── P0 #4: ratingKey re-resolution drops stale items / skips on large drift ─────
def test_stale_items_dropped_by_reresolution():
    # Plan references 'a' (still valid) and 'z' (gone from fresh inventory) → only 'a' written.
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "z")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    create = [w for w in api.writes if w[0] == "create"][0]
    assert create[2]["items"] == ["a"]                            # 'z' dropped, 'a' kept


def test_large_drift_skips_user():
    # 2 of 3 items stale (>50%) → user skipped entirely, no write.
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "y", "z")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert api.writes == []
    assert any("exceeds" in w for w in m.logger.warns)


# ── P0 #5: in-place diff + steady-state no-op ──────────────────────────────────
def test_steady_state_is_a_noop():
    cache = _Cache({
        "plex/playlists/tv_plan/rob": _plan("a", "b"),
        f"{_ANCHOR_KEY}/rob": "555",
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [
        {"ratingKey": "a", "playlistItemID": "p1"}, {"ratingKey": "b", "playlistItemID": "p2"}]})
    m = _mgr(cache, api)
    users = _Users({"rob": "OWNER"}); users.tracked_users = [_OWNER_USER]
    stats = m._writeback([_OWNER_USER], [{"uuid": "u-rob"}], users, _TV_INV, {})
    assert api.writes == []
    assert stats["updated"] == 0 and stats["created"] == 0


def test_in_place_diff_adds_missing_item_without_recreating():
    # Anchor has [a]; desired [a, b] → one add (no delete/recreate), b only.
    cache = _Cache({
        "plex/playlists/tv_plan/rob": _plan("a", "b"),
        f"{_ANCHOR_KEY}/rob": "555",
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [{"ratingKey": "a", "playlistItemID": "p1"}]})
    m = _mgr(cache, api)
    users = _Users({"rob": "OWNER"}); users.tracked_users = [_OWNER_USER]
    m._writeback([_OWNER_USER], [{"uuid": "u-rob"}], users, _TV_INV, {})
    adds = [w for w in api.writes if w[0] == "add"]
    assert len(adds) == 1 and adds[0][2] == ["b"]
    assert not any(w[0] == "delete" for w in api.writes)


# ── P0 #6: orphan cleanup vs the LIVE roster (PIN-skip left alone) ──────────────
def test_orphan_left_alone_when_user_still_in_roster_pin_skipped():
    # 'kid' has a managed anchor but is NOT tracked this run (PIN-mint failed). It is STILL in
    # the live roster → its playlist must be LEFT ALONE (not deleted).
    cache = _Cache({
        f"{_ANCHOR_KEY}/kid": "555",
        f"{_ANCHOR_KEY}/_index": {"kid": "555"},
        "plex/identity_map": {"u-kid": {"safe_key": "kid"}},
    })
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    users = _Users({}); users.tracked_users = []          # kid NOT tracked (pin-skipped)
    roster = [{"uuid": "u-kid", "title": "Kid"}]          # but present in the live roster
    stats = m._writeback([], roster, users, {}, {})
    assert not any(w[0] == "delete" for w in api.writes)  # left alone
    assert stats["orphans"] == 0


def test_anchor_get_treats_empty_dict_cache_sentinel_as_no_anchor():
    # REGRESSION: the real file cache returns {} (not None) for a MISSING key. _anchor_get must
    # treat that as "no anchor", else _handle_empty logs a bogus "would DELETE" for a playlist that
    # was never created (seen in a real dry-run: a 'would DELETE' per user for the disabled family).
    m = _mgr(_Cache({f"{_ANCHOR_KEY}/kid::Fresh Arrivals": {}}), _FakeAPI())
    assert m._anchor_get("kid", "Fresh Arrivals") is None
    # and end-to-end: a disabled family whose anchor key is the {} sentinel → no spurious delete.
    cache = _Cache({
        "plex/playlists/combined_plan/kid": _plan("a"),
        f"{_ANCHOR_KEY}/kid::Fresh Arrivals": {},
    })
    api = _FakeAPI(token="OWNER")
    cfg = {"plex": {"playlists": {"writeback": {"enabled": True}, "mood_lists": {"enabled": True}}}}
    mm = _mgr(cache, api, config=cfg)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = mm._writeback([_KID_USER], [], users, _TV_INV, {})
    spew = mm.logger.infos + mm.logger.files.get("playlists", [])
    assert not any("would DELETE" in i for i in spew) and stats["deleted"] == 0


def test_large_drift_leaves_existing_playlist_untouched():
    # REGRESSION (review): a transient >50% drift run must NOT delete the user's existing managed
    # playlist — it returns None (skip), the next clean run rewrites it.
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("a", "y", "z"),    # 2/3 stale (>50%)
        f"{_ANCHOR_KEY}/kid": "555",
        f"{_ANCHOR_KEY}/_index": {"kid": "555"},
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [{"ratingKey": "a", "playlistItemID": "p1"}]})
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [{"uuid": "u-kid"}], users, _TV_INV, {})
    assert not any(w[0] == "delete" for w in api.writes)        # NOT deleted
    assert cache.get(f"{_ANCHOR_KEY}/kid") == "555"             # anchor intact


# ── multiple managed playlists (The Long Glide / Touch & Go) ───────────────────
def test_disabled_family_tears_down_its_leftover_playlist():
    # REGRESSION (review): mood_lists was ON (created "Kid The Long Glide"); turned OFF → that
    # playlist must be deleted, not stranded on the member account.
    cache = _Cache({
        "plex/playlists/combined_plan/kid": _plan("a"),
        f"{_ANCHOR_KEY}/kid::The Long Glide": "556",
        f"{_ANCHOR_KEY}/_index": {"kid::The Long Glide": "556"},
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"556": [{"ratingKey": "a", "playlistItemID": "p1"}]})
    m = _mgr(cache, api)                                        # default config: mood_lists OFF
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [{"uuid": "u-kid"}], users, _TV_INV, {})
    assert ("delete", "556", None, "KIDTOK") in api.writes      # the now-disabled family torn down
    assert cache.get(f"{_ANCHOR_KEY}/kid::The Long Glide") is None



def test_writes_one_playlist_per_enabled_family():
    cache = _Cache({
        "plex/playlists/combined_plan/kid": _plan("a", "b"),   # Up Next
        "plex/playlists/glide_plan/kid": _plan("a"),           # The Long Glide
        "plex/playlists/touchgo_plan/kid": _plan("b"),         # Touch & Go
    })
    api = _FakeAPI(token="OWNER")
    cfg = {"plex": {"playlists": {"writeback": {"enabled": True}, "mood_lists": {"enabled": True}}}}
    m = _mgr(cache, api, config=cfg)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    titles = {w[2]["title"] for w in api.writes if w[0] == "create"}
    assert titles == {"Kid Up Next", "Kid The Long Glide", "Kid Touch & Go"}
    assert stats["created"] == 3
    assert cache.get(f"{_ANCHOR_KEY}/kid") is not None                     # Up Next at the LEGACY key
    assert cache.get(f"{_ANCHOR_KEY}/kid::The Long Glide") is not None     # extra family namespaced


def test_extra_family_not_written_when_flag_off():
    cache = _Cache({
        "plex/playlists/combined_plan/kid": _plan("a"),
        "plex/playlists/glide_plan/kid": _plan("a"),           # cached but mood_lists OFF
    })
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)                                        # default config: mood_lists absent → off
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert {w[2]["title"] for w in api.writes if w[0] == "create"} == {"Kid Up Next"}
    assert stats["created"] == 1                               # only Up Next


def test_twih_families_written_when_enabled():
    # this_week_in_history ON + cached anniversary plans ⇒ the two new families are created.
    cache = _Cache({
        "plex/playlists/twih_movie_plan/kid": _plan("a"),      # Anniversary Picks (owned movie rk 'a')
        "plex/playlists/twih_show_plan/kid": _plan("c"),       # On This Week (owned episode rk 'c')
    })
    api = _FakeAPI(token="OWNER")
    cfg = {"plex": {"playlists": {"writeback": {"enabled": True},
                                  "this_week_in_history": {"enabled": True}}}}
    m = _mgr(cache, api, config=cfg)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    titles = {w[2]["title"] for w in api.writes if w[0] == "create"}
    assert titles == {"Kid Anniversary Picks", "Kid On This Week"}
    assert cache.get(f"{_ANCHOR_KEY}/kid::Anniversary Picks") is not None
    assert cache.get(f"{_ANCHOR_KEY}/kid::On This Week") is not None


def test_twih_family_torn_down_when_flag_off():
    # feature OFF ⇒ a leftover "Anniversary Picks" playlist is deleted, never stranded.
    cache = _Cache({
        "plex/playlists/twih_movie_plan/kid": _plan("a"),       # cached but feature OFF
        f"{_ANCHOR_KEY}/kid::Anniversary Picks": "777",
        f"{_ANCHOR_KEY}/_index": {"kid::Anniversary Picks": "777"},
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"777": [{"ratingKey": "a", "playlistItemID": "p1"}]})
    m = _mgr(cache, api)                                         # default config: feature absent → off
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [{"uuid": "u-kid"}], users, _TV_INV, {})
    assert ("delete", "777", None, "KIDTOK") in api.writes
    assert cache.get(f"{_ANCHOR_KEY}/kid::Anniversary Picks") is None


def test_orphan_sweep_deletes_namespaced_extra_family_anchor():
    cache = _Cache({
        f"{_ANCHOR_KEY}/gone::Touch & Go": "556",
        f"{_ANCHOR_KEY}/_index": {"gone": "555", "gone::Touch & Go": "556"},
    })
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    users = _Users({}); users.tracked_users = []
    stats = m._writeback([], [], users, {}, {})                # departed → both anchors swept
    deleted = {w[1] for w in api.writes if w[0] == "delete"}
    assert deleted == {"555", "556"} and stats["deleted"] == 2


def test_orphan_deleted_when_user_gone_from_roster():
    cache = _Cache({
        f"{_ANCHOR_KEY}/gone": "555",
        f"{_ANCHOR_KEY}/_index": {"gone": "555"},
    })
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    users = _Users({}); users.tracked_users = []
    stats = m._writeback([], [], users, {}, {})           # empty roster → user truly departed
    assert ("delete", "555", None, "OWNER") in api.writes
    assert stats["deleted"] == 1 and stats["orphans"] == 1


def test_orphan_sweep_inert_when_disarmed():
    cache = _Cache({
        f"{_ANCHOR_KEY}/gone": "555",
        f"{_ANCHOR_KEY}/_index": {"gone": "555"},
    })
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api, config={"plex": {"playlists": {"writeback": {"enabled": False}}}})
    m._writeback([], [], _Users({}), {}, {})
    assert api.writes == []                               # disarmed → no delete


# ── exclude_users + empty restricted plan ──────────────────────────────────────
def test_excluded_user_skipped_and_counted():
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api, config={"plex": {"playlists": {
        "writeback": {"enabled": True}, "exclude_users": ["Kid"]}}})
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert api.writes == []
    assert stats["skipped"] == 1


def test_empty_plan_deletes_existing_managed_playlist():
    # A restricted user whose plan age-gates to empty → delete any existing managed anchor.
    cache = _Cache({
        "plex/playlists/tv_plan/kid": {"items": []},
        f"{_ANCHOR_KEY}/kid": "555",
        f"{_ANCHOR_KEY}/_index": {"kid": "555"},
    })
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [{"uuid": "u-kid"}], users, _TV_INV, {})
    assert ("delete", "555", None, "KIDTOK") in api.writes
    assert cache.get(f"{_ANCHOR_KEY}/kid") is None
    assert stats["deleted"] == 1


def test_banner_logged_every_run():
    api = _FakeAPI()
    m = _mgr(_Cache(), api, config={"plex": {"playlists": {"writeback": {"enabled": False}}}})
    m._writeback([], [], _Users({}), {}, {})
    assert any("disarmed" in i for i in m.logger.infos)


# ── tiny registry stand-in for the one run() that reads it ─────────────────────
class _Reg:
    def __init__(self, users): self._users = users
    def get(self, kind, name): return self._users if name == "PlexUsersManager" else None
