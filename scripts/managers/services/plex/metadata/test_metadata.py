"""Tests for the Plex GUID resolver — the join key every other signal needs.

Covers the two-tier resolution (free Guid[]/raw parse → bridge → paid Discover hop),
the persistent memo, the empty-not-cached rule, and legacy ``com.plexapp.agents.*``
GUID parsing. Exercises real methods via a stub manager (object.__new__)."""
from __future__ import annotations

from scripts.managers.services.plex.metadata import PlexMetadataManager, _any_id


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class _API:
    """Fake PlexAPI: records Discover hops, returns a canned resolution."""
    def __init__(self, discover=None):
        self.discover = discover or {}
        self.hops = 0
    def resolve_discover_metadata(self, rating_key, token=None, fallback=None):
        self.hops += 1
        return self.discover.get(str(rating_key))


def _mgr(cache=None, api=None):
    m = object.__new__(PlexMetadataManager)
    m.logger = _Logger()
    m.global_cache = cache or _Cache()
    m.plex_api = api
    m._guid_map = {}
    m._dirty = False
    m._network_hops = 0
    m._unresolved = {}
    m._imdb_to_tmdb = {}
    m._tvdb_to_tmdb = {}
    m._primed = True
    return m


# ── _parse_guid ──────────────────────────────────────────────────────────────
def test_parse_modern_guids():
    p = PlexMetadataManager._parse_guid
    assert p("tmdb://157336") == ("tmdb", "157336")
    assert p("imdb://tt0816692") == ("imdb", "tt0816692")
    assert p("tvdb://121361/1/2") == ("tvdb", "121361")
    assert p("plex://movie/5d77") == ("", "")
    assert p(None) == ("", "")


def test_parse_legacy_agent_guids():
    p = PlexMetadataManager._parse_guid
    assert p("com.plexapp.agents.themoviedb://157336?lang=en") == ("tmdb", "157336")
    assert p("com.plexapp.agents.thetvdb://121361/1/2?lang=en") == ("tvdb", "121361")
    assert p("com.plexapp.agents.imdb://tt0816692?lang=en") == ("imdb", "tt0816692")


# ── resolve: free tier (Guid[] array) ────────────────────────────────────────
def test_resolve_free_from_guid_array():
    m = _mgr()
    ids = m.resolve("plex://movie/abc", guids_list=[
        {"id": "tmdb://157336"}, {"id": "imdb://tt0816692"}, {"id": "tvdb://99"}])
    assert ids["tmdb"] == 157336 and ids["imdb"] == "tt0816692" and ids["tvdb"] == 99
    assert ids["resolved_via"] == "guid_array"
    # cached → second call hits the persistent memo (no re-parse)
    assert "plex://movie/abc" in m._guid_map
    assert m.resolve("plex://movie/abc") == m._guid_map["plex://movie/abc"]


def test_resolve_free_from_raw_guid_when_no_array():
    m = _mgr()
    ids = m.resolve("tmdb://603", guids_list=[])
    assert ids["tmdb"] == 603 and ids["resolved_via"] == "raw_guid"


# ── resolve: bridge tier ──────────────────────────────────────────────────────
def test_resolve_bridge_imdb_to_tmdb():
    m = _mgr()
    m._imdb_to_tmdb = {"tt0111161": 278}
    ids = m.resolve("plex://movie/x", guids_list=[{"id": "imdb://tt0111161"}])
    assert ids["tmdb"] == 278 and ids["imdb"] == "tt0111161"
    assert ids["resolved_via"] == "bridge_imdb"


# ── resolve: paid tier (Discover hop) ─────────────────────────────────────────
def test_resolve_network_hop_for_bare_plex_guid():
    api = _API(discover={"rk1": {"MediaContainer": {"Metadata": [
        {"Guid": [{"id": "tmdb://12345"}]}]}}})
    m = _mgr(api=api)
    ids = m.resolve("plex://movie/bare", guids_list=[], rating_key="rk1")
    assert ids["tmdb"] == 12345 and ids["resolved_via"] == "discover_hop"
    assert api.hops == 1 and m._network_hops == 1
    assert m.flush() == 1   # network hops reported


def test_confirmed_discover_miss_is_memoized_not_rehopped():
    # Discover RESPONDS but the item has no external ids → confirmed miss → memoized so
    # a future resolve of the same guid never re-hops (honours "resolve at most once ever").
    api = _API(discover={"rk1": {"MediaContainer": {"Metadata": [{"Guid": []}]}}})
    m = _mgr(api=api)
    ids1 = m.resolve("plex://miss", guids_list=[], rating_key="rk1")
    assert not _any_id(ids1) and api.hops == 1
    assert m._guid_map["plex://miss"]["resolved_via"] == "discover_miss"   # negative cached
    ids2 = m.resolve("plex://miss", guids_list=[], rating_key="rk1")
    assert not _any_id(ids2) and api.hops == 1            # memo hit — no second hop


def test_transient_hop_failure_not_memoized_but_deduped_within_run():
    # Discover returns None (transient) → NOT memoized (stays retryable next run), but the
    # same rating_key shared across household users is hopped at most ONCE per run.
    api = _API(discover={})   # any rating_key → None (transient)
    m = _mgr(api=api)
    m.resolve("plex://shared", guids_list=[], rating_key="rkS")      # user 1
    assert api.hops == 1 and "plex://shared" not in m._guid_map       # not memoized
    m.resolve("plex://shared", guids_list=[], rating_key="rkS")      # user 2, same item
    assert api.hops == 1                                              # in-run dedup by rating_key
    m._attempted_hops = set()                                        # simulate a fresh run (prime resets)
    m.resolve("plex://shared", guids_list=[], rating_key="rkS")
    assert api.hops == 2                                             # retried on the new run


def test_network_hop_suppressed_when_allow_network_false():
    api = _API(discover={"rk1": {"MediaContainer": {"Metadata": [{"Guid": [{"id": "tmdb://1"}]}]}}})
    m = _mgr(api=api)
    ids = m.resolve("plex://movie/bare", guids_list=[], rating_key="rk1", allow_network=False)
    assert not _any_id(ids) and api.hops == 0


# ── empty resolutions are NOT cached (don't poison a later richer attempt) ─────
def test_empty_resolution_not_cached_but_bucketed():
    m = _mgr()
    ids = m.resolve("plex://movie/unknown", guids_list=[])
    assert not _any_id(ids)
    assert "plex://movie/unknown" not in m._guid_map
    assert "plex://movie/unknown" in m._unresolved
    # a later attempt WITH ids resolves and caches
    ids2 = m.resolve("plex://movie/unknown", guids_list=[{"id": "tmdb://7"}])
    assert ids2["tmdb"] == 7 and "plex://movie/unknown" in m._guid_map


def test_prime_loads_existing_guid_map_and_bridges(monkeypatch):
    cache = _Cache({"plex/guid_map": {"tmdb://1": {"tmdb": 1, "tvdb": None, "imdb": None}},
                    "radarr.movies.standard.full": [{"imdbId": "tt1", "tmdbId": 50}]})
    m = object.__new__(PlexMetadataManager)
    m.logger = _Logger(); m.global_cache = cache; m.plex_api = None
    m._guid_map = {}; m._dirty = False; m._network_hops = 0; m._unresolved = {}
    m._imdb_to_tmdb = {}; m._tvdb_to_tmdb = {}; m._primed = False
    m.config = {"radarr_instances": {}, "sonarr_instances": {}}
    m.prime()
    assert m._guid_map["tmdb://1"]["tmdb"] == 1
    assert m._imdb_to_tmdb["tt1"] == 50
