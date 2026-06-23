"""Tests for the occupancy table — slot keying, copy-on-write mutators, the on-disk cap accounting,
and the queue scrub."""
from __future__ import annotations

from scripts.managers.machine_learning.discovery import occupancy as occ


def test_slot_key_uses_ownership_id():
    assert occ.slot_key("movie", 603) == "movie:603"
    assert occ.slot_key("show", 1234) == "show:1234"


def test_add_slot_is_copy_on_write_and_defaults_occupied():
    base = occ.new_occupancy((2024, 52), 14)
    out = occ.add_slot(base, "movie", 603, instance="radarr", arr_id=7, tag_id=3, title="The Matrix")
    assert base["slots"] == {}                               # original untouched (copy-on-write)
    slot = out["slots"]["movie:603"]
    assert slot["state"] == "occupied" and slot["arr_id"] == 7 and slot["tag_id"] == 3
    assert slot["title"] == "The Matrix" and out["cap"] == 14


def test_open_slots_counts_only_on_disk_states():
    o = occ.new_occupancy((2024, 52), 3)
    o = occ.add_slot(o, "movie", 1)
    o = occ.add_slot(o, "movie", 2)
    o = occ.add_slot(o, "show", 3)
    assert occ.open_slots(o) == 0                            # 3 occupied, cap 3
    o = occ.set_state(o, "movie:1", "purged")                # delete frees a slot
    assert occ.open_slots(o) == 1
    o = occ.set_state(o, "movie:2", "graduated")             # graduate frees a slot
    assert occ.open_slots(o) == 2
    o = occ.set_state(o, "show:3", "deferred")               # still on disk (seeding) → no free
    assert occ.open_slots(o) == 2
    assert [s["id"] for s in occ.active_slots(o)] == [3]


def test_set_state_no_op_on_missing_key():
    o = occ.add_slot(occ.new_occupancy((2024, 1), 5), "movie", 9)
    same = occ.set_state(o, "movie:404", "purged")
    assert same["slots"]["movie:9"]["state"] == "occupied"


def test_open_slots_never_negative_and_empty_table():
    assert occ.open_slots(None) == 0
    over = occ.new_occupancy((2024, 1), 1)
    over = occ.add_slot(over, "movie", 1)
    over = occ.add_slot(over, "movie", 2)                    # 2 on disk, cap 1
    assert occ.open_slots(over) == 0                         # clamped, not -1


def test_scrub_queue_keeps_only_current_week():
    q = [{"media": "movie", "id": 1, "week": [2024, 52]},
         {"media": "movie", "id": 2, "week": [2024, 51]},    # stale week
         "junk"]                                             # non-dict
    assert occ.scrub_queue(q, (2024, 52)) == [{"media": "movie", "id": 1, "week": [2024, 52]}]
    assert occ.scrub_queue([], (2024, 52)) == []
