"""TautulliUsersManager._family_scope — household-affinity scoping to Plex HOME members.

``household_affinity.family_only`` keeps non-family accounts (shared friends streaming
remotely) out of the HOUSEHOLD genre/actor/director maps while their per-user matrices
still build. The family definition is the identity map the Plex users pass persisted
LAST run (`plex/identity_map` — this manager aggregates before PlexUsersManager runs),
resolved to the stable Tautulli ``user_id`` the history rows carry. Fail direction is
OPEN and loud: an absent map ({} — this cache's missing-key sentinel), an id-less map,
or a raising read skips scoping for the run with a warning, because an EMPTY household
aggregate would hurt the whole system far more than one run of outsider drift."""
from __future__ import annotations

from scripts.managers.services.tautulli.users import (
    TautulliUsersManager,
    _FAMILY_IDENTITY_KEY,
)


class _Log:
    """Stub with the same SURFACE as LoggerManager, log_table included. A stub that
    omits a method the code under test calls does not model the dependency -- it
    just hides an AttributeError until production."""
    def __init__(self):
        self.warns, self.infos, self.tables = [], [], []
    def log_info(self, m): self.infos.append(str(m))
    def log_debug(self, m): pass
    def log_error(self, m): pass
    def log_warning(self, m): self.warns.append(str(m))
    def log_table(self, headers, data, title="", descriptions=None, caption=""):
        self.tables.append({"headers": list(headers), "rows": [list(r) for r in data],
                            "title": title, "caption": caption})


class _GC:
    """Mirrors GlobalCacheManager's missing-key sentinel: get() returns {}."""
    def __init__(self, d=None, boom=False):
        self.d = dict(d or {})
        self.boom = boom
    def get(self, k):
        if self.boom:
            raise RuntimeError("cache down")
        return self.d.get(k, {})


def _mgr(cfg=None, gc=None):
    m = object.__new__(TautulliUsersManager)
    m.logger = _Log()
    m.config = cfg or {}
    m.global_cache = gc if gc is not None else _GC()
    return m


_IDMAP = {
    "uuid-a": {"tautulli_username": "Trizzd", "tautulli_user_id": 101, "matched_via": "plex_id"},
    "uuid-b": {"tautulli_username": "Aiden", "tautulli_user_id": 102, "matched_via": "email"},
    # A Home member the crosswalk could not resolve contributes no id — and must not
    # accidentally admit anyone.
    "uuid-c": {"tautulli_username": None, "tautulli_user_id": None, "matched_via": "unmatched"},
}

_HIST = (
    [{"user_id": 101, "user": "Trizzd", "rating_key": i} for i in range(6)]
    + [{"user_id": 102, "user": "Aiden", "rating_key": 10 + i} for i in range(4)]
    + [{"user_id": 999, "user": "stacee", "rating_key": 90 + i} for i in range(5)]
    + [{"user": "mystery", "rating_key": 200}]          # no user_id at all
)


def test_off_by_default_is_byte_identical_passthrough():
    m = _mgr(cfg={})
    ents, note = m._family_scope(list(_HIST))
    assert ents == _HIST and note == "" and not m.logger.warns


def test_family_scope_excludes_non_family_and_keeps_unknown_owner():
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    ents, note = m._family_scope(list(_HIST))
    assert len(ents) == 11                               # 6 + 4 family + 1 unknown-owner
    assert not any(e.get("user") == "stacee" for e in ents)
    assert any(e.get("user") == "mystery" for e in ents)  # unknown kept, by stated choice
    for frag in ("11/16 plays", "2 family id(s)", "excluded 5 play(s)",
                 "1 non-family account(s)", "1 unknown-owner"):
        assert frag in note
    assert not m.logger.warns                            # scoped runs are quiet


def test_summary_log_carries_the_scope_note():
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    m.compute_genre_affinity(list(_HIST), {})
    assert m.logger.infos and "family-scoped" in m.logger.infos[-1]


def test_per_user_matrices_still_build_for_outsiders():
    """The other half of the feature: the outsider LOSES the household vote but KEEPS
    their own grading — compute_per_user_genre_affinity sees the full history."""
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    pu = m.compute_per_user_genre_affinity(
        list(_HIST), {},
        [{"user_id": 101, "username": "Trizzd"}, {"user_id": 999, "username": "stacee"}])
    assert "stacee" in pu and "Trizzd" in pu


