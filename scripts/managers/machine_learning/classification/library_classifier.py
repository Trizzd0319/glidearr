"""
library_classifier.py — single source of truth for "which TV library does this
show belong in".
================================================================================
RELOCATED into the brain (ML Step 5a) — already dependency-free (stdlib only), so
it moved here verbatim; ``scripts/support/utilities/library_classifier.py`` is now
a re-export shim (dual-import so standalone scripts run inside ``scripts/`` keep
working). Deleted at MIGRATION.md Step 10.

Shared by the acquisition resolver (add-time routing of new grabs) and the
``router_show`` sweep (reconciling the existing Sonarr library), so both paths
make the *same* decision.

A show maps to exactly one category. Precedence is fixed and intentional:

    preschool(genre) → anime → kids(genre) → kids(network) → reality → documentary → kids(cert) → series

The 'Preschool' GENRE wins outright — genuine toddler content stays in Kids even when it
is also anime. ANIME then beats the soft children/family genres, so mainstream anime (One
Piece/Pokémon, often rated TV-Y7) lands in the Anime library rather than Kids. A title only
reaches Kids on a genuine child-oriented signal — a kids/family GENRE, a kids NETWORK, or a
kid-safe CERTIFICATE. Common Sense Media age is a kids CEILING ONLY: an age over the cutoff
demotes a show OUT of Kids, but a low/absent CSM age never routes it INTO Kids on its own
(operator policy: "never trust Common Sense alone" — Star Trek: DS9 is CSM-rated ~age 10 yet
is an adult drama, so a borderline age must be corroborated). A kids *certificate* alone
(TV-G/…) does NOT override anime/reality/documentary — it applies only when nothing else matched.

Signals, in precedence order:
  • preschool   — the 'Preschool' GENRE only (NOT a cert). TOP precedence: beats anime and
                  the lifestyle veto, so genuine toddler content always reaches Kids.
  • anime       — a source hint (MAL/AniList candidate) OR a genre in ``anime_genres`` OR
                  *anime-language animation* (animated AND ``original_language`` is
                  Japanese, Korean, or Chinese — donghua). A bare Sonarr
                  ``seriesType == "anime"`` is honoured ONLY when nothing contradicts it
                  (no known non-anime language) — Western cartoons get mistyped as anime
                  (e.g. Curious George), so it is not trusted on its own. This same test
                  backs :func:`is_anime_media` for Sonarr ``seriesType``, so a
                  children-genre anime routes to Kids but still keeps anime episode parsing.
  • CSM age     — Common Sense Media ``recommended_age`` (e.g. from MDBList) as a kids
                  CEILING ONLY: OLDER than the cutoff → NOT Kids (suppressing the genre +
                  network + cert Kids routes, but reality/documentary/series still resolve).
                  At/under the cutoff or absent it neither routes to Kids nor blocks it — a
                  child-oriented signal must corroborate ("never trust Common Sense alone").
  • kids(genre) — a genre in ``kids_genres`` (Children/Family/Kids). A hard Children/Kids
                  genre beats the lifestyle veto; the soft 'Family' genre is veto- and
                  rating-gated. Skipped when CSM rates the show over the cutoff.
  • kids(network) — the show airs on a genuine KIDS network (``kids_networks``: Disney
                  Junior/Channel/XD, Nickelodeon, Cartoon Network, PBS Kids, …). A strong
                  made-for-children signal that catches kids shows with sparse genre/cert
                  metadata; gated like the cert route (skipped when vetoed, CSM-over-cutoff,
                  or an adult certificate).
  • reality     — a genre in ``reality_genres``.
  • documentary — a genre in ``documentary_genres``.
  • kids(cert)  — a US certification in ``kids_certs`` (TV-Y/TV-Y7/TV-G/G/PG),
                  applied LAST so a cert never pulls anime/docs into Kids.
  • series      — nothing else matched.

:func:`classify_show_explained` returns ``(category, reason)`` so callers can
audit *why* a show was routed (used by ``router_show --explain``).

The category → root-folder mapping lives in config (``rootFolders``) and is
applied via :func:`root_folder_for`, so the same standard drives both the Sonarr
add payload and on-disk relocation.

This module is intentionally dependency-free (stdlib only) so it can be imported
both as ``scripts.support.utilities.library_classifier`` (in-app) and as
``support.utilities.library_classifier`` (from a standalone script run inside
``scripts/``).
"""
from __future__ import annotations

