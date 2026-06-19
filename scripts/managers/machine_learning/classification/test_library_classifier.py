"""Tests for the Kids/Family classification rules (2026-06-11 revision).

Pins the behaviour:
  • the "Family requires Animation" gate is GONE — a live-action Family show counts
    as Kids on its own, so curated family shows aren't evicted to Series;
  • but the SOFT Family genre is rating-gated: it routes to Kids only when kid-safe
    rated (≤ TV-PG/PG) or unrated, so adult "family drama" (TV-14/TV-MA) stays out
    of the Kids library;
  • a HARD Children/Kids/Preschool genre always wins — it beats the lifestyle/reality
    veto AND the rating gate (a 'Children, Food' kids-cooking show is still kids).
"""
from __future__ import annotations

from scripts.managers.machine_learning.classification.library_classifier import (
    classify_movie_explained,
    classify_show_explained,
    is_anime_media,
)


def _show(genres, cert="", **kw):
    return classify_show_explained(genres=genres, certification=cert, **kw)[0]


def _movie(genres, cert="", **kw):
    return classify_movie_explained(genres=genres, certification=cert, **kw)[0]


# ── live-action Family is no longer evicted to Series (animation gate dropped) ──
def test_family_unrated_is_kids():
    assert _show(["Drama", "Family"]) == "kids"
    assert _show(["Adventure", "Comedy", "Family"]) == "kids"


def test_family_kid_safe_rating_is_kids():
    assert _show(["Comedy", "Family"], "TV-PG") == "kids"
    assert _show(["Drama", "Family"], "TV-G") == "kids"
    assert _show(["Comedy", "Family"], "PG") == "kids"


# ── the rating gate keeps adult "family drama" OUT of Kids ──────────────────────
def test_family_adult_rated_is_not_kids():
    assert _show(["Drama", "Family", "Fantasy"], "TV-14") == "series"   # His Dark Materials
    assert _show(["Drama", "Family", "Mystery"], "TV-MA") == "series"   # Apples Never Fall
    assert _show(["Comedy", "Family"], "TV-MA") == "series"             # #blackAF
    assert _show(["Comedy", "Family"], "16") == "series"


# ── hard Children/Kids genre beats BOTH the lifestyle veto and the rating gate ──
def test_children_genre_beats_lifestyle_veto():
    assert _show(["Children", "Food"], "TV-Y") == "kids"                # The Tiny Chef Show
    assert _show(["Children", "Drama", "Soap"]) == "kids"               # kids telenovela
    # even adult-rated, an explicit Children tag is honoured
    assert _show(["Children", "Drama"], "TV-14") == "kids"


def test_preschool_beats_anime_and_veto():
    # Preschool + Japanese animation → Kids (preschool beats anime)
    assert _show(["Preschool", "Animation"], original_language="Japanese") == "kids"
    # Preschool + a lifestyle-veto genre → still Kids
    assert _show(["Preschool", "Food"]) == "kids"


# ── soft Family is still blocked by the lifestyle/reality veto ──────────────────
def test_family_with_reality_veto_is_not_kids():
    # Family + Reality → the reality library, not Kids (veto blocks the soft family route)
    assert _show(["Family", "Reality"]) == "reality"
    # Family + Soap (vetoed) → not kids
    assert _show(["Comedy", "Family", "Soap"], "TV-PG") == "series"


# ── anime still beats the (soft) family/kids genres ────────────────────────────
def test_anime_beats_family():
    assert _show(["Anime", "Family"], "TV-PG") == "anime"


# ── a bare 'Anime' genre is vetoed by a KNOWN non-anime language (Avatar: TLA) ──
def test_anime_genre_tag_vetoed_by_known_non_anime_language():
    # TheTVDB tags some Western cartoons under an 'Anime' genre; an English/French original
    # language must veto the bare-genre anime route so they don't leave Kids for the Anime lib.
    assert _show(["Anime", "Family"], original_language="English") == "kids"    # Avatar: TLA
    assert _show(["Anime", "Drama"], original_language="French") == "series"    # no kids signal
    # Real anime is untouched: a known anime language, or an unknown/missing one still trusts.
    assert _show(["Anime"], original_language="Japanese") == "anime"
    assert _show(["Anime", "Family"], "TV-PG") == "anime"                       # unknown lang trusts genre


