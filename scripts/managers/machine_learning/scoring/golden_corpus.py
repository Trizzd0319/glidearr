"""scoring/golden_corpus.py — the seeded byte-identity corpus for score_movie.
================================================================================
The SAFETY NET for the score_movie vectorisation (the deferred crown-jewel of the ML
migration). ``golden_corpus(n, seed)`` deterministically builds ``n`` diverse
``score_movie`` keyword-argument dicts exercising every signal group (A1–G4) and its
edge cases. ``test_score_golden`` freezes the CURRENT ``score_movie(**case)`` output
(final score + full breakdown) for each case into ``golden_scores.json`` and asserts any
future implementation reproduces it byte-for-byte — so a vectorised batch scorer can be
built slice-by-slice behind this gate without drifting the watchability_score oracle.

CLOCK STABILITY: ``score_movie`` calls ``datetime.now()`` internally for F3 (recency) and
G4 (availability). The corpus therefore uses ONLY clock-stable date inputs — release dates
are either absent or fixed far in the PAST (``_FAR_PAST`` → F3 = 0; a far-past date with
``is_available=False`` → has-past-release → G4 = 0; no date + unavailable → G4 = −5). So the
frozen scores never depend on the day the test runs. Recent-date F3/G4 behaviour is covered
separately (test_breakdown + a dedicated relative-date test).

PURE — stdlib + the scorer only; importable as a fixture, no I/O here.
"""
from __future__ import annotations

import random

# Far-past release date: always > 2 years old → F3 recency contributes 0 on any run day,
# and (with is_available=False) it counts as a past release → G4 = 0. Clock-stable.
_FAR_PAST = "2000-01-01"

_KEEP_POLICIES = [None, None, "keep_forever", "keep_movie", "keep_universe", "universe"]
_GENRES = ["Action", "Drama", "Horror", "Comedy", "Sci-Fi", "Animation", "Family",
           "Documentary", "Thriller", "Romance"]
_LANGS = ["en", "fr", "ja", "ko", "es", "de"]
_CERTS = [None, "G", "PG", "PG-13", "R", "NC-17", "TV-MA", "TV-Y", "TV-G", "NR"]
_PLATFORMS = ["Apple TV", "Chromecast", "Roku", "webOS", "Android", "Xbox One",
              "Chrome", "iPhone", "Samsung"]
_CODECS = ["", "h264", "hevc", "h265", "vp9", "av1", "mpeg2"]
_NAMES = [f"Person {i}" for i in range(40)]
_STUDIOS = [f"Studio {i}" for i in range(12)]
_USERS = ["alice", "bob", "cara", "dan", "kid1", "kid2"]


def _maybe(rng, value, p=0.5, default=None):
    return value if rng.random() < p else default


def _rating(rng, scale_100=False):
    if rng.random() < 0.25:
        return None
    return round(rng.uniform(0.1, 100 if scale_100 else 10), 1)


def _affinity_map(rng, keys, hi=30):
    """A {name: count} affinity sub-map over a random subset of keys."""
    out = {}
    for k in keys:
        if rng.random() < 0.5:
            out[k] = rng.randint(1, hi)
    return out