# Display / grouping order — NOT the routing precedence. The actual precedence is
# encoded in the check order of classify_show_explained (preschool → anime → kids-genre →
# kids-network → reality → documentary → kids-cert → series; CSM age is a kids ceiling only).
CATEGORY_ORDER: tuple[str, ...] = ("anime", "kids", "reality", "documentary", "series")

# Sensible defaults so routing stays correct even when a config list is empty or
# missing. Kept deliberately TIGHT — especially documentary — so scripted
# crime / war / history dramas are NOT swept into the Documentaries library.
DEFAULT_ANIME_GENRES = frozenset({"anime"})
DEFAULT_KIDS_GENRES = frozenset({"children", "family", "kids", "preschool"})
# Matches sd_replace.py / SonarrSeriesQualityManager.KIDS_CERTS — keep in sync.
DEFAULT_KIDS_CERTS = frozenset({"tv-y", "tv-y7", "tv-g", "g", "pg"})
# Kid-safe ratings (≤ TV-PG/PG) that let the SOFT 'Family' genre route to Kids. On
# TVDB 'Family' often means "family DRAMA" (His Dark Materials, Apples Never Fall),
# so a 'Family' show only counts as Kids when it is kid-safe rated OR unrated —
# TV-14/TV-MA/R/16+ 'Family' content routes to its normal library so adult dramas
# never land in the Kids library. A hard Children/Kids/Preschool genre ignores this.
KID_SAFE_FAMILY_CERTS = frozenset({"tv-y", "tv-y7", "tv-g", "tv-pg", "g", "pg"})
DEFAULT_REALITY_GENRES = frozenset({"reality", "reality-tv", "reality tv", "game show", "talk show"})
DEFAULT_DOCUMENTARY_GENRES = frozenset({"documentary", "biography", "nature"})
# Preschool / toddler content beats anime → Kids, but ONLY via the 'Preschool'
# GENRE — NOT a cert. Certs proved unreliable for this: TV-Y/TV-Y7 is the rating
# for most mainstream shōnen anime (One Piece, Pokémon, Yu-Gi-Oh, Digimon), so a
# cert-based preschool guard dragged real anime out of the Anime library into Kids.
# Toddler anime you genuinely want in Kids gets the force-kids tag instead.
DEFAULT_PRESCHOOL_GENRES = frozenset({"preschool"})
# Lifestyle / reality genres that HARD-BLOCK a show from the Kids library even when
# it is also tagged Children/Family/Animation — a Family-tagged reality, cooking, or
# home-reno show is not kids content. (These still route normally via their own
# genres, e.g. a reality show → the reality library.)
DEFAULT_NON_KIDS_GENRES = frozenset(
    {"reality", "game show", "talk show", "food", "home and garden", "soap"}
)
# Genuine KIDS networks/channels — a show airing on one is strong "made for children"
# evidence, used to route kids shows whose genre/cert metadata is sparse. Matched as a
# lower-cased SUBSTRING against the show's single ``network`` string, so "Disney Junior",
# "Disney Channel", and "Disney XD" all match the "disney …" entries. DELIBERATELY excludes
# general networks and late-night adult blocks (Adult Swim is its own network string), and
# the route is additionally cert-gated. Override per-deployment via the ``kidsNetworks`` config.
DEFAULT_KIDS_NETWORKS = frozenset({
    "disney junior", "disney channel", "disney xd", "playhouse disney", "toon disney",
    "nickelodeon", "nick jr", "nickjr", "nicktoons", "teennick",
    "cartoon network", "cartoonito", "boomerang",
    "pbs kids", "pbs kids sprout", "sprout", "universal kids", "nbc kids",
    "cbeebies", "cbbc", "kids' wb", "kids wb", "fox kids", "jetix",
    "the hub", "hub network", "discovery kids", "baby tv", "babytv",
    "treehouse", "ytv", "teletoon", "family jr", "qubo", "kidsclick",
})
# Certificates that BLOCK the kids-network route — a grown-up show mislabeled onto a kids
# feed is not kids. (CSM-over-cutoff already covers the common case; this is the belt-and-
# braces for titles that carry no CSM age.)
_ADULT_TV_CERTS = frozenset({"tv-14", "tv-ma", "r", "nc-17", "ma15+", "18", "16"})
# 'Not Rated' / 'Unrated' is NOT an adult rating — normalise to UNRATED so a kid-safe
# 'Family' title (e.g. Disney animated shorts carry the literal cert "NR") still routes to
# Kids instead of being treated as adult-rated. (Empty/missing cert is already unrated.)
UNRATED_CERTS = frozenset({"", "nr", "not rated", "unrated", "none", "n/a", "notrated"})
# Adult-signal genres that BLOCK the soft 'Family' → Kids MOVIE route when present WITHOUT a
# hard Children/Kids genre — keeps war epics, crime-thrillers and horror that carry a loose
# TMDB 'Family' tag out of the Kids movie library. Movies only; TV routing is unaffected.
DEFAULT_ADULT_VETO_GENRES = frozenset({"war", "crime", "thriller", "horror"})

