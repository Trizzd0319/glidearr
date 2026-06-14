"""trakt.shows — TV-show watchability scoring + per-tvdbId Trakt data cache.

Mirrors trakt.movies (score_movie / TraktMovieCacheManager) for the Sonarr side:
  * scorer.py  — score_show(): 0-100 watchability engine, same group structure
                 (A-G) and the same critic boost as the movie scorer.
  * cache.py   — TraktShowCacheManager: gz reader keyed by tvdbId over the
                 enrich-daemon's show buckets (people + ratings).
"""
