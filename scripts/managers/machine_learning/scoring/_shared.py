"""
scoring/_shared.py — the cross-engine scoring commons (pure).
================================================================================
The constants AND pure helpers that the movie and show watchability engines BOTH
use. Extracted (ML Step 2.1) so neither scorer imports the other: previously
``show_scorer`` reached into ``movie_scorer`` for the device/cert tables, the
``score_to_profile`` mapping and a copy-pasted affinity helper, which coupled the
two engines and risked silent drift. Now ``movie_scorer`` and ``show_scorer`` are
siblings that each import from here; **this module imports neither of them**, so
there is exactly one definition of every shared symbol and no directional
dependency between the engines.

Folds in what MIGRATION.md Step 2 earmarked for ``scoring/constants.py`` — but a
plain ``constants.py`` could not host the three pure helpers below, so the single
shared space carries both the tables and the helpers that operate on them.

Contents (all pure — no I/O, no service imports, no global_cache):
  * QUALITY_PROFILE_THRESHOLDS  — score → profile-name-pattern ladder
  * _DEVICE_RESOLUTION_CEILING  — device platform → max supported resolution
  * _TRANSCODE_FRIENDLY_CODECS  — codecs that direct-play on typical devices
  * _KIDS_CERTS                 — certifications that mark kids content
  * normalize_lang(value)                               — language NAME or ISO 639-2
                                                          code → ISO 639-1 code, so the
                                                          G1 penalty compares like with like
  * affinity_topk(names, aff_map, cap)                  — top-3 mean affinity → cap
  * user_rating_score(user_rating, *, slope, pos_cap, neg_cap, confidence)
                                                        — Group-A4 declared-rating bump
                                                          (movie defaults vs gentler show knobs)
  * related_graph_affinity(related_ids, watched_ids, *, cap)
                                                        — Group-C3 collaborative neighbour-watch
                                                          signal (generalises C1/C2 onto Trakt's
                                                          related graph)
  * score_to_profile(score)                             — score → profile-name pattern
  * select_profile_id(score, ranked_profiles, target_resolution=None) — score → id
"""
from __future__ import annotations


# ── Quality profile thresholds (score → profile name pattern) ────────────────
QUALITY_PROFILE_THRESHOLDS: list[tuple[int, str]] = [
    (80, "Remux 2160p"),          # Dolby Vision tier — full intent
    (70, "Remux 2160p"),          # HDR tier — franchise + 4K device
    (60, "Remux 1080p"),          # Strong affinity
    (50, "Bluray 1080p"),         # Watched content
    (35, "WEBDL 1080p"),          # Good affinity
    (0,  "HD 720p"),              # Minimum floor — SD absorbed into 720p
                                  # (older content without HD masters still
                                  #  benefits from 720p container/metadata)
]

# Known device platform → max supported resolution
# Extend this map as new devices are observed in Tautulli history.
_DEVICE_RESOLUTION_CEILING: dict[str, int] = {
    "apple tv":      2160,
    "apple tv 4k":   2160,
    "tv":            2160,
    "lg tv":         2160,
    "samsung tv":    2160,
    "chromecast":    1080,
    "chromecast ultra": 2160,
    "roku":          2160,
    "fire tv":       2160,
    "ipad":          1080,
    "iphone":        1080,
    "android":       1080,
    "android tv":    2160,
    "playstation":   2160,
    "xbox":          2160,
    "web":           1080,
    "chrome":        1080,
    "safari":        1080,
    "windows":       2160,
    "mac":           2160,
    "linux":         2160,
    "kodi":          2160,
}

# Codecs that commonly require transcoding on typical consumer devices.
# If the household has never transcoded this codec, score it well.
_TRANSCODE_FRIENDLY_CODECS: set[str] = {
    "h264", "avc", "hevc", "h265", "av1",
    "aac", "ac3", "eac3",
}

# Kids certifications
_KIDS_CERTS: frozenset[str] = frozenset(
    {"g", "pg", "tv-g", "tv-y", "tv-y7", "all", "e", "u"}
)


