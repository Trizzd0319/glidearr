"""lifecycle/restore_policy.py — restore recovered deletions (pure).
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: Decide which previously-deleted items to re-acquire on score recovery.

PULLS FROM (decision cores to migrate here):
  - radarr/repair/anomaly::restore_recovered_deletions (decision core)
  - sonarr/cache/episode_files::restore_recovered_episode_deletions (decision core)

PUBLIC API (to implement):
  - plan_restores(restore_set, current_scores, config) -> list[AcquirePlan]

DEPENDS ON: contracts
SERVICE REMAINDER (stays in the service as the thin adapter): Service keeps the restore-set read + re-monitor/search APPLY.

────────────────────────────────────────────────────────────────────────────────
LANDED: THE DELETED-EPISODE LEDGER'S RELEASE IDENTITY
────────────────────────────────────────────────────────────────────────────────
``sonarr/{inst}/deleted_episodes`` used to record only ``{series_id: {episodes:
[[s,e]…], ts}}`` — WHAT was deleted, with nothing about WHICH release it was. On
restore that left only a blind ``EpisodeSearch``: whatever the indexers happen to
serve today, at whatever quality, from whatever group. Since the per-viewer
retention rule (``lifecycle.viewer_retention``) deliberately bets that
re-acquisition is cheap, the bet is only honest if what comes back is what left.

RECORDED (all already on the parquet row being deleted — NO live indexer lookup;
a guid is worth seconds, not months, and a stored one is a liability):
``scene_name``, ``release_group``, ``quality_name``, ``resolution``, ``size_bytes``.

SCHEMA + MIGRATION: v1 entries (no ``releases`` map) stay valid forever. Readers
go through :func:`ledger_releases`, which returns ``{}`` for a v1 entry, and every
consumer treats "no recorded release" exactly like "the recorded release did not
match" → the existing blind search. The fallback is TOTAL by construction: a
missing, stale or unmatchable scene_name can never block a restore.

Public API (landed):
  * LEDGER_SCHEMA_VERSION / RELEASE_FIELDS
  * episode_key(season, episode)          -> "S02E15"
  * release_record(row)                   -> the recorded identity, or None
  * merge_ledger_entry(existing, incoming) -> one entry, v1-safe
  * ledger_releases(entry)                -> {"S02E15": {...}}  ({} for v1)
  * match_release(releases, recorded)     -> the chosen release dict, or None
"""
from __future__ import annotations

import re

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

LEDGER_SCHEMA_VERSION = 2

# The identity fields lifted off the parquet row at delete time. Deliberately NOT
# a guid/indexerId: those expire (indexer caches roll over in hours-to-days) while
# a restore may fire months later, and a dead guid is worse than no guid because
# it looks actionable.
RELEASE_FIELDS = ("scene_name", "release_group", "quality_name", "resolution", "size_bytes")

# Scene names are compared on a normalised form: case-folded, punctuation → space,
# runs of whitespace collapsed. That absorbs the .-vs-_ vs-space churn between what
# Sonarr stored as sceneName and what an indexer serves as the release title.
_NORM_RE = re.compile(r"[^0-9a-z]+")


def _norm(value) -> str:
    return _NORM_RE.sub(" ", str(value or "").lower()).strip()


def episode_key(season, episode) -> "str | None":
    """``(2, 15)`` → ``"S02E15"`` — the per-episode key inside a ledger entry's
    ``releases`` map. None when either index is missing/unparseable."""
    try:
        return f"S{int(season):02d}E{int(episode):02d}"
    except (TypeError, ValueError):
        return None


def release_record(row) -> "dict | None":
    """Project a parquet row (any mapping) down to :data:`RELEASE_FIELDS`.

    Returns None when the row carries NO usable identity at all — recording an
    all-null stub would just make a v2 entry that behaves exactly like v1 while
    pretending otherwise. ``scene_name`` is populated on roughly a fifth of rows
    and ``release_group`` on roughly a third, so partial records are the norm and
    are kept: a group + quality still narrows a targeted search considerably."""
    if not row:
        return None
    out = {}
    for field in RELEASE_FIELDS:
        val = row.get(field) if hasattr(row, "get") else None
        if val is None or val != val:            # None or NaN
            continue
        if field in ("resolution", "size_bytes"):
            try:
                out[field] = int(val)
            except (TypeError, ValueError):
                continue
        else:
            s = str(val).strip()
            if s:
                out[field] = s
    return out or None


