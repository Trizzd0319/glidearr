"""
TraktMovieScorer  — 100-point watchability engine
==================================================
Replaces the old 1-10 integer scale with a 0-100 float score built from
weighted, independently-capped signal groups.  The final score drives a
direct quality-profile selection rather than a separate threshold table.

Score groups and maximum contributions
---------------------------------------
  GROUP A — Household Intent          (max 33 pts)
    A1  keep_policy tag               +15   keep_forever/keep_movie = explicit curation
    A2  completion rate               ±12   watched to end vs abandoned
    A3  rewatch count                 +8    rewatched ≥ 2× = strong signal
    A4  user Trakt rating             +10   household 8+/10 = thumbs-up quality
    A5  watchlist intent              +8    EXPLICIT: a member put it on a watchlist.
                                            Graded by source (the acquisition scorer's own
                                            feed ranking), by how many members asked, and
                                            by staleness where the feed is dated. Also
                                            SHIELDS the title from deletion while the
                                            asking member is active — see
                                            _shared.intent_hold_active.

  GROUP B — Household Affinity        (max 20 pts)
    B1  actor affinity                +8    top-10 cast vs watch history
    B2  director affinity             +6    director vs watch history
    B3  writer affinity               +4    credited writers vs watch history
    B4  genre affinity                +4    per-genre vs household watches
    B5  studio affinity               +3    production company patterns

  GROUP C — Collection / Universe     (max 16 pts)
    C1  collection completeness       +8    ≥75% of collection watched
    C2  universe siblings watched     +4    MCU/DC/etc — franchise continuity
    C3  related-graph affinity        +4    Trakt-related neighbours watched —
                                            generalises C1/C2 onto the similarity
                                            graph (collaborative "people like me")

  GROUP D — Device / Playback Fit     (v2: 0 to −15 pts — a PENALTY, not a bonus)
    D4  transcode risk               −15    weighted P(this title transcodes) over the
                                            five causes the household is OBSERVED to
                                            transcode for (audio 38% / bitrate-res 36% /
                                            subtitle 14% / codec 11% / container). A title
                                            that will direct-play scores ~0; only likely-
                                            to-transcode is punished. See scoring/device_fit.py.
    D1  primary device capability      0    v1 LEGACY, reachable with
    D2  transcode avoidance            0    ``scoring.device_fit_v2: false`` — restores
    D3  platform resolution ceiling    0    the old +6/+5/+4 bonuses byte-for-byte.
                                            v1 gave 92% of this library EXACTLY 12.0.

  GROUP E — Audience Alignment        (max 10 pts)
    E1  kids content on kids devices  +6    G/PG + Aiden/Raina viewing pattern
    E2  adult content affinity        +4    R/NR + adult viewer pattern
    E3  library routing               +4    correct Plex library placement

  GROUP F — Content Quality           (max 24 pts)
    F1  critic consensus              +20   IMDb 35% + Trakt 25% + RT 25% + MC 15% —
                                            the strongest single positive signal so a
                                            critically-acclaimed title survives the prune
                                            and earns monitoring even while unwatched
                                            (deliberately ranked ABOVE director affinity)
    F2  popularity                    +2    trending on Trakt/TMDb
    F3  recency                       +2    released within 2 years

  GROUP G — Penalties                 (up to −18 pts)
    G1  language mismatch             −8    non-preferred language, no history
    G2  abandoned / never watched     −10   < 20% watched or completion_pct=0
    G3  critically panned             −5    weighted critic avg < 4.0
    G4  not yet available             −5    no physical/digital release date passed

Score → Quality Profile mapping
---------------------------------
CALIBRATED to the FILE-OWNING population's post-Group-D-v2 score distribution (see
the percentile table on ``scoring/_shared.QUALITY_PROFILE_THRESHOLDS``, including the
recorded caveat about the 4K rung):

   0 – 24   →  HD-720p               (floor; SD absorbed into 720p)
  25 – 28   →  WEBDL-1080p           p97   — good affinity
  29 – 32   →  Bluray-1080p          p98   — watched content
  33 – 37   →  Remux-1080p           p99   — strong affinity
  38 – 49   →  Remux-2160p           p99.5 — 4K entry
  50 – 100  →  Remux-2160p           p99.9 — top tier

The ``watch_likelihood.uhd_cutoff`` gate (75, a DIFFERENT scale) still caps the
actual tier on top of this — the score ladder proposes, the likelihood cap disposes.

No external I/O — pure function, safe to call from any context.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# Shared scoring commons — the single source of truth for the device/cert tables,
# the score->profile mapping, the affinity helper and the profile-id selector that
# the SHOW scorer ALSO uses. Imported here (and re-exported via this module for the
# trakt shim) so neither engine reads from the other. See scoring/_shared.py.
from scripts.managers.machine_learning.scoring._shared import (
    QUALITY_PROFILE_THRESHOLDS,           # noqa: F401  (re-exported for callers/shim)
    _DEVICE_CAPABILITIES,                 # noqa: F401  (re-exported for callers/shim)
    _DEVICE_RESOLUTION_CEILING,           # noqa: F401  (re-exported for back-compat readers)
    INTENT_HALF_LIFE_DAYS,
    INTENT_STALE_FLOOR,
    _KIDS_CERTS,
    _TRANSCODE_FRIENDLY_CODECS,
    affinity_topk as _affinity,
    codec_transcode_prior,
    device_resolution_ceiling,
    normalize_lang,
    person_affinity_score,
    related_graph_affinity,
    score_to_profile,                     # noqa: F401  (re-exported for callers/shim)
    select_profile_id,
    user_rating_score,
    watchlist_intent_score,
)
# Group-C4 reads the title's people-by-role from the SAME credits dict the scorer
# already receives (route_people classifies them like flatten_trakt_people, keyed on
# tmdb_person_id). Pure brain→brain import — no I/O.
from scripts.managers.machine_learning.people_matrix.build import route_people


# ── Group D v1 (LEGACY) ───────────────────────────────────────────────────────

def _score_group_d_legacy(movie, breakdown, platform_usage, transcode_stats,
                          target_resolution, video_codec, device_capabilities) -> None:
    """The v1 Group-D bonus terms, LIFTED VERBATIM out of ``score_movie``.

    Reachable whenever no ``transcode_profile`` is supplied — i.e. whenever
    ``scoring.device_fit_v2`` is off, or the household has no transcode evidence at all,
    or a legacy caller never passed one. Byte-for-byte identical to the pre-v2 code
    (the golden-corpus fixture is the proof); it was extracted only so the two paths sit
    side by side instead of interleaved. See scoring/device_fit.py for why v2 exists.

    Writes D1/D2/D3 into *breakdown*; the caller adds them to the score."""
    # D1  Primary device resolution capability  (+6)
    d1 = 0.0
    if platform_usage and target_resolution:
        # Identify the household's primary device (most-used platform)
        primary_platform = max(platform_usage, key=platform_usage.get, default="")
        # Fuzzy match against the device capability matrix. resolve_device_capability
        # prefers the MOST SPECIFIC entry ("Roku Ultra" -> 2160, bare "Roku" -> the
        # conservative 1080), where the old inline loop took the first insertion-order
        # hit and so could never see a model-specific key.
        device_ceil = device_resolution_ceiling(primary_platform, device_capabilities)
        if device_ceil is not None:
            if target_resolution <= device_ceil:
                # Device can handle this resolution
                d1 = 6.0 if target_resolution == device_ceil else 3.0
            else:
                # Would need downscaling — mild penalty
                d1 = -2.0
    elif not platform_usage:
        # No device data — neutral
        d1 = 0.0
    breakdown["D1_device_capability"] = d1

    # D2  Transcode avoidance  (+5)
    # The codec arrives as an explicit argument (the movie_files `video_codec` column,
    # threaded by features/movie_features). The old `movie.get("videoCodec")` read stays
    # as the fallback for legacy callers, but it never fired in the cache path: Radarr
    # nests the codec at movieFile.mediaInfo.videoCodec, so the top-level key was always
    # absent and D2 collapsed to the constant +2.0 branch for every movie in the library.
    #
    # OBSERVATION DOMINATES: a codec the household has actually been caught transcoding
    # is still scored from that observation and the static matrix is never consulted.
    # The matrix only fills the branch where there is NO observation — where D2 used to
    # award a flat +5 on the strength of "we have never seen this transcode", which on a
    # history with no per-codec detail blessed AV1 and XviD alike. See
    # ``codec_transcode_prior``: AV1 can never reach the direct-play rung (Plex
    # transcodes it whatever the hardware decodes) and a legacy codec no device in the
    # house direct-plays drops to +1.
    d2 = 0.0
    vcodec = str(video_codec or movie.get("videoCodec", "") or "")
    if vcodec and transcode_stats is not None:
        # Check if any transcode events involved this codec
        codec_transcoded = any(
            vcodec.lower() in pair.lower()
            for pair in (transcode_stats or {})
        )
        if not codec_transcoded:
            # No observed transcode for this codec — fall back to the device prior.
            d2 = codec_transcode_prior(vcodec, platform_usage, device_capabilities)
        elif vcodec.lower() in _TRANSCODE_FRIENDLY_CODECS:
            d2 = 2.0   # friendly codec even if some transcoding occurred
    elif not vcodec:
        d2 = 2.0       # unknown codec — assume moderate
    breakdown["D2_transcode_avoidance"] = d2

    # D3  Platform resolution ceiling (all devices)  (+4)
    d3 = 0.0
    if platform_usage and target_resolution:
        # Count share of plays from devices that support the target resolution
        total_plays   = sum(platform_usage.values()) or 1
        capable_plays = 0
        for platform, plays in platform_usage.items():
            ceil_ = device_resolution_ceiling(platform, device_capabilities)
            if ceil_ is not None and target_resolution <= ceil_:
                capable_plays += plays
        capable_pct = capable_plays / total_plays
        if capable_pct >= 0.75:
            d3 = 4.0
        elif capable_pct >= 0.5:
            d3 = 2.0
        elif capable_pct >= 0.25:
            d3 = 1.0
    breakdown["D3_platform_ceiling"] = d3


# ── Main scorer ───────────────────────────────────────────────────────────────

def score_movie(
    movie: dict,
    completion_pct: float,
    completion_threshold: float,
    collection_members: dict[int, set[int]],
    watched_tmdb_ids: set[int],
    genre_affinity: dict,
    credits: dict,
    *,
    # ── GROUP A extras ────────────────────────────────────────────────────────
    watch_count: int = 0,
    user_rating: float | None = None,        # Trakt user rating 0-10
    # A5 — explicit watchlist intent. ``intent_entry`` is this title's entry from
    # next_watch.build_intent_index ({"sources","members","dated","anchor"}); ``now`` is the
    # run-stable clock the staleness decay measures against. cap DEFAULT 0.0 → the term
    # contributes 0.0 → byte-identical until a caller opts in (see the C4 twin below).
    # The two decay knobs default to the module constants, so a caller that omits them
    # scores exactly as before; the service passes the CONFIG values through
    # _shared.resolve_intent_inputs (scoring.watchlist_intent.half_life_days/stale_floor).
    intent_entry: dict | None = None,
    intent_cap: float = 0.0,
    intent_now=None,
    intent_half_life_days: float = INTENT_HALF_LIFE_DAYS,
    intent_stale_floor: float = INTENT_STALE_FLOOR,
    # ── GROUP D — device/playback context ─────────────────────────────────────
    platform_usage: dict | None = None,      # {platform_name: play_count}
    transcode_stats: dict | None = None,     # {codec_pair: count} from Tautulli
    target_resolution: int | None = None,    # resolution we're evaluating for
    video_codec: str | None = None,          # the file's codec; falls back to movie["videoCodec"]
    device_capabilities: dict | None = None,  # platform -> DeviceCapability; None = shipped table
    # ── GROUP D v2 — transcode risk (scoring.device_fit_v2) ───────────────────
    # ``transcode_profile`` IS the switch. None (the default, and what every legacy caller
    # passes) → the v1 D1/D2/D3 bonus terms, byte-identical. A profile (built once per
    # pass by device_fit.build_transcode_profile) → the single negative D4 term. The
    # remaining kwargs are the file facts the risk axes need; all optional, all "unknown
    # → that axis is renormalised out", so a caller can adopt them incrementally.
    transcode_profile=None,
    video_bitrate: float | None = None,      # bits/sec; 0/None → derived from size÷runtime
    size_bytes: float | None = None,
    runtime_seconds: float | None = None,
    audio_codec: str | None = None,          # "AC3" / "DTS-HD MA" / "EAC3 Atmos" ...
    audio_channels: float | None = None,     # 2.0 / 5.1 / 7.1
    audio_languages: str | None = None,      # "eng" / "jpn/eng" (subtitle-forced-use test)
    subtitles: str | None = None,            # "eng/eng/rum" — track COUNT is the signal
    relative_path: str | None = None,        # container comes from the file extension
    # ── GROUP E — audience alignment ──────────────────────────────────────────
    per_user_affinity: dict | None = None,   # {username: affinity_dict}
    kids_users: list[str] | None = None,     # usernames flagged as kids
    adult_users: list[str] | None = None,    # usernames flagged as adults
    # ── GROUP F — content quality ─────────────────────────────────────────────
    imdb_rating: float | None = None,
    tmdb_rating: float | None = None,
    trakt_rating: float | None = None,
    metacritic_score: float | None = None,
    rotten_tomatoes_score: float | None = None,
    popularity: float | None = None,
    in_cinemas_date: str | None = None,
    physical_release_date: str | None = None,
    digital_release_date: str | None = None,
    # ── Other enrichment ──────────────────────────────────────────────────────
    certification: str | None = None,
    original_language: str | None = None,
    is_franchise_entry: bool = False,
    universe_name: str | None = None,
    keep_policy: str | None = None,
    preferred_languages: list[str] | None = None,
    # Fraction (0..1) of the movie's file watchable in a preferred language (dub OR sub;
    # 1.0/0.0 for a single-file movie). When provided, G1 is FILE-aware: no penalty when
    # an English dub/sub is present regardless of the film's foreign origin.
    language_consumable_fraction: float | None = None,
    is_available: bool = True,
    # Multiplier on the Group-B cast/crew/studio/genre weight caps. >1 lets strong
    # affinity drive the score (and the watch-likelihood upgrade tier) higher.
    affinity_boost: float = 1.0,
    # ── GROUP C3 — related-graph collaborative affinity ────────────────────────
    # related_tmdb_ids: this movie's Trakt-related neighbour TMDb ids (from the
    # daemon-cached movie_related bucket). Intersected with watched_tmdb_ids.
    related_tmdb_ids: set[int] | None = None,
    related_graph_cap: float = 4.0,
    # ── GROUP C4 — person-affinity (id-keyed cast/crew taste overlap) ───────────
    # person_weights: {tmdb_person_id: weight} household affinity (people_matrix); the
    # title's own people are read from `credits` (already passed). cap DEFAULT 0.0 →
    # the term contributes 0.0 and the score is byte-identical until a caller opts in.
    person_weights: dict | None = None,
    person_affinity_cap: float = 0.0,
    # ── Detailed breakdown output ─────────────────────────────────────────────
    return_breakdown: bool = False,
) -> int | tuple[int, dict]:
    """
    Return a 0-100 watchability score for *movie*.

    Parameters
    ----------
    movie:
        Radarr/Trakt movie dict.  Used fields: ``tmdbId``, ``genres``, ``collection``.
    completion_pct:
        Fraction watched (0.0-1.0).  0.0 = never watched.
    completion_threshold:
        Fraction required to count as "watched" (e.g. 0.9).
    collection_members:
        ``{collection_tmdb_id: set_of_tmdb_ids}`` from full library.
    watched_tmdb_ids:
        All tmdbIds the household has watched.
    genre_affinity:
        From ``TautulliUsersManager.compute_genre_affinity``.
        Keys: ``"genres"``, ``"actors"``, ``"directors"``, ``"studios"``,
        ``"format_metrics"``.
    credits:
        Normalised Trakt credits — REQUIRED (pass ``{}`` if unavailable).
    watch_count:
        Number of times this movie has been watched (0 = never).
    user_rating:
        Household Trakt rating (0-10 scale).
    platform_usage:
        ``{platform_name: play_count}`` from Tautulli devices manager.
        Used to determine primary device and its resolution ceiling.
    transcode_stats:
        ``{codec_pair: count}`` from Tautulli transcode manager.
        Codec pairs that appear here have caused transcoding events. These OBSERVED
        events dominate D2: the static device matrix is consulted only for a codec
        that appears in none of them.
    target_resolution:
        The resolution (e.g. 1080, 2160) the device-fit terms are asked about.
        **Semantics — the resolution we CURRENTLY HOLD** (``movie_files.resolution``,
        i.e. the mediaInfo height of the file on disk), NOT the resolution the
        title's quality profile would acquire. Two reasons, in order of weight:

        1. *Non-circularity.* The profile is chosen FROM the score
           (``score_to_profile`` → ``select_profile_id``). Feeding the profile's
           resolution back into D1/D3 would close a feedback loop — score raises
           the tier, the higher tier raises D1/D3, which raises the score — and
           the ladder rungs would no longer mean anything stable.
        2. *The question D1/D3 actually ask* is "can the household's devices play
           what we have?". That is a statement about the file on disk. A title we
           hold at 720p on a 4K-only household is a real playback-fit fact; a
           hypothetical 2160p acquisition is not.

        This also makes the movie path agree with the SHOW path, where
        ``show_features.build_show_feature_row`` has always set
        ``target_resolution`` to ``max(rows["resolution"])`` — the held files.
        ``None`` (no file / unknown resolution) → D1 and D3 contribute 0.0.
    video_codec:
        The file's video codec (``movie_files.video_codec``), used by D2. When
        omitted it falls back to ``movie["videoCodec"]`` so pre-existing callers
        that packed the codec into the movie dict are byte-identical. Radarr
        nests the codec at ``movieFile.mediaInfo.videoCodec``, never at the top
        level, so the fallback alone always resolved to "" in the cache path —
        which is why D2 was pinned to the constant +2.0 "unknown codec" branch.
    device_capabilities:
        ``{platform_substring: DeviceCapability}`` overriding the shipped cold-start
        matrix (``_shared._DEVICE_CAPABILITIES``) that D1/D2/D3 fuzzy-match platform
        names against. ``None`` uses the shipped table. Build one from config with
        ``_shared.resolve_device_capabilities(config)`` — the ``scoring.device_capabilities``
        escape hatch for a device the shipped table has never heard of.
    per_user_affinity:
        Per-user affinity from ``compute_per_user_genre_affinity``.
        Keys are Tautulli usernames.
    kids_users / adult_users:
        Usernames for household segmentation (from rating_groups config).
    return_breakdown:
        If True, return ``(score, breakdown_dict)`` instead of just the int.

    Returns
    -------
    int  (or tuple[int, dict] if return_breakdown=True)
        Score in [0, 100].  Use ``score_to_profile()`` to map to a quality
        profile name.
    """
    if preferred_languages is None:
        preferred_languages = ["en"]
    if kids_users is None:
        kids_users = []
    if adult_users is None:
        adult_users = []

    breakdown: dict[str, float] = {}
    score: float = 0.0

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP A — Household Intent  (max 25)
    # ═══════════════════════════════════════════════════════════════════════════

    # A1  keep_policy tag  (+15)
    a1 = 0.0
    if keep_policy in ("keep_forever", "keep_movie"):
        a1 = 15.0
    elif keep_policy in ("keep_universe",):
        a1 = 8.0
    elif keep_policy == "universe":
        a1 = 4.0
    score += a1
    breakdown["A1_keep_policy"] = a1

    # A2  Completion rate  (±12)
    a2 = 0.0
    if completion_pct >= completion_threshold:
        a2 = 12.0
    elif completion_pct >= 0.75:
        a2 = 6.0
    elif completion_pct >= 0.5:
        a2 = 2.0
    elif completion_pct >= 0.2:
        a2 = -3.0
    elif completion_pct > 0:
        a2 = -6.0
    # completion_pct == 0 → 0 (not watched at all — neutral here, penalised in G2)
    score += a2
    breakdown["A2_completion"] = a2

    # A3  Rewatch count  (+8)
    a3 = 0.0
    if watch_count >= 3:
        a3 = 8.0
    elif watch_count == 2:
        a3 = 5.0
    elif watch_count == 1:
        a3 = 2.0
    score += a3
    breakdown["A3_rewatch"] = a3

    # A4  User Trakt rating  (+10) — shared formula (scoring/_shared.user_rating_score):
    # linear, 0 at 5/10, +10 at 10/10, negative below 5.
    a4 = user_rating_score(user_rating)
    score += a4
    breakdown["A4_user_rating"] = a4

    # A5  Watchlist intent  (+ intent_cap, DEFAULT 0.0 → byte-identical)
    # The scorecard's only EXPLICIT-INTENT term: somebody in this house put this title on a
    # watchlist. Group A is its home because A1 (keep tags) is already explicit human
    # curation and this group is literally titled "Household Intent — do they want this?";
    # every other group INFERS desire from behaviour, this one reads a statement of it.
    # Graded by source strength x staleness x member count — see
    # _shared.watchlist_intent_score. Inert until a caller passes a positive cap.
    a5 = 0.0
    if intent_cap > 0 and intent_entry:
        a5 = watchlist_intent_score(intent_entry, intent_cap, now=intent_now,
                                    half_life_days=intent_half_life_days,
                                    stale_floor=intent_stale_floor)
    score += a5
    breakdown["A5_intent"] = a5

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP B — Household Affinity  (max 20)
    # ═══════════════════════════════════════════════════════════════════════════

    # GROUP B/E affinity bumps use the shared affinity_topk helper (imported as
    # _affinity at module level) — one definition, shared with the show scorer.
    actors_aff    = genre_affinity.get("actors",    {})
    directors_aff = genre_affinity.get("directors", {})
    writers_aff   = genre_affinity.get("writers",   {})
    genres_aff    = genre_affinity.get("genres",    {})
    studios_aff   = genre_affinity.get("studios",   {})

    cast_list  = credits.get("cast") or []
    crew_list  = credits.get("crew") or []

    cast_sorted = sorted(
        [m for m in cast_list if m.get("name")],
        key=lambda m: m.get("order", 999),
    )
    actor_names = [m["name"] for m in cast_sorted[:10]]

    directors = [
        m["name"] for m in crew_list
        if m.get("job", "").lower() == "director" and m.get("name")
    ]
    writers = [
        m["name"] for m in crew_list
        if m.get("job", "").lower() in {
            "screenplay", "story", "writer",
            "original screenplay", "original story",
        } and m.get("name")
    ]

    movie_genres  = movie.get("genres") or []
    prod_companies = [
        c.get("name") for c in (movie.get("productionCompanies") or [])
        if c.get("name")
    ] or [movie.get("studio")] if movie.get("studio") else []

    _ab = max(1.0, float(affinity_boost or 1.0))   # boost cast/crew/studio/genre weight
    # Caps redistributed 2026-08-07 (operator ruling, see PERSON_ROLE_WEIGHTS): actors
    # 8→10, writers 4→2 — SUM-PRESERVING (25 → 25, no axis change). Cast-following is
    # the signal; writer overlap is largely franchise continuation the saga machinery
    # already rewards. Directors stay 6.0 here — the Russo-pattern trim is applied once,
    # on the shared id-side table, not twice.
    b1 = _affinity(actor_names,    actors_aff,    10.0 * _ab)
    b2 = _affinity(directors,      directors_aff, 6.0 * _ab)
    b3 = _affinity(writers,        writers_aff,   2.0 * _ab)
    b4 = _affinity(movie_genres,   genres_aff,    4.0 * _ab)
    b5 = _affinity(prod_companies, studios_aff,   3.0 * _ab)

    score += b1 + b2 + b3 + b4 + b5
    breakdown.update({
        "B1_actor_affinity":    b1,
        "B2_director_affinity": b2,
        "B3_writer_affinity":   b3,
        "B4_genre_affinity":    b4,
        "B5_studio_affinity":   b5,
    })

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP C — Collection / Universe  (max 12)
    # ═══════════════════════════════════════════════════════════════════════════

    movie_tmdb = movie.get("tmdbId")
    coll       = movie.get("collection") or {}
    coll_id    = coll.get("tmdbId")

    # C1  Collection completeness  (+8)
    c1 = 0.0
    if coll_id and coll_id in collection_members:
        all_in_coll = collection_members[coll_id]
        others      = all_in_coll - ({movie_tmdb} if movie_tmdb else set())
        if others:
            watched_others = others & watched_tmdb_ids
            pct = len(watched_others) / len(others)
            if pct >= 0.75:
                c1 = 8.0
            elif pct >= 0.5:
                c1 = 5.0
            elif pct >= 0.25:
                c1 = 2.0
    score += c1
    breakdown["C1_collection"] = c1

    # C2  Universe siblings  (+4)
    c2 = 0.0
    in_universe = bool(is_franchise_entry or (universe_name and universe_name.strip()))
    if in_universe and movie_tmdb:
        siblings_watched = sum(
            len((members - {movie_tmdb}) & watched_tmdb_ids)
            for cid, members in collection_members.items()
            if movie_tmdb in members
        )
        if siblings_watched >= 5:
            c2 = 4.0
        elif siblings_watched >= 2:
            c2 = 2.5
        elif siblings_watched >= 1:
            c2 = 1.0
    score += c2
    breakdown["C2_universe"] = c2

    # C3  Related-graph affinity  (+ related_graph_cap, default +4)
    # Generalises C1/C2 from formal collections/universes to Trakt's similarity
    # graph: how many of this movie's related neighbours the household has watched.
    c3 = related_graph_affinity(related_tmdb_ids, watched_tmdb_ids, cap=related_graph_cap)
    score += c3
    breakdown["C3_related_graph"] = c3

    # C4  Person-affinity  (+ person_affinity_cap, DEFAULT 0.0 → byte-identical)
    # How strongly this title's cast/crew (by tmdb_person_id, from `credits`) overlaps
    # the household's person-affinity. Id-keyed — immune to name-spelling drift that
    # Group-B's name match suffers. Inert until a caller passes a positive cap.
    c4 = 0.0
    if person_affinity_cap > 0 and person_weights:
        c4 = person_affinity_score(route_people(credits), person_weights, person_affinity_cap)
    score += c4
    breakdown["C4_person_affinity"] = c4

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP D — Device / Playback Fit
    # ═══════════════════════════════════════════════════════════════════════════
    # v2 (``transcode_profile`` supplied, i.e. scoring.device_fit_v2 on): ONE negative
    # term, ``D4_transcode_risk``, and D1/D2/D3 report 0.0. v1 (profile None): the three
    # legacy bonus terms below, byte-for-byte unchanged. See scoring/device_fit.py for
    # why v1 had to go — 92% of this library scored EXACTLY 12.0 on it.
    if transcode_profile is not None:
        from scripts.managers.machine_learning.scoring.device_fit import device_fit_penalty
        d4, _d_detail = device_fit_penalty(
            transcode_profile,
            resolution=target_resolution,
            video_codec=(video_codec or movie.get("videoCodec") or None),
            video_bitrate=video_bitrate,
            size_bytes=size_bytes,
            runtime_seconds=runtime_seconds,
            audio_codec=audio_codec,
            audio_channels=audio_channels,
            audio_languages=audio_languages,
            subtitles=subtitles,
            relative_path=relative_path,
        )
        score += d4
        # D1/D2/D3 stay in the breakdown at 0.0 so every consumer keyed on the group
        # vocabulary (the persisted watchability_breakdown schema, the Hidden Gems taste
        # filter, space_greenfield's file-derived split, the ML snapshot columns) keeps
        # working without a migration.
        breakdown["D1_device_capability"] = 0.0
        breakdown["D2_transcode_avoidance"] = 0.0
        breakdown["D3_platform_ceiling"] = 0.0
        breakdown["D4_transcode_risk"] = d4
    else:
        _score_group_d_legacy(
            movie, breakdown, platform_usage, transcode_stats,
            target_resolution, video_codec, device_capabilities,
        )
        score += (breakdown["D1_device_capability"]
                  + breakdown["D2_transcode_avoidance"]
                  + breakdown["D3_platform_ceiling"])
        breakdown["D4_transcode_risk"] = 0.0

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP E — Audience Alignment  (max 10)
    # ═══════════════════════════════════════════════════════════════════════════

    cert_lower = (certification or "").upper().strip()
    is_kids_cert  = cert_lower in {c.upper() for c in _KIDS_CERTS}
    is_adult_cert = cert_lower in ("R", "NC-17", "TV-MA", "NR", "18", "M")

    # E1  Kids content watched by kids users  (+6)
    e1 = 0.0
    if is_kids_cert and per_user_affinity and kids_users:
        kids_genre_aff = {}
        for user in kids_users:
            ua = per_user_affinity.get(user, {})
            for genre, cnt in (ua.get("genres") or {}).items():
                kids_genre_aff[genre] = kids_genre_aff.get(genre, 0) + cnt
        kids_genre_bonus = _affinity(movie_genres, kids_genre_aff, 6.0)
        e1 = kids_genre_bonus
    elif is_kids_cert:
        e1 = 2.0   # kids cert but no per-user data — small boost
    score += e1
    breakdown["E1_kids_alignment"] = e1

    # E2  Adult content watched by adult users  (+4)
    e2 = 0.0
    if is_adult_cert and per_user_affinity and adult_users:
        adult_genre_aff = {}
        for user in adult_users:
            ua = per_user_affinity.get(user, {})
            for genre, cnt in (ua.get("genres") or {}).items():
                adult_genre_aff[genre] = adult_genre_aff.get(genre, 0) + cnt
        e2 = _affinity(movie_genres, adult_genre_aff, 4.0)
    score += e2
    breakdown["E2_adult_alignment"] = e2

    # E3  Library routing fit  (+4)
    # If the movie is an animation/family in the kids library it belongs there;
    # same for anime — +4 if genres match the expected library.
    e3 = 0.0
    anime_genres  = {"anime", "animation"}
    family_genres = {"family", "children", "animation", "kids"}
    movie_genre_lower = {g.lower() for g in movie_genres}
    if is_kids_cert and bool(movie_genre_lower & family_genres):
        e3 = 4.0
    elif bool(movie_genre_lower & anime_genres) and not is_kids_cert:
        e3 = 2.0   # anime — different library but still a fit signal
    score += e3
    breakdown["E3_library_fit"] = e3

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP F — Content Quality  (max 10)
    # ═══════════════════════════════════════════════════════════════════════════

    # F1  Critic consensus  (+6)
    f1       = 0.0
    c_scores: list[tuple[float, float]] = []
    if imdb_rating      and 0 < imdb_rating      <= 10:  c_scores.append((imdb_rating,              0.35))
    if trakt_rating     and 0 < trakt_rating     <= 100: c_scores.append((trakt_rating / 10.0,      0.25))
    if rotten_tomatoes_score and 0 < rotten_tomatoes_score <= 100:
        c_scores.append((rotten_tomatoes_score / 10.0, 0.25))
    if metacritic_score and 0 < metacritic_score <= 100: c_scores.append((metacritic_score / 10.0,  0.15))
    if not c_scores and tmdb_rating and 0 < tmdb_rating <= 10:
        c_scores.append((tmdb_rating, 1.0))

    if c_scores:
        tw  = sum(w for _, w in c_scores)
        avg = sum(s * w for s, w in c_scores) / tw
        if avg >= 8.5:
            f1 = 20.0   # masterpiece — strongest single positive signal
        elif avg >= 7.5:
            f1 = 14.0   # critically acclaimed
        elif avg >= 6.5:
            f1 = 8.0    # well-reviewed — already above director affinity (+6)
        elif avg >= 5.5:
            f1 = 3.0
        # below 5.5 — no bonus (penalty applied in G3)
    score += f1
    breakdown["F1_critic_consensus"] = f1

    # F2  Popularity  (+2)
    f2 = 0.0
    if popularity and popularity > 0:
        if popularity >= 100:
            f2 = 2.0
        elif popularity >= 50:
            f2 = 1.5
        elif popularity >= 20:
            f2 = 0.75
    score += f2
    breakdown["F2_popularity"] = f2

    # F3  Recency  (+2)
    f3 = 0.0
    _now = datetime.now(tz=timezone.utc)
    for date_str in (in_cinemas_date, physical_release_date, digital_release_date):
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_years = (_now - dt).days / 365.25
            if age_years <= 1.0:
                f3 = 2.0
            elif age_years <= 2.0:
                f3 = 1.0
            break
        except (ValueError, TypeError):
            continue
    score += f3
    breakdown["F3_recency"] = f3

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP G — Penalties  (up to −18)
    # ═══════════════════════════════════════════════════════════════════════════

    # G1  Language mismatch  (−8)
    # Normalise both sides to ISO 639-1 (the *arr API hands us the display NAME
    # "English", not "en") so a preferred-language title isn't wrongly penalised.
    g1 = 0.0
    if original_language:
        lang = normalize_lang(original_language)
        pref = {normalize_lang(p) for p in preferred_languages}
        pref.discard(None)
        if lang and lang not in pref:
            # FILE-AWARE (preferred): penalty scales with the unwatchable fraction — a
            # preferred-language AUDIO (dub) or SUBTITLE (sub) track means watchable, so a
            # movie with an English dub/sub gets 0 regardless of foreign origin; a file
            # with neither gets -8 (a re-acquisition candidate). Single-file movies are
            # binary (1.0 → 0, 0.0 → -8).
            if language_consumable_fraction is not None:
                frac = max(0.0, min(1.0, language_consumable_fraction))
                g1 = round(-8.0 * (1.0 - frac), 1)
            else:
                # No per-file track data — preserve the legacy household-history shape
                # so callers that don't pass a fraction stay byte-identical.
                fmt = genre_affinity.get("format_metrics", {})
                audio_langs = fmt.get("audio_language", {})
                lang_plays  = sum(
                    v for k, v in audio_langs.items()
                    if lang in (normalize_lang(k) or k.lower())
                )
                if lang_plays == 0:
                    g1 = -8.0
                elif lang_plays <= 2:
                    g1 = -4.0
                else:
                    g1 = -1.0   # household watches this language regularly
    score += g1
    breakdown["G1_language"] = g1

    # G2  Abandoned / never watched  (−10)
    g2 = 0.0
    if completion_pct > 0 and completion_pct < 0.2:
        g2 = -10.0   # started but abandoned very early
    # completion_pct == 0 means never attempted — no penalty here since
    # A2 already gives 0 for that case
    score += g2
    breakdown["G2_abandoned"] = g2

    # G3  Critically panned  (−5)
    g3 = 0.0
    if c_scores:
        tw  = sum(w for _, w in c_scores)
        avg = sum(s * w for s, w in c_scores) / tw
        if avg < 4.0:
            g3 = -5.0
        elif avg < 5.0:
            g3 = -2.0
    score += g3
    breakdown["G3_panned"] = g3

    # G4  Not yet available  (−5)
    g4 = 0.0
    if not is_available:
        has_past_release = False
        for date_str in (physical_release_date, digital_release_date, in_cinemas_date):
            if not date_str:
                continue
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= _now:
                    has_past_release = True
                    break
            except (ValueError, TypeError):
                continue
        if not has_past_release:
            g4 = -5.0
    score += g4
    breakdown["G4_not_available"] = g4

    # ── Final clamp ──────────────────────────────────────────────────────────
    final = max(0, min(100, round(score)))
    breakdown["_total_raw"]  = round(score, 2)
    breakdown["_total_final"] = final

    if return_breakdown:
        return final, breakdown
    return final


# ── Profile selector ─────────────────────────────────────────────────────────

def score_to_radarr_profile_id(
    score: int,
    ranked_profiles: list[dict],
    target_resolution: int | None = None,
    ladder=None,
) -> int | None:
    """
    Select a Radarr quality profile ID for the given score.

    Thin wrapper over the shared ``select_profile_id`` (scoring/_shared.py). The
    Sonarr twin ``score_to_sonarr_profile_id`` delegates to the SAME helper, so the
    Radarr/Sonarr profile-id selection can never drift. ``ranked_profiles`` is the
    list from ``_fetch_ranked_profiles`` (ascending by max resolution); if
    ``target_resolution`` is given, no profile exceeding it is returned.
    """
    return select_profile_id(score, ranked_profiles, target_resolution, ladder)
