"""run_pilot_720_upgrade -- the shared core that reprofiles genuine sub-720 pilot stubs to their family
720 cap and EpisodeSearches ONLY the pilot (Sonarr upgrades in place; nothing deleted). Proves the
reprofile targeting, batched search, dry-run silence, cooldown/resume skip, and cooperative yield."""
from __future__ import annotations

from scripts.managers.services.sonarr.cache.pilot_720_upgrade import run_pilot_720_upgrade, ledger_key


class _Cache:
    def __init__(self, seed=None): self.store = dict(seed or {})
    def get(self, k): return self.store.get(k)
    def set(self, k, v, *a, **k2): self.store[k] = v; return True


class _Log:
    def __init__(self): self.warnings = []
    def log_info(self, *a, **k): pass
    def log_warning(self, m="", *a, **k): self.warnings.append(str(m))
    def log_debug(self, *a, **k): pass


def _make_request(record, episodes):
    """A fake Sonarr: records every write; answers GET episode?seriesId=<sid> from ``episodes``
    ({sid: [ {id,seasonNumber,episodeNumber}, ... ]})."""
    def mk(instance, endpoint, method="GET", payload=None, fallback=None):
        record.append((method, endpoint, payload))
        if method == "GET" and endpoint.startswith("episode?seriesId="):
            sid = int(endpoint.split("=")[1])
            return episodes.get(sid, [])
        return {"ok": True}
    return mk


def _item(sid, cur, tgt, season=1, episode=1):
    return {"series_id": sid, "season": season, "episode": episode, "series_title": f"S{sid}",
            "target_profile_id": tgt, "current_profile_id": cur}


_EPS = {1: [{"id": 11, "seasonNumber": 1, "episodeNumber": 1}],
        2: [{"id": 21, "seasonNumber": 1, "episodeNumber": 1}],
        3: [{"id": 31, "seasonNumber": 1, "episodeNumber": 1}]}


def test_reprofiles_only_mismatched_then_searches_pilots():
    rec = []
    gc = _Cache()
    # sid 1 on a >720 profile (6) -> reprofile to 3; sid 2 already on 3 -> search only.
    items = [_item(1, cur=6, tgt=3), _item(2, cur=3, tgt=3)]
    out = run_pilot_720_upgrade(make_request=_make_request(rec, _EPS), logger=_Log(),
                                global_cache=gc, instance="standard", items=items)
    puts = [r for r in rec if r[0] == "PUT" and r[1] == "series/editor"]
    assert puts == [("PUT", "series/editor", {"seriesIds": [1], "qualityProfileId": 3})]  # only sid 1
    searches = [r for r in rec if r[0] == "POST" and r[1] == "command"]
    assert len(searches) == 1 and set(searches[0][2]["episodeIds"]) == {11, 21}          # both pilots
    assert out["reprofiled"] == 1 and out["searched"] == 2
    # ledger recorded both series (cooldown/resume)
    assert set(gc.get(ledger_key("standard")).keys()) == {"1", "2"}


def test_dry_run_writes_nothing():
    rec = []
    gc = _Cache()
    out = run_pilot_720_upgrade(make_request=_make_request(rec, _EPS), logger=_Log(),
                                global_cache=gc, instance="standard",
                                items=[_item(1, cur=6, tgt=3)], dry_run=True)
    assert not [r for r in rec if r[0] in ("PUT", "POST")]     # no writes
    assert out["searched"] == 1 and out["reprofiled"] == 0
    assert gc.get(ledger_key("standard")) is None             # no cooldown burned


def test_cooldown_skips_recently_searched():
    from datetime import datetime, timezone
    rec = []
    gc = _Cache({ledger_key("standard"): {"1": {"at": datetime.now(tz=timezone.utc).isoformat(),
                                                 "result": "searched"}}})
    out = run_pilot_720_upgrade(make_request=_make_request(rec, _EPS), logger=_Log(),
                                global_cache=gc, instance="standard",
                                items=[_item(1, cur=3, tgt=3), _item(2, cur=3, tgt=3)])
    # sid 1 is within cooldown -> skipped; only sid 2 searched
    searches = [r for r in rec if r[0] == "POST" and r[1] == "command"]
    assert len(searches) == 1 and searches[0][2]["episodeIds"] == [21]
    assert out["skipped_cooldown"] == 1 and out["searched"] == 1


def test_unresolved_pilot_is_skipped_not_seriessearched():
    rec = []
    gc = _Cache()
    # sid 3's episode list has no matching pilot row -> unresolved, never SeriesSearched
    out = run_pilot_720_upgrade(make_request=_make_request(rec, {3: [{"id": 99, "seasonNumber": 2, "episodeNumber": 5}]}),
                                logger=_Log(), global_cache=gc, instance="standard",
                                items=[_item(3, cur=3, tgt=3)])
    assert out["unresolved"] == 1 and out["searched"] == 0
    assert not [r for r in rec if r[0] == "POST"]             # no EpisodeSearch fired


def test_yields_to_higher_priority_before_searching():
    rec = []
    gc = _Cache()
    out = run_pilot_720_upgrade(make_request=_make_request(rec, _EPS), logger=_Log(),
                                global_cache=gc, instance="standard",
                                items=[_item(1, cur=3, tgt=3)], should_yield=lambda: True)
    assert out["yielded"] is True and out["searched"] == 0
    assert not [r for r in rec if r[0] == "POST"]             # yielded before any search