# Language NAME / ISO 639-2 (3-letter) → ISO 639-1 (2-letter). The *arr APIs hand
# us the display NAME ("English") under originalLanguage.name, and mediaInfo audio
# tracks use 3-letter codes ("eng"); the G1 penalty and the preferred_languages
# config both speak ISO 639-1 ("en"). Without this map, "english" != "en" so EVERY
# English title wrongly earned the −8 non-preferred-language penalty. Covers the
# languages that actually appear in a typical *arr library; unknown values pass
# through unchanged (an unrecognised non-preferred language still gets penalised).
_LANGUAGE_ALIAS_TO_ISO1: dict[str, str] = {
    # display names
    "english": "en", "japanese": "ja", "french": "fr", "korean": "ko",
    "hindi": "hi", "chinese": "zh", "mandarin": "zh", "cantonese": "zh",
    "italian": "it", "german": "de", "spanish": "es", "castilian": "es",
    "portuguese": "pt", "russian": "ru", "dutch": "nl", "flemish": "nl",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi",
    "polish": "pl", "turkish": "tr", "arabic": "ar", "hebrew": "he",
    "thai": "th", "vietnamese": "vi", "indonesian": "id", "malay": "ms",
    "tagalog": "tl", "filipino": "tl", "greek": "el", "czech": "cs",
    "hungarian": "hu", "romanian": "ro", "ukrainian": "uk", "tamil": "ta",
    "telugu": "te", "malayalam": "ml", "kannada": "kn", "bengali": "bn",
    "marathi": "mr", "punjabi": "pa", "urdu": "ur", "persian": "fa", "farsi": "fa",
    "catalan": "ca", "icelandic": "is", "croatian": "hr", "serbian": "sr",
    "slovak": "sk", "slovenian": "sl", "bulgarian": "bg", "lithuanian": "lt",
    "latvian": "lv", "estonian": "et", "latin": "la",
    # ISO 639-2/B (and a couple /T) 3-letter codes seen in mediaInfo audio tracks
    "eng": "en", "jpn": "ja", "fra": "fr", "fre": "fr", "kor": "ko", "hin": "hi",
    "zho": "zh", "chi": "zh", "ita": "it", "deu": "de", "ger": "de", "spa": "es",
    "por": "pt", "rus": "ru", "nld": "nl", "dut": "nl", "swe": "sv", "nor": "no",
    "dan": "da", "fin": "fi", "pol": "pl", "tur": "tr", "ara": "ar", "heb": "he",
    "tha": "th", "vie": "vi", "ind": "id", "ell": "el", "gre": "el", "ces": "cs",
    "cze": "cs", "hun": "hu", "ron": "ro", "rum": "ro", "ukr": "uk", "fas": "fa",
    "per": "fa",
}


def preferred_language_available(audio_languages, subtitles, preferred_languages) -> bool:
    """True if a title is WATCHABLE in a preferred language — a preferred-language
    AUDIO track (dub) OR a preferred-language SUBTITLE track (sub) is present on the
    actual file.

    ``audio_languages`` / ``subtitles`` are the parquet's slash/comma-joined ISO-code
    strings (e.g. ``"jpn/eng"`` for an anime with an English dub, ``"eng/eng"`` for
    English subs); ``preferred_languages`` is the config list (e.g. ``["en"]``). Empty
    preference → True (no language gate at all).

    This makes the Group-G1 penalty FILE-AWARE: an anime whose ORIGINAL language is
    Japanese but which ships an English dub (audio) OR English subtitles is consumable
    and must not be penalised for its origin. Only a file with NEITHER a preferred
    audio NOR a preferred subtitle track is genuinely un-watchable as-is (→ G1 penalty
    + a candidate for language re-acquisition).
    """
    pref = {normalize_lang(p) for p in (preferred_languages or [])}
    pref.discard(None)
    if not pref:
        return True
    for blob in (audio_languages, subtitles):
        if not blob:
            continue
        for code in str(blob).replace(",", "/").split("/"):
            code = code.strip()
            if code and normalize_lang(code) in pref:
                return True
    return False


def normalize_lang(value) -> str | None:
    """Language display NAME or ISO 639-2 code → ISO 639-1 code (lowercased).

    ``"English"`` / ``"english"`` / ``"eng"`` → ``"en"``; an already-2-letter code
    (``"en"``) passes through; an unknown value passes through lowercased (so an
    unrecognised non-preferred language is still treated as non-preferred). Empty /
    None → None. Pure — used by both scorers' G1 so the penalty compares like-for-like.
    """
    if not value:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    return _LANGUAGE_ALIAS_TO_ISO1.get(s, s)


# ── Shared pure helpers ──────────────────────────────────────────────────────

def affinity_topk(names: list[str], aff_map: dict, cap: float) -> float:
    """Top-3 mean affinity of *names* against *aff_map*, scaled to *cap*.

    The single source of truth for the Group-B / Group-E affinity bump used by
    BOTH ``score_movie`` (where it was a nested closure) and ``score_show`` (where
    it was a module-level duplicate). Returns 0.0 when there is nothing to match.

    *aff_map* is a ``{name: weight}`` map (e.g. actor/director/genre/studio
    affinity). Each present name contributes ``weight / max_weight``; the top-3
    such ratios are averaged and scaled to *cap*. Names are matched case-folded.
    """
    if not names or not aff_map:
        return 0.0
    top = max(aff_map.values(), default=1) or 1
    matched = [
        aff_map.get(n, aff_map.get(n.lower(), 0)) / top
        for n in names
        if aff_map.get(n, aff_map.get(n.lower(), 0)) > 0
    ]
    if not matched:
        return 0.0
    # Use top-3 average to reduce noise from long cast lists
    top3 = sorted(matched, reverse=True)[:3]
    return round(min(cap, (sum(top3) / len(top3)) * cap), 3)


