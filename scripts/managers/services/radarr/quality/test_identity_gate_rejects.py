"""GLD-RAD-30 — the identity gate must REJECT a release that does not name the movie.

WHY THIS FILE EXISTS. ``_pick_stepdown_release`` runs BEFORE the delete in the
space-pressure step-down: verify a smaller release exists → DELETE the file →
grab by guid. If the gate lets a mismatched release through, the household loses
the file it had and gets something else in its place. That is the 2026-08-15
incident this guard was written for.

The gate had no rejection test. §0.1 #74 relabelled every fixture in
``test_universe_realize_downgrade`` and ``test_exhaustive_downgrade`` from bare
labels (``"hd"``, ``"r1"``, ``"tiny"``) to titles that NAME the movie, because
the gate was correctly refusing them and the tests read as a broken delete path.
Correct fix — but making fixtures satisfy a guard silently removes the coverage
OF that guard, and nothing was left asserting it still says no.

Three independent gates share this loop and all three are exercised here:
Radarr's own mapping verdict (``rejections``), the filename-parse identity check,
and the audio-language gate.
"""
from __future__ import annotations

from scripts.managers.services.radarr.quality.space_pressure import (
    RadarrSpacePressureManager as SP,
)

_MOVIE = "The Godfather Part III"
_YEAR = 1990


def _rel(title, res=1080, size_gb=8.0, **kw):
    d = {
        "title": title,
        "size": int(size_gb * (1024 ** 3)),
        "quality": {"quality": {"resolution": res}},
        "rejections": [],
        "guid": f"guid-{title}",
        "indexerId": 1,
    }
    d.update(kw)
    return d


def _pick(releases, **kw):
    kw.setdefault("movie_title", _MOVIE)
    kw.setdefault("movie_year", _YEAR)
    kw.setdefault("current_res", 2160)
    kw.setdefault("target_res", 1080)
    return SP._pick_stepdown_release(releases, **kw)


# ── the gate says NO ─────────────────────────────────────────────────────────

def test_a_release_naming_a_different_film_is_refused():
    """The whole point. A perfectly-sized, perfectly-graded release for the WRONG
    film must not be picked — the pick precedes the delete."""
    assert _pick([_rel("Goodfellas.1990.1080p.BluRay.x264-GRP")]) is None


def test_a_bare_label_is_refused():
    """The exact shape §0.1 #74 found in every pre-existing fixture."""
    for label in ("hd", "r1", "tiny", "same4k", "e1101"):
        assert _pick([_rel(label)]) is None, label


def test_radarrs_own_unknown_movie_verdict_is_fatal():
    """Quality rejections are expected here (we are deliberately stepping DOWN),
    but a mapping rejection means Radarr could not identify the release at all."""
    for verdict in ("Unknown movie", "Unable to parse release title",
                    "Release title does not match", "Not a match for this movie"):
        assert _pick([_rel(f"{_MOVIE}.1990.1080p.BluRay-GRP",
                           rejections=[verdict])]) is None, verdict


def test_a_quality_rejection_alone_does_not_disqualify():
    """The counterpart: stepping down MEANS accepting a release Radarr would
    reject on quality. Only mapping verdicts are fatal."""
    got = _pick([_rel(f"{_MOVIE}.1990.1080p.BluRay-GRP",
                      rejections=["Quality Bluray-1080p is below cutoff"])])
    assert got is not None


def test_a_torrent_with_zero_seeders_is_refused():
    """It would never complete. Usenet reports no seeders at all, so only a
    present-and-zero value disqualifies — absent must stay eligible."""
    assert _pick([_rel(f"{_MOVIE}.1990.1080p-GRP", seeders=0)]) is None
    assert _pick([_rel(f"{_MOVIE}.1990.1080p-GRP", seeders=None)]) is not None


# ── the gate says YES ────────────────────────────────────────────────────────

def test_a_release_naming_the_movie_is_accepted():
    got = _pick([_rel(f"{_MOVIE}.1990.1080p.BluRay.x264-GRP")])
    assert got is not None and got.get("guid")


def test_an_alt_title_is_accepted():
    """Radarr carries alternate titles precisely because indexers do not agree on
    naming; refusing them would fail SAFE but would also make the step-down inert
    for every film with a regional or re-cut release name.

    THE CONTRACT, verified against the matcher rather than assumed: the alternate
    title must appear IN FULL as a contiguous token run in the release name. My
    first version of this test passed the movie's *full* alternate
    ("The Godfather Coda The Death of Michael Corleone") against a release named
    only "The.Godfather.Coda" and expected a match — the release does not contain
    the whole alternate, so the gate correctly said no. The gate was right and the
    fixture was wrong, which is the same mistake §0.1 #74 documents: an assertion
    written from intent instead of from behaviour.
    """
    rel_name = "The.Godfather.Coda.1990.1080p.BluRay-GRP"
    assert _pick([_rel(rel_name)], alt_titles=("The Godfather Coda",)) is not None
    # and the partial-containment case stays REFUSED, which is the safe direction
    assert _pick([_rel(rel_name)],
                 alt_titles=("The Godfather Coda The Death of Michael Corleone",)) is None


def test_foreign_audio_is_refused():
    """The third gate sharing this loop. A correctly-named release in the wrong
    audio language is still the wrong file for this household."""
    assert _pick([_rel(f"{_MOVIE}.1990.TRUEFRENCH.1080p-GRP")]) is None


def test_no_movie_title_skips_the_identity_check():
    """`movie_title=None` is the legacy caller path and must stay byte-identical —
    the gate is additive, so an old call site cannot start refusing releases it
    used to accept."""
    assert _pick([_rel("anything at all")], movie_title=None) is not None


# ── the gate is not the only refusal ─────────────────────────────────────────

def test_identity_passing_still_leaves_the_size_floor_in_charge():
    """§0.1 #74 found `test_no_smaller_release...` passing for the WRONG REASON —
    refused by identity, not by the 300 MiB floor it exists to test. This pins
    the two apart: a correctly-named release that is absurdly small is still
    refused, and by the floor rather than the gate."""
    tiny = _rel(f"{_MOVIE}.1990.1080p.BluRay-GRP", size_gb=0.05)   # ~51 MiB
    assert _pick([tiny]) is None
    assert _pick([_rel(f"{_MOVIE}.1990.1080p.BluRay-GRP", size_gb=8.0)]) is not None