# Production studios whose MOVIE output is overwhelmingly children's/family content. A film
# from one of these routes to Kids on the studio signal alone (as long as it is not adult-
# rated — see MOVIE_ADULT_CERTS), so live-action family films from these houses are kept even
# without the 'Family' genre. Lower-cased; matched against Radarr's single ``studio`` string.
# Anime houses (Toei/OLM/ufotable/Kyoto/…) are DELIBERATELY excluded — their films route to the
# Anime library via the anime step. Override per-deployment via the ``kids_studios`` argument.
DEFAULT_KIDS_STUDIOS = frozenset({
    "pixar", "pixar animation studios", "pixar canada",
    "walt disney animation studios", "walt disney feature animation",
    "walt disney productions", "walt disney pictures",
    "disney television animation", "disneytoon studios",
    "dreamworks animation", "dreamworks animation skg", "dreamworks animation television",
    "illumination", "illumination entertainment",
    "blue sky studios",
    "sony pictures animation",
    "laika", "laika entertainment",
    "aardman animations", "aardman",
    "nickelodeon movies", "nickelodeon animation studio", "nickelodeon animation studios",
    "warner bros. animation", "warner animation group",
    "cartoon network studios", "cartoon network",
    "paramount animation",
    "skydance animation",
    "20th century animation", "20th century fox animation",
    "green gold animation",
    "wildbrain", "wildbrain studios",
    "spin master", "spin master entertainment",
    "mattel television", "mattel playground productions", "mattel films",
    "gkids", "reel fx", "reel fx animation studios", "cinesite",
    # Disney Channel Original Movie production houses + dedicated kids-franchise shops.
    # Their output is kids/tween; the cert gate keeps any stray PG-13 out. (Disney-named
    # labels are matched by the 'disney' token below, so they aren't repeated here.)
    "salty pictures", "bad angels productions, ltd.", "it's a laugh productions",
    "brownhouse productions", "alan sacks productions", "first street films",
    "rainforest productions", "gwave productions", "princessa productions",
    "de passe entertainment", "just singer entertainment", "key pix productions",
    "keystone family pictures", "whitaker entertainment", "caravan pictures",
    "avnet/kerner productions", "firelily", "emshell producers", "dic entertainment",
    # Clearly-kids franchise houses still dropping from the recall gap.
    "troublemaker studios", "hughes entertainment", "bottom of the ninth productions",
    "lee mendelson film productions", "united feature syndicate", "n21 studios",
})
# Brand substrings SAFE to match ANYWHERE in the studio string (still cert-gated): every
# '…Disney…' label is family (Touchstone / Hollywood Pictures are NOT named Disney), as are
# Pixar / Nickelodeon / Muppet / Henson. Deliberately NOT 'dreamworks' (DreamWorks PICTURES
# makes adult films) or 'warner' (Warner Bros. Pictures) — those stay exact-match only.
KIDS_STUDIO_TOKENS = frozenset({"disney", "pixar", "nickelodeon", "muppet", "henson"})
# Movie certs that DISQUALIFY the studio signal — a PG-13/R film is not a Kids movie even from a
# kids studio (e.g. 'Walt Disney Pictures' also made the PG-13 Pirates films). Unrated/G/PG pass.
MOVIE_ADULT_CERTS = frozenset({"pg-13", "r", "nc-17", "x", "tv-14", "tv-ma"})