# ── seriesType=anime + the literal 'anime' genre AGREEING override an English original
#    language (common for dubbed/older anime), but a HARD kids genre still demotes a Western
#    cartoon TheTVDB mistagged with BOTH signals (Craig of the Creek / Transformers / Barbie). ──
def test_seriestype_plus_anime_genre_survive_english_original_language():
    # Genuine Japanese anime whose Sonarr originalLanguage is (wrongly) reported English: the
    # seriesType=anime + 'Anime' genre pair overrides the language → stays in the Anime library.
    assert _show(["Animation", "Anime", "Science Fiction"],
                 series_type="anime", original_language="English") == "anime"   # Space Dandy
    assert _show(["Action", "Adventure", "Anime", "Fantasy"],
                 series_type="anime", original_language="English") == "anime"   # Tales of Phantasia
    # The SOFT 'Family' genre is not a Western tell — a Family-tagged anime keeps its anime route.
    assert _show(["Animation", "Anime", "Family", "Romance"],
                 series_type="anime", original_language="English") == "anime"   # Gakuen Alice


def test_western_cartoon_with_western_dev_house_demoted_despite_seriestype():
    # A WESTERN dev house (Sonarr network / Radarr studio) + English marks a cartoon mistagged
    # 'Anime' + seriesType=anime → NOT anime; routes to Kids on its Children genre (seriesType is
    # corrected elsewhere). Without a Western dev house the pair is trusted, protecting dubbed anime.
    assert _show(["Adventure", "Animation", "Anime", "Children", "Comedy", "Drama", "Family"],
                 series_type="anime", original_language="English",
                 network="Cartoon Network") == "kids"   # Craig of the Creek
    assert _show(["Animation", "Anime", "Children", "Comedy"],
                 series_type="anime", original_language="English",
                 network="Nickelodeon") == "kids"       # Barbie Dream Squad
    # A KNOWN anime network (TV Tokyo) vetoes the demotion → a dubbed Japanese kids anime reported
    # English with a 'Children' tag stays anime; a NULL network falls through to the kids-genre tell
    # and IS demoted (the Barbie Dream Squad case — covered fully in its own test below).
    assert _show(["Animation", "Anime", "Children", "Comedy"],
                 series_type="anime", original_language="English",
                 network="TV Tokyo") == "anime"
    assert _show(["Animation", "Anime", "Children", "Comedy"],
                 series_type="anime", original_language="English") == "kids"   # null network → demoted


def test_western_nonkids_cartoon_demoted_by_dev_house():
    # RWBY / Castlevania / Teen Titans: seriesType=anime + 'Anime' + English + a Western dev house
    # but NO kids genre → demoted to series (the case the kids-genre-only tell missed entirely).
    assert _show(["Animation", "Anime", "Action", "Fantasy"], series_type="anime",
                 original_language="English", network="Rooster Teeth") == "series"   # RWBY
    assert _movie(["Animation", "Anime", "Action"], original_language="English",
                  studio="Rooster Teeth Productions") == "standard"   # movie bucket, not anime
    # Dubbed Japanese anime reported English from a non-Western (Japanese) house → KEPT anime.
    assert _show(["Animation", "Anime", "Science Fiction"], series_type="anime",
                 original_language="English", network="Tokyo MX") == "anime"


def test_dual_use_anime_network_needs_kids_genre_corroboration():
    # Cartoon Network (Toonami) and Disney+ AIR imported anime, so the network alone is not proof
    # of Western origin: a Western-cartoon demotion there requires a HARD kids genre. A genuine
    # Western cartoon carries one (Craig/Transformers → 'Children') and is still demoted; an
    # imported anime that merely aired there does NOT (Blue Submarine No. 6, Star Wars: Visions) and
    # is spared. Unambiguous Western houses (Rooster Teeth, Nickelodeon) keep demoting on English.
    # -- imported anime on a dual-use network, NO kids genre → KEPT anime --
    assert _show(["Action", "Animation", "Anime", "Science Fiction"], series_type="anime",
                 original_language="English", network="Cartoon Network") == "anime"   # Blue Submarine No.6
    assert _show(["Action", "Adventure", "Animation", "Anime", "Fantasy", "Science Fiction"],
                 series_type="anime", original_language="English",
                 network="Disney+") == "anime"                                        # Star Wars: Visions
    # -- genuine Western cartoon on a dual-use network, hard Children genre → still demoted --
    assert _show(["Animation", "Anime", "Children", "Science Fiction"], series_type="anime",
                 original_language="English", network="Cartoon Network") == "kids"    # Transformers: Armada
    # -- unambiguous Western house (NOT dual-use) demotes on English alone, no genre needed --
    assert _show(["Animation", "Anime", "Action", "Fantasy"], series_type="anime",
                 original_language="English", network="Rooster Teeth") == "series"    # RWBY
    # is_anime_media mirrors it: the dual-use guard spares the import, keeps anime parsing.
    assert is_anime_media(genres=["Action", "Animation", "Anime", "Science Fiction"],
                          series_type="anime", original_language="English",
                          studio="Cartoon Network") is True                           # Blue Submarine No.6
    # Disney CHANNEL (not Disney+) is a Western kids network, not an anime importer → demotes.
    assert is_anime_media(genres=["Animation", "Anime", "Comedy"], series_type="anime",
                          original_language="English", studio="Disney Channel") is False


