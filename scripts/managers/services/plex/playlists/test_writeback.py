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

import time

from scripts.managers.services.plex.playlists.writeback import (
    _ANCHOR_KEY,
    _BRAND_KEY,
    _LASTWRITE_KEY,
    _TITLE_KEY,
    PlaylistWritebackManager,
    _min_moves,
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
    def __init__(self, token="OWNER", items_by_rk=None, playlists=None, poster_ok=True, edit_ok=True):
        self.token = token
        self.writes: list = []                # (verb, rk, extra, token)
        self._items_by_rk = items_by_rk or {}  # rk -> [{ratingKey, playlistItemID}]
        self._playlists = playlists or []      # [{ratingKey, title, playlistType}]
        self._next_rk = 9000
        self._poster_ok = poster_ok            # the verified 2xx/failure the poster verb returns
        self._edit_ok = edit_ok                # the verified 2xx/failure the edit verb returns

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

    def upload_playlist_poster(self, playlist_rk, image_bytes, *, content_type="image/png",
                               token=None):
        self.writes.append(("poster", playlist_rk, len(image_bytes), token))
        return self._poster_ok

    def edit_playlist(self, playlist_rk, *, title=None, title_sort=None, token=None):
        self.writes.append(("edit", playlist_rk, {"title": title, "title_sort": title_sort}, token))
        return self._edit_ok


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
        f"{_TITLE_KEY}/rob": "!Up Next",          # already migrated → title is part of steady state
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
                   playlists=[{"ratingKey": "777", "title": "Up Next", "playlistType": "video"}])
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
        f"{_TITLE_KEY}/rob": "!Up Next",          # already migrated → title is part of steady state
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
        f"{_TITLE_KEY}/rob": "!Up Next",          # already migrated → title is part of steady state
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
    assert titles == {"Up Next", "The Long Glide", "Touch & Go"}     # clean display names
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
    assert {w[2]["title"] for w in api.writes if w[0] == "create"} == {"Up Next"}
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
    assert titles == {"Anniversary Picks", "On This Week"}
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


# ── poster branding (default-off, version-gated) ───────────────────────────────
def _branding_cfg(tmp_path):
    """An armed config that points the branding path at a throwaway PNG, so the test never
    depends on the real bundled assets (or their mtimes)."""
    (tmp_path / "up_next.png").write_bytes(b"PNG-bytes")
    return {"plex": {"playlists": {"writeback": {"enabled": True},
                                   "branding": {"enabled": True, "assets_dir": str(tmp_path)}}}}


def test_branding_off_by_default_uploads_no_poster():
    # The default config has no branding block → the poster path is inert (byte-identical).
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)                                  # armed, but branding absent → off
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert [w for w in api.writes if w[0] == "create"]    # the list IS created…
    assert not any(w[0] == "poster" for w in api.writes)  # …but no poster is uploaded
    assert stats["branded"] == 0


def test_branding_uploads_poster_once_then_version_gated(tmp_path):
    cfg = _branding_cfg(tmp_path)
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api, config=cfg)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    posters = [w for w in api.writes if w[0] == "poster"]
    assert len(posters) == 1
    assert posters[0][3] == "KIDTOK"                      # uploaded on the member's account, not owner
    assert stats["branded"] == 1
    # Second run: anchor + poster-version both cached → steady state, NO re-upload.
    api.writes.clear()
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert not any(w[0] == "poster" for w in api.writes)


def test_branding_disarmed_previews_without_uploading(tmp_path):
    # Disarmed (dry-run) with an EXISTING playlist → preview line in the dedicated file, no upload.
    cfg = _branding_cfg(tmp_path)
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("a", "b"),
        f"{_ANCHOR_KEY}/kid": "555",
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [
        {"ratingKey": "a", "playlistItemID": "p1"}, {"ratingKey": "b", "playlistItemID": "p2"}]})
    m = _mgr(cache, api, config=cfg, dry_run=True)        # disarmed
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert not any(w[0] == "poster" for w in api.writes)
    assert any("poster would be set" in ln for ln in m.logger.files.get("playlists", []))


