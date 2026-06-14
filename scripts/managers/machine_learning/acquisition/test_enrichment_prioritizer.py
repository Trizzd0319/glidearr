"""Tests for acquisition.enrichment_prioritizer — the pure selection cores of the
Trakt enrich_movies run (ML Step 8). The service keeps the I/O (cache freshness
reads, the global_cache cursor get/set, the live fetch, logging); these cover the
extracted decision: priority membership, the round-robin cursor window, and the
per-row enrich/defer/skip precedence.
"""
from __future__ import annotations

from scripts.managers.machine_learning.acquisition.enrichment_prioritizer import (
    chunk_pool,
    chunk_window,
    enrich_action,
    priority_set,
    relevance_rank,
    relevance_window,
)


# ── priority_set ────────────────────────────────────────────────────────────────
def test_priority_set_by_id_and_title():
    movies = [
        {"tmdbId": 1, "title": "Alpha"},                 # by id
        {"tmdbId": 2, "title": "  BeTa  "},              # by normalised title
        {"tmdbId": 3, "title": "Gamma"},                 # neither -> excluded
        {"title": "no id"},                              # no tmdbId -> excluded
    ]
    got = priority_set(movies, watched_ids={1}, watched_titles_norm={"beta"})
    assert got == {1, 2}


def test_priority_set_empty_signals():
    movies = [{"tmdbId": 1, "title": "Alpha"}]
    assert priority_set(movies, watched_ids=set(), watched_titles_norm=set()) == set()


# ── chunk_pool ──────────────────────────────────────────────────────────────────
def test_chunk_pool_sorts_excludes_and_drops_no_id():
    candidates = [
        {"tmdbId": 30}, {"tmdbId": 10}, {"tmdbId": 20},
        {"tmdbId": 40},        # in exclude
        {"title": "no id"},    # dropped
    ]
    assert chunk_pool(candidates, exclude_ids={40}) == [10, 20, 30]


# ── chunk_window ────────────────────────────────────────────────────────────────
def test_chunk_window_fresh_start():
    w = chunk_window([10, 20, 30], last_id=-1, size=2)
    assert (w.start, w.end, w.ids) == (0, 2, {10, 20})
    assert w.cursor == {"last_tmdb_id": 20, "position": 2, "chunk_size": 2, "total": 3}


def test_chunk_window_resumes_past_last_id():
    w = chunk_window([10, 20, 30, 40], last_id=20, size=2)
    assert (w.start, w.end, w.ids) == (2, 4, {30, 40})
    assert w.cursor["last_tmdb_id"] == 40


def test_chunk_window_wraps_at_end():
    # last_id at/after the max -> bisect lands past the end -> wrap to 0.
    w = chunk_window([10, 20, 30], last_id=30, size=2)
    assert (w.start, w.end, w.ids) == (0, 2, {10, 20})
    assert w.cursor["last_tmdb_id"] == 20


def test_chunk_window_size_exceeds_pool():
    w = chunk_window([10, 20], last_id=-1, size=5)
    assert (w.start, w.end, w.ids) == (0, 2, {10, 20})
    assert w.cursor == {"last_tmdb_id": 20, "position": 2, "chunk_size": 5, "total": 2}


def test_chunk_window_empty_pool():
    w = chunk_window([], last_id=-1, size=2)
    assert (w.start, w.end, w.ids, w.cursor) == (0, 0, set(), None)


# ── enrich_action precedence ────────────────────────────────────────────────────
def _act(has_file=True, is_priority=False, already_cached=False, selected_for_fetch=False, has_file_only=False):
    return enrich_action(has_file=has_file, is_priority=is_priority,
                         already_cached=already_cached, selected_for_fetch=selected_for_fetch,
                         has_file_only=has_file_only)


def test_enrich_action_skip_no_file_only_when_unowned_uncached_nonpriority():
    assert _act(has_file=False, has_file_only=True) == "skip_no_file"
    # priority / cached / selected each defeat the skip:
    assert _act(has_file=False, has_file_only=True, is_priority=True) == "defer"   # priority escapes skip but isn't auto-enriched anymore
    assert _act(has_file=False, has_file_only=True, already_cached=True) == "enrich"
    assert _act(has_file=True,  has_file_only=True, selected_for_fetch=True) == "enrich"
    # has_file_only owned but nothing selects it -> defer (not skip)
    assert _act(has_file=True, has_file_only=True) == "defer"


def test_enrich_action_enrich_paths():
    # cached attaches for free; a fetch-selected row enriches live.
    assert _act(already_cached=True) == "enrich"
    assert _act(selected_for_fetch=True) == "enrich"


def test_enrich_action_priority_no_longer_forces_enrich():
    # The watched-tier cap: an uncached, NOT-fetch-selected priority row defers
    # (it used to enrich unconditionally). Only the cap/cursor decides via
    # selected_for_fetch; cached priority still attaches via already_cached.
    assert _act(is_priority=True, already_cached=False, selected_for_fetch=False) == "defer"
    assert _act(is_priority=True, already_cached=True) == "enrich"
    assert _act(is_priority=True, selected_for_fetch=True) == "enrich"


def test_enrich_action_defer():
    # eligible owned row outside the chunk -> defer
    assert _act(has_file=True) == "defer"
    # unowned row in a non-has_file_only run, nothing selects it -> defer, NOT skip
    assert _act(has_file=False, has_file_only=False) == "defer"


# ── relevance_rank ──────────────────────────────────────────────────────────────
def test_relevance_rank_popularity_then_critic_then_id():
    rows = [
        (10, 5.0, 7.0),
        (11, 9.0, 1.0),   # highest popularity -> first
        (12, 5.0, 8.0),   # ties 10 on popularity, higher critic -> before 10
        (13, 5.0, 8.0),   # ties 12 fully -> id asc -> after 12
    ]
    assert relevance_rank(rows) == [11, 12, 13, 10]


def test_relevance_rank_missing_values_sort_last():
    rows = [
        (10, None, 9.0),   # no popularity -> last despite high critic
        (11, 1.0, None),   # low popularity but present -> before 10
        (12, 8.0, 2.0),    # highest popularity -> first
    ]
    assert relevance_rank(rows) == [12, 11, 10]


# ── relevance_window (done-set cursor) ──────────────────────────────────────────
def test_relevance_window_fresh_cycle_takes_top_size():
    win, done = relevance_window([1, 2, 3, 4], done_ids=set(), size=2)
    assert win == {1, 2} and done == {1, 2}


def test_relevance_window_resumes_then_resets():
    win, done = relevance_window([1, 2, 3, 4], done_ids={1, 2}, size=2)
    assert win == {3, 4} and done == {1, 2, 3, 4}
    # cycle complete -> next call resets and refills from the top THIS run
    win2, done2 = relevance_window([1, 2, 3, 4], done_ids={1, 2, 3, 4}, size=2)
    assert win2 == {1, 2} and done2 == {1, 2}


def test_relevance_window_partial_tail():
    # only one id left this cycle -> window is just that id (not padded)
    win, done = relevance_window([1, 2, 3], done_ids={1, 2}, size=2)
    assert win == {3} and done == {1, 2, 3}


def test_relevance_window_prunes_stale_done_ids():
    # a done id no longer in the pool must not keep the cycle from completing
    # nor persist into the new done-set.
    win, done = relevance_window([1, 2], done_ids={1, 99}, size=5)
    assert win == {2} and done == {1, 2}        # 99 dropped