def test_western_cartoon_with_missing_network_demoted_by_hard_kids_genre():
    # The dev-house tell is a UNION with the hard-kids-genre tell, because each catches what the
    # other misses. A real Western cartoon whose Sonarr `network` is missing/None (Barbie Dream
    # Squad — confirmed network=None in the live cache) has NO dev-house signal, so the dev-house
    # tell ALONE would wrongly leave it in /anime; the hard 'Children' genre still demotes it.
    assert _show(["Animation", "Anime", "Children", "Comedy"], series_type="anime",
                 original_language="English", network=None) == "kids"          # Barbie Dream Squad
    assert is_anime_media(genres=["Animation", "Anime", "Children"], series_type="anime",
                          original_language="English", studio=None) is False
    # The SOFT 'Family' genre is NOT a hard tell — with no network it can't demote a real anime.
    assert _show(["Animation", "Anime", "Family", "Romance"], series_type="anime",
                 original_language="English", network=None) == "anime"
    # And the two tells are independent: a Western dev-house with NO kids genre still demotes
    # (RWBY), while a hard kids genre with NO/!Western house still demotes (Barbie) — proven above.


def test_anime_house_veto_protects_dubbed_anime_on_japanese_networks():
    # A KNOWN anime network/distributor vetoes the kids-genre demotion tell, so a dubbed Japanese
    # kids anime reported English with a 'Children' tag stays anime. Covers TheTVDB's country-tag
    # convention ("ABC (JA)" / "CTC (JA)" / "YTV (JP)" = Japanese broadcasters), the 'nhk'/'asahi'
    # tokens (so "NHK Educational TV" / "Kyushu Asahi Broadcasting" match), and donghua platforms.
    for net in ["ABC (JA)", "CTC (JA)", "YTV (JP)", "NHK Educational TV",
                "Kyushu Asahi Broadcasting", "Bilibili", "Niconico", "TV Tokyo"]:
        assert _show(["Animation", "Anime", "Children", "Comedy"], series_type="anime",
                     original_language="English", network=net) == "anime", net
        assert is_anime_media(genres=["Animation", "Anime", "Children"], series_type="anime",
                              original_language="English", studio=net) is True, net
    # The country tag is Japan-specific: "ABC (US)" is NOT an anime house, so a 'Children'-tagged
    # English cartoon there is still demoted; plain Canadian "YTV" stays a Western house (NOT an
    # anime house), so an English cartoon there is demoted out of anime (it then routes to kids on
    # YTV being a kids network — the point here is only that the anime-house veto does NOT apply).
    assert _show(["Animation", "Anime", "Children", "Comedy"], series_type="anime",
                 original_language="English", network="ABC (US)") == "kids"
    assert is_anime_media(genres=["Animation", "Anime", "Action"], series_type="anime",
                          original_language="English", studio="YTV") is False   # Canadian YTV → Western


def test_is_anime_media_seriestype_genre_pair_vs_western_kids():
    # is_anime_media drives the Sonarr seriesType correction in the re-organizer. The pair
    # (seriesType=anime + 'anime' genre) is genuine anime even reported English; a hard kids
    # genre flips it to a Western cartoon; and ONE signal alone is never enough.
    assert is_anime_media(genres=["Animation", "Anime", "Science Fiction"],
                          series_type="anime", original_language="English") is True   # FIXED (was False)
    assert is_anime_media(genres=["Animation", "Anime", "Science Fiction"],
                          series_type="anime", original_language="Japanese") is True
    assert is_anime_media(genres=["Animation", "Anime", "Science Fiction"],
                          series_type="anime", original_language=None) is True
    # Hard 'Children' genre + English: NOT anime media on a Western/dual-use house (Cartoon Network)
    # OR a null house (Barbie Dream Squad — via the kids-genre tell); but a KNOWN anime network
    # (TV Tokyo) vetoes that tell, so a dubbed Japanese kids anime reported English stays anime.
    assert is_anime_media(genres=["Animation", "Anime", "Children"], series_type="anime",
                          original_language="English", studio="Cartoon Network") is False
    assert is_anime_media(genres=["Animation", "Anime", "Children"], series_type="anime",
                          original_language="English", studio=None) is False           # Barbie (null house)
    assert is_anime_media(genres=["Animation", "Anime", "Children"], series_type="anime",
                          original_language="English", studio="TV Tokyo") is True       # dubbed JP kids anime
    # One signal alone is not enough: a bare 'anime' genre + English with no seriesType is demoted,
    # as is a bare seriesType=anime (mistyped Western cartoon) carrying no 'anime' genre.
    assert is_anime_media(genres=["Anime", "Family"], original_language="English") is False
    assert is_anime_media(genres=["Animation", "Comedy"],
                          series_type="anime", original_language="English") is False