def _as_set(values, default) -> set[str]:
    """Lower-cased, stripped set from an iterable; fall back to ``default`` when empty."""
    cleaned = {str(v).strip().lower() for v in (values or []) if str(v).strip()}
    return cleaned or set(default)


_JAPANESE = frozenset({"japanese", "ja", "jp", "jpn"})
_KOREAN = frozenset({"korean", "ko", "kor"})
_CHINESE = frozenset({"chinese", "zh", "zho", "cmn", "mandarin", "cantonese", "yue"})
# Languages whose animation routes to the *anime* library — Japanese anime, Korean
# aeni, and Chinese donghua. Animated shows in one of these go to anime; animated
# shows in a known OTHER language (English/French/…) do not.
_ANIME_LANGUAGES = _JAPANESE | _KOREAN | _CHINESE


def _anime_match(g: set, stype: str, olang: str, anime_g: set, is_anime_hint: bool) -> tuple[bool, str]:
    """
    Genuine-anime detection, returning ``(is_anime, reason)``. This is the truth
    for Sonarr ``seriesType`` and is independent of kids routing — a children-genre
    anime is still "anime media" here even though it lands in the Kids library.
    """
    if is_anime_hint:
        return True, "hint:source"
    # Typed/tagged anime with unknown language is kept (don't demote on missing data); a
    # KNOWN non-anime language (English/French/…) is the demotion signal.
    known_non_anime_lang = bool(olang) and olang not in _ANIME_LANGUAGES
    # A bare 'anime' GENRE is trusted UNLESS the original language is a known non-anime one.
    # TheTVDB tags some Western cartoons (Avatar: The Last Airbender, RWBY, Castlevania, Teen
    # Titans) under an 'Anime' genre; without this guard a literal 'anime' tag pulls an
    # English-language animated show out of Kids into the Anime library and flips its
    # seriesType. Mirrors the seriesType=anime discipline below (unknown language still trusts).
    if (g & anime_g) and not known_non_anime_lang:
        return True, "genre:anime"
    animated = ("animation" in g) or (stype == "anime") or bool(g & anime_g)
    if animated and olang in _JAPANESE:
        return True, "japanese-animation"
    if animated and olang in _KOREAN:
        return True, "korean-animation"
    if animated and olang in _CHINESE:
        return True, "chinese-animation"
    if animated and stype == "anime" and not known_non_anime_lang:
        return True, "seriesType=anime"
    return False, ""


def is_anime_media(
    *,
    genres=None,
    series_type: str | None = None,
    original_language: str | None = None,
    is_anime_hint: bool = False,
    anime_genres=None,
) -> bool:
    """
    True if a show is genuinely anime for *parsing* purposes (Sonarr
    ``seriesType``), independent of which library it routes to. A Japanese/Korean
    kids anime routes to the Kids library but is anime media here, so callers keep
    its seriesType at ``anime`` instead of demoting it to ``standard``.
    """
    g = {str(x).strip().lower() for x in (genres or []) if str(x).strip()}
    return _anime_match(
        g,
        (series_type or "").strip().lower(),
        (original_language or "").strip().lower(),
        _as_set(anime_genres, DEFAULT_ANIME_GENRES),
        is_anime_hint,
    )[0]


def _family_rating_ok(cert: str) -> bool:
    """The soft 'Family' genre routes to Kids only when the cert is kid-safe (≤ TV-PG/PG) or
    UNRATED. 'NR'/'Not Rated'/'Unrated' count as unrated (neutral), NOT adult — so a kid-safe
    Family title rated 'NR' is not evicted from Kids."""
    return (cert in UNRATED_CERTS) or (cert in KID_SAFE_FAMILY_CERTS)