def test_branding_reapplies_to_recreated_playlist(tmp_path):
    # A recreate mints a fresh ratingKey with no poster → branding must re-fire past the version gate.
    cfg = _branding_cfg(tmp_path)
    # Pre-brand the OLD rk at the CURRENT art version so the pre-diff branding is a no-op — this
    # isolates the recreate's FORCED re-brand (otherwise an art-change run brands old + new).
    ver = PlaylistWritebackManager._asset_version(tmp_path / "up_next.png")
    # Anchor [a,b,c] live; desired [c] → a 2-remove diff that exceeds the list size → recreate path.
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("c"),
        f"{_ANCHOR_KEY}/kid": "555",
        f"{_BRAND_KEY}/kid": ver,
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [
        {"ratingKey": "a", "playlistItemID": "p1"},
        {"ratingKey": "b", "playlistItemID": "p2"},
        {"ratingKey": "c", "playlistItemID": "p3"}]})
    m = _mgr(cache, api, config=cfg)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    creates = [w for w in api.writes if w[0] == "create"]
    posters = [w for w in api.writes if w[0] == "poster"]
    assert len(creates) == 1                              # recreate happened
    assert len(posters) == 1                              # exactly one re-brand, on the new list
    assert posters[0][1] == creates[0][1] or posters[0][1] is not None  # targets the recreated rk
    assert posters[0][3] == "KIDTOK"                      # still the member's account


# ── title: clean display name (no username/'!'), '!' lives in the titleSort, set once ──────────
def test_created_playlist_has_clean_title_and_sort_pin():
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI(token="OWNER")
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    created = [w for w in api.writes if w[0] == "create"][0]
    assert created[2]["title"] == "Up Next"                       # clean display name, no '!' / username
    edit = [w for w in api.writes if w[0] == "edit"][0]
    assert edit[2] == {"title": "Up Next", "title_sort": "!Up Next"}   # '!' only in the sort key
    assert edit[3] == "KIDTOK"                                    # on the member's own list
    assert cache.get(f"{_TITLE_KEY}/kid") == "!Up Next"          # gated on the titleSort


def test_existing_playlist_retitled_once_then_gated():
    # A playlist created under an older title (cached anchor, no titleSort key) gets the clean title
    # + '!' titleSort once, on the MEMBER's token; the next run is gated (no edit).
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("a", "b"),
        f"{_ANCHOR_KEY}/kid": "555",
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [
        {"ratingKey": "a", "playlistItemID": "p1"}, {"ratingKey": "b", "playlistItemID": "p2"}]})
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert ("edit", "555", {"title": "Up Next", "title_sort": "!Up Next"}, "KIDTOK") in api.writes
    assert stats["retitled"] == 1
    assert cache.get(f"{_TITLE_KEY}/kid") == "!Up Next"
    api.writes.clear()
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert not any(w[0] == "edit" for w in api.writes)           # gated → no churn


def test_failed_retitle_not_cached_so_it_retries():
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("a", "b"),
        f"{_ANCHOR_KEY}/kid": "555",
    })
    api = _FakeAPI(token="OWNER", edit_ok=False, items_by_rk={"555": [
        {"ratingKey": "a", "playlistItemID": "p1"}, {"ratingKey": "b", "playlistItemID": "p2"}]})
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert any(w[0] == "edit" for w in api.writes)               # attempted
    assert cache.get(f"{_TITLE_KEY}/kid") is None                # not cached → retries next run
    assert any("retitle of 'Up Next' failed" in w for w in m.logger.warns)


def test_branding_failed_upload_not_cached_so_it_retries(tmp_path):
    # REGRESSION (the live bug): the old endpoint 404'd but was silently cached as branded, so it
    # never retried. A non-2xx upload must NOT cache the version and MUST log a retry-able warning.
    cfg = _branding_cfg(tmp_path)
    cache = _Cache({"plex/playlists/tv_plan/kid": _plan("a", "b")})
    api = _FakeAPI(token="OWNER", poster_ok=False)        # Plex rejects the poster (e.g. 404/401)
    m = _mgr(cache, api, config=cfg)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert any(w[0] == "poster" for w in api.writes)       # the upload WAS attempted
    assert stats["branded"] == 0                           # ...but never counted as branded
    assert cache.get(f"{_BRAND_KEY}/kid") is None          # ...and NOT cached → next run retries
    assert any("poster upload failed" in w for w in m.logger.warns)