def person_affinity_score(
    media_people_ids: dict,
    person_weights: dict,
    cap: float,
    *,
    role_weights: dict | None = None,
) -> float:
    """Group-C4 person-affinity: a title's people-overlap with the household's taste.

    The id-keyed parallel to Group-B's name-based ``affinity_topk`` — it is immune to
    "Scarlett Johansson" vs alias name-drift because it intersects stable
    ``tmdb_person_id`` ints. It MUST stay a separate function: ``affinity_topk`` does
    ``aff_map.get(n.lower())`` which raises ``AttributeError`` on an int key.

    ``media_people_ids`` — the title's people by role ``{role: [tmdb_person_id]}`` (from
                           the people_matrix forward map OR the live credits dict the
                           scorer already receives).
    ``person_weights``   — ``{tmdb_person_id: weight}`` household affinity (watched-set
                           derived; see ``aggregate_person_affinity``).
    ``cap``              — max contribution. With ``cap <= 0`` (the scorer default) this
                           returns 0.0 → the term is byte-identical until a caller opts in.

    Per matched person: ``(weight / max_weight) * role_weight``; the top-3 such products
    are averaged and scaled to ``cap`` (mirrors ``affinity_topk``'s top-3-mean shape so
    C4 behaves like the other affinity bumps, only keyed on ids). 0.0 when either map is
    empty or nothing matches.
    """
    if not media_people_ids or not person_weights or cap <= 0:
        return 0.0
    from scripts.managers.machine_learning.people_matrix.build import PERSON_ROLE_WEIGHTS
    role_weights = role_weights or PERSON_ROLE_WEIGHTS
    top = max(person_weights.values(), default=1) or 1

    contributions: list[float] = []
    for role, pids in media_people_ids.items():
        rw = role_weights.get(role, 0.0)
        if rw <= 0:
            continue
        for pid in pids:
            w = person_weights.get(pid, 0)
            if w > 0:
                contributions.append((w / top) * rw)
    if not contributions:
        return 0.0
    top3 = sorted(contributions, reverse=True)[:3]
    return round(min(cap, (sum(top3) / len(top3)) * cap), 3)