def _kids_by_genre(g, kids_g, vetoed, family_rating_ok, adult_g=frozenset()) -> tuple[bool, str]:
    """
    Kids-by-genre test (2026-06-11 revision).

    HARD kids genres (Children/Kids/Preschool) always win and even beat the
    lifestyle/reality veto and the rating gate — an explicit children's tag is
    unambiguous, so a ``Children, Food`` kids-cooking show or a ``Children, Soap``
    kids telenovela is still kids content. ``Family`` is the SOFT signal: the earlier
    "Family requires Animation" gate is DROPPED so curated live-action family shows
    are no longer evicted to Series, but ``Family`` counts as Kids ONLY when it is
    both (a) not lifestyle/reality-vetoed AND (b) kid-safe rated (``family_rating_ok``:
    ≤ TV-PG/PG or unrated) — so a ``Family, Reality`` cooking show or an adult
    ``Family`` drama (His Dark Materials TV-14, Apples Never Fall TV-MA) is NOT pulled
    into Kids.

    ``adult_g`` (MOVIES only — empty for TV) additionally blocks the SOFT 'Family' route
    when a war/crime/thriller/horror genre is present without a hard kids genre, so a loose
    TMDB 'Family' tag on a war epic or crime-thriller (e.g. a PG ``Family, War`` classic) does
    not pull it into Kids. Returns ``(is_kids, reason)``.
    """
    hard = g & (kids_g - {"family"})
    if hard:                                       # children/kids/preschool — beat veto + rating gate
        return True, f"genre:{sorted(hard)[0]}"
    if (not vetoed and family_rating_ok and "family" in kids_g and "family" in g
            and not (g & adult_g)):
        return True, "genre:family"                # soft: kid-safe-rated, non-adult-genre family only
    return False, ""