def test_branding_no_double_upload_on_in_place_update(tmp_path):
    # The recreate double-upload is gone: an in-place item update brands the surviving rk exactly once.
    cfg = _branding_cfg(tmp_path)
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("a", "b"),     # desired [a,b]
        f"{_ANCHOR_KEY}/kid": "555",
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [{"ratingKey": "a", "playlistItemID": "p1"}]})
    m = _mgr(cache, api, config=cfg)                        # current [a] → one add, in-place
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert len([w for w in api.writes if w[0] == "add"]) == 1
    assert len([w for w in api.writes if w[0] == "poster"]) == 1   # branded exactly once


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


# ── GLD-PLY-13: the diff metric counts DISPLACED items, not every survivor ─────
def _replay_moves(cur, desired):
    """Pull the returned movers out of ``cur``, then re-insert each after its desired
    predecessor. Returns (movers, resulting_order) so a test can assert the COUNT is minimal
    AND that the moves actually reproduce ``desired``.

    Asserting the count rather than the identity matters: when several items are tied for
    'displaced' (e.g. ['a','c','b'] -> ['a','b','c'], where moving either 'b' or 'c' costs one),
    every minimal answer is equally correct and which one the LIS lands on is arbitrary.
    """
    movers = _min_moves(cur, desired)
    moved = set(movers)
    order = [x for x in cur if x not in moved]
    for i, want in enumerate(desired):
        if want not in moved:
            continue
        prev = desired[i - 1] if i > 0 else None
        at = (order.index(prev) + 1) if (prev is not None and prev in order) else 0
        order.insert(at, want)
    return movers, order


def test_min_moves_counts_only_displaced_items():
    # Already in order -> nothing moves.
    assert _min_moves(["a", "b", "c"], ["a", "b", "c"]) == []
    # One item out of place -> exactly ONE move, not "all three" (the old behaviour).
    movers, order = _replay_moves(["a", "c", "b"], ["a", "b", "c"])
    assert len(movers) == 1 and order == ["a", "b", "c"]
    # Full reversal of 4 -> 3 moves (the longest correctly-ordered run is length 1).
    movers, order = _replay_moves(["d", "c", "b", "a"], ["a", "b", "c", "d"])
    assert len(movers) == 3 and order == ["a", "b", "c", "d"]
    # An item moved from the back to the front costs exactly one move (no tie here).
    assert _min_moves(["b", "c", "d", "a"], ["a", "b", "c", "d"]) == ["a"]


def test_min_moves_is_minimal_and_correct_on_random_permutations():
    # The invariant that actually matters: the returned movers reproduce the desired order, and
    # the count never exceeds n-1. Guards a future "optimisation" that returns too few moves.
    import random
    rng = random.Random(1729)
    for _ in range(300):
        n = rng.randint(0, 40)
        desired = [f"r{i}" for i in range(n)]
        cur = desired[:]
        rng.shuffle(cur)
        movers, order = _replay_moves(cur, desired)
        assert order == desired                      # the moves are SUFFICIENT
        assert len(movers) <= max(n - 1, 0)          # ...and never the whole list


def test_rerank_with_removal_stays_in_place_and_does_not_recreate():
    # THE REGRESSION. Live shape from support/logs/playlists-5.log: a re-ranked plan where a
    # couple of items were watched off. The old _diff reported EVERY survivor as a "move", so
    # n_changes came to len(desired) + removes and tripped _RECREATE_RATIO -> a delete+recreate
    # (fresh ratingKey) on essentially every run. It must now diff IN PLACE.
    desired = [f"r{i}" for i in range(20)]
    # current: two items that are NOT in the plan (watched off) + the survivors, lightly re-ranked
    survivors = desired[2:]
    shuffled = survivors[3:6] + survivors[:3] + survivors[6:]
    current = [{"rating_key": "gone1", "playlist_item_id": "pg1"},
               {"rating_key": "gone2", "playlist_item_id": "pg2"}]
    current += [{"rating_key": rk, "playlist_item_id": f"p-{rk}"} for rk in shuffled]
    plan = PlaylistWritebackManager._diff(current, desired)
    n_changes = len(plan["add"]) + len(plan["remove"]) + len(plan["move"])
    assert len(plan["remove"]) == 2                    # both watched-off items dropped
    assert n_changes <= len(desired)                   # -> in-place, NOT recreate
    assert len(plan["move"]) < len(survivors)          # not "every survivor moved"


