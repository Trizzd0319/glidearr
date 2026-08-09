"""labels/recommendations.py — the RECOMMENDATION EVENT ledger (append-only, pure logging).
================================================================================
The first surface in the product that generates its own labels writes them here.

WHY IT LIVES IN ``labels/`` AND NOT NEXT TO THE SHELF
-----------------------------------------------------
``labels/`` is the package that turns "what the system believed" into "what the household
then did" — ``snapshots.py`` writes the per-entity score/feature snapshot, ``labeling.py``
joins it to watch history. A recommendation event is exactly that kind of artifact, and a
STRONGER one: an owned title we surfaced and then observed being played (or not) is a real
PROSPECTIVE label with no counterfactual problem. An acquisition of an unowned title can
never be labelled honestly ("would they have watched it if we hadn't grabbed it?" is
unanswerable); a gem pick can. So it belongs with the other label sources, not inside the
Plex service that happens to publish the shelf — and the offline harnesses already point at
``<cache>/ml/**``, so a sibling directory is one glob away from being joinable.

    <global cache base dir>/ml/recommendations/<surface>/{YYYY-MM}.parquet

Every convention is ``snapshots.py``'s, deliberately: month-partitioned Parquet,
read-modify-write append, drop_duplicates(keep="last") on a stable key, atomic tmp+replace,
a read-tolerant loader, and gated hooks that can NEVER raise into a run.

Row schema (one row per published pick per profile per day):
    recommended_at   UTC ISO timestamp the shelf was published
    recommended_date YYYY-MM-DD (part of the dedupe key — one row per pick per profile per day)
    surface          "hidden_gems" (the shelf that published it — this ledger is shared)
    profile          the de-identified ``safe_user`` handle, never the real profile name
    entity_id        str(tmdb_id) for movies (mirrors snapshots.py's entity_id convention)
    tmdb_id / title / year / media
                     ``title``+``year`` are stored so the outcome join keeps working after the
                     title leaves the library (a deleted movie has no ratingKey left, but the
                     (title, year) watch identity the playlist builders use still resolves)
    taste_score      the TASTE-ONLY 0-100 score the pick was ranked on (discovery/gems.py)
    rank             0-based position on the published shelf
    window_days      the measurement window this pick was published under, so re-reading an
                     old row uses the window it was PUBLISHED with, not today's config

DEDUPE / IDEMPOTENCE: the key is (recommended_date, surface, profile, entity_id). Two runs on
the same day republishing the same shelf collapse to one row per pick (the later run wins, so
a rank change within a day is honoured); the next day re-publishes as new rows, which is what
makes "recommended again after the window closed" a distinct, separately-measurable event.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

RECOMMENDATION_SUBDIR = ("ml", "recommendations")
SURFACE_HIDDEN_GEMS = "hidden_gems"

#: Namespace prefixes for NON-MOVIE entity ids.
#:
#: A movie's ``entity_id`` is its bare tmdb id as a string, and it stays that way
#: forever: the live ledger already holds rows keyed that way, and ``_DEDUP_KEYS``
#: joins on ``entity_id``, so re-keying movies would orphan every published pick
#: still inside its measurement window.
#:
#: TV ids are therefore PREFIXED rather than bare. That is not cosmetic - tmdb and
#: tvdb number-spaces overlap, so a bare "550" could be Fight Club or a series,
#: and since ``media`` is NOT part of the dedup key the two would collide into one
#: row and silently lose a pick.
ENTITY_SHOW = "tvdb"        # tvdb:121361
ENTITY_EPISODE = "tvdb"     # tvdb:121361:1:4  (season/episode appended)
_DEDUP_KEYS = ["recommended_date", "surface", "profile", "entity_id"]

#: Columns every row carries, in order — so an empty load still has a usable frame shape.
#:
#: ``rating_key`` is the Plex handle AS IT WAS WHEN SURFACED. It is a SECONDARY
#: identity, never the join key: ``entity_id`` is authoritative because a re-scan
#: retires ratingKeys. But recording it costs nothing and buys a direct match for
#: the common case - a play shortly after the recommendation carries the same
#: ratingKey the ledger saw, even if a later re-scan has since moved the item and
#: the CURRENT inventory no longer agrees with either. Old partitions written
#: before this column existed load as NaN, which the resolver reads as "absent",
#: not as "no ratingKey".
COLUMNS = ("recommended_at", "recommended_date", "surface", "profile", "entity_id",
           "tmdb_id", "rating_key", "title", "year", "media", "taste_score", "rank",
           "window_days")


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision) — same helper contract as
    ``snapshots.utc_now_iso`` so the two ledgers timestamp identically."""
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def recommendation_dir(base_dir, surface: str = SURFACE_HIDDEN_GEMS) -> Path:
    """``<base_dir>/ml/recommendations/<surface>``"""
    return Path(base_dir).joinpath(*RECOMMENDATION_SUBDIR, str(surface))


