"""
test_ml_backfill_snapshots.py — truncated-replay backfill correctness.
================================================================================
Fixed-seed mini-history round-trip through the REAL chain (backfill tool ->
labels/snapshots writer -> labels/labeling), asserting the non-negotiables:

  * TRUNCATION — an event at time e influences ONLY snapshots with t > e; an
    event at EXACTLY t influences neither the features at t (strict <) nor the
    label at t (the label window is (t, t+H]).
  * ADDED-DATE FILTERING — a movie appears on the grid only once its Radarr
    'added' date has passed; a movie with NO resolvable added date is included
    everywhere and flagged ``no_added_date``.
  * PROVENANCE — every backfilled row carries source="backfill",
    reconstruction_version, leakage_flags; prospective rows stay "prospective";
    legacy parquets without the column load as "prospective".
  * NON-COLLISION — backfill can never write on/after the first prospective
    snapshot date (grid guard), so the writer's (date, instance, entity) dedupe
    cannot merge the two populations; re-runs are idempotent.
  * CONSUMER GATE — ml_forward_validation / ml_weight_refit exclude backfill by
    default and only mix it under --include-backfill (with source counts + a
    leakage warning).

Run:  python -m pytest scripts/support/tools/test_ml_backfill_snapshots.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from scripts.managers.machine_learning.labels.labeling import build_labels          # noqa: E402
from scripts.managers.machine_learning.labels.snapshots import (                    # noqa: E402
    append_snapshot,
    build_movie_snapshot_rows,
    load_snapshots,
)

SEED = 42
UTC = timezone.utc


def _load_tool(name: str):
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_bt_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _epoch(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp())


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _build_fixture(cache: Path) -> Path:
    """A deterministic (SEED=42) mini household cache.

    Movies (movie_files.parquet, instance=standard):
      Alpha  tmdb 101  added 2026-01-01  Drama   collection 9001 (with tmdb 404)
      Beta   tmdb 202  added 2026-02-15  Comedy
      Gamma  tmdb 303  added UNKNOWN     (no_added_date path)

    Events:
      e1  movie Alpha  2026-01-20 10:00  pct 100
      e4  movie Alpha  2026-02-09 00:00:00 EXACTLY (a grid instant)  pct 100
      e2  movie Beta   2026-02-20 12:00  pct 100
      + seeded episode/track noise with unmapped rating keys
    Trakt: tmdb 404 (Alpha's collection sibling) watched 2026-02-01."""
    rng = random.Random(SEED)
    events = [
        {"date": _epoch(2026, 1, 20, 10), "media_type": "movie", "rating_key": "r101",
         "title": "Alpha", "percent_complete": 100, "user": "Trizzd", "user_id": 1,
         "platform": "Roku", "transcode_decision": "direct play"},
        {"date": _epoch(2026, 2, 9, 0, 0, 0), "media_type": "movie", "rating_key": "r101",
         "title": "Alpha", "percent_complete": 100, "user": "Trizzd", "user_id": 1,
         "platform": "Roku", "transcode_decision": "direct play"},
        {"date": _epoch(2026, 2, 20, 12), "media_type": "movie", "rating_key": "r202",
         "title": "Beta", "percent_complete": 100, "user": "Mom", "user_id": 2,
         "platform": "Roku", "transcode_decision": "direct play"},
    ]
    for k in range(12):   # deterministic noise the replay must shrug off
        events.append({
            "date": _epoch(2026, 1, 5) + rng.randrange(0, 55 * 86400),
            "media_type": rng.choice(["episode", "track"]),
            "rating_key": f"noise{k}", "grandparent_title": "ShowX",
            "title": f"Ep{k}", "percent_complete": rng.randrange(0, 101),
            "user": "Aiden", "user_id": 3, "platform": "Firestick",
            "transcode_decision": "transcode"})
    _write_json(cache / "tautulli" / "history" / "all.json", events)

    _write_json(cache / "tautulli" / "metadata" / "index.json", {
        "r101": {"genres": ["Drama"], "actors": ["Actor A", "Actor B"],
                 "directors": ["Dir D"], "studios": ["StudioX"]},
        "r202": {"genres": ["Comedy"], "actors": ["Actor C"], "directors": [],
                 "studios": []},
    })
    _write_json(cache / "plex" / "movies" / "owned_inventory.json", {
        "101": {"rating_key": "r101", "title": "Alpha", "year": 2000},
        "202": {"rating_key": "r202", "title": "Beta", "year": 2001},
        "303": {"rating_key": "r303", "title": "Gamma", "year": 2002},
    })
    _write_json(cache / "tautulli" / "group" / "household" / "tmdb_completions.json", {})
    _write_json(cache / "trakt" / "history" / "movies.json", [
        {"watched_at": "2026-02-01T00:00:00.000Z", "type": "movie",
         "movie": {"title": "Alpha Two", "ids": {"tmdb": 404}}},
    ])
    _write_json(cache / "radarr.movies.standard.full.json", [
        {"tmdbId": 101, "title": "Alpha", "genres": ["Drama"],
         "added": "2026-01-01T00:00:00Z",
         "collection": {"tmdbId": 9001, "name": "Alpha Saga"}},
        {"tmdbId": 404, "title": "Alpha Two", "genres": ["Drama"],
         "added": "2026-01-01T00:00:00Z",
         "collection": {"tmdbId": 9001, "name": "Alpha Saga"}},
        {"tmdbId": 202, "title": "Beta", "genres": ["Comedy"],
         "added": "2026-02-15T00:00:00Z"},
        # Gamma (303) deliberately ABSENT -> no added-date fallback.
    ])

    def _movie_row(tmdb, title, added, genres, coll_id=None, coll_name=None):
        return {
            "tmdb_id": tmdb, "title": title, "added_at": added,
            "genres": json.dumps(genres), "percent_complete": 0.0,
            "watch_count": 0, "is_watched": False, "last_watched_at": None,
            "size_bytes": 4_000_000_000 + tmdb, "resolution": 1080,
            "movie_file_id": tmdb * 10, "imdb_rating": None, "tmdb_rating": None,
            "trakt_rating": None, "rotten_tomatoes_score": None,
            "metacritic_score": None, "popularity": None, "certification": None,
            "original_language": "English", "in_cinemas_date": None,
            "physical_release_date": None, "digital_release_date": None,
            "keep_policy": None, "is_franchise_entry": False, "universe_name": None,
            "is_available": True, "audio_languages": None, "subtitles": None,
            "collection_tmdb_id": coll_id, "collection_name": coll_name,
        }

    mf = pd.DataFrame([
        _movie_row(101, "Alpha", "2026-01-01T00:00:00Z", ["Drama"], 9001, "Alpha Saga"),
        _movie_row(202, "Beta", "2026-02-15T00:00:00Z", ["Comedy"]),
        _movie_row(303, "Gamma", None, []),
    ])
    (cache / "radarr" / "standard").mkdir(parents=True, exist_ok=True)
    mf.to_parquet(cache / "radarr" / "standard" / "movie_files.parquet", index=False)

    cfg_path = cache / "config.json"
    _write_json(cfg_path, {"radarr_instances": {"standard": {}}})
    return cfg_path


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    cache = tmp_path_factory.mktemp("bf_cache")
    cfg = _build_fixture(cache)
    return {"cache": cache, "cfg": cfg, "bf": _load_tool("ml_backfill_snapshots")}


def _run_backfill(env, end="2026-03-01"):
    return env["bf"].main([
        "--cache-base", str(env["cache"]), "--config", str(env["cfg"]),
        "--start", "2026-01-10", "--end", end,
        "--grid-days", "10", "--horizon-days", "14", "--no-write"])


def _bf_rows(env) -> pd.DataFrame:
    df = load_snapshots(env["cache"], services=("radarr",), instance="standard")
    return df[df["source"] == "backfill"].reset_index(drop=True)


def _row(df, tmdb, date):
    m = df[(df["tmdb_id"] == tmdb) & (df["snapshot_date"] == date)]
    assert len(m) == 1, f"expected exactly one row for {tmdb}@{date}, got {len(m)}"
    return m.iloc[0]


# ── pure helper boundaries ────────────────────────────────────────────────────

def test_truncation_helpers_strict_before(env):
    bf = env["bf"]
    t = datetime(2026, 2, 9, tzinfo=UTC)
    e_at = (t, {"media_type": "movie", "rating_key": "r1", "title": "X",
                "percent_complete": 100})
    wm = bf.title_watch_map([e_at])
    assert wm["X"]["watch_count"] == 1          # the map itself just aggregates
    # the STRICT-BEFORE cut lives in the bisect prefix: an event AT t is excluded
    ts_list = [t]
    assert __import__("bisect").bisect_left(ts_list, t) == 0
    watched = bf.watched_tmdbs_at([], {"r1": 7}, [t], [55], t)
    assert watched == set()                     # trakt watch at exactly t excluded
    watched = bf.watched_tmdbs_at([], {"r1": 7}, [t], [55],
                                  t.replace(second=1))
    assert watched == {55}


# ── the round-trip ────────────────────────────────────────────────────────────

def test_backfill_truncation_added_dates_and_provenance(env):
    assert _run_backfill(env) == 0
    df = _bf_rows(env)
    assert not df.empty
    grid = ["2026-01-10", "2026-01-20", "2026-01-30",
            "2026-02-09", "2026-02-19", "2026-03-01"]
    assert sorted(df["snapshot_date"].unique()) == grid

    # provenance on EVERY row
    assert (df["source"] == "backfill").all()
    # Tracks the constant rather than pinning a literal: the version has to MOVE
    # whenever the reconstruction's semantics change (it went 1 -> 2 with the global
    # watched bar, which made title_watch_map count WATCHES instead of plays), and a
    # hard-coded 1 would have to be edited every time — i.e. it would stop testing
    # anything. What matters is that every row carries the CURRENT version.
    from scripts.support.tools.ml_backfill_snapshots import RECONSTRUCTION_VERSION
    assert RECONSTRUCTION_VERSION >= 2       # the watched-bar bump landed
    assert (df["reconstruction_version"] == RECONSTRUCTION_VERSION).all()
    assert df["leakage_flags"].str.startswith(
        "credits_today,metadata_today,deletions_unknown").all()

    # Alpha before any event: nothing fired
    r = _row(df, 101, "2026-01-10")
    assert not r["watched_before"]
    assert r["sig_A2_completion"] == 0 and r["sig_A3_rewatch"] == 0
    assert r["sig_B4_genre_affinity"] == 0 and r["sig_C1_collection"] == 0
    assert pd.isna(r["days_since_last_watch"])

    # e1 lands at 2026-01-20T10:00 — AFTER the 01-20T00:00 snapshot instant
    r = _row(df, 101, "2026-01-20")
    assert not r["watched_before"] and r["sig_A3_rewatch"] == 0

    # after e1: watched, A2 full, one watch, Drama affinity present
    r = _row(df, 101, "2026-01-30")
    assert r["watched_before"]
    assert r["sig_A2_completion"] == 12.0 and r["sig_A3_rewatch"] == 2.0
    assert r["sig_B4_genre_affinity"] > 0
    assert 9.0 < r["days_since_last_watch"] < 10.0
    assert r["sig_C1_collection"] == 0        # trakt sibling watch is 02-01

    # THE boundary assert: e4 at EXACTLY 2026-02-09T00:00:00 must NOT influence
    # the 02-09 snapshot (events < t, strictly)
    r = _row(df, 101, "2026-02-09")
    assert r["sig_A3_rewatch"] == 2.0          # still ONE watch, not two
    assert r["days_since_last_watch"] > 1.0    # measured from e1, not e4
    assert r["sig_C1_collection"] == 8.0       # trakt 404 watch (02-01) now counts
    # ...and the first grid point AFTER it sees the rewatch
    r = _row(df, 101, "2026-02-19")
    assert r["sig_A3_rewatch"] == 5.0          # watch_count == 2

    # added-date filtering: Beta (added 02-15) absent before, present after
    beta_dates = sorted(df[df["tmdb_id"] == 202]["snapshot_date"].unique())
    assert beta_dates == ["2026-02-19", "2026-03-01"]
    assert _row(df, 202, "2026-02-19")["sig_A2_completion"] == 0.0
    assert _row(df, 202, "2026-03-01")["sig_A2_completion"] == 12.0  # e2 (02-20) < t

    # no added date: Gamma everywhere + flagged
    gamma = df[df["tmdb_id"] == 303]
    assert sorted(gamma["snapshot_date"].unique()) == grid
    assert gamma["leakage_flags"].str.endswith(",no_added_date").all()

    # labels through the REAL labeler: window (t, t+14], event at t excluded
    labeled = build_labels(df, env["cache"], horizon_days=14)
    pos = labeled[labeled["watched_within_h"]]
    got = sorted(zip(pos["tmdb_id"].astype(int), pos["snapshot_date"]))
    assert got == [(101, "2026-01-10"), (101, "2026-01-20"),
                   (101, "2026-01-30"), (202, "2026-02-19")]
    assert labeled["label_mature"].all()

    # idempotence: a second identical run appends nothing new
    n_before = len(_bf_rows(env))
    assert _run_backfill(env) == 0
    assert len(_bf_rows(env)) == n_before


def test_prospective_guard_and_dedupe_non_collision(env):
    cache = env["cache"]
    # plant a FAKE prospective row (the real writer path) on 2026-03-20
    mf = pd.DataFrame([{
        "tmdb_id": 101, "title": "Alpha", "watchability_score": 33.0,
        "watchability_breakdown": json.dumps({"A1_keep_policy": 0.0}),
        "size_bytes": 1, "resolution": 1080, "is_watched": True,
        "watch_count": 2, "planned_action": None,
    }])
    rows = build_movie_snapshot_rows(mf, "standard",
                                     snapshot_ts="2026-03-20T12:00:00+00:00")
    assert rows and rows[0]["source"] == "prospective"
    assert append_snapshot(cache, "radarr", "standard", rows) == 1

    # re-run past the prospective date: the guard must stop the grid below it
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = _run_backfill(env, end="2026-03-25")
    assert rc == 0
    assert "GUARD" in out.getvalue() and "2026-03-21" in out.getvalue()

    store = load_snapshots(cache, services=("radarr",), instance="standard")
    bf = store[store["source"] == "backfill"]
    prosp = store[store["source"] == "prospective"]
    # 03-11 extended the grid; nothing on/after the prospective 03-20
    assert "2026-03-11" in set(bf["snapshot_date"])
    assert (bf["snapshot_date"] < "2026-03-20").all()
    # the prospective row survived untouched, alongside backfill rows for the
    # SAME entity on other dates (dedupe key includes the date)
    assert len(prosp) == 1
    p = prosp.iloc[0]
    assert p["watchability_score"] == 33.0 and p["snapshot_date"] == "2026-03-20"
    assert (bf["entity_id"] == "101").sum() >= 6


def test_legacy_partition_reads_as_prospective(tmp_path):
    d = tmp_path / "ml" / "snapshots" / "radarr"
    d.mkdir(parents=True)
    pd.DataFrame([{
        "snapshot_ts": "2025-12-01T00:00:00+00:00", "snapshot_date": "2025-12-01",
        "service": "radarr", "instance": "standard", "entity_id": "9",
        "tmdb_id": 9, "watchability_score": 5.0,
    }]).to_parquet(d / "2025-12.parquet", index=False)
    df = load_snapshots(tmp_path, services=("radarr",))
    assert "source" in df.columns and (df["source"] == "prospective").all()


def test_cli_gate_default_excludes_backfill(env):
    cache = str(env["cache"])
    fv = _load_tool("ml_forward_validation")
    wr = _load_tool("ml_weight_refit")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = fv.main(["--cache-base", cache, "--service", "radarr", "--no-write"])
    text = out.getvalue()
    assert rc == 0
    assert "backfill snapshot row(s) EXCLUDED" in text
    assert "(n=1," in text                       # only the single prospective row

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = fv.main(["--cache-base", cache, "--service", "radarr", "--no-write",
                      "--include-backfill"])
    text = out.getvalue()
    assert rc == 0
    assert "rows by source" in text and "known leakage" in text
    assert "'backfill'" in text.replace('"', "'")

    # refit: default sees zero positives (the lone prospective row is unwatched
    # within its horizon) and refuses; --include-backfill fits and warns
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc_default = wr.main(["--cache-base", cache, "--service", "radarr",
                              "--no-write"])
    assert rc_default == 1
    assert "EXCLUDED" in out.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc_inc = wr.main(["--cache-base", cache, "--service", "radarr",
                          "--no-write", "--include-backfill"])
    text = out.getvalue()
    assert rc_inc == 0
    assert "rows by source" in text and "fitted" in text
    assert "HIGH-VARIANCE" in text               # n_pos < 100 caveat fires