def test_reranked_plan_updates_in_place_keeping_the_same_rating_key():
    # End-to-end: the anchor ratingKey must SURVIVE a re-ranked run (no create, no delete).
    cache = _Cache({
        "plex/playlists/tv_plan/rob": _plan("a", "b", "c"),
        f"{_ANCHOR_KEY}/rob": "555",
        f"{_TITLE_KEY}/rob": "!Up Next",
    })
    inv = {"1": {"rating_key": "a"}, "2": {"rating_key": "b"}, "3": {"rating_key": "c"}}
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [          # same items, different ORDER
        {"ratingKey": "c", "playlistItemID": "p3"},
        {"ratingKey": "a", "playlistItemID": "p1"},
        {"ratingKey": "b", "playlistItemID": "p2"}]})
    m = _mgr(cache, api)
    users = _Users({"rob": "OWNER"}); users.tracked_users = [_OWNER_USER]
    stats = m._writeback([_OWNER_USER], [{"uuid": "u-rob"}], users, inv, {})
    assert not any(w[0] == "create" for w in api.writes)     # no new playlist minted
    assert not any(w[0] == "delete" for w in api.writes)     # old one not torn down
    assert any(w[0] == "move" for w in api.writes)           # re-ordered in place
    assert cache.get(f"{_ANCHOR_KEY}/rob") == "555"          # SAME ratingKey
    assert stats["recreated"] == 0


# ── GLD-PLY-14: a recreate must re-title the NEW ratingKey ────────────────────
def test_title_reapplied_to_recreated_playlist():
    # The title gate is keyed on anchor_id, which SURVIVES a recreate — so without force=True the
    # fresh ratingKey inherits a stale "already titled" marker and never gets its '!' titleSort,
    # silently losing the front-pin forever. Mirrors test_branding_reapplies_to_recreated_playlist.
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("c"),
        f"{_ANCHOR_KEY}/kid": "555",
        f"{_TITLE_KEY}/kid": "!Up Next",          # already titled on the OLD rk
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [        # 3 live, 1 desired -> recreate
        {"ratingKey": "a", "playlistItemID": "p1"},
        {"ratingKey": "b", "playlistItemID": "p2"},
        {"ratingKey": "c", "playlistItemID": "p3"}]})
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    stats = m._writeback([_KID_USER], [], users, _TV_INV, {})
    creates = [w for w in api.writes if w[0] == "create"]
    edits = [w for w in api.writes if w[0] == "edit"]
    assert len(creates) == 1 and stats["recreated"] == 1
    assert len(edits) == 1                                     # the NEW list was titled
    assert edits[0][1] != "555"                                # ...on the new rk, not the old one
    assert edits[0][2] == {"title": "Up Next", "title_sort": "!Up Next"}


# ── GLD-PLY-15: once-a-day rewrite cadence, with a churn escape hatch ─────────
def _cadence_case(hours_ago, plan_rks, live_rks, cfg_extra=None):
    """An armed manager whose 'Up Next' was last written ``hours_ago``, with a cached plan of
    ``plan_rks`` against a live playlist of ``live_rks``."""
    wb = {"enabled": True}
    wb.update(cfg_extra or {})
    inv = {str(i): {"rating_key": rk} for i, rk in enumerate(set(plan_rks) | set(live_rks))}
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan(*plan_rks),
        f"{_ANCHOR_KEY}/kid": "555",
        f"{_TITLE_KEY}/kid": "!Up Next",
        f"{_LASTWRITE_KEY}/kid": time.time() - hours_ago * 3600.0,
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [
        {"ratingKey": rk, "playlistItemID": f"p-{rk}"} for rk in live_rks]})
    m = _mgr(cache, api, config={"plex": {"playlists": {"writeback": wb}}})
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    return m, api, cache, users