def iso_to_epoch(ts):
    """ISO-8601 (with or without an explicit offset) -> POSIX seconds, or ``None``.

    A naive timestamp is read as UTC — every writer here stamps UTC, and mis-reading one as
    local time would shift a pick's whole 30-day window."""
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def entity_of(pick, media: str = "movie"):
    """``(entity_id, tmdb_id, media)`` for one pick, or ``None`` when unidentifiable.

    WHY THIS EXISTS. ``build_events`` used to require ``int(tmdb_id)`` and skip
    anything without it. That is correct for Hidden Gems, which is movies-only,
    and silently wrong for every other surface: Anniversary publishes 23 movies
    AND 97 shows per profile, and all 97 would vanish - not as an error, but as
    an absence indistinguishable from "no TV was surfaced". The dangerous state
    is the silent skip, not the missing feature.

    ``media`` is resolved PER PICK (``pick["media"]`` wins over the call-level
    default) because one shelf legitimately mixes both.

    Accepted identities, in order of preference:
      movie    ``tmdb_id``                          -> ``"550"``
      episode  ``tvdb_join_key`` or tvdb+season+ep  -> ``"tvdb:121361:1:4"``
      show     ``tvdb_id`` / ``series_tvdb_id``     -> ``"tvdb:121361"``
    """
    if not isinstance(pick, dict):
        return None
    kind = str(pick.get("media") or media or "movie").strip().lower()

    if kind == "movie":
        try:
            tmdb = int(pick.get("tmdb_id"))
        except (TypeError, ValueError):
            return None
        return str(tmdb), tmdb, "movie"

    # An explicit join key already carries the whole identity.
    join = pick.get("tvdb_join_key") or pick.get("join_key")
    if join:
        text = str(join).strip()
        if text:
            return (text if text.startswith(f"{ENTITY_EPISODE}:")
                    else f"{ENTITY_EPISODE}:{text}"), None, "episode"

    tvdb = pick.get("tvdb_id") or pick.get("series_tvdb_id")
    try:
        tvdb = int(tvdb)
    except (TypeError, ValueError):
        return None
    season, episode = pick.get("season_number"), pick.get("episode_number")
    try:
        if season is not None and episode is not None:
            return (f"{ENTITY_EPISODE}:{tvdb}:{int(season)}:{int(episode)}",
                    None, "episode")
    except (TypeError, ValueError):
        pass                                  # fall through to series identity
    return f"{ENTITY_SHOW}:{tvdb}", None, "show"


def build_events(picks, *, profile: str, surface: str = SURFACE_HIDDEN_GEMS,
                 recommended_at: "str | None" = None, media: str = "movie",
                 window_days: int = 30) -> list:
    """One event row per published pick. PURE (no I/O; the clock is an argument).

    ``picks`` are the shelf items :func:`discovery.gems.apply_diversity_caps` returned — each
    carrying ``tmdb_id``, ``taste_score`` and ``rank``.

    MOVIES, SHOWS AND EPISODES are all recordable; see :func:`entity_of` for the
    identity each resolves to. A pick that resolves to NOTHING is skipped and
    COUNTED — the caller gets ``(rows, skipped)`` from :func:`build_events_ex` if
    it wants to know, because a silent skip is how 97 shows a night disappear
    without anyone noticing.

    The movie path is unchanged, byte for byte: bare tmdb ``entity_id``, populated
    ``tmdb_id`` column. Existing rows and existing joins are untouched."""
    rows, _skipped = build_events_ex(
        picks, profile=profile, surface=surface, recommended_at=recommended_at,
        media=media, window_days=window_days)
    return rows


def build_events_ex(picks, *, profile: str, surface: str = SURFACE_HIDDEN_GEMS,
                    recommended_at: "str | None" = None, media: str = "movie",
                    window_days: int = 30) -> tuple:
    """:func:`build_events`, but returns ``(rows, skipped)``.

    ``skipped`` is the count of picks with no resolvable identity. Surfaced
    without being recordable is a real state and it must be reportable: the
    alternative is a shelf that publishes 97 items and logs nothing about the 97
    that never reached the ledger.
    """
    ts = recommended_at or utc_now_iso()
    rows: list = []
    skipped = 0
    for i, p in enumerate(picks or []):
        ident = entity_of(p, media)
        if ident is None:
            skipped += 1
            continue
        entity_id, tmdb, kind = ident
        rank = p.get("rank")
        try:
            year = int(p.get("year"))
        except (TypeError, ValueError):
            year = None
        rows.append({
            "recommended_at": ts,
            "recommended_date": ts[:10],
            "surface": str(surface),
            "profile": str(profile),
            "entity_id": entity_id,
            "tmdb_id": tmdb,
            "rating_key": (str(p.get("rating_key")) if p.get("rating_key") is not None else None),
            "title": (str(p.get("title")) if p.get("title") is not None else None),
            "year": year,
            "media": kind,
            "taste_score": (float(p["taste_score"]) if p.get("taste_score") is not None else None),
            "rank": int(rank) if rank is not None else i,
            "window_days": int(window_days),
        })
    return rows, skipped


