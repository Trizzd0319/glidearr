"""Parity + behaviour tests for space.delete_planner.build_movie_delete_candidates.

Guards the ML Step-7c extraction of the radarr ``run_deletions`` decision core:
the brain candidate-build must be byte-identical to the pre-extraction inline
logic (closure + loop + sort) across every guard / tier / critic-source branch,
and the apply loop in the service relies on the exact tuple shape + ordering.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from scripts.managers.machine_learning.space.delete_planner import (
    bare_universe_protected,
    build_movie_delete_candidates,
)
from scripts.managers.machine_learning.space.downgrade_planner import UNIVERSE_PROTECT_MIN

NOW = datetime(2026, 6, 8, tzinfo=timezone.utc)
COLLECTION_WINDOW_DAYS = 30
NO_DELETE_CUTOFF = NOW - timedelta(days=COLLECTION_WINDOW_DAYS)


def _old_candidates(df, score_map, marked, *, franchise_file_ids, include_unwatched, ceiling):
    """Verbatim snapshot of the pre-extraction inline logic (the parity oracle)."""
    def _critic_avg(idx):
        vals = []
        for col, scale in (("imdb_rating", 1.0), ("tmdb_rating", 1.0),
                           ("trakt_rating", 0.1), ("rotten_tomatoes_score", 0.1),
                           ("metacritic_score", 0.1)):
            if col in df.columns:
                v = df.at[idx, col]
                if pd.notna(v):
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    if fv > 0:
                        vals.append(fv * scale)
        return (sum(vals) / len(vals)) if vals else None

    candidates = []
    for idx in df.index:
        fid = df.at[idx, "movie_file_id"]
        if pd.isna(fid):
            continue
        keep_policy = df.at[idx, "keep_policy"] if "keep_policy" in df.columns else None
        is_fe = bool(df.at[idx, "is_franchise_entry"]) if "is_franchise_entry" in df.columns else False
        if is_fe or keep_policy in ("keep_forever", "keep_movie", "keep_universe"):
            continue
        if fid in franchise_file_ids:
            continue
        lw = df.at[idx, "last_watched_at"] if "last_watched_at" in df.columns else None
        if lw:
            try:
                if pd.to_datetime(lw, utc=True) >= NO_DELETE_CUTOFF:
                    continue
            except Exception:
                pass
        score = int(score_map.get(idx, 5))
        size = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0
        critic = _critic_avg(idx)
        if bool(marked.loc[idx]):
            tier = 0
        else:
            if not include_unwatched or score >= ceiling:
                continue
            da = df.at[idx, "date_added"] if "date_added" in df.columns else None
            if da:
                try:
                    if pd.to_datetime(da, utc=True) >= NO_DELETE_CUTOFF:
                        continue
                except Exception:
                    pass
            tier = 1
        candidates.append((tier, score, critic, -size, idx, int(fid), size))
    candidates.sort(key=lambda c: (c[0], c[1], c[2] if c[2] is not None else 5.0, c[3]))
    return candidates


def _build_df():
    old = (NOW - timedelta(days=400)).isoformat()
    recent = (NOW - timedelta(days=5)).isoformat()
    rows = [
        dict(movie_file_id=np.nan, title="nofile"),                                    # 0 no file -> skip
        dict(movie_file_id=1, keep_policy="keep_forever", marked_for_deletion=True),   # 1 keep -> skip
        dict(movie_file_id=2, keep_policy="keep_movie", marked_for_deletion=True),     # 2 keep -> skip
        dict(movie_file_id=3, keep_policy="keep_universe", marked_for_deletion=True),  # 3 keep -> skip
        dict(movie_file_id=4, is_franchise_entry=True, marked_for_deletion=True),      # 4 fe -> skip
        dict(movie_file_id=999, marked_for_deletion=True),                             # 5 franchise file id -> skip
        dict(movie_file_id=6, last_watched_at=recent, marked_for_deletion=True),       # 6 recent watch -> skip
        dict(movie_file_id=7, last_watched_at=old, marked_for_deletion=True,           # 7 tier 0, full critic
             size_bytes=10 * 1024**3, imdb_rating=7.0, tmdb_rating=8.0,
             trakt_rating=80, rotten_tomatoes_score=90, metacritic_score=70),
        dict(movie_file_id=8, marked_for_deletion=True, size_bytes=5 * 1024**3),       # 8 tier 0, critic None
        dict(movie_file_id=9, marked_for_deletion=False, date_added=old,               # 9 tier 1
             size_bytes=3 * 1024**3, imdb_rating=4.5),
        dict(movie_file_id=10, marked_for_deletion=False, date_added=old,              # 10 score >= ceiling -> skip
             size_bytes=3 * 1024**3),
        dict(movie_file_id=11, marked_for_deletion=False, date_added=recent,           # 11 freshly added -> skip
             size_bytes=3 * 1024**3),
        dict(movie_file_id=12, marked_for_deletion=True, size_bytes=8 * 1024**3,       # 12 partial/NaN/0 critic
             imdb_rating=np.nan, tmdb_rating=0.0, trakt_rating=60),
        dict(movie_file_id=13, last_watched_at=old, marked_for_deletion=True,          # 13 tie on size w/ 7
             size_bytes=10 * 1024**3, imdb_rating=5.0),
        dict(movie_file_id=14, marked_for_deletion=False, date_added=old,              # 14 tier 1, large file
             size_bytes=20 * 1024**3),
    ]
    df = pd.DataFrame(rows)
    # Sparse dicts leave is_franchise_entry as NaN, and bool(NaN) is True — which
    # would skip every row and make the test vacuous. The real frame carries a
    # proper bool column.
    df["is_franchise_entry"] = df["is_franchise_entry"].fillna(False).astype(bool)
    return df


SCORE_MAP = {7: 2, 8: 2, 9: 3, 10: 20, 11: 3, 12: 1, 13: 2, 14: 4}
FRANCHISE_FILE_IDS = {999}


def _marked(df):
    return df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)


def test_byte_identical_to_inline_oracle():
    df = _build_df()
    marked = _marked(df)
    for include_unwatched in (True, False):
        for ceiling in (20, 5):
            old = _old_candidates(df, SCORE_MAP, marked,
                                  franchise_file_ids=FRANCHISE_FILE_IDS,
                                  include_unwatched=include_unwatched, ceiling=ceiling)
            new = build_movie_delete_candidates(
                df, SCORE_MAP, marked,
                franchise_file_ids=FRANCHISE_FILE_IDS,
                no_delete_cutoff=NO_DELETE_CUTOFF,
                include_unwatched=include_unwatched, ceiling=ceiling)
            assert old == new, (include_unwatched, ceiling, old, new)


def test_guards_drop_protected_and_recent_rows():
    df = _build_df()
    out = build_movie_delete_candidates(
        df, SCORE_MAP, _marked(df), franchise_file_ids=FRANCHISE_FILE_IDS,
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20)
    fids = {c[5] for c in out}
    # keep-tagged (1,2,3), franchise entry (4), franchise file (999), recent (6),
    # no-file (NaN), score>=ceiling (10), freshly-added (11) all excluded.
    assert fids == {7, 8, 9, 12, 13, 14}, fids


# ── bare-universe ageing (default-off delete-pass guard) ──────────────────────────
def test_bare_universe_protected_off_is_byte_identical():
    da = (NOW - timedelta(days=1)).isoformat()
    # age_days None -> never protects, any policy/date (byte-identical)
    assert bare_universe_protected("universe", da, NOW, age_days=None) is False
    # non-bare-universe policies are never protected by this guard, even when enabled
    for kp in ("keep_universe", "keep_movie", None, "keep_forever"):
        assert bare_universe_protected(kp, da, NOW, age_days=30) is False


def test_bare_universe_protected_dwell_gate():
    young = (NOW - timedelta(days=5)).isoformat()
    old = (NOW - timedelta(days=40)).isoformat()
    assert bare_universe_protected("universe", young, NOW, age_days=30) is True   # still ageing
    assert bare_universe_protected("universe", old, NOW, age_days=30) is False    # aged → eligible
    # missing / unparseable date_added -> protected (not proven old enough)
    assert bare_universe_protected("universe", None, NOW, age_days=30) is True
    assert bare_universe_protected("universe", "garbage", NOW, age_days=30) is True


def test_build_excludes_still_ageing_bare_universe():
    # Bare-universe, unwatched low score, added 40d ago — past the 30d freshly-added guard
    # so it's normally eligible, but the 90d universe dwell protects it until it's older.
    df = pd.DataFrame([{
        "movie_file_id": 501, "keep_policy": "universe", "is_franchise_entry": False,
        "last_watched_at": None, "date_added": (NOW - timedelta(days=40)).isoformat(),
        "size_bytes": 5 * 1024**3, "marked_for_deletion": False,
    }])
    sm = {df.index[0]: 1}
    common = dict(franchise_file_ids=frozenset(), no_delete_cutoff=NO_DELETE_CUTOFF,
                  include_unwatched=True, ceiling=20)
    # off → eligible (tier 1)
    assert {c[5] for c in build_movie_delete_candidates(df, sm, _marked(df), **common)} == {501}
    # on (90d dwell), only 40d on disk → still ageing → excluded
    assert build_movie_delete_candidates(
        df, sm, _marked(df), universe_age_days=90, now=NOW, **common) == []
    # on, but aged past the 90d dwell → eligible again
    df.at[df.index[0], "date_added"] = (NOW - timedelta(days=120)).isoformat()
    assert {c[5] for c in build_movie_delete_candidates(
        df, sm, _marked(df), universe_age_days=90, now=NOW, **common)} == {501}


# ── borrowed franchise/universe credit (hot saga resists deletion; stale one drops) ──
def _credit_df(credit):
    old = (NOW - timedelta(days=400)).isoformat()
    df = pd.DataFrame([{
        "movie_file_id": 700, "keep_policy": None, "is_franchise_entry": False,
        "last_watched_at": old, "marked_for_deletion": True,
        "size_bytes": 10 * 1024**3, "universe_credit": credit,
    }])
    df["is_franchise_entry"] = df["is_franchise_entry"].astype(bool)
    return df


def test_hot_universe_credit_protected_from_deletion():
    # A marked, low-score, old-watched movie that WOULD be a tier-0 delete candidate is held because
    # it carries borrowed credit >= UNIVERSE_PROTECT_MIN (an untagged hot-saga member) — deletion must
    # not be more aggressive than the downgrade guard that already protects it.
    df = _credit_df(2.0)
    st = {}
    out = build_movie_delete_candidates(
        df, {df.index[0]: 1}, _marked(df), franchise_file_ids=frozenset(),
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
    assert out == []
    assert st["skipped_universe"] == 1


def test_stale_universe_credit_is_deletable():
    # The same movie with a DECAYED credit (< UNIVERSE_PROTECT_MIN) is no longer protected — the
    # recency bias makes a stale saga member deletable again. No stats bump.
    df = _credit_df(0.4)
    st = {}
    out = build_movie_delete_candidates(
        df, {df.index[0]: 1}, _marked(df), franchise_file_ids=frozenset(),
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
    assert {c[5] for c in out} == {700}
    assert st.get("skipped_universe", 0) == 0


def test_universe_credit_guard_byte_identical_when_column_absent():
    # No universe_credit column (the common case until refresh_scores' credit pass runs) -> the guard
    # never fires and the queue is identical with or without a stats dict passed.
    df = _build_df()
    base = build_movie_delete_candidates(
        df, SCORE_MAP, _marked(df), franchise_file_ids=FRANCHISE_FILE_IDS,
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20)
    st = {}
    withstats = build_movie_delete_candidates(
        df, SCORE_MAP, _marked(df), franchise_file_ids=FRANCHISE_FILE_IDS,
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
    assert base == withstats
    assert st.get("skipped_universe", 0) == 0


def test_universe_credit_at_floor_is_protected():
    # Inclusive boundary: credit EXACTLY at UNIVERSE_PROTECT_MIN is held (the guard is `>=`).
    # An undecayed single watch yields credit ~1.0, so this is a real production value, not a
    # degenerate edge — a `>` regression would silently make at-floor saga members deletable.
    df = _credit_df(UNIVERSE_PROTECT_MIN)
    st = {}
    out = build_movie_delete_candidates(
        df, {df.index[0]: 1}, _marked(df), franchise_file_ids=frozenset(),
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
    assert out == []
    assert st["skipped_universe"] == 1


def test_nan_or_none_universe_credit_is_deletable():
    # Column present but the cell is NaN/None (partial population) -> unprotected (deletable),
    # no stat bump. Exercises the `uc is not None and pd.notna(uc)` short-circuit.
    for credit in (np.nan, None):
        df = _credit_df(credit)
        st = {}
        out = build_movie_delete_candidates(
            df, {df.index[0]: 1}, _marked(df), franchise_file_ids=frozenset(),
            no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
        assert {c[5] for c in out} == {700}, credit
        assert st.get("skipped_universe", 0) == 0, credit


def test_malformed_universe_credit_is_deletable():
    # A non-numeric credit can't coerce -> the except branch passes (does NOT protect), so the row
    # stays deletable with no stat bump. A bad value must never shield a title from deletion.
    df = _credit_df("garbage")
    st = {}
    out = build_movie_delete_candidates(
        df, {df.index[0]: 1}, _marked(df), franchise_file_ids=frozenset(),
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
    assert {c[5] for c in out} == {700}
    assert st.get("skipped_universe", 0) == 0


def test_keep_tagged_high_credit_not_counted_universe():
    # A keep-tagged row with high credit is dropped by the KEEP guard (which runs first), so it must
    # be excluded WITHOUT a skipped_universe bump — the stat counts only UNTAGGED hot-saga members.
    df = _credit_df(2.0)
    df.at[df.index[0], "keep_policy"] = "keep_movie"
    st = {}
    out = build_movie_delete_candidates(
        df, {df.index[0]: 1}, _marked(df), franchise_file_ids=frozenset(),
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
    assert out == []
    assert st.get("skipped_universe", 0) == 0


def test_multiple_protected_rows_accumulate():
    # skipped_universe is INCREMENTED per protected row (not set): two hot rows -> 2, while the
    # decayed row is the only one left deletable.
    old = (NOW - timedelta(days=400)).isoformat()
    df = pd.DataFrame([
        {"movie_file_id": 701, "keep_policy": None, "is_franchise_entry": False,
         "last_watched_at": old, "marked_for_deletion": True, "size_bytes": 10 * 1024**3,
         "universe_credit": 2.0},
        {"movie_file_id": 702, "keep_policy": None, "is_franchise_entry": False,
         "last_watched_at": old, "marked_for_deletion": True, "size_bytes": 10 * 1024**3,
         "universe_credit": 1.5},
        {"movie_file_id": 703, "keep_policy": None, "is_franchise_entry": False,
         "last_watched_at": old, "marked_for_deletion": True, "size_bytes": 10 * 1024**3,
         "universe_credit": 0.4},
    ])
    df["is_franchise_entry"] = df["is_franchise_entry"].astype(bool)
    st = {}
    out = build_movie_delete_candidates(
        df, {i: 1 for i in df.index}, _marked(df), franchise_file_ids=frozenset(),
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
    assert {c[5] for c in out} == {703}
    assert st["skipped_universe"] == 2


def test_unwatched_hot_credit_protected():
    # The guard fires BEFORE the marked/unwatched tier split, so an unwatched tier-1 candidate
    # (low score, old date_added) with high credit is held too — not just marked tier-0 rows.
    old = (NOW - timedelta(days=400)).isoformat()
    df = pd.DataFrame([{
        "movie_file_id": 704, "keep_policy": None, "is_franchise_entry": False,
        "last_watched_at": None, "date_added": old, "marked_for_deletion": False,
        "size_bytes": 10 * 1024**3, "universe_credit": 2.0,
    }])
    df["is_franchise_entry"] = df["is_franchise_entry"].astype(bool)
    st = {}
    out = build_movie_delete_candidates(
        df, {df.index[0]: 1}, _marked(df), franchise_file_ids=frozenset(),
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20, stats=st)
    assert out == []
    assert st["skipped_universe"] == 1


def test_tier0_before_tier1_and_neutral_critic():
    df = _build_df()
    out = build_movie_delete_candidates(
        df, SCORE_MAP, _marked(df), franchise_file_ids=FRANCHISE_FILE_IDS,
        no_delete_cutoff=NO_DELETE_CUTOFF, include_unwatched=True, ceiling=20)
    tiers = [c[0] for c in out]
    assert tiers == sorted(tiers), tiers          # watched-grace (0) before unwatched (1)
    # The two 10 GiB tier-0 rows: file 13 (critic 5.0) sorts before file 7
    # (critic ~7.65) at equal tier+score+size — lowest critic first.
    tier0 = [c for c in out if c[0] == 0]
    assert tier0[0][5] == 12  # score 1, lowest watchability, deleted first
    assert {c[5] for c in tier0} == {7, 8, 12, 13}