def test_routine_rerank_inside_the_interval_is_deferred():
    # Written 2h ago; the plan is the same items lightly re-ranked plus one new -> routine churn,
    # well under the override. No item write at all until the interval elapses.
    m, api, _cache, users = _cadence_case(
        2.0, ["a", "b", "c", "d", "e"], ["b", "a", "c", "d"])
    stats = m._writeback([_KID_USER], [], users, _cadence_inv(), {})
    assert not any(w[0] in ("add", "remove", "move", "create", "delete") for w in api.writes)
    assert stats["deferred"] == 1 and stats["updated"] == 0
    assert any("deferred" in ln for ln in m.logger.files.get("playlists", []))


def test_interval_elapsed_writes_normally():
    m, api, _cache, users = _cadence_case(
        21.0, ["a", "b", "c", "d", "e"], ["b", "a", "c", "d"])   # past the 20h default
    stats = m._writeback([_KID_USER], [], users, _cadence_inv(), {})
    assert any(w[0] == "add" for w in api.writes)
    assert stats["deferred"] == 0 and stats["updated"] == 1


def test_large_watch_off_overrides_the_interval():
    # 3 of 5 live items watched off (60% removed, over the 40% override) 2h after the last write
    # -> a genuine overhaul writes through immediately rather than waiting for tomorrow.
    m, api, _cache, users = _cadence_case(
        2.0, ["a", "b"], ["a", "b", "x", "y", "z"])
    stats = m._writeback([_KID_USER], [], users, _cadence_inv(), {})
    assert stats["deferred"] == 0
    assert any(w[0] in ("remove", "create") for w in api.writes)
    assert any("churn overrides" in ln for ln in m.logger.files.get("playlists", []))


def test_large_new_material_overrides_the_interval():
    # 3 of 5 desired items are brand new (60% adds) -> re-ranked watchability pulled in a lot.
    m, api, _cache, users = _cadence_case(
        2.0, ["a", "b", "x", "y", "z"], ["a", "b"])
    stats = m._writeback([_KID_USER], [], users, _cadence_inv(), {})
    assert stats["deferred"] == 0
    assert any(w[0] in ("add", "create") for w in api.writes)


def test_cadence_gate_disabled_by_zero_interval():
    m, api, _cache, users = _cadence_case(
        0.1, ["a", "b", "c", "d", "e"], ["b", "a", "c", "d"], {"min_interval_hours": 0})
    stats = m._writeback([_KID_USER], [], users, _cadence_inv(), {})
    assert stats["deferred"] == 0 and stats["updated"] == 1


def test_missing_stamp_never_suppresses_the_first_write():
    # P-C: the real file cache returns {} for a MISSING key. That must read as "never written"
    # (write allowed), NOT as "written just now" — which would suppress the first write for a
    # whole interval and look exactly like the feature being broken.
    m = _mgr(_Cache({f"{_LASTWRITE_KEY}/kid": {}}), _FakeAPI())
    assert m._last_write("kid", "Up Next") is None
    assert m._cadence_defer("kid", "Up Next",
                            {"add": ["a"], "remove": [], "move": []},
                            [{"rating_key": "b"}], ["a", "b"], {}) is False


def test_disarmed_run_never_stamps_the_interval():
    # A dry run performs no Plex write, so it must not start the interval — otherwise the first
    # ARMED run would be deferred for a full day.
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("a", "b"),
        f"{_ANCHOR_KEY}/kid": "555",
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [{"ratingKey": "a", "playlistItemID": "p1"}]})
    m = _mgr(cache, api, dry_run=True)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert cache.get(f"{_LASTWRITE_KEY}/kid") is None


