"""Tests for playlists/grouping — connected-components grouping + coverage."""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.grouping import coverage_stats, group_items
from scripts.managers.machine_learning.playlists.models import (
    FRANCHISE,
    SERIES,
    STANDALONE,
    UNIVERSE,
    PlaylistInput,
)


def mk(rk, **kw):
    return PlaylistInput(rating_key=rk, medium=kw.pop("medium", "movie"), **kw)


def _group_of(groups, rk):
    for g in groups:
        if any(m.rating_key == rk for m in g.members):
            return g
    raise AssertionError(f"{rk} in no group")


# ── the red-team CRITICAL: partial multi-universe overlap must stay contiguous ──
def test_overlapping_universes_form_one_contiguous_group():
    # A∈{mcu,spiderman}, B∈{spiderman}, C∈{mcu} — alphabetical "first label" would
    # scatter B(spiderman) from C(mcu); connected-components keeps all three together.
    items = [mk("A", universes=("mcu", "spiderman")),
             mk("B", universes=("spiderman",)),
             mk("C", universes=("mcu",)),
             mk("D", universes=("dc",))]
    groups = group_items(items)
    gA = _group_of(groups, "A")
    assert {m.rating_key for m in gA.members} == {"A", "B", "C"}
    assert gA.kind == UNIVERSE
    assert {m.rating_key for m in _group_of(groups, "D").members} == {"D"}   # dc separate


def test_transitive_merge_via_shared_label():
    # A{mcu} - B{mcu,xmen} - C{xmen}: transitively one component.
    items = [mk("A", universes=("mcu",)), mk("B", universes=("mcu", "xmen")),
             mk("C", universes=("xmen",))]
    assert len(group_items(items)) == 1


def test_universe_label_is_case_insensitive():
    items = [mk("A", universes=("MCU",)), mk("B", universes=("mcu",))]
    assert len(group_items(items)) == 1


def test_franchise_and_series_grouping():
    items = [mk("m1", franchise="Toy Story"), mk("m2", franchise="Toy Story"),
             mk("e1", medium="episode", series_id=7, season=1, episode=1),
             mk("e2", medium="episode", series_id=7, season=1, episode=2),
             mk("solo")]
    groups = group_items(items)
    assert _group_of(groups, "m1").kind == FRANCHISE
    assert {m.rating_key for m in _group_of(groups, "e1").members} == {"e1", "e2"}
    assert _group_of(groups, "e1").kind == SERIES
    assert _group_of(groups, "solo").kind == STANDALONE


def test_universe_beats_franchise_in_labeling():
    # a movie carrying both a universe and a franchise → broadest (universe) names it
    items = [mk("A", universes=("mcu",), franchise="avengers"),
             mk("B", universes=("mcu",))]
    assert _group_of(group_items(items), "A").kind == UNIVERSE


def test_placeholder_universe_label_does_not_fuse():
    # The literal "universe" leaks from enrichment (a bare keep-universe tag with no specific
    # franchise) — it must NEVER bind unrelated movies into one mega-group. Real labels still fuse.
    items = [mk("A", universes=("universe",)), mk("B", universes=("universe",)),
             mk("C", universes=("mcu",)), mk("D", universes=("mcu",))]
    groups = group_items(items)
    assert {m.rating_key for m in _group_of(groups, "A").members} == {"A"}      # not fused
    assert _group_of(groups, "A").kind == STANDALONE
    assert {m.rating_key for m in _group_of(groups, "C").members} == {"C", "D"}  # real mcu still fuses


def test_placeholder_label_never_names_a_real_group():
    # A franchise-bound pair where one member also carries the junk "universe" label: the
    # group must be NAMED by the real franchise, never the placeholder.
    items = [mk("A", franchise="Toy Story", universes=("universe",)),
             mk("B", franchise="Toy Story")]
    g = _group_of(group_items(items), "A")
    assert g.kind == FRANCHISE and g.key == "toy story"


def test_coverage_stats_degradation_signal():
    items = [mk("A", universes=("mcu",)), mk("B", universes=("mcu",)),
             mk("e1", medium="episode", series_id=1, season=1, episode=1),
             mk("solo1"), mk("solo2")]
    cov = coverage_stats(group_items(items))
    assert cov["items"] == 5 and cov[UNIVERSE] == 2 and cov[STANDALONE] == 2
    assert cov["grouped_pct"] == 60.0     # 3 of 5 in a real (non-singleton) group


def test_empty_input():
    assert group_items([]) == [] and coverage_stats([]) == {
        "items": 0, "groups": 0, UNIVERSE: 0, FRANCHISE: 0, SERIES: 0,
        STANDALONE: 0, "grouped_pct": 0.0}
