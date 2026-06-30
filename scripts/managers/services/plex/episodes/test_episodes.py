"""Tests for plex/episodes — owned-episode tvdb→ratingKey map + coverage probe."""
from __future__ import annotations

from scripts.managers.services.plex.episodes import (
    _INVENTORY_KEY,
    _STATS_KEY,
    PlexEpisodesManager,
)


# ── fakes ─────────────────────────────────────────────────────────────────────
class _Api:
    def __init__(self, *, sections=None, pages=None):
        self._sections = sections or []
        self._pages = pages or {}            # {(key, plex_type): [page0_items, page1_items, ...]}
        self.calls: list = []
        self.sections_calls = 0

    def get_sections(self):
        self.sections_calls += 1
        return {"MediaContainer": {"Directory": self._sections}}

    def get_section_all(self, key, plex_type, start, size):
        self.calls.append((key, plex_type, start))
        pages = self._pages.get((key, plex_type), [])
        idx = start // size
        page = pages[idx] if idx < len(pages) else []     # past end → empty → terminator
        return {"MediaContainer": {"Metadata": page}}


class _Meta:
    def __init__(self, rk_to_tvdb):
        self.m = rk_to_tvdb

    def resolve(self, guid, guids, rating_key=None, allow_network=True):
        return {"tvdb": self.m.get(str(rating_key))}


class _Cache:
    def __init__(self, initial=None):
        self.d = dict(initial or {})

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v


class _Reg:
    def __init__(self, meta):
        self.meta = meta

    def get(self, kind, name):
        return self.meta if name == "PlexMetadataManager" else None


def show(rk, title="Show"):
    return {"ratingKey": rk, "type": "show", "guid": f"plex://show/{rk}", "Guid": [], "title": title}


def ep(rk, s, e, gp, title="Ep"):
    return {"ratingKey": rk, "type": "episode", "parentIndex": s, "index": e,
            "title": title, "grandparentTitle": "Show", "grandparentRatingKey": gp}


class _Log:
    def __init__(self):
        self.infos: list = []
        self.warns: list = []

    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): self.warns.append(m)
    def log_error(self, m): pass


def _mgr(api, meta, cache):
    m = PlexEpisodesManager.__new__(PlexEpisodesManager)
    m.plex_api = api
    m.global_cache = cache
    m.registry = _Reg(meta)
    m.dry_run = False
    m.logger = _Log()
    m.config = {}
    return m


# ── core: episode resolved via its SHOW's tvdb + (season, episode) ─────────────
def test_resolves_episode_via_show_tvdb():
    api = _Api(pages={("5", 2): [[show("100")], []],
                      ("5", 4): [[ep("1001", 1, 1, "100")], []]})
    cache = _Cache({"plex/sections": {"5": {"type": "show", "title": "TV"}}})
    m = _mgr(api, _Meta({"100": 555}), cache)
    stats = m.run()
    inv = cache.get(_INVENTORY_KEY)
    assert inv["555:1:1"]["rating_key"] == "1001"      # the EPISODE key, not the show
    assert inv["555:1:1"]["section"] == "5"            # source section recorded for per-user scoping
    assert stats["episodes_resolved"] == 1 and stats["resolution_pct"] == 100.0
    assert cache.get(_STATS_KEY)["shows_resolved"] == 1


def test_episode_dropped_when_series_tvdb_unresolved():
    api = _Api(pages={("5", 2): [[show("100")], []],
                      ("5", 4): [[ep("1001", 1, 1, "100")], []]})
    cache = _Cache({"plex/sections": {"5": {"type": "show"}}})
    m = _mgr(api, _Meta({}), cache)                      # show 100 → no tvdb
    stats = m.run()
    assert cache.get(_INVENTORY_KEY) == {}
    assert stats["unresolved_no_series_tvdb"] == 1 and stats["resolution_pct"] == 0.0


def test_episode_missing_index_counted():
    api = _Api(pages={("5", 2): [[show("100")], []],
                      ("5", 4): [[ep("1001", None, 1, "100")], []]})
    cache = _Cache({"plex/sections": {"5": {"type": "show"}}})
    stats = _mgr(api, _Meta({"100": 9}), cache).run()
    assert stats["unresolved_missing_index"] == 1 and stats["episodes_resolved"] == 0