def test_absent_map_is_unscoped_with_a_loud_warning():
    """{} is this cache's MISSING sentinel (first run after enabling): scoping skips,
    the aggregate survives, and the log says so."""
    m = _mgr(cfg={"household_affinity": {"family_only": True}}, gc=_GC())
    ents, note = m._family_scope(list(_HIST))
    assert ents == _HIST and note == ""
    assert m.logger.warns and "UNSCOPED" in m.logger.warns[0]


def test_idless_map_and_raising_cache_both_degrade_open():
    m1 = _mgr(cfg={"household_affinity": {"family_only": True}},
              gc=_GC({_FAMILY_IDENTITY_KEY: {"u": {"tautulli_user_id": None}}}))
    assert m1._family_scope(list(_HIST))[0] == _HIST and m1.logger.warns

    m2 = _mgr(cfg={"household_affinity": {"family_only": True}}, gc=_GC(boom=True))
    assert m2._family_scope(list(_HIST))[0] == _HIST and m2.logger.warns


def test_str_and_int_user_ids_are_equivalent():
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: {"u": {"tautulli_user_id": 101}}}))
    ents, _ = m._family_scope([{"user_id": "101", "user": "Trizzd"}])
    assert len(ents) == 1


# per-account grading table
# The affinity line CLAIMS outsiders keep their own grading. These pin the evidence.

_USERS = [
    {"user_id": 101, "username": "Trizzd"},
    {"user_id": 102, "username": "Aiden"},
    {"user_id": 999, "username": "stacee"},      # outside: real plays, must be graded
    {"user_id": 777, "username": "ghost"},       # outside: no plays in the window
]
# keys are STRING rating_keys - the brain looks up str(entry["rating_key"])
_META = {str(i): {"genres": ["Drama"], "actors": ["A"], "directors": ["D"]} for i in range(6)}
_META.update({str(10 + i): {"genres": ["Comedy"], "actors": ["B"]} for i in range(4)})
_META.update({str(90 + i): {"genres": ["Horror", "Drama"], "actors": ["C"]} for i in range(5)})


def _table(m):
    return m.logger.tables[-1] if m.logger.tables else None


def _row(tbl, account):
    return next(r for r in tbl["rows"] if r[0] == account)


def _cell(tbl, account, column):
    """Resolve a cell by HEADER NAME, never by position - the table has gained a
    column twice now, and positional assertions break silently each time."""
    return _row(tbl, account)[tbl["headers"].index(column)]


def test_table_shows_outsiders_graded_while_scoped_out():
    """The whole point: 'stacee' is scope=outside AND carries a real grading."""
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    m.compute_per_user_genre_affinity(list(_HIST), _META, _USERS)
    tbl = _table(m)
    assert tbl is not None and tbl["headers"][:2] == ["Account", "Scope"]
    assert _cell(tbl, "Trizzd", "Scope") == "family"
    assert _cell(tbl, "Aiden", "Scope") == "family"
    assert _cell(tbl, "stacee", "Scope") == "outside"
    assert _cell(tbl, "stacee", "Genres") >= 1           # graded, with content
    assert "Horror" in _cell(tbl, "stacee", "Top genres")
    assert "1 outside account(s) graded" in tbl["caption"]


def test_table_classification_matches_the_aggregate_exactly():
    """A row must never claim 'family' for an account the aggregate excluded."""
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    scoped, note = m._family_scope(list(_HIST))
    kept_ids = {str(e.get("user_id")) for e in scoped if e.get("user_id") is not None}
    m.compute_per_user_genre_affinity(list(_HIST), _META, _USERS)
    tbl = _table(m)
    for user in _USERS:
        scope = _cell(tbl, user["username"], "Scope")
        assert (scope == "family") == (str(user["user_id"]) in kept_ids)


def test_no_history_is_distinct_from_zero_genres():
    """P-C: absent from the result (never graded) vs graded-with-nothing-matched."""
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    # 'ghost' has no plays; Aiden plays exist but with EMPTY metadata -> graded, no genres
    m.compute_per_user_genre_affinity(list(_HIST), {}, _USERS)
    tbl = _table(m)
    assert _cell(tbl, "ghost", "Top genres") == "no history in window"
    assert _cell(tbl, "ghost", "Genres") == "-"
    assert _cell(tbl, "Aiden", "Top genres") == "none"   # scored, matched nothing
    assert _cell(tbl, "Aiden", "Genres") == 0