def ledger_releases(entry) -> dict:
    """The ``{episode_key: release_record}`` map of a ledger entry.

    ``{}`` for a v1 entry (or any malformed one) — the whole migration story in
    one accessor: old records simply have no recorded release, which every caller
    already handles as "fall back to the blind search"."""
    if not isinstance(entry, dict):
        return {}
    rel = entry.get("releases")
    return rel if isinstance(rel, dict) else {}


def merge_ledger_entry(existing, incoming) -> dict:
    """Merge a freshly-deleted batch into whatever is already on disk for one
    series, tolerating BOTH schema versions on either side.

    * ``episodes`` — deduped union of the ``[season, episode]`` pairs (unchanged
      v1 shape, so an older build reading this ledger still works).
    * ``releases`` — per-episode identity, later write wins (a re-delete after a
      restore recorded the release that is actually gone NOW).
    * ``ts``       — the incoming timestamp; the restore cooldown measures from the
      most recent deletion, not the first.
    """
    cur = dict(existing) if isinstance(existing, dict) else {}
    inc = incoming if isinstance(incoming, dict) else {}

    episodes = [list(x) for x in (cur.get("episodes") or [])
                if isinstance(x, (list, tuple)) and len(x) == 2]
    have = {tuple(x) for x in episodes}
    for se in (inc.get("episodes") or []):
        if isinstance(se, (list, tuple)) and len(se) == 2 and tuple(se) not in have:
            episodes.append(list(se))
            have.add(tuple(se))

    releases = dict(ledger_releases(cur))
    releases.update(ledger_releases(inc))

    out = {"episodes": episodes, "ts": inc.get("ts") or cur.get("ts")}
    if releases:
        out["releases"] = releases
        out["v"] = LEDGER_SCHEMA_VERSION
    elif cur.get("v"):
        out["v"] = cur["v"]
    return out


def match_release(releases, recorded, *, min_confidence=2) -> "dict | None":
    """Pick the release from a Sonarr interactive-search result that IS the one we
    recorded at delete time, or None to fall back to the blind ``EpisodeSearch``.

    ``releases`` is the raw ``GET /release?episodeId=`` list; ``recorded`` is a
    :func:`release_record`. Each candidate scores:

      +3  its normalised title EQUALS the recorded scene_name (a certain match)
      +2  its normalised title CONTAINS the recorded scene_name (or vice versa —
          Sonarr stores sceneName without the extension, indexers vary)
      +1  same release group (case-insensitive)
      +1  same quality name
      +1  same resolution

    ``min_confidence`` (2) is the bar: a group match alone (+1) is NOT enough to
    call a release "the same one", but group + quality is, and any title match is.
    Ties break on the highest score, then on the closest ``size_bytes`` to the
    recorded size — two encodes from the same group at the same quality differ
    mostly by bitrate, and the one nearest the original is the honest restore.

    Returns None on an empty/garbled search result, an empty ``recorded``, or when
    nothing clears the bar. The caller MUST treat None as "blind search" — that
    total fallback is why a stale scene_name can never block a restore."""
    if not recorded or not isinstance(releases, list):
        return None
    want_title = _norm(recorded.get("scene_name"))
    want_group = _norm(recorded.get("release_group"))
    want_quality = _norm(recorded.get("quality_name"))
    try:
        want_res = int(recorded["resolution"]) if recorded.get("resolution") is not None else None
    except (TypeError, ValueError):
        want_res = None
    try:
        want_size = int(recorded["size_bytes"]) if recorded.get("size_bytes") is not None else None
    except (TypeError, ValueError):
        want_size = None

    best = None
    best_key = None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        score = 0
        title = _norm(rel.get("title"))
        if want_title and title:
            if title == want_title:
                score += 3
            elif want_title in title or title in want_title:
                score += 2
        if want_group and _norm(rel.get("releaseGroup")) == want_group:
            score += 1
        qual = ((rel.get("quality") or {}).get("quality") or {})
        if want_quality and _norm(qual.get("name")) == want_quality:
            score += 1
        if want_res is not None:
            try:
                if int(qual.get("resolution")) == want_res:
                    score += 1
            except (TypeError, ValueError):
                pass
        if score < min_confidence:
            continue
        try:
            size_gap = abs(int(rel.get("size") or 0) - want_size) if want_size else 0
        except (TypeError, ValueError):
            size_gap = 0
        key = (score, -size_gap)
        if best_key is None or key > best_key:
            best, best_key = rel, key
    return best