# ── shows: CSM age is a kids CEILING ONLY — never routes to Kids by itself ────────
def test_show_csm_age_alone_does_not_route_to_kids():
    # "Never trust Common Sense alone": a low CSM age no longer pulls a show into Kids
    # without a corroborating kids signal. Star Trek: DS9 (adult drama, CSM ~10) → series.
    assert _show(["Drama"], recommended_age=8) == "series"
    assert _show(["Drama", "Science Fiction"], "TV-PG", recommended_age=10) == "series"   # DS9
    # CSM permits but does not promote: a Reality show at CSM 6 stays Reality (no kids signal).
    assert _show(["Reality"], recommended_age=6) == "reality"
    # A corroborating signal is still required AND honoured: a kid-safe cert at a low CSM age → kids.
    assert _show(["Comedy"], "TV-Y7", recommended_age=8) == "kids"


# ── shows: a genuine KIDS NETWORK is a positive kids signal (the Trek franchise split) ──
def test_show_kids_network_routes_to_kids():
    # A kids network routes to Kids even with no kids genre/cert (sparse metadata).
    assert _show(["Drama"], network="Disney Junior") == "kids"
    assert _show(["Adventure"], network="Nickelodeon") == "kids"
    # Star Trek: Prodigy (Nickelodeon kids Trek, CSM ~10) → Kids via the network…
    assert _show(["Science Fiction", "Adventure"], network="Nickelodeon", recommended_age=10) == "kids"
    # …while the adult Treks on general networks (no kids signal) stay in Series.
    assert _show(["Science Fiction", "Drama"], "TV-PG", network="Syndication", recommended_age=10) == "series"
    assert _show(["Science Fiction", "Drama"], "TV-PG", network="Paramount+", recommended_age=10) == "series"


def test_show_kids_network_is_gated():
    # The network route respects the same gates as the cert route.
    assert _show(["Reality"], network="Nickelodeon") == "reality"                     # lifestyle veto wins
    assert _show(["Drama"], network="Cartoon Network", recommended_age=16) == "series"  # CSM over cutoff
    assert _show(["Drama"], "TV-MA", network="Cartoon Network") == "series"           # adult cert (Adult Swim-style)
    assert _show(["Drama"], network="HBO") == "series"                               # not a kids network


def test_show_csm_over_cutoff_blocks_soft_family():
    # Without CSM this kid-safe-rated 'Family' show is Kids; a CSM age over the cutoff
    # (CSM says NOT kids) suppresses the soft-Family kids route → series.
    assert _show(["Drama", "Family"], "TV-PG") == "kids"               # baseline (no CSM)
    assert _show(["Drama", "Family"], "TV-PG", recommended_age=15) == "series"


def test_show_csm_over_cutoff_overrides_hard_genre_and_cert():
    # CSM over the cutoff beats BOTH a hard Children genre and the TV-G cert route.
    assert _show(["Children"], recommended_age=16) == "series"         # hard kids genre suppressed
    assert _show(["Comedy"], "TV-G", recommended_age=16) == "series"   # cert route suppressed


def test_show_csm_over_cutoff_still_reality_and_documentary():
    # CSM>cutoff blocks the KIDS routes but the show must still classify normally elsewhere.
    assert _show(["Reality"], recommended_age=16) == "reality"
    assert _show(["Documentary"], recommended_age=16) == "documentary"


def test_show_preschool_beats_csm():
    # An explicit 'Preschool' GENRE is unambiguous toddler content — it wins even when CSM
    # rates the title older (documented exception: preschool sits ABOVE CSM).
    assert _show(["Preschool"], recommended_age=16) == "kids"