def test_pagination_across_pages_then_empty():
    api = _Api(pages={("5", 2): [[show("100")], []],
                      ("5", 4): [[ep("a", 1, 1, "100")], [ep("b", 1, 2, "100")], []]})
    cache = _Cache({"plex/sections": {"5": {"type": "show"}}})
    stats = _mgr(api, _Meta({"100": 7}), cache).run()
    assert stats["episodes_resolved"] == 2 and stats["max_pages_hit"] is False
    assert sorted(cache.get(_INVENTORY_KEY)) == ["7:1:1", "7:1:2"]


def test_no_show_sections_is_a_noop():
    api = _Api(pages={})
    cache = _Cache({"plex/sections": {"3": {"type": "movie"}}})   # only a movie section
    stats = _mgr(api, _Meta({}), cache).run()
    assert api.calls == [] and stats["episodes_seen"] == 0 and cache.get(_INVENTORY_KEY) == {}


def test_cached_sections_preferred_over_fetch():
    api = _Api(pages={("5", 2): [[]], ("5", 4): [[]]})
    cache = _Cache({"plex/sections": {"5": {"type": "show"}}})
    _mgr(api, _Meta({}), cache).run()
    assert api.sections_calls == 0                       # used the cached index


def test_sections_fetched_when_not_cached():
    api = _Api(sections=[{"key": "5", "type": "show", "title": "TV"}],
               pages={("5", 2): [[show("100")], []],
                      ("5", 4): [[ep("1001", 1, 1, "100")], []]})
    cache = _Cache()                                     # no plex/sections cached
    stats = _mgr(api, _Meta({"100": 1}), cache).run()
    assert api.sections_calls == 1 and stats["episodes_resolved"] == 1


def test_low_coverage_logs_actionable_warning():
    # 1 of 2 episodes resolves (the other's show has no tvdb) → 50% → low-coverage warn
    api = _Api(pages={("5", 2): [[show("100")], []],
                      ("5", 4): [[ep("1001", 1, 1, "100"), ep("1002", 1, 2, "999")], []]})
    cache = _Cache({"plex/sections": {"5": {"type": "show"}}})
    m = _mgr(api, _Meta({"100": 555}), cache)
    stats = m.run()
    assert stats["resolution_pct"] == 50.0
    assert any("match a Plex item" in w for w in m.logger.warns)   # guidance surfaced


def test_full_coverage_logs_no_warning():
    api = _Api(pages={("5", 2): [[show("100")], []],
                      ("5", 4): [[ep("1001", 1, 1, "100")], []]})
    cache = _Cache({"plex/sections": {"5": {"type": "show"}}})
    m = _mgr(api, _Meta({"100": 555}), cache)
    m.run()
    assert m.logger.warns == []


# ── plex.exclude_sections: a "Coming Soon" placeholder library is never scanned ──
def test_excluded_show_section_is_skipped():
    # two show sections; the excluded one's placeholder tvdb must NOT enter owned_inventory
    # (else an unreleased show resolves to "owned" and its real grab is suppressed).
    api = _Api(pages={
        ("5", 2): [[show("100")], []], ("5", 4): [[ep("1001", 1, 1, "100")], []],
        ("9", 2): [[show("900")], []], ("9", 4): [[ep("9001", 1, 1, "900")], []],
    })
    cache = _Cache({"plex/sections": {
        "5": {"type": "show", "title": "TV Shows-Series"},
        "9": {"type": "show", "title": "Coming Soon TV"},
    }})
    m = _mgr(api, _Meta({"100": 555, "900": 999}), cache)
    m.config = {"plex": {"exclude_sections": ["coming soon tv"]}}   # case-insensitive
    stats = m.run()
    inv = cache.get(_INVENTORY_KEY)
    assert "555:1:1" in inv                                  # real library kept
    assert "999:1:1" not in inv                              # placeholder section skipped
    assert stats["show_sections"] == 1
    assert all(c[0] != "9" for c in api.calls)              # excluded section never queried
