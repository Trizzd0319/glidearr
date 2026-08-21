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

# ── pass-level rate limit ──────────────────────────────────────────────
#
# DIFFERENT MECHANISM FROM THE PER-ITEM BACKOFF ABOVE, and the distinction is the
# whole reason this exists.
#
#   entry_key(id, res)   "we searched THIS title at THIS resolution and found
#                        nothing smaller" - a FAILURE backoff, deliberately keyed
#                        so any change to the file retries immediately.
#   pass_key(...)        "the exhaustive step-down PASS ran" - a RATE LIMIT on
#                        the pass itself, regardless of which titles it touched.
#
# The per-item key cannot rate-limit a pass, and not because it is wrong: a title
# walking 2160 -> 1080 -> 720 legitimately uses three different keys, since the
# release landscape genuinely differs at each tier.
#
# WHAT THIS WAS BUILT FROM (2026-08-08). The pressure pass read 1546 GB free
# against a 3500 GB floor, declared CRITICAL, and admitted 523 titles to an
# exhaustive step-down. Radarr's deletion history then shows Edge of Tomorrow
# deleted SIX times in one day (4x 2160p, 2x 1080p), and 720p files - Iron Man,
# Spider-Man: Far From Home - being deleted the following day, i.e. the step-down
# REPLACEMENTS were themselves replaced. Roughly 5.6 TiB went to the recycle bin.
#
# And the deficit was never real in the way it looked: when the bin's retention
# dropped to a day and it cleared, free space went to 8.25 TB. The library was
# downgraded to satisfy pressure that would have resolved itself.
#
# A single pass is defensible. A pass that can re-run every cycle, each time
# admitting whatever the previous one just created, is a loop.

#: Hours between exhaustive step-down passes. Config: `space_stepdown_min_hours`.
#: Twelve, so a pass can still run twice a day if pressure is sustained, but the
#: output of one pass cannot become the input of the next within the same evening.
_PASS_MIN_HOURS = 12.0

#: Free GB below which the rate limit is IGNORED. Config: `space_stepdown_extreme_gb`.
#: Deliberately far under the ordinary floor (3500 GB at time of writing): this is
#: for "the array is about to stop accepting writes", not for "we are in the
#: pressure band". The 2026-08-08 incident sat at 1546 GB and would NOT have
#: qualified - which is the intended behaviour, because that deficit resolved on
#: its own.
_PASS_EXTREME_GB = 500.0


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


def pass_key(name: str = "exhaustive_stepdown") -> str:
    """Ledger key for a PASS-level record.

    Double-underscored so it cannot collide with an item key: those are ``<id>``
    or ``<id>:<res>`` and always start with a digit.
    """
    return f"__pass__:{name}"


def _pass_hours(config) -> float:
    try:
        v = float((config or {}).get("space_stepdown_min_hours") or _PASS_MIN_HOURS)
        return v if v > 0 else _PASS_MIN_HOURS
    except (TypeError, ValueError):
        return _PASS_MIN_HOURS


def _pass_extreme_gb(config) -> float:
    try:
        v = float((config or {}).get("space_stepdown_extreme_gb") or _PASS_EXTREME_GB)
        return v if v > 0 else _PASS_EXTREME_GB
    except (TypeError, ValueError):
        return _PASS_EXTREME_GB


def pass_allowed(ledger, *, free_gb=None, config=None, now=None,
                 name: str = "exhaustive_stepdown") -> dict:
    """May the exhaustive step-down pass run? ``{allowed, reason, hours_left, ...}``.

    Two ways to be allowed: the interval has elapsed, or free space is under the
    EXTREME threshold. Nothing else overrides it.

    UNKNOWN FREE SPACE DOES NOT UNLOCK THE OVERRIDE. ``free_gb=None`` means the
    caller could not establish how much room there is, and "we cannot tell" must
    not be read as "it is an emergency" - that would turn every failed space read
    into an unthrottled step-down pass, which is the loop this prevents (P-C).

    A ledger with NO record allows the pass: a first run must not be blocked by
    the absence of a prior one.
    """
    hours = _pass_hours(config)
    extreme = _pass_extreme_gb(config)

    try:
        free = float(free_gb) if free_gb is not None else None
        if free is not None and free != free:      # NaN
            free = None
    except (TypeError, ValueError):
        free = None

    entry = (ledger or {}).get(pass_key(name)) or {}
    last = entry.get("at")
    if not last:
        return {"allowed": True, "reason": "no prior pass recorded",
                "hours_left": 0.0, "hours_since": None, "extreme": False}

    try:
        then = datetime.fromisoformat(str(last))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        # An unparseable stamp is not evidence the interval elapsed, but blocking
        # forever on a corrupt record is worse than one extra pass. Allow, and say so.
        return {"allowed": True, "reason": f"unparseable last-run stamp {last!r}",
                "hours_left": 0.0, "hours_since": None, "extreme": False}

    now = now or datetime.now(timezone.utc)
    since = (now - then).total_seconds() / 3600.0
    left = max(0.0, hours - since)

    if since >= hours:
        return {"allowed": True, "reason": f"{since:.1f}h since last pass (>= {hours:.0f}h)",
                "hours_left": 0.0, "hours_since": since, "extreme": False}

    if free is not None and free < extreme:
        return {"allowed": True, "extreme": True, "hours_since": since, "hours_left": left,
                "reason": (f"EXTREME pressure override: {free:,.0f} GB free is below "
                           f"{extreme:,.0f} GB - running {left:.1f}h early")}

    return {"allowed": False, "extreme": False, "hours_since": since, "hours_left": left,
            "reason": (f"last exhaustive step-down was {since:.1f}h ago; "
                       f"{left:.1f}h left of the {hours:.0f}h interval"
                       + (f" ({free:,.0f} GB free, above the {extreme:,.0f} GB "
                          f"extreme threshold)" if free is not None else ""))}


def stamp_pass(ledger, *, now=None, name: str = "exhaustive_stepdown",
               admitted: int = 0) -> dict:
    """Record that the pass RAN. Caller persists the ledger.

    Stamped on RUN, not on success: a pass that admitted titles and then failed
    still churned the library, and is exactly what the interval exists to space out.
    """
    ledger = ledger if isinstance(ledger, dict) else {}
    ledger[pass_key(name)] = {
        "at": (now or datetime.now(timezone.utc)).isoformat(),
        "admitted": int(admitted or 0),
    }
    return ledger


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