def classify_show_explained(
    *,
    genres=None,
    certification: str | None = None,
    series_type: str | None = None,
    original_language: str | None = None,
    network: str | None = None,
    is_anime_hint: bool = False,
    recommended_age: "int | None" = None,
    kids_age_max: int = 11,        # CSM CEILING: an age OVER this demotes a show out of Kids
    anime_genres=None,
    kids_genres=None,
    kids_certs=None,
    kids_networks=None,
    reality_genres=None,
    documentary_genres=None,
    preschool_genres=None,
    non_kids_genres=None,
) -> tuple[str, str]:
    """
    Return ``(category, reason)``. ``category`` is one of :data:`CATEGORY_ORDER`;
    the first matching rule wins, so the order of the checks encodes precedence:

        preschool → anime → kids(genre) → kids(network) → reality → documentary → kids(cert) → series

    ``reason`` is a short tag (e.g. ``"japanese-animation"``, ``"network:nickelodeon"``)
    explaining the match. All genre/cert/network arguments are optional; when omitted (or
    empty) the module defaults are used.

    Key rules:
      • PRESCHOOL (the 'Preschool' GENRE only — not a cert) beats anime AND the
        lifestyle veto, so genuine toddler content stays in Kids; ANIME beats the
        children/family kids genres so mainstream anime (One Piece/Pokémon, often
        rated TV-Y7) → the anime library.
      • COMMON SENSE MEDIA age (``recommended_age``, e.g. from MDBList) is a kids
        CEILING ONLY (operator policy: "never trust Common Sense alone"). An age OVER
        ``kids_age_max`` means CSM says NOT kids and SUPPRESSES every Kids route below
        (genre + network + cert); the show still resolves to reality/documentary/series.
        A low/absent CSM age neither routes to Kids nor blocks it — CSM can demote a show
        OUT of Kids but never promotes one INTO it. (Star Trek: DS9 is CSM-rated ~age 10
        yet is an adult drama — so a borderline age must be corroborated by a real signal.)
      • A HARD kids genre (Children/Kids) routes to Kids and BEATS the lifestyle veto
        — an explicit children's tag is unambiguous (a 'Children, Food' kids cooking
        show is still kids). ``Family`` also routes to Kids on its own (no longer
        animation-gated), but ONLY when not vetoed AND kid-safe rated (≤ TV-PG/PG or
        unrated) — so adult 'family drama' (TV-14/TV-MA) stays out of the Kids library.
      • A kids NETWORK (``network`` ∈ ``kids_networks``: Disney Junior/Channel/XD,
        Nickelodeon, Cartoon Network, PBS Kids, …) routes to Kids — a strong made-for-
        children signal that rescues kids shows with sparse genre/cert metadata (e.g.
        Star Trek: Prodigy on Nickelodeon → Kids, while the adult Treks → series). Gated
        like the cert route: skipped when vetoed, CSM-over-cutoff, or an adult cert.
      • The lifestyle/reality veto (``non_kids_genres``: reality/game show/talk show/
        food/home and garden/soap) blocks the SOFT Kids routes (``Family`` genre, the
        kids network, and the kids certificate) but NOT a hard Children/Kids/Preschool genre.
      • A kids *certificate* (TV-G/G/PG) is applied LAST — and only to NON-anime,
        NON-documentary, non-vetoed shows (so it can't pull anime/docs into Kids).
    """
    g = {str(x).strip().lower() for x in (genres or []) if str(x).strip()}
    cert = (certification or "").strip().lower()
    stype = (series_type or "").strip().lower()
    olang = (original_language or "").strip().lower()
    netw = (network or "").strip().lower()

    anime_g = _as_set(anime_genres, DEFAULT_ANIME_GENRES)
    kids_g = _as_set(kids_genres, DEFAULT_KIDS_GENRES)
    kids_c = _as_set(kids_certs, DEFAULT_KIDS_CERTS)
    kids_nets = _as_set(kids_networks, DEFAULT_KIDS_NETWORKS)
    reality_g = _as_set(reality_genres, DEFAULT_REALITY_GENRES)
    doc_g = _as_set(documentary_genres, DEFAULT_DOCUMENTARY_GENRES)
    pre_g = _as_set(preschool_genres, DEFAULT_PRESCHOOL_GENRES)
    nonkids_g = _as_set(non_kids_genres, DEFAULT_NON_KIDS_GENRES)

    # A lifestyle/reality genre blocks every Kids route below (but not anime/reality).
    vetoed = bool(g & nonkids_g)
    # The soft 'Family' genre only routes to Kids when kid-safe rated (≤ TV-PG/PG) or
    # unrated (incl. 'NR') — keeps adult 'family DRAMA' (TV-14/TV-MA/…) out of the Kids library.
    family_rating_ok = _family_rating_ok(cert)
    # Common Sense Media age (recommended_age) is a kids CEILING ONLY (operator policy:
    # "never trust Common Sense alone"). An age OVER the cutoff means CSM says NOT kids and
    # SUPPRESSES every Kids route below (genre + network + cert); the show stays eligible for
    # reality/documentary/series. A low/absent CSM age does NOT route to Kids on its own — a
    # child-oriented signal (kids genre, kids network, or kid-safe cert) must corroborate.
    csm_blocks_kids = recommended_age is not None and recommended_age > kids_age_max

    # ── 1. Preschool GENRE → Kids (beats anime AND the lifestyle veto) ─────────
    pre_genre_hit = g & pre_g
    if pre_genre_hit:
        return "kids", f"preschool:{sorted(pre_genre_hit)[0]}"

    # ── 2. Anime — beats the children/family kids genres (step 3) ─────────────
    anime_ok, anime_reason = _anime_match(g, stype, olang, anime_g, is_anime_hint)
    if anime_ok:
        return "anime", anime_reason

    # ── 3. Kids by GENRE: Children/Kids beat the veto; Family is veto- + rating-gated ─
    #    Skipped when CSM has rated this show OVER the kids cutoff (csm_blocks_kids).
    kids_ok, kids_reason = _kids_by_genre(g, kids_g, vetoed, family_rating_ok)
    if kids_ok and not csm_blocks_kids:
        return "kids", kids_reason

    # ── 4. Kids by NETWORK — a genuine kids network is strong made-for-children evidence,
    #    rescuing kids shows with sparse genre/cert metadata (Star Trek: Prodigy on
    #    Nickelodeon → Kids). Gated like the cert route: skipped when lifestyle/reality-
    #    vetoed, when CSM rates it over the cutoff, or when the cert is adult. ──────────────
    if (netw and not vetoed and not csm_blocks_kids and cert not in _ADULT_TV_CERTS
            and any(tok in netw for tok in kids_nets)):
        return "kids", f"network:{netw}"

    # ── 5. Reality ────────────────────────────────────────────────────────────
    real_hit = g & reality_g
    if real_hit:
        return "reality", f"genre:{sorted(real_hit)[0]}"

    # ── 6. Documentary ────────────────────────────────────────────────────────
    doc_hit = g & doc_g
    if doc_hit:
        return "documentary", f"genre:{sorted(doc_hit)[0]}"

    # ── 7. Kids by CERTIFICATE (TV-G/G/PG) — last; skipped if vetoed or CSM>cutoff ─
    if not vetoed and not csm_blocks_kids and cert and cert in kids_c:
        return "kids", f"cert:{cert}"

    # ── 8. Default catch-all ──────────────────────────────────────────────────
    return "series", "default"