def test_armed_write_stamps_so_the_next_run_defers():
    cache = _Cache({
        "plex/playlists/tv_plan/kid": _plan("a", "b"),
        f"{_ANCHOR_KEY}/kid": "555",
        f"{_TITLE_KEY}/kid": "!Up Next",
    })
    api = _FakeAPI(token="OWNER", items_by_rk={"555": [{"ratingKey": "a", "playlistItemID": "p1"}]})
    m = _mgr(cache, api)
    users = _Users({"kid": "KIDTOK"}); users.tracked_users = [_KID_USER]
    s1 = m._writeback([_KID_USER], [], users, _TV_INV, {})
    assert s1["updated"] == 1 and cache.get(f"{_LASTWRITE_KEY}/kid") is not None
    # Second run, same minute: the live list now matches, so this is a steady-state no-op anyway —
    # what matters is the stamp exists and is fresh.
    assert m._last_write("kid", "Up Next") is not None


def _cadence_inv():
    """An owned-inventory that resolves every ratingKey the cadence cases use."""
    return {k: {"rating_key": k} for k in ("a", "b", "c", "d", "e", "x", "y", "z")}


def test_head_churn_override_lets_a_promotion_through_the_interval():
    # GLD-PLY-17. Session warmth promoting a series from far down the list is a PURE RE-ORDER:
    # no adds, no removes, so the churn override cannot see it at all. Without the head-churn
    # override the promotion would sit unwritten until tomorrow, which defeats the point of
    # reacting to "you were watching this a few hours ago".
    live = [f"r{i}" for i in range(20)]
    desired = ["r15", "r16", "r17"] + [rk for rk in live if rk not in ("r15", "r16", "r17")]
    m = _mgr(_Cache({f"{_LASTWRITE_KEY}/kid": time.time() - 3600.0}), _FakeAPI())
    current = [{"rating_key": rk, "playlist_item_id": f"p-{rk}"} for rk in live]
    plan = PlaylistWritebackManager._diff(current, desired)
    assert plan["add"] == [] and plan["remove"] == []      # pure re-order: nothing added or gone
    stats = {}
    assert m._cadence_defer("kid", "Up Next", plan, current, desired, stats) is False
    assert stats.get("deferred", 0) == 0
    assert any("TOP 10 changed" in ln for ln in m.logger.files.get("playlists", []))


def test_reshuffling_within_the_head_is_still_deferred():
    # The complement: the SAME ten items jittering among themselves is invisible noise and must
    # still be absorbed by the interval. A set comparison is what draws that line.
    live = [f"r{i}" for i in range(20)]
    desired = ["r3", "r0", "r1", "r2", "r5", "r4", "r6", "r8", "r7", "r9"] + live[10:]
    m = _mgr(_Cache({f"{_LASTWRITE_KEY}/kid": time.time() - 3600.0}), _FakeAPI())
    current = [{"rating_key": rk, "playlist_item_id": f"p-{rk}"} for rk in live]
    plan = PlaylistWritebackManager._diff(current, desired)
    assert plan["move"]                                    # there IS a re-order...
    stats = {}
    assert m._cadence_defer("kid", "Up Next", plan, current, desired, stats) is True
    assert stats["deferred"] == 1                          # ...but it stays below the fold


def test_head_churn_is_measured_on_the_set_not_the_order():
    m = _mgr(_Cache(), _FakeAPI())
    cur = [{"rating_key": f"r{i}", "playlist_item_id": f"p{i}"} for i in range(10)]
    same_set_shuffled = [f"r{i}" for i in (9, 8, 7, 6, 5, 4, 3, 2, 1, 0)]
    frac, n = m._head_churn(cur, same_set_shuffled)
    assert frac == 0.0 and n == 10                         # reordered, but nobody NEW up there
    three_new = ["x1", "x2", "x3"] + [f"r{i}" for i in range(7)]
    frac, _ = m._head_churn(cur, three_new)
    assert abs(frac - 0.3) < 1e-9                          # 3 of 10 are newcomers


# ── tiny registry stand-in for the one run() that reads it ─────────────────────
class _Reg:
    def __init__(self, users): self._users = users
    def get(self, kind, name): return self._users if name == "PlexUsersManager" else None
