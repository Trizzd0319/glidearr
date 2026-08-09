"""stepdown_cooldown.py — don't re-probe a title that has no smaller release.

WHY. A step-down that finds nothing smaller re-probed on the VERY NEXT run, forever. On a
real library that is a large standing set — concert films, very recent releases, obscure
back-catalogue — 31 titles in one observed case simply have no smaller encode at any
indexer. Every run paid an interactive search for each of them, and before the grab-result
check landed, a failed grab could delete a file it was never going to replace.

SHAPE. Deliberately the same ledger idiom ``legacy_regrab`` already uses: a dict in
``global_cache``, ``{key: {"at": iso, "attempts": n}}``, read with a ``_recent``-style
check. One convention across the codebase rather than two.

KEYED BY THE ITEM, NOT THE FILE. This is the one place it diverges from legacy_regrab, and
deliberately: legacy_regrab keys on ``episode_file_id`` because it re-grabs files that stay
put. A step-down DELETES the file, so on the ``grab_failed`` path the id is gone and a
file-keyed entry would be orphaned exactly when the backoff matters most. Movies key on
tmdb id, episodes on episode id — both survive the replacement.

ESCALATING, NEVER PERMANENT. base × attempts, capped, so one bad night costs a week and a
title that has failed repeatedly backs off to about a quarter. An indexer that gains the
release later still finds it; it just isn't asked every hour. Any success clears the entry.

PURE — no I/O. The caller loads the ledger, passes it in, and persists it.
"""
from __future__ import annotations

from datetime import datetime, timezone

_BASE_DAYS = 7        # config: space_downgrade_retry_days
_MAX_DAYS = 90        # ceiling, so nothing is written off permanently


def ledger_key(service: str, instance: str) -> str:
    """Cache key for one service/instance ledger, e.g. ``radarr/ultra/stepdown_cooldown``."""
    return f"{service}/{instance}/stepdown_cooldown"


def entry_key(item_id, resolution=None) -> str:
    """Ledger entry key: ``<id>`` or ``<id>:<resolution>``.

    SELF-INVALIDATING. A "no smaller release" finding is only true for the resolution the
    file was AT when it was made: a title at 2160p has step-down options a title at 720p
    does not. Folding the resolution into the key means any change to the file - an
    upgrade, a manual grab, Radarr's own cutoff upgrade, a restore from the recycle bin -
    lands on a fresh key and the title is immediately retryable, while a DIFFERENT file at
    the SAME resolution correctly keeps its backoff (the release landscape at that tier
    has not changed).

    Clearing on the upgrade path alone would only have covered upgrades this pass
    performed, missing every other way a file can change underneath us.

    ``resolution=None`` falls back to the bare id, so a caller that cannot determine it
    still gets a working (if coarser) backoff.
    """
    try:
        r = int(resolution) if resolution is not None else None
    except (TypeError, ValueError):
        r = None
    return f"{item_id}:{r}" if r else f"{item_id}"


def _base_days(config) -> float:
    try:
        v = float((config or {}).get("space_downgrade_retry_days") or _BASE_DAYS)
        return v if v > 0 else _BASE_DAYS
    except (TypeError, ValueError):
        return _BASE_DAYS


def cooldown_left(ledger, key, config=None, now=None) -> float:
    """Days remaining before ``key`` may be re-attempted. ``0.0`` = eligible now.

    A malformed or unparseable entry returns 0.0: a backoff bug must never silently
    freeze part of the library.
    """
    try:
        ent = (ledger or {}).get(str(key))
        if not ent:
            return 0.0
        n = max(1, int(ent.get("attempts") or 1))
        window = min(_base_days(config) * n, _MAX_DAYS)
        at = datetime.fromisoformat(str(ent.get("at")).strip().replace("Z", "+00:00"))
        elapsed = ((now or datetime.now(tz=timezone.utc)) - at).total_seconds() / 86400.0
        return max(0.0, window - elapsed)
    except Exception:
        return 0.0


def stamp_failure(ledger, key, now=None) -> int:
    """Record a failed step-down for ``key``; returns the new attempt count.

    Mutates ``ledger`` in place — the caller persists it once per run rather than per item.
    """
    if ledger is None:
        return 0
    k = str(key)
    ent = ledger.get(k) or {}
    try:
        n = int(ent.get("attempts") or 0) + 1
    except (TypeError, ValueError):
        n = 1
    ledger[k] = {"at": (now or datetime.now(tz=timezone.utc)).isoformat(), "attempts": n}
    return n


def clear(ledger, key) -> None:
    """A successful step-down wipes the slate — the title is immediately retryable."""
    if ledger is not None:
        ledger.pop(str(key), None)


def wait_days(ledger, key, config=None) -> float:
    """The FULL backoff window currently applied to ``key`` (for log lines), not the
    remaining time. 0.0 when the key has no entry."""
    ent = (ledger or {}).get(str(key))
    if not ent:
        return 0.0
    try:
        n = max(1, int(ent.get("attempts") or 1))
    except (TypeError, ValueError):
        n = 1
    return min(_base_days(config) * n, _MAX_DAYS)


def prune(ledger, config=None, now=None) -> int:
    """Drop entries whose window has fully elapsed. Returns how many were removed.

    Keeps the artifact from growing without bound across years of runs; an expired entry
    carries no information, since the next attempt starts the count again.
    """
    if not ledger:
        return 0
    dead = [k for k in list(ledger) if cooldown_left(ledger, k, config, now) <= 0.0]
    for k in dead:
        ledger.pop(k, None)
    return len(dead)