def classify_show(**kwargs) -> str:
    """Return just the category. See :func:`classify_show_explained`."""
    return classify_show_explained(**kwargs)[0]


def classify_from_config(show: dict, config_get, *, is_anime_hint: bool = False,
                         recommended_age: "int | None" = None) -> str:
    """
    Convenience wrapper: classify a Sonarr/Trakt show dict, reading the genre and
    certification lists from config via ``config_get`` (a ``config.get``-style
    callable taking ``(key, default)``). ``recommended_age`` (the Common Sense age,
    when the caller has it) is forwarded as the primary kids signal.
    """
    ol = show.get("originalLanguage")
    return classify_show(
        genres=show.get("genres"),
        certification=show.get("certification"),
        series_type=show.get("seriesType"),
        original_language=ol.get("name") if isinstance(ol, dict) else ol,
        network=show.get("network"),
        is_anime_hint=is_anime_hint or bool(show.get("is_anime")),
        recommended_age=recommended_age,
        anime_genres=config_get("animeGenres", None),
        kids_genres=config_get("kidsGenres", None),
        kids_certs=config_get("kidsCertifications", None),
        kids_networks=config_get("kidsNetworks", None),
        reality_genres=config_get("realityGenres", None),
        documentary_genres=config_get("documentaryGenres", None),
        preschool_genres=config_get("preschoolGenres", None),
        non_kids_genres=config_get("nonKidsGenres", None),
    )


def root_folder_for(category: str, root_folders: dict, *, fallback: str = "series") -> str | None:
    """Map a category to its configured root folder, falling back to ``series``."""
    rf = root_folders or {}
    return rf.get(category) or rf.get(fallback)


# ── Movie classification ──────────────────────────────────────────────────────
# Movies use a smaller, partly different taxonomy than TV:
#       kids → anime → 4k → standard
# kids/anime are CONTENT axes (genre / cert / language); 4k is a RESOLUTION axis
# (the file is 2160p/UHD). CONTENT WINS: a 4K kids/anime film routes to kids/anime,
# so the 4k library holds only non-kids, non-anime UHD movies. There is no
# reality/documentary movie library, and movies carry no Sonarr seriesType.
MOVIE_CATEGORY_ORDER: tuple[str, ...] = ("anime", "kids", "4k", "standard")

# A file at or above this pixel height counts as UHD / 4K.
UHD_MIN_HEIGHT = 2160


def is_uhd_resolution(*, height=None, resolution=None, quality_name=None) -> bool:
    """
    True when a movie file is 2160p/UHD. Checks (any of): pixel height, a numeric
    ``resolution`` field, or a ``2160``/``4k``/``uhd`` token in the quality name.
    Centralised so the resolver and ``router_movie`` agree on the threshold.
    """
    for v in (height, resolution):
        try:
            if v is not None and int(v) >= UHD_MIN_HEIGHT:
                return True
        except (TypeError, ValueError):
            pass
    q = (quality_name or "").strip().lower()
    return ("2160" in q) or ("4k" in q) or ("uhd" in q)


