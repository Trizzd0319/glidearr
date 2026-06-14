"""lifecycle/household_watch.py — all-household-watched state (pure).
==============================================================================
The pure decision core of ``sonarr/cache/episode_files._resolve_household_watch_state``
(ML Step 8). Resolves whether the household has watched an episode — a delete-guard
input: a file every (or, with a quorum, most) member has watched may be grace-marked,
while one nobody-but-X has finished stays protected. The Tautulli per-user history
FETCH stays in the service; only this side-effect-free resolution lives here.

PURE — stdlib only; no HTTP, no global_cache, no service imports.

Public API:
  * resolve_household_watch(per_user, household_members, *, quorum=None)
        -> (household_watched: bool, household_last_watched_at: str | None)
"""
from __future__ import annotations


def resolve_household_watch(per_user, household_members, *, quorum=None):
    """Whether the household has watched an episode, and the latest watch timestamp.

    ``per_user`` is ``{username: latest_watch_iso | None}`` (None = watched but no
    timestamp). ``household_members`` is the configured username list (matched
    case-insensitively); an empty list means "no household tracking" → ``(True, None)``
    so the caller falls back to ordinary ``last_watched_at`` behaviour.

    DEFAULT ``quorum=None`` (or any ``quorum >= len(members)``) requires EVERY member to
    have watched — byte-identical to the original all-watched gate. A smaller ``quorum``
    (a per-member quorum, e.g. a household majority) returns ``watched=True`` once at least
    ``quorum`` members have watched, so a title most of the household has finished is no
    longer held forever by the single member who hasn't. ``household_last_watched_at`` is
    the latest timestamp among the members who DID watch (or None when none is recorded)."""
    if not household_members:
        return True, None
    per_user_lower = {k.lower(): v for k, v in per_user.items()}
    watched = [m for m in household_members if m.lower() in per_user_lower]
    total = len(household_members)
    need = total if (quorum is None or quorum >= total) else max(1, int(quorum))
    if len(watched) < need:
        return False, None
    timestamps = [
        v for m in watched if (v := per_user_lower.get(m.lower())) is not None
    ]
    return True, (max(timestamps) if timestamps else None)