def append_events(base_dir, rows: list, *, surface: str = SURFACE_HIDDEN_GEMS) -> int:
    """Append *rows* to ``<base_dir>/ml/recommendations/<surface>/<YYYY-MM>.parquet``.

    Read-modify-write append deduped on (recommended_date, surface, profile, entity_id) keeping
    LAST, atomic (tmp + os.replace) so a crash can never leave a truncated partition. Returns
    the number of NEW rows (post-dedupe delta) — republishing an identical shelf twice in one
    day therefore returns 0 the second time, which is the idempotence contract."""
    if not rows:
        return 0
    target_dir = recommendation_dir(base_dir, surface)
    target_dir.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(rows)
    written = 0
    for month, month_df in new_df.groupby(new_df["recommended_at"].astype(str).str[:7]):
        path = target_dir / f"{month}.parquet"
        if path.exists():
            try:
                old = pd.read_parquet(path)
            except Exception:
                # Unreadable partition -> preserve it out of the way, start fresh (snapshots.py).
                try:
                    os.replace(path, path.with_suffix(".parquet.corrupt"))
                except OSError:
                    pass
                old = pd.DataFrame()
        else:
            old = pd.DataFrame()
        before = len(old)
        if old.empty:
            merged = month_df
        else:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                merged = pd.concat([old, month_df], ignore_index=True)
        merged = merged.drop_duplicates(subset=_DEDUP_KEYS, keep="last").reset_index(drop=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        written += max(0, len(merged) - before)
    return written


def load_events(base_dir, *, surface: str = SURFACE_HIDDEN_GEMS,
                profile: "str | None" = None) -> pd.DataFrame:
    """Every month partition for ``surface`` as one DataFrame (empty, correctly-columned frame
    when nothing has been published). Missing dirs / unreadable partitions are skipped, so a
    half-written cache degrades to "fewer events", never to an exception."""
    d = recommendation_dir(base_dir, surface)
    frames: list = []
    if d.is_dir():
        for path in sorted(d.glob("*.parquet")):
            try:
                frames.append(pd.read_parquet(path))
            except Exception:
                continue
    if not frames:
        return pd.DataFrame(columns=list(COLUMNS))
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(frames, ignore_index=True)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    if profile is not None:
        df = df[df["profile"].astype(str) == str(profile)].reset_index(drop=True)
    return df


def open_picks(df, *, profile: str, now_ts: float, window_days: int = 30) -> list:
    """``[{"tmdb_id", "recommended_at", "rank"}]`` — the picks this profile was shown whose
    measurement window is still OPEN, oldest-first then by published rank, deduped on tmdb
    (keeping the EARLIEST publication, which is the one being measured). PURE.

    These are the shelf's HELD slots. A pick is offered for the whole window, not for one run:
    dropping it the next day would give the household a single day to act on it, churn the
    rendered playlist completely between runs, and make the resulting label meaningless.

    A row whose own ``window_days`` is longer than the caller's is honoured at THEIR length, so
    shortening the config knob never cuts short a pick that is still being measured under the
    longer window it was published with."""
    out: list = []
    if df is None or getattr(df, "empty", True):
        return out
    for rec in df.to_dict("records"):
        if str(rec.get("profile")) != str(profile):
            continue
        ts = iso_to_epoch(rec.get("recommended_at"))
        if ts is None:
            continue
        try:
            row_window = int(rec.get("window_days") or window_days)
        except (TypeError, ValueError):
            row_window = window_days
        if now_ts - ts >= max(row_window, window_days) * 86400.0:
            continue
        try:
            tmdb = int(rec.get("tmdb_id"))
        except (TypeError, ValueError):
            continue
        try:
            rank = int(rec.get("rank"))
        except (TypeError, ValueError):
            rank = 0
        out.append({"tmdb_id": tmdb, "recommended_at": ts, "rank": rank})
    out.sort(key=lambda r: (r["recommended_at"], r["rank"], r["tmdb_id"]))
    seen: set = set()
    deduped: list = []
    for r in out:
        if r["tmdb_id"] in seen:
            continue
        seen.add(r["tmdb_id"])
        deduped.append(r)
    return deduped


def recent_entity_ids(df, *, profile: str, now_ts: float, window_days: int = 30) -> set:
    """The tmdb ids in :func:`open_picks` as a set — the re-surface cooldown. Re-offering a
    pick as a NEW recommendation mid-window would corrupt the label (which of the two
    recommendations caused the play?), so a title is only published once per window."""
    return {r["tmdb_id"] for r in open_picks(df, profile=profile, now_ts=now_ts,
                                             window_days=window_days)}


# ── service-facing gated hooks (never raise) ──────────────────────────────────

def _resolve_base_dir(global_cache):
    """The global cache base dir — from the live manager when available, else the default
    CacheKeyBuilder resolves. Mirrors ``snapshots._resolve_base_dir``."""
    root = getattr(global_cache, "cache_root", None)
    if root:
        return Path(root)
    from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
    return CacheKeyBuilder().base_dir