def classify_movie_explained(
    *,
    genres=None,
    certification: str | None = None,
    original_language: str | None = None,
    studio: str | None = None,
    recommended_age: "int | None" = None,
    kids_age_max: int = 11,        # oldest CSM age that still counts as 'kids' (= oldest
                                   # genuine Pixar/Disney animation; 12+ are live-action outliers)
    is_anime_hint: bool = False,
    is_uhd: bool = False,
    anime_genres=None,
    kids_genres=None,
    kids_certs=None,
    kids_studios=None,
    preschool_genres=None,
    non_kids_genres=None,
    adult_veto_genres=None,
) -> tuple[str, str]:
    """
    Return ``(category, reason)`` for a movie; ``category`` is one of
    :data:`MOVIE_CATEGORY_ORDER`. First matching rule wins, so the check order
    encodes precedence:

      1. anime    — the 'anime' genre, a Japanese/Korean/Chinese animated film, or a
                    source hint. This is the ONLY genre/language route kept.
      2. kids     — a typical kids/family STUDIO (``kids_studios``, cert-gated by
                    :data:`MOVIE_ADULT_CERTS`) — the ONLY positive kids signal for movies.
                    Common Sense Media age is a kids CEILING ONLY (operator policy: "never
                    trust Common Sense alone"): an age OVER ``kids_age_max`` blocks this
                    studio route, but a low/absent CSM age never routes a movie to Kids by
                    itself — a kids studio must identify it.
      3. 4k       — the file is 2160p/UHD (``is_uhd``) and it is neither kids nor anime.
      4. standard — everything else (the default movie library).

    GENRE is deliberately NOT a kids route for movies: TMDB 'Family'/'Children'/etc. tags
    are spread across far too much general/foreign/adult cinema (Bollywood dramas, classics,
    sports dramas) and flooded the Kids library. A kids STUDIO is the only positive signal;
    CSM age only DEMOTES a studio match that CSM rates too old. ``kids_genres`` / ``kids_certs``
    / ``preschool_genres`` / ``non_kids_genres`` / ``adult_veto_genres`` are accepted for
    signature compatibility but UNUSED.
    """
    g = {str(x).strip().lower() for x in (genres or []) if str(x).strip()}
    cert = (certification or "").strip().lower()
    olang = (original_language or "").strip().lower()
    studio_l = (studio or "").strip().lower()

    anime_g = _as_set(anime_genres, DEFAULT_ANIME_GENRES)
    kids_s = _as_set(kids_studios, DEFAULT_KIDS_STUDIOS)

    # 1. Anime — the 'anime' genre or Japanese/Korean/Chinese animation (the ONLY
    #    genre/language route kept for movies). No seriesType for movies → "".
    anime_ok, anime_reason = _anime_match(g, "", olang, anime_g, is_anime_hint)
    if anime_ok:
        return "anime", anime_reason

    # 2. Common Sense Media recommended age is a kids CEILING ONLY (operator policy: "never
    #    trust Common Sense alone"). An age OVER the cutoff says NOT kids and blocks the studio
    #    route below; CSM never routes a movie to Kids by itself.
    csm_blocks_kids = recommended_age is not None and recommended_age > kids_age_max

    # 3. Kids/family STUDIO (cert-gated by MOVIE_ADULT_CERTS) — the ONLY positive kids signal
    #    for movies (GENRE is deliberately NOT a movie kids route: TMDB Family tags flood the
    #    Kids library). Suppressed when CSM rates the film over the cutoff.
    if not csm_blocks_kids and studio_l and cert not in MOVIE_ADULT_CERTS and (
        studio_l in kids_s or any(tok in studio_l for tok in KIDS_STUDIO_TOKENS)
    ):
        return "kids", f"studio:{studio_l}"

    # 4. Resolution split for everything else.
    if is_uhd:
        return "4k", "resolution:2160p"

    # 5. Default catch-all.
    return "standard", "default"


def classify_movie(**kwargs) -> str:
    """Return just the category. See :func:`classify_movie_explained`."""
    return classify_movie_explained(**kwargs)[0]


def movie_root_folder_for(category: str, movie_root_folders: dict, *, fallback: str = "standard") -> str | None:
    """Map a movie category to its configured root folder, falling back to ``standard``."""
    rf = movie_root_folders or {}
    return rf.get(category) or rf.get(fallback)
