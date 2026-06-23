"""'This Week in History' occupancy table — PURE slot bookkeeping shared by the acquisition (Phase 3)
and rollover (Phase 4) managers. It tracks the household's STANDING trial slots so grabs stay bounded
and the purge knows exactly what it grabbed.

A slot is keyed by OWNERSHIP id (``movie:<tmdb>`` / ``show:<tvdb>`` — never a title) and carries the
*arr ``arr_id`` + discovery ``tag_id`` the purge needs. ``state`` drives the cap:
  * ``occupied`` / ``deferred`` (still on disk — a grabbed trial, or kept pending a torrent seed
    obligation) → COUNT against the standing cap;
  * ``graduated`` (kept, tag dropped — left the trial pool), ``purged`` (deleted), ``cancelled``
    (never-completed download removed) → FREE the slot.

No I/O — the manager persists the dict at ``discovery/this_week/occupancy``. Copy-on-write (every
mutator returns a new dict) so a caller can't alias the cached table.
"""
from __future__ import annotations

import copy

ON_DISK = ("occupied", "deferred")          # states that still consume a trial slot
FREED = ("graduated", "purged", "cancelled")  # terminal states that release the slot


def slot_key(media, ext_id) -> str:
    """Stable per-trial key — the ownership id, never a title (remake/same-name collisions)."""
    return f"{media}:{ext_id}"


def new_occupancy(week, cap) -> dict:
    """A fresh table for ``week`` (a JSON-safe list, e.g. the Sun–Sat week stamp) with the standing
    ``cap``. ``week=None`` is allowed for a not-yet-stamped table."""
    return {"week": list(week) if week is not None else None, "cap": int(cap), "slots": {}}


def add_slot(occ, media, ext_id, **fields) -> dict:
    """Record a grabbed trial as ``occupied`` (copy-on-write). Extra fields (instance, arr_id, tag_id,
    title, grabbed_at, rating_key) are stored when not None. Re-adding an existing key overwrites it."""
    occ = copy.deepcopy(occ) if occ else new_occupancy(None, 0)
    slot = {"media": media, "id": ext_id, "state": "occupied"}
    slot.update({k: v for k, v in fields.items() if v is not None})
    occ.setdefault("slots", {})[slot_key(media, ext_id)] = slot
    return occ


def set_state(occ, key, state, **fields) -> dict:
    """Flip a slot's ``state`` (copy-on-write) — the rollover marks slots purged/graduated/deferred/
    cancelled. No-op when the key is absent. Extra fields (e.g. a reason) merge in when not None."""
    occ = copy.deepcopy(occ) if occ else new_occupancy(None, 0)
    slot = occ.get("slots", {}).get(key)
    if slot is not None:
        slot["state"] = state
        slot.update({k: v for k, v in fields.items() if v is not None})
    return occ


def active_slots(occ) -> list:
    """Slots still consuming disk (``occupied``/``deferred``) — what counts against the cap."""
    return [s for s in (occ.get("slots", {}) if occ else {}).values() if s.get("state") in ON_DISK]


def open_slots(occ) -> int:
    """Free trial slots = cap − on-disk slots (never negative)."""
    if not occ:
        return 0
    return max(0, int(occ.get("cap", 0)) - len(active_slots(occ)))


def scrub_queue(queue, week) -> list:
    """Drop bounded-queue entries not stamped for ``week`` (the rollover scrub). Entries are dicts
    carrying a ``week`` list; a non-dict or mismatched-week entry is dropped."""
    wk = list(week) if week is not None else None
    return [q for q in (queue or []) if isinstance(q, dict) and q.get("week") == wk]