def test_show_anime_beats_csm():
    # Anime precedence is preserved: a kid-rated anime still routes to the Anime library.
    assert _show(["Anime"], recommended_age=8) == "anime"
    assert _show(["Animation"], original_language="Japanese", recommended_age=8) == "anime"


def test_show_no_csm_leaves_genre_cert_flow_unchanged():
    # Regression: with no CSM age the existing genre/cert routing is unchanged.
    assert _show(["Comedy", "Family"], "TV-PG") == "kids"
    assert _show(["Comedy"], "TV-G") == "kids"
    assert _show(["Drama"]) == "series"


# ── movies: CSM age is a kids CEILING ONLY — a kids STUDIO is the only positive ───
def test_movie_csm_age_alone_does_not_route_to_kids():
    # "Never trust Common Sense alone": a low CSM age no longer routes a movie to Kids
    # without a kids studio (genre is not a movie kids route).
    assert _movie(["Drama"], recommended_age=8) == "standard"
    assert _movie(["Family", "Comedy"], "PG", recommended_age=7) == "standard"
    # CSM still DEMOTES: an age over the cutoff blocks even a kids studio.
    assert _movie(["Comedy"], "G", studio="Pixar", recommended_age=15) == "standard"
    # A kids studio at an in-range CSM age → kids (the studio is the positive signal).
    assert _movie(["Comedy"], "G", studio="Pixar", recommended_age=8) == "kids"


# ── movies: GENRE is no longer a kids route (only anime keeps genre/language) ────
def test_movie_genre_is_not_a_kids_route():
    # No CSM age + no kids studio → genre alone never routes a movie to Kids.
    assert _movie(["Family", "Comedy"], "PG") == "standard"
    assert _movie(["Children", "Comedy"]) == "standard"                 # even a hard 'Children' tag
    assert _movie(["Animation", "Family", "Comedy"], "NR") == "standard"
    assert _movie(["Preschool", "Adventure"]) == "standard"


# ── movies: NO bare-cert route — a G/PG certificate ALONE must not route to Kids ──
def test_movie_g_pg_cert_alone_is_not_kids():
    # Rating inflation means classics/franchises carry G/PG but aren't kids films.
    assert _movie(["Action", "Adventure", "Science Fiction", "Thriller"], "PG") == "standard"  # Star Trek II
    assert _movie(["Adventure", "History", "War"], "PG") == "standard"                          # Lawrence of Arabia
    assert _movie(["Drama", "Romance", "War"], "G") == "standard"                               # Gone with the Wind
    assert _movie(["Comedy"], "PG") == "standard"                                               # bare PG comedy


def test_tv_cert_route_unchanged():
    # TV KEEPS its certificate route — a TV-G/TV-Y7 show with no genre signal is still Kids.
    assert _show(["Comedy"], "TV-G") == "kids"
    assert _show(["Adventure"], "TV-Y7") == "kids"


# ── movies: kids/family STUDIO is the only fallback when CSM has no rating ───────
def test_movie_studio_fallback_when_no_csm():
    assert _movie(["Comedy"], "G", studio="Pixar") == "kids"               # kid-safe cert + kids studio
    assert _movie(["Comedy"], studio="Walt Disney Pictures") == "kids"     # unrated + kids studio
    assert _movie(["Comedy"], "PG-13", studio="Pixar") == "standard"       # adult cert disqualifies studio
    assert _movie(["Comedy"], "G", studio="A24") == "standard"             # not a kids studio
    # CSM is authoritative: an older CSM age overrides the kids studio.
    assert _movie(["Comedy"], studio="Pixar", recommended_age=15) == "standard"


# ── anime keeps its genre/language route — now including Chinese (donghua) ───────
def test_movie_anime_includes_chinese():
    assert _movie(["Animation"], original_language="Japanese") == "anime"
    assert _movie(["Animation"], original_language="Korean") == "anime"
    assert _movie(["Animation"], original_language="Chinese") == "anime"    # NEW: donghua
    assert _movie(["Anime"]) == "anime"
    # English animation is NOT anime; with no kids studio it is standard (CSM age alone no
    # longer routes a movie to Kids).
    assert _movie(["Animation"], original_language="English", recommended_age=8) == "standard"
    assert _movie(["Animation"], original_language="English") == "standard"


# ── shows: 'NR'/'Not Rated' still normalises to UNRATED for the soft-Family route ─
def test_show_nr_cert_treated_as_unrated():
    # TV keeps genre routing; a kid-safe Family show rated 'NR' still reaches Kids.
    assert _show(["Comedy", "Family"], "NR") == "kids"
    assert _show(["Drama", "Family"], "Not Rated") == "kids"
