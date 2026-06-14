"""
space/delete_planner.py — what to delete (ranked, tiered) (pure).
================================================================================
Relocated from ``radarr/quality/space_pressure.run_deletions`` (ML Step 7c, the
movie twin; the cross-service coordinator ranking already moved in 7b
``space.coordinator_ranker``). The DECISION half: build the tiered, guarded,
lowest-rated-first candidate queue the target loop drains to reach the U target.
PURE — reads the movie_files frame cells + the score map; no HTTP, no
global_cache. The service computes the context (score map, franchise file ids,
cutoff, config knobs) and APPLIES the result (moviefile DELETE + restore-set +
ledger stamp), draining candidates until projected free >= U.

The per-row critic blend delegates to ``scoring.critic.critic_avg`` (ML Step 2),
so the secondary rank key is the single shared implementation — no duplicate
inline copy here.

Public API:
  * bare_universe_protected(keep_policy, date_added, now, *, age_days) -> bool
        whether a bare 'universe' title is still ageing and spared from the delete pass.
  * build_movie_delete_candidates(df, score_map, marked, *, franchise_file_ids,
        no_delete_cutoff, include_unwatched, ceiling, universe_age_days, now) -> list[tuple]

Candidate tuple shape (preserved verbatim from the service for the apply loop):
    (tier, score, critic_or_None, -size, idx, int(movie_file_id), size)
sorted ascending by (tier, score, critic-or-5.0, -size) — i.e. watched-and-grace-
expired before unwatched, then lowest watchability, then lowest critic (a missing
rating sorts NEUTRAL at 5.0, never to the protected end), then largest file first.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.scoring.critic import critic_avg

# Critic source columns the per-row blend reads (scales live in scoring.critic).
_CRITIC_COLS = (
    "imdb_rating", "tmdb_rating", "trakt_rating",
    "rotten_tomatoes_score", "metacritic_score",
)

_KEEP_TAGS = ("keep_forever", "keep_movie", "keep_universe")


def bare_universe_protected(keep_policy, date_added, now, *, age_days) -> bool:
    """Whether a BARE ``'universe'`` title (the last-resort-deletable universe tier, NOT
    ``keep_universe``) is still ageing and should be spared from this delete pass — a
    universe entry gets a dwell on disk before it can be shed.

    DEFAULT (``age_days`` None) -> False: no protection, byte-identical. Enabled -> True
    until the title has been on disk for >= ``age_days`` (measured from ``date_added``).
    A missing / unparseable ``date_added`` is treated as NOT yet proven old enough, so it
    stays protected rather than being deleted on an unknown age."""
    if age_days is None or keep_policy != "universe":
        return False
    if not date_added:
        return True
    try:
        return (now - pd.to_datetime(date_added, utc=True)) < pd.Timedelta(days=age_days)
    except Exception:
        return True


def build_movie_delete_candidates(
    df,
    score_map: dict,
    marked,
    *,
    franchise_file_ids,
    no_delete_cutoff,
    include_unwatched: bool,
    ceiling: int,
    universe_age_days=None,
    now=None,
) -> "list[tuple]":
    """Build the tiered, guarded, lowest-rated-first movie delete queue.

    Guards (skip): no movie file on disk; keep_forever/keep_movie/keep_universe;
    franchise entry or franchise-file id; watched within COLLECTION_WINDOW_DAYS
    (``no_delete_cutoff``); a still-ageing bare 'universe' title when ``universe_age_days``
    is set (default None -> no extra guard, byte-identical). Tiering: tier 0 = marked-for-deletion (watched +
    grace-expired); tier 1 = unwatched low-watchability (only when
    ``include_unwatched`` and score < ``ceiling`` and not freshly added within the
    window). ``marked`` is the bool Series the service derived from
    ``marked_for_deletion``. Returns the sorted candidate-tuple list (see module
    docstring); the service drains it under the U target.
    """
    candidates: "list[tuple]" = []
    for idx in df.index:
        fid = df.at[idx, "movie_file_id"]
        if pd.isna(fid):
            continue
        keep_policy = df.at[idx, "keep_policy"] if "keep_policy" in df.columns else None
        is_fe       = bool(df.at[idx, "is_franchise_entry"]) if "is_franchise_entry" in df.columns else False
        if is_fe or keep_policy in _KEEP_TAGS:
            continue
        if fid in franchise_file_ids:
            continue
        # Bare-universe ageing (default-off): spare a 'universe' title still inside its
        # on-disk dwell from the last-resort delete pass.
        if bare_universe_protected(
            keep_policy, df.at[idx, "date_added"] if "date_added" in df.columns else None,
            now, age_days=universe_age_days,
        ):
            continue
        lw = df.at[idx, "last_watched_at"] if "last_watched_at" in df.columns else None
        if lw:
            try:
                if pd.to_datetime(lw, utc=True) >= no_delete_cutoff:
                    continue   # protect anything watched within the window
            except Exception:
                pass

        score  = int(score_map.get(idx, 5))
        size   = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0
        critic = critic_avg({c: df.at[idx, c] for c in _CRITIC_COLS if c in df.columns})

        if bool(marked.loc[idx]):
            tier = 0                                   # watched + grace-expired
        else:
            if not include_unwatched or score >= ceiling:
                continue
            da = df.at[idx, "date_added"] if "date_added" in df.columns else None
            if da:
                try:
                    if pd.to_datetime(da, utc=True) >= no_delete_cutoff:
                        continue                       # don't delete a freshly-added unwatched movie
                except Exception:
                    pass
            tier = 1
        # Store the RAW critic (may be None); the neutral sort value (5.0) is
        # applied in the sort key so a missing rating doesn't masquerade as a high
        # one and protect exactly the obscure/unrated titles you'd shed first.
        candidates.append((tier, score, critic, -size, idx, int(fid), size))

    candidates.sort(key=lambda c: (c[0], c[1], c[2] if c[2] is not None else 5.0, c[3]))
    return candidates