def test_unresolvable_identity_map_refuses_to_guess():
    """Rather than mislabel a family member as an outsider, the Scope column blanks
    and the caption says scoping is INACTIVE."""
    m = _mgr(cfg={"household_affinity": {"family_only": True}}, gc=_GC())   # {} sentinel
    m.compute_per_user_genre_affinity(list(_HIST), _META, _USERS)
    tbl = _table(m)
    assert {r[tbl["headers"].index("Scope")] for r in tbl["rows"]} == {"-"}
    assert "INACTIVE" in tbl["caption"]


def test_family_only_off_renders_without_claiming_scope():
    m = _mgr(cfg={"household_affinity": {"family_only": False}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    m.compute_per_user_genre_affinity(list(_HIST), _META, _USERS)
    tbl = _table(m)
    assert {r[tbl["headers"].index("Scope")] for r in tbl["rows"]} == {"-"}
    assert "family_only is OFF" in tbl["caption"]


def test_rows_are_plain_ascii_and_ordered_family_first():
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    m.compute_per_user_genre_affinity(list(_HIST), _META, _USERS)
    tbl = _table(m)
    scopes = [r[tbl["headers"].index("Scope")] for r in tbl["rows"]]
    assert scopes == sorted(scopes, key=lambda s: {"family": 0, "outside": 1, "-": 2}[s])
    for r in tbl["rows"]:                      # cp1252 console cannot encode fancy glyphs
        for cell in r:
            str(cell).encode("ascii")


def test_empty_user_list_logs_no_table():
    m = _mgr(cfg={"household_affinity": {"family_only": True}},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    m.compute_per_user_genre_affinity(list(_HIST), _META, [])
    assert m.logger.tables == []


# account_links - two logins, one person, one grading
# Mom + mirandan75 shaped: the same viewer on a managed profile and the owner account.

_LINK_USERS = [
    {"user_id": 101, "username": "Trizzd"},
    {"user_id": 102, "username": "Aiden"},
    {"user_id": 555, "username": "mirandan75"},   # primary
    {"user_id": 556, "username": "Mom"},          # same person, second login
]
_LINK_HIST = (
    [{"user_id": 101, "user": "Trizzd", "rating_key": i} for i in range(6)]
    + [{"user_id": 555, "user": "mirandan75", "rating_key": str(10 + i)} for i in range(4)]
    + [{"user_id": 556, "user": "Mom", "rating_key": str(90 + i)} for i in range(5)]
)
_LINK_CFG = {"household_affinity": {"family_only": False},
             "account_links": [["mirandan75", "Mom"]]}


def test_linked_accounts_grade_as_one_viewer():
    """Both logins' plays land in ONE matrix, and BOTH accounts receive it."""
    m = _mgr(cfg=_LINK_CFG, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    res = m.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    primary, secondary = res["mirandan75"], res["Mom"]
    assert primary["genres"] == secondary["genres"]          # identical recommendations
    assert primary["actors"] == secondary["actors"]
    # the union, not either half: Comedy (mirandan75) AND Horror (Mom)
    assert "Comedy" in primary["genres"] and "Horror" in primary["genres"]
    # unlinked accounts are untouched
    assert "Comedy" not in res["Trizzd"]["genres"]


def test_secondary_would_otherwise_lose_its_grading_entirely():
    """The failure this fan-out prevents: rewriting Mom's plays onto the primary
    leaves Mom matching nothing, so without the fan-out linking would make her
    WORSE off than not linking at all."""
    m = _mgr(cfg=_LINK_CFG, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    linked, note = m._link_history(list(_LINK_HIST), _LINK_USERS)
    assert all(e["user_id"] != 556 for e in linked)          # Mom's plays rewritten
    assert "5 play(s) merged" in note
    res = m.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    assert res.get("Mom") is not None and res["Mom"]["genres"]


def test_linking_does_not_mutate_the_caller_list():
    """compute_genre_affinity shares this list; rewriting user_id in place would
    re-route plays through the FAMILY filter and corrupt the household aggregate."""
    m = _mgr(cfg=_LINK_CFG, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    hist = list(_LINK_HIST)
    before = [dict(e) for e in hist]
    m.compute_per_user_genre_affinity(hist, _META, _LINK_USERS)
    assert hist == before


def test_each_member_gets_its_own_copy():
    """Shared dicts would let a consumer annotating one account write through to
    the other -- invisible until it is a bug in a different package."""
    m = _mgr(cfg=_LINK_CFG, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    res = m.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    res["Mom"]["genres"]["Injected"] = 1.0
    assert "Injected" not in res["mirandan75"]["genres"]


def test_links_are_case_insensitive_and_accept_ids():
    m = _mgr(cfg={"account_links": [[555, "mOm"]]},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    res = m.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    assert res["Mom"]["genres"] == res["mirandan75"]["genres"]


def test_no_links_configured_is_byte_identical():
    m1 = _mgr(cfg={}, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    m2 = _mgr(cfg={"account_links": []}, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    a = m1.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    b = m2.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    assert a == b
    assert a["Mom"]["genres"] != a["mirandan75"]["genres"]    # unlinked = separate
    ents, note = m1._link_history(list(_LINK_HIST), _LINK_USERS)
    assert note == ""


def test_unknown_primary_is_ignored_loudly():
    m = _mgr(cfg={"account_links": [["nosuchuser", "Mom"]]},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    res = m.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    assert any("matches no Tautulli account" in w for w in m.logger.warns)
    assert res["Mom"]["genres"] != res["mirandan75"]["genres"]   # unchanged


def test_single_member_group_is_a_noop():
    m = _mgr(cfg={"account_links": [["Mom"]]}, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    alias, groups = m._account_links()
    assert alias == {} and groups == []


def test_flat_string_list_is_rejected_not_iterated_as_characters():
    """`["a", "b"]` instead of `[["a", "b"]]` -- the shape a comma-separated env
    overlay would produce. Iterating a string yields CHARACTERS, which would build
    aliases out of single letters and link accounts nobody named.

    The warning fires ONCE PER RUN (from `_link_history`), not once per parse: the
    parser is called three times a run (scoping, fan-out, the gradings table), and
    the previous inline version warned from all three.
    """
    m = _mgr(cfg={"account_links": ["mirandan75", "Mom"]},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    alias, groups = m._account_links()
    assert alias == {} and groups == []
    assert m.logger.warns == []                      # parsing alone must stay silent
    res = m.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    bad = [w for w in m.logger.warns if "expected a LIST" in w]
    assert len(bad) == 2                             # one per malformed entry
    m._account_links()                               # re-parsing adds no more noise
    assert len([w for w in m.logger.warns if "expected a LIST" in w]) == 2
    assert res["Mom"]["genres"] != res["mirandan75"]["genres"]   # nothing linked


def test_garbage_config_shapes_never_raise():
    for bad in ({"account_links": "mirandan75"}, {"account_links": 7},
                {"account_links": [None]}, {"account_links": [[]]},
                {"account_links": [["a", None, ""]]}):
        m = _mgr(cfg=bad, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
        alias, groups = m._account_links()
        assert isinstance(alias, dict) and isinstance(groups, list)


def test_link_column_labels_primary_and_member():
    m = _mgr(cfg=_LINK_CFG, gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    m.compute_per_user_genre_affinity(list(_LINK_HIST), _META, _LINK_USERS)
    tbl = _table(m)
    assert _cell(tbl, "mirandan75", "Link") == "primary"
    assert _cell(tbl, "Mom", "Link") == "-> mirandan75"
    assert _cell(tbl, "Trizzd", "Link") == "-"


def test_linking_never_widens_the_family_aggregate():
    """A link is a per-viewer grading tool. If it could drag a non-Home account
    into the household maps it would reopen the exact leak family_only closes."""
    m = _mgr(cfg={"household_affinity": {"family_only": True},
                  "account_links": [["Trizzd", "stacee"]]},
             gc=_GC({_FAMILY_IDENTITY_KEY: _IDMAP}))
    scoped, note = m._family_scope(list(_HIST))
    assert all(str(e.get("user_id")) != "999" for e in scoped)   # outsider still out
    assert "excluded 5 play(s)" in note
