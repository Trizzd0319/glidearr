"""
TraktShowScorer — 100-point TV-show watchability engine
=======================================================
The series-level twin of ``trakt.movies.scorer.score_movie``.  Same group
structure A-G, the same independently-capped accumulation, the same critic
boost (>=8.5 -> +20, deliberately above director affinity), and the same final
clamp to [0, 100].  Two things change for television:

  * GROUP A — Household Intent is measured by RECENCY + BREADTH, not lifetime
    completion.  Shows are watched episodically, so a long-running series
    (Simpsons, long anime) never reaches a high lifetime-completion fraction the
    way a movie does — yet a show you actively follow should score high.  A2
    therefore blends "how recently" with "how broadly" the household watches.

  * GROUP F — Content Quality blends the two rating signals television actually
    exposes: Sonarr's aggregate series rating and the Trakt show audience rating
    (both 0-10).  Movies get a 4-source critic blend (IMDb/RT/MC/Trakt); shows
    do not, so F1 averages whatever 0-10 ratings are available and applies the
    identical tier table.

  * GROUP C — Collection / Universe is 0: shows have no native collection
    concept (franchise value already flows through keep-tags in Group A).

Shared device/transcode/cert constants, the affinity helper and the
score->profile mapping live in ``scoring/_shared.py``; this scorer and the movie
scorer both import them from there, so the two engines can never drift and
neither reads from the other.

No external I/O — pure function, safe to call from any context.  All series-level
aggregation (watched-episode counts, recency, modal codec, latest air date,
credits, ratings) is done by the caller (``_build_show_score_map``) and passed in.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Shared scoring commons (scoring/_shared.py) — the device/cert tables, the
# score->profile mapping, the affinity helper (imported as _affinity) and the
# profile-id selector. The MOVIE scorer imports the SAME module, so the two
# engines share one definition of every symbol and neither reads from the other.
from scripts.managers.machine_learning.scoring._shared import (
    QUALITY_PROFILE_THRESHOLDS,           # noqa: F401  (re-exported for callers/shim)
    _DEVICE_RESOLUTION_CEILING,
    _KIDS_CERTS,
    _TRANSCODE_FRIENDLY_CODECS,
    affinity_topk as _affinity,
    normalize_lang,
    person_affinity_score,
    related_graph_affinity,
    score_to_profile,                     # noqa: F401  (re-exported for callers/shim)
    select_profile_id,
    user_rating_score,
)
# Group-C4 reads the series' people-by-role from the SAME credits dict the scorer
# already receives (route_people, keyed on tmdb_person_id). Pure brain→brain import.
from scripts.managers.machine_learning.people_matrix.build import route_people


def score_show(
    show: dict,
    *,
    # ── GROUP A — Household Intent (recency + breadth) ─────────────────────────
    watched_episodes: int = 0,
    total_episodes: int = 0,
    days_since_last_watch: float | None = None,   # None = never watched
    max_episode_watch_count: int = 0,             # most-rewatched episode
    keep_policy: str | None = None,               # keep_series | keep_season | keep_universe | ...
    user_rating: float | None = None,             # household Trakt show rating 0-10
    # ── GROUP B — Household Affinity ──────────────────────────────────────────
    genre_affinity: dict | None = None,
    credits: dict | None = None,                  # {cast:[...], crew:[...]} from TraktShowCacheManager
    # ── GROUP D — Device / Playback Fit ───────────────────────────────────────
    platform_usage: dict | None = None,
    transcode_stats: dict | None = None,
    target_resolution: int | None = None,
    video_codec: str | None = None,               # representative (modal) codec for the series
    # ── GROUP E — Audience Alignment ──────────────────────────────────────────
    per_user_affinity: dict | None = None,
    kids_users: list[str] | None = None,
    adult_users: list[str] | None = None,
    # ── GROUP F — Content Quality ─────────────────────────────────────────────
    sonarr_rating: float | None = None,           # Sonarr series.ratings.value (0-10)
    trakt_rating: float | None = None,            # Trakt show audience rating (0-10)
    trakt_votes: int | None = None,
    latest_air_date: str | None = None,           # ISO date of the most recent episode
    # ── Other ─────────────────────────────────────────────────────────────────
    preferred_languages: list[str] | None = None,
    # Fraction (0..1) of the series' episode files watchable in a preferred language
    # (per-episode dub OR sub; computed in show_features). When provided, G1 becomes
    # FILE-aware: penalty scales with the UNWATCHABLE fraction (all episodes have an
    # English dub/sub → 0; none → full -8) regardless of the show's foreign origin.
    language_consumable_fraction: float | None = None,
    is_available: bool = True,
    # ── GROUP C3 — related-graph collaborative affinity ────────────────────────
    # related_tvdb_ids: this show's Trakt-related neighbour TVDb ids (from the
    # daemon-cached show_related bucket). Intersected with watched_tvdb_ids (the
    # household's watched series). Fills the otherwise-empty Group C for TV.
    related_tvdb_ids: set[int] | None = None,
    watched_tvdb_ids: set[int] | None = None,
    related_graph_cap: float = 4.0,
    # ── GROUP C4 — person-affinity (id-keyed cast/crew taste overlap) ───────────
    # person_weights: {tmdb_person_id: weight} household affinity; the series' people
    # are read from `credits`. cap DEFAULT 0.0 → byte-identical until a caller opts in.
    person_weights: dict | None = None,
    person_affinity_cap: float = 0.0,
    # ── Group-A4 declared-rating shape (config-tunable via scoring.show_user_rating;
    #    the caller passes overrides, these defaults define the gentler TV behavior) ──
    ur_slope: float = 1.5,
    ur_pos_cap: float = 8.0,
    ur_neg_cap: float = -3.0,
    ur_conf_divisor: float = 8.0,
    return_breakdown: bool = False,
) -> int | tuple[int, dict]:
    """Return a 0-100 watchability score for a SERIES.

    Parameters mirror ``score_movie`` but expect series-level aggregates. ``show``
    supplies ``genres`` (list), ``network`` (str), ``certification`` (str) and
    ``original_language`` (str). Use ``score_to_profile()`` to map the result.
    """
    if preferred_languages is None:
        preferred_languages = ["en"]
    if kids_users is None:
        kids_users = []
    if adult_users is None:
        adult_users = []
    genre_affinity = genre_affinity or {}
    credits = credits or {}

    breakdown: dict[str, float] = {}
    score: float = 0.0

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP A — Household Intent  (max 25) — RECENCY + BREADTH
    # ═══════════════════════════════════════════════════════════════════════════

    # A1  keep_policy tag  (+15)
    a1 = 0.0
    if keep_policy in ("keep_forever", "keep_series"):
        a1 = 15.0
    elif keep_policy in ("keep_season", "keep_universe"):
        a1 = 8.0
    elif keep_policy == "universe":
        a1 = 4.0
    score += a1
    breakdown["A1_keep_policy"] = a1

    # A2  Engagement = recency (max 7) + breadth (max 5)  (±12)
    # An actively-watched show scores high even at a low lifetime-completion %;
    # a show sampled long ago and never resumed scores ~0 here (and is penalised
    # in G2). Never-watched -> 0 (neutral, like the movie completion==0 case).
    a2 = 0.0
    if watched_episodes > 0:
        d = days_since_last_watch if days_since_last_watch is not None else 1e9
        if d <= 30:
            rec = 7.0
        elif d <= 90:
            rec = 5.0
        elif d <= 180:
            rec = 3.0
        elif d <= 365:
            rec = 1.0
        else:
            rec = 0.0
        frac = (watched_episodes / total_episodes) if total_episodes > 0 else 0.0
        if frac >= 0.5 or watched_episodes >= 20:
            br = 5.0
        elif frac >= 0.25 or watched_episodes >= 10:
            br = 3.0
        elif frac >= 0.1 or watched_episodes >= 3:
            br = 1.5
        else:
            br = 0.5
        a2 = rec + br
    score += a2
    breakdown["A2_engagement"] = a2

    # A3  Rewatch  (+8) — most-rewatched episode signals binge/rewatch behaviour
    a3 = 0.0
    if max_episode_watch_count >= 3:
        a3 = 8.0
    elif max_episode_watch_count == 2:
        a3 = 5.0
    elif max_episode_watch_count == 1:
        a3 = 2.0
    score += a3
    breakdown["A3_rewatch"] = a3

    # A4  User Trakt rating — WEAKER + CONFIDENCE-GATED for TV. A declared series
    # rating is a stickier, weaker signal than revealed episode engagement (A2), so
    # it gets a lower slope/cap and a softened penalty; and it is trusted only in
    # proportion to how much of the series has actually been watched (a 10/10 after
    # two episodes counts for less than after four seasons). confidence = the larger
    # of "episodes watched / 8" and the watched fraction, capped at 1.0.
    _ur_confidence = min(1.0, max(
        (watched_episodes / ur_conf_divisor) if ur_conf_divisor > 0 else 1.0,
        (watched_episodes / total_episodes) if total_episodes > 0 else 0.0,
    ))
    a4 = user_rating_score(
        user_rating, slope=ur_slope, pos_cap=ur_pos_cap, neg_cap=ur_neg_cap,
        confidence=_ur_confidence,
    )
    score += a4
    breakdown["A4_user_rating"] = a4

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP B — Household Affinity  (max 20)
    # ═══════════════════════════════════════════════════════════════════════════

    actors_aff    = genre_affinity.get("actors",    {})
    directors_aff = genre_affinity.get("directors", {})
    writers_aff   = genre_affinity.get("writers",   {})
    genres_aff    = genre_affinity.get("genres",    {})
    studios_aff   = genre_affinity.get("studios",   {})

    cast_list = credits.get("cast") or []
    crew_list = credits.get("crew") or []

    cast_sorted = sorted(
        [m for m in cast_list if m.get("name")],
        key=lambda m: m.get("order", 999),
    )
    actor_names = [m["name"] for m in cast_sorted[:10]]

    # For TV the authorial signal is the creator as much as a single director;
    # fold "creator" into both the director and writer name sets.
    directors = [
        m["name"] for m in crew_list
        if m.get("job", "").lower() in {"director", "creator"} and m.get("name")
    ]
    writers = [
        m["name"] for m in crew_list
        if m.get("job", "").lower() in {
            "screenplay", "story", "writer",
            "original screenplay", "original story", "creator",
        } and m.get("name")
    ]

    show_genres = show.get("genres") or []
    network = show.get("network")
    networks = [network] if network else []

    b1 = _affinity(actor_names, actors_aff,    8.0)
    b2 = _affinity(directors,   directors_aff, 6.0)
    b3 = _affinity(writers,     writers_aff,   4.0)
    b4 = _affinity(show_genres, genres_aff,    4.0)
    b5 = _affinity(networks,    studios_aff,   3.0)

    score += b1 + b2 + b3 + b4 + b5
    breakdown.update({
        "B1_actor_affinity":    b1,
        "B2_director_affinity": b2,
        "B3_writer_affinity":   b3,
        "B4_genre_affinity":    b4,
        "B5_network_affinity":  b5,
    })

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP C — Collection / Universe (max 16) — shows have no native collection/
    # universe, so C1/C2 stay 0; C3 (related-graph affinity) fills the group via
    # Trakt's similarity graph — how many of this show's related neighbours the
    # household watches (the same collaborative signal the movie scorer gets).
    # ═══════════════════════════════════════════════════════════════════════════
    breakdown["C1_collection"] = 0.0
    breakdown["C2_universe"]   = 0.0
    c3 = related_graph_affinity(related_tvdb_ids, watched_tvdb_ids, cap=related_graph_cap)
    score += c3
    breakdown["C3_related_graph"] = c3

    # C4  Person-affinity (+ person_affinity_cap, DEFAULT 0.0 → byte-identical).
    # Series' cast/crew (by tmdb_person_id, from `credits`) overlap with household taste.
    c4 = 0.0
    if person_affinity_cap > 0 and person_weights:
        c4 = person_affinity_score(route_people(credits or {}), person_weights, person_affinity_cap)
    score += c4
    breakdown["C4_person_affinity"] = c4

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP D — Device / Playback Fit  (max 15)
    # ═══════════════════════════════════════════════════════════════════════════

    # D1  Primary device resolution capability  (+6)
    d1 = 0.0
    if platform_usage and target_resolution:
        primary_platform = max(platform_usage, key=platform_usage.get, default="")
        primary_key = primary_platform.lower().strip()
        device_ceil = None
        for dev_key, ceil in _DEVICE_RESOLUTION_CEILING.items():
            if dev_key in primary_key or primary_key in dev_key:
                device_ceil = ceil
                break
        if device_ceil is not None:
            if target_resolution <= device_ceil:
                d1 = 6.0 if target_resolution == device_ceil else 3.0
            else:
                d1 = -2.0
    score += d1
    breakdown["D1_device_capability"] = d1

    # D2  Transcode avoidance  (+5)
    d2 = 0.0
    vcodec = video_codec or ""
    if vcodec and transcode_stats is not None:
        codec_transcoded = any(
            vcodec.lower() in pair.lower() for pair in (transcode_stats or {})
        )
        if not codec_transcoded:
            d2 = 5.0
        elif vcodec.lower() in _TRANSCODE_FRIENDLY_CODECS:
            d2 = 2.0
    elif vcodec in ("", None):
        d2 = 2.0
    score += d2
    breakdown["D2_transcode_avoidance"] = d2

    # D3  Platform resolution ceiling (all devices)  (+4)
    d3 = 0.0
    if platform_usage and target_resolution:
        total_plays = sum(platform_usage.values()) or 1
        capable_plays = 0
        for platform, plays in platform_usage.items():
            pkey = platform.lower().strip()
            ceil_ = next(
                (c for k, c in _DEVICE_RESOLUTION_CEILING.items()
                 if k in pkey or pkey in k),
                None,
            )
            if ceil_ is not None and target_resolution <= ceil_:
                capable_plays += plays
        capable_pct = capable_plays / total_plays
        if capable_pct >= 0.75:
            d3 = 4.0
        elif capable_pct >= 0.5:
            d3 = 2.0
        elif capable_pct >= 0.25:
            d3 = 1.0
    score += d3
    breakdown["D3_platform_ceiling"] = d3

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP E — Audience Alignment  (max 10)
    # ═══════════════════════════════════════════════════════════════════════════

    cert_lower = (show.get("certification") or "").upper().strip()
    is_kids_cert  = cert_lower in {c.upper() for c in _KIDS_CERTS}
    is_adult_cert = cert_lower in ("R", "NC-17", "TV-MA", "NR", "18", "M")

    show_genre_lower = {g.lower() for g in show_genres}

    # E1  Kids content watched by kids users  (+6)
    e1 = 0.0
    if is_kids_cert and per_user_affinity and kids_users:
        kids_genre_aff: dict = {}
        for user in kids_users:
            ua = per_user_affinity.get(user, {})
            for genre, cnt in (ua.get("genres") or {}).items():
                kids_genre_aff[genre] = kids_genre_aff.get(genre, 0) + cnt
        e1 = _affinity(show_genres, kids_genre_aff, 6.0)
    elif is_kids_cert:
        e1 = 2.0
    score += e1
    breakdown["E1_kids_alignment"] = e1

    # E2  Adult content watched by adult users  (+4)
    e2 = 0.0
    if is_adult_cert and per_user_affinity and adult_users:
        adult_genre_aff: dict = {}
        for user in adult_users:
            ua = per_user_affinity.get(user, {})
            for genre, cnt in (ua.get("genres") or {}).items():
                adult_genre_aff[genre] = adult_genre_aff.get(genre, 0) + cnt
        e2 = _affinity(show_genres, adult_genre_aff, 4.0)
    score += e2
    breakdown["E2_adult_alignment"] = e2

    # E3  Library routing fit  (+4)
    e3 = 0.0
    anime_genres  = {"anime", "animation"}
    family_genres = {"family", "children", "animation", "kids"}
    if is_kids_cert and bool(show_genre_lower & family_genres):
        e3 = 4.0
    elif bool(show_genre_lower & anime_genres) and not is_kids_cert:
        e3 = 2.0
    score += e3
    breakdown["E3_library_fit"] = e3

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP F — Content Quality  (max 24)
    # ═══════════════════════════════════════════════════════════════════════════

    # F1  Critic consensus  (+20) — blend of the two 0-10 ratings TV exposes.
    #     Same tier table as the movie scorer (boosted: >=8.5 -> 20, deliberately
    #     above director affinity). Sonarr weighted a little higher than the Trakt
    #     audience number as it tends to aggregate more sources.
    f1 = 0.0
    c_scores: list[tuple[float, float]] = []
    if sonarr_rating and 0 < sonarr_rating <= 10:
        c_scores.append((sonarr_rating, 0.6))
    if trakt_rating and 0 < trakt_rating <= 10:
        c_scores.append((trakt_rating, 0.4))
    avg = None
    if c_scores:
        tw = sum(w for _, w in c_scores)
        avg = sum(s * w for s, w in c_scores) / tw
        if avg >= 8.5:
            f1 = 20.0   # masterpiece — strongest single positive signal
        elif avg >= 7.5:
            f1 = 14.0   # critically acclaimed
        elif avg >= 6.5:
            f1 = 8.0    # well-reviewed — above director affinity (+6)
        elif avg >= 5.5:
            f1 = 3.0
    score += f1
    breakdown["F1_critic_consensus"] = f1

    # F2  Popularity  (+2) — Trakt vote volume as an interest proxy
    f2 = 0.0
    if trakt_votes and trakt_votes > 0:
        if trakt_votes >= 10000:
            f2 = 2.0
        elif trakt_votes >= 1000:
            f2 = 1.5
        elif trakt_votes >= 100:
            f2 = 0.75
    score += f2
    breakdown["F2_popularity"] = f2

    # F3  Recency  (+2) — most recent episode aired within 1-2 years (still airing)
    f3 = 0.0
    if latest_air_date:
        try:
            dt = datetime.fromisoformat(str(latest_air_date).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_years = (datetime.now(tz=timezone.utc) - dt).days / 365.25
            if age_years <= 1.0:
                f3 = 2.0
            elif age_years <= 2.0:
                f3 = 1.0
        except (ValueError, TypeError):
            pass
    score += f3
    breakdown["F3_recency"] = f3

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP G — Penalties  (up to −18)
    # ═══════════════════════════════════════════════════════════════════════════

    # G1  Language mismatch  (−8)
    # Normalise both sides to ISO 639-1 (Sonarr hands us the display NAME "English",
    # not "en") so a preferred-language series isn't wrongly penalised.
    g1 = 0.0
    original_language = show.get("original_language")
    if original_language:
        lang = normalize_lang(original_language)
        pref = {normalize_lang(p) for p in preferred_languages}
        pref.discard(None)
        if lang and lang not in pref:
            # FILE-AWARE (preferred): the penalty scales with how much of the series you
            # CAN'T watch — the fraction of episodes lacking a preferred-language audio
            # (dub) OR subtitle (sub) track. Every episode dubbed/subbed → 0; none → -8;
            # half → -4 (those episodes are the language re-acquisition candidates). This
            # is PER-EPISODE (a dub/sub on only some episodes does not pass the series).
            if language_consumable_fraction is not None:
                frac = max(0.0, min(1.0, language_consumable_fraction))
                g1 = round(-8.0 * (1.0 - frac), 1)
            else:
                # No per-file track data — preserve the legacy household-history shape
                # (how often the household watches this original language) so callers
                # that don't pass a fraction stay byte-identical.
                fmt = genre_affinity.get("format_metrics", {})
                audio_langs = fmt.get("audio_language", {})
                lang_plays = sum(v for k, v in audio_langs.items() if lang in (normalize_lang(k) or k.lower()))
                if lang_plays == 0:
                    g1 = -8.0
                elif lang_plays <= 2:
                    g1 = -4.0
                else:
                    g1 = -1.0
    score += g1
    breakdown["G1_language"] = g1

    # G2  Sampled & abandoned  (−10) — watched a little, never got into it, and
    #     hasn't returned in over a year. Recency-gated so an actively-watched
    #     long-runner (low lifetime %, recent watch) is NOT penalised.
    g2 = 0.0
    if watched_episodes > 0:
        d = days_since_last_watch if days_since_last_watch is not None else 1e9
        frac = (watched_episodes / total_episodes) if total_episodes > 0 else 1.0
        if d > 365 and frac < 0.2 and watched_episodes < 5:
            g2 = -10.0
    score += g2
    breakdown["G2_abandoned"] = g2

    # G3  Critically panned  (−5) — weighted rating avg below 4/5
    g3 = 0.0
    if avg is not None:
        if avg < 4.0:
            g3 = -5.0
        elif avg < 5.0:
            g3 = -2.0
    score += g3
    breakdown["G3_panned"] = g3

    # G4  Not available  (−5)
    g4 = 0.0
    if not is_available:
        g4 = -5.0
    score += g4
    breakdown["G4_not_available"] = g4

    # ── Final clamp ──────────────────────────────────────────────────────────
    final = max(0, min(100, round(score)))
    breakdown["_total_raw"]   = round(score, 2)
    breakdown["_total_final"] = final

    if return_breakdown:
        return final, breakdown
    return final


def score_to_sonarr_profile_id(
    score: int,
    ranked_profiles: list[dict],
    target_resolution: int | None = None,
) -> int | None:
    """Select a Sonarr quality-profile id for *score* (Phase 3 downgrade helper).

    Thin wrapper over the shared ``select_profile_id`` (scoring/_shared.py) — the
    Radarr twin ``score_to_radarr_profile_id`` delegates to the SAME helper, so the
    Sonarr/Radarr profile-id selection can never drift. Sonarr quality profiles
    share the Radarr items/quality/resolution shape, hence one selector.
    """
    return select_profile_id(score, ranked_profiles, target_resolution)