def resolve_person_affinity_inputs(config, affinity_raw) -> "tuple[dict, float]":
    """Owned-scorer Group-C4 inputs ``(person_weights, cap)`` from config + the cached
    household person-affinity (``people_matrix/affinity``, ``{str(person_id): weight}``).

    The single gate for BOTH the movie (space_pressure) and show (episode_files) upgrade
    paths so they can't drift. ``cap`` is forced to 0.0 — making C4 byte-identical — when
    the term is config-disabled (``scoring.person_affinity.enabled``) OR the people-matrix
    affinity is empty, so a library that never built the matrix is wholly unaffected.
    Default cap 8.0 (mirrors the Group-C2 ratios + the C4 integration test) when enabled
    and weights exist."""
    weights: dict[int, float] = {}
    for k, v in (affinity_raw or {}).items():
        try:
            weights[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    pa = ((config or {}).get("scoring", {}) or {}).get("person_affinity", {}) or {}
    enabled = bool(pa.get("enabled", True)) if isinstance(pa, dict) else bool(pa)
    try:
        cap = float(pa.get("cap", 8.0)) if isinstance(pa, dict) else 8.0
    except (TypeError, ValueError):
        cap = 8.0
    if not enabled or not weights or cap <= 0:
        return weights, 0.0
    return weights, cap


def user_rating_score(
    user_rating: float | None,
    *,
    slope: float = 2.0,
    pos_cap: float = 10.0,
    neg_cap: float = -5.0,
    confidence: float = 1.0,
) -> float:
    """Group-A4 declared-rating bump — ONE formula, parameterised per medium.

    Linear about 5/10: ``(rating - 5) * slope``, clamped to ``[neg_cap, pos_cap]``,
    then scaled by ``confidence`` in ``[0, 1]``. Returns 0.0 when unrated or
    non-positive.

    The DEFAULTS reproduce the original symmetric movie term (slope 2, +10/-5,
    full confidence) — so ``score_movie`` is byte-for-byte unchanged. ``score_show``
    passes a gentler shape because a declared series rating is a stickier, weaker
    signal than revealed episode engagement (A2): lower slope/cap, a softened
    penalty, and a ``confidence`` derived from how much of the series has actually
    been watched (a 10/10 after two episodes is trusted less than after four
    seasons). Kept in the shared space so movie/show A4 can DIFFER without the math
    drifting — only the knobs differ.
    """
    if user_rating is None or user_rating <= 0:
        return 0.0
    raw = min(pos_cap, max(neg_cap, (user_rating - 5.0) * slope))
    return round(raw * max(0.0, min(1.0, confidence)), 2)


def related_graph_affinity(
    related_ids,
    watched_ids,
    *,
    cap: float = 4.0,
) -> float:
    """Collaborative 'related-graph' affinity, shared by both scorers (Group-C3).

    How many of a title's Trakt-RELATED neighbours the household has watched. This
    generalises the Group-C collection/universe terms (C1 collection-completeness,
    C2 universe-siblings) from FORMAL franchises to Trakt's similarity graph — the
    "people like me who watch the neighbours of this title enjoy it" signal that
    works for owned content (unlike Trakt's personalised recommendations, which only
    surface titles you do NOT own).

    ``related_ids``  — the title's related-neighbour ids (TMDb for movies, TVDb for
                       shows), e.g. extracted from the daemon-cached related bucket.
    ``watched_ids``  — the household watched-set in the SAME id space.
    ``cap``          — max contribution (default +4, mirroring C2; configurable).

    Count-based tiers (a related list runs ~20-100 long, so the absolute number of
    watched neighbours is the meaningful axis — mirrors C2's sibling-count style),
    scaled to ``cap``:
        >= 10 watched neighbours -> cap        (household is deep in this cluster)
        >=  5                    -> cap * 0.75
        >=  2                    -> cap * 0.375
        ==  1                    -> cap * 0.25
        0  (or either set empty) -> 0.0
    """
    if not related_ids or not watched_ids:
        return 0.0
    n = len(set(related_ids) & set(watched_ids))
    if n <= 0:
        return 0.0
    if n >= 10:
        frac = 1.0
    elif n >= 5:
        frac = 0.75
    elif n >= 2:
        frac = 0.375
    else:
        frac = 0.25
    return round(min(cap, cap * frac), 2)


def score_to_profile(score: int) -> str:
    """
    Map a 0-100 watchability score to a target quality profile name pattern.

    The returned string is a *pattern* — callers should fuzzy-match it against
    their actual Radarr/Sonarr quality profile names.

    Minimum floor is HD-720p — SD content is absorbed into 720p since older
    movies without HD masters still benefit from the 720p container and metadata.

    Returns
    -------
    str  one of the QUALITY_PROFILE_THRESHOLDS labels.
    """
    for threshold, profile in QUALITY_PROFILE_THRESHOLDS:
        if score >= threshold:
            return profile
    return "SD"


def select_profile_id(
    score: int,
    ranked_profiles: list[dict],
    target_resolution: int | None = None,
) -> int | None:
    """
    Select a quality-profile id for *score* from *ranked_profiles*.

    Shared by ``score_to_radarr_profile_id`` and ``score_to_sonarr_profile_id`` —
    Radarr and Sonarr quality profiles share the same items/quality/resolution
    shape, so the selection is identical: map the score to a profile-name pattern,
    never exceed *target_resolution*, and prefer the highest-resolution match.

    ``ranked_profiles`` should be the list from ``_fetch_ranked_profiles`` (sorted
    ascending by max resolution). Returns None if no suitable profile is found.
    """
    if not ranked_profiles:
        return None

    profile_label = score_to_profile(score)

    def _max_res(p: dict) -> int:
        best = 0
        for item in (p.get("items") or []):
            if not item.get("allowed"):
                continue
            res = (item.get("quality") or {}).get("resolution", 0)
            if isinstance(res, (int, float)):
                best = max(best, int(res))
            for sub in (item.get("items") or []):
                if sub.get("allowed"):
                    sr = (sub.get("quality") or {}).get("resolution", 0)
                    if isinstance(sr, (int, float)):
                        best = max(best, int(sr))
        return best

    # Filter by resolution ceiling if provided
    eligible = [
        p for p in ranked_profiles
        if target_resolution is None or _max_res(p) <= target_resolution
    ]
    if not eligible:
        eligible = ranked_profiles  # fallback: ignore ceiling

    # Match by name pattern (case-insensitive substring)
    label_lower = profile_label.lower()
    matched = [p for p in eligible if label_lower in (p.get("name") or "").lower()]
    if matched:
        # Return highest-ranked matching profile
        return sorted(matched, key=_max_res)[-1]["id"]

    # Fallback: return the highest eligible profile below the score tier
    return sorted(eligible, key=_max_res)[-1]["id"]