def _one_case(rng, idx):
    n_cast = rng.randint(0, 12)
    cast = [{"name": rng.choice(_NAMES), "order": j} for j in range(n_cast)]
    crew = []
    for _ in range(rng.randint(0, 6)):
        job = rng.choice(["Director", "Screenplay", "Story", "Writer", "Producer", "Editor"])
        crew.append({"name": rng.choice(_NAMES), "job": job})
    credits = {"cast": cast, "crew": crew}

    genres = rng.sample(_GENRES, rng.randint(0, 4))
    prod = [{"name": rng.choice(_STUDIOS)} for _ in range(rng.randint(0, 3))]
    movie_tmdb = 1000 + idx
    coll_id = rng.choice([None, None, 5000, 5001, 5002])
    movie = {
        "tmdbId": movie_tmdb,
        "genres": genres,
        "videoCodec": rng.choice(_CODECS),
    }
    if prod:
        movie["productionCompanies"] = prod
    elif rng.random() < 0.5:
        movie["studio"] = rng.choice(_STUDIOS)
    if coll_id is not None:
        movie["collection"] = {"tmdbId": coll_id}

    # Collection / universe membership: a shared collection with some watched siblings.
    collection_members = {}
    watched = set()
    if coll_id is not None:
        sibs = set(rng.sample(range(6000, 6040), rng.randint(2, 10))) | {movie_tmdb}
        collection_members[coll_id] = sibs
        for s in sibs:
            if s != movie_tmdb and rng.random() < 0.5:
                watched.add(s)
    # extra universe-style membership maps (C2 sums across maps that contain movie_tmdb)
    for _ in range(rng.randint(0, 2)):
        cid = rng.randint(7000, 7100)
        members = set(rng.sample(range(8000, 8060), rng.randint(1, 8))) | {movie_tmdb}
        collection_members[cid] = members
        for s in members:
            if s != movie_tmdb and rng.random() < 0.4:
                watched.add(s)

    genre_affinity = {
        "actors": _affinity_map(rng, _NAMES),
        "directors": _affinity_map(rng, _NAMES),
        "writers": _affinity_map(rng, _NAMES),
        "genres": _affinity_map(rng, _GENRES),
        "studios": _affinity_map(rng, _STUDIOS),
        "format_metrics": {
            "audio_language": {lng: rng.randint(0, 8) for lng in rng.sample(_LANGS, rng.randint(0, 3))}
        },
    }

    platform_usage = None
    if rng.random() < 0.7:
        platform_usage = {p: rng.randint(1, 50) for p in rng.sample(_PLATFORMS, rng.randint(1, 4))}
    transcode_stats = None
    if rng.random() < 0.6:
        transcode_stats = {f"{rng.choice(_CODECS) or 'x'}/{rng.choice(_CODECS) or 'y'}": rng.randint(1, 5)
                           for _ in range(rng.randint(1, 4))}

    per_user_affinity = None
    if rng.random() < 0.6:
        per_user_affinity = {
            u: {"genres": {g: rng.randint(1, 20) for g in rng.sample(_GENRES, rng.randint(0, 3))}}
            for u in rng.sample(_USERS, rng.randint(1, 4))
        }
    kids_users = rng.sample(["kid1", "kid2"], rng.randint(0, 2))
    adult_users = rng.sample(["alice", "bob", "cara", "dan"], rng.randint(0, 3))

    related = None
    if rng.random() < 0.5:
        related = set(rng.sample(range(6000, 6040), rng.randint(0, 8)))

    # Clock-stable dates only (see module docstring).
    is_available = rng.random() < 0.8
    date_val = rng.choice([None, None, _FAR_PAST])

    return {
        "movie": movie,
        "completion_pct": round(rng.choice([0.0, 0.0, 0.1, 0.3, 0.6, 0.8, 0.95, 1.0]), 2),
        "completion_threshold": 0.9,
        "collection_members": collection_members,
        "watched_tmdb_ids": watched,
        "genre_affinity": genre_affinity,
        "credits": credits,
        "watch_count": rng.choice([0, 0, 1, 2, 3, 5]),
        "user_rating": rng.choice([None, None, 0.0, 3.0, 5.0, 7.0, 9.0, 10.0]),
        "platform_usage": platform_usage,
        "transcode_stats": transcode_stats,
        "target_resolution": rng.choice([None, 720, 1080, 2160]),
        "per_user_affinity": per_user_affinity,
        "kids_users": kids_users,
        "adult_users": adult_users,
        "imdb_rating": _rating(rng),
        "tmdb_rating": _rating(rng),
        "trakt_rating": _rating(rng, scale_100=True),
        "metacritic_score": _rating(rng, scale_100=True),
        "rotten_tomatoes_score": _rating(rng, scale_100=True),
        "popularity": rng.choice([None, 5.0, 25.0, 60.0, 150.0]),
        "in_cinemas_date": date_val,
        "physical_release_date": rng.choice([None, _FAR_PAST]),
        "digital_release_date": rng.choice([None, _FAR_PAST]),
        "certification": rng.choice(_CERTS),
        "original_language": rng.choice(_LANGS),
        "is_franchise_entry": rng.random() < 0.3,
        "universe_name": rng.choice([None, None, "MCU", "DCEU"]),
        "keep_policy": rng.choice(_KEEP_POLICIES),
        "preferred_languages": rng.choice([["en"], ["en", "ja"], ["fr"]]),
        "is_available": is_available,
        "affinity_boost": rng.choice([1.0, 1.0, 1.5, 2.0]),
        "related_tmdb_ids": related,
        "related_graph_cap": rng.choice([4.0, 6.0]),
    }


def golden_corpus(n: int = 300, seed: int = 1_234_567) -> "list[dict]":
    """Deterministically build ``n`` diverse score_movie kwarg dicts (same seed → same
    corpus on every run / platform). Sets are built from the seeded RNG; ``watched_tmdb_ids``
    / ``related_tmdb_ids`` are real sets (score_movie intersects them, order-independent)."""
    rng = random.Random(seed)
    return [_one_case(rng, i) for i in range(n)]
