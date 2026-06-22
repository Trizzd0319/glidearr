"""Edge-isolation resilience for the TV-franchise generator: a single failed/rate-limited source
(WDQS especially) must NOT crash the whole regen — the catalog builds from the edges that landed, and
only a total wipe-out refuses to write (so the prior catalog is left intact)."""
from __future__ import annotations

import pytest

import scripts.support.tools.generate_tv_franchises as g


def test_wdqs_failure_degrades_to_wiki_only(monkeypatch):
    # Wikidata (p2512 + p179) rate-limited to exhaustion, but a Wikipedia edge lands.
    def boom():
        raise RuntimeError("429 exhausted (WDQS down)")
    monkeypatch.setattr(g, "fetch_p2512", boom)
    monkeypatch.setattr(g, "fetch_p179", boom)
    monkeypatch.setattr(g, "_franchise_category_members", lambda: {"X": [("A", "Q1"), ("B", "Q2")]})
    monkeypatch.setattr(g, "_resolve_qids_to_tvdb", lambda qids: {"Q1": 11, "Q2": 22})
    monkeypatch.setattr(g, "fetch_wikipedia_categories",
                        lambda m, r: ([(11, 22)], {11: {"title": "A"}, 22: {"title": "B"}}))
    monkeypatch.setattr(g, "fetch_infobox_related", lambda m, r: ([], {}))

    catalog, edges, nodes = g.generate(min_members=2)
    assert catalog and edges == [(11, 22)]                       # built despite WDQS down
    fam = next(iter(catalog.values()))
    assert fam["sources"] == ["wiki-cat"]                        # only the source that survived — no phantom p2512


def test_total_edge_wipeout_refuses_to_write(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("all sources down")
    for name in ("fetch_p2512", "fetch_p179", "_franchise_category_members",
                 "fetch_wikipedia_categories", "fetch_infobox_related"):
        monkeypatch.setattr(g, name, boom)
    with pytest.raises(RuntimeError, match="every franchise edge failed"):
        g.generate(min_members=2)
