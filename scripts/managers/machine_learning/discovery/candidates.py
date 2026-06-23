"""'This Week in History' candidate generation — PURE. Turns Radarr movie rows + owned-TV episode rows
into this-week anniversary candidates, deduped on the OWNERSHIP key (never a title-string join) and
gated by a fail-closed min-popularity prior. No I/O — the manager loads + normalizes the rows and builds
the global 'owned anywhere' set across instances.

Net-new is the point of the shelf; owned titles that also aired this week are the FREE fallback (no
grab). Movies are title-level; TV is SERIES-level (the pilot is the entry point, surfaced downstream).
"""
from __future__ import annotations

from scripts.managers.machine_learning.discovery.window import released_this_week, years_ago


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _movie_release(row):
    """The title's REAL historical release date — theatrical first, then a normalized override."""
    return row.get("release_date") or row.get("in_cinemas_date")


def _popularity_ok(row, min_votes) -> bool:
    """Fail-closed min-popularity prior: passes only when the TMDb vote count clears ``min_votes`` —
    so an obscure title nobody anywhere watched never surfaces (no vote data ⇒ excluded). ``min_votes
    <= 0`` disables the floor."""
    if min_votes <= 0:
        return True
    return (_to_int(row.get("vote_count")) or 0) >= min_votes


def movie_candidates(rows, now, *, tz=None, owned_tmdbs=None, min_votes=0) -> list:
    """Anniversary MOVIE candidates from the Radarr catalog: each movie RELEASED this week in history
    (any past year, already-released), deduped by ``tmdb_id``, classified ``owned`` (a file exists —
    ``tmdb in owned_tmdbs``) vs net-new, passing the popularity floor. PURE."""
    owned = {t for t in (_to_int(x) for x in (owned_tmdbs or [])) if t is not None}
    seen: set = set()
    out: list = []
    for r in rows or []:
        tmdb = _to_int(r.get("tmdb_id"))
        if tmdb is None or tmdb in seen:
            continue
        rel = _movie_release(r)
        if not released_this_week(rel, now, tz=tz) or not _popularity_ok(r, min_votes):
            continue
        seen.add(tmdb)
        out.append({"media": "movie", "tmdb_id": tmdb, "title": r.get("title"), "release": rel,
                    "years_ago": years_ago(rel, now, tz=tz), "owned": tmdb in owned})
    return out


def episode_candidates(rows, now, *, tz=None) -> list:
    """Anniversary OWNED-TV candidates: owned + monitored SERIES whose episode AIRED this week in
    history, deduped to SERIES level (keeping the OLDEST matching anniversary for the strongest "N years
    ago" hook). Season-0 specials and null/sentinel air dates are excluded. All ``owned`` (net-new TV is
    the deferred Phase 7). PURE."""
    by_series: dict = {}
    for r in rows or []:
        tvdb = _to_int(r.get("tvdb_id") if r.get("tvdb_id") is not None else r.get("series_tvdb_id"))
        season = _to_int(r.get("season") if r.get("season") is not None else r.get("season_number"))
        if tvdb is None or season is None or season == 0:        # exclude season-0 specials
            continue
        air = r.get("air_date_utc")
        if not released_this_week(air, now, tz=tz):              # null/sentinel air → released_this_week False
            continue
        ya = years_ago(air, now, tz=tz)
        cur = by_series.get(tvdb)
        if cur is None or (ya is not None and ya > (cur["years_ago"] if cur["years_ago"] is not None else -1)):
            by_series[tvdb] = {
                "media": "show", "tvdb_id": tvdb,
                "series_title": r.get("series_title") or r.get("title"), "season": season,
                "episode": _to_int(r.get("episode") if r.get("episode") is not None else r.get("episode_number")),
                "air": air, "years_ago": ya, "owned": True,
            }
    return list(by_series.values())


def partition_net_new(candidates):
    """``(net_new, owned_fallback)`` — net-new FIRST (the discovery point), owned the free fallback."""
    net_new = [c for c in candidates if not c.get("owned")]
    owned = [c for c in candidates if c.get("owned")]
    return net_new, owned
