"""eval/split.py — pure temporal train/test splits for the recommendation eval harness.
==============================================================================
Phase 0 of DESIGN_recommendation_enhancement.md. Recommendation eval must split by
TIME, never randomly: you train/score on the past and test on the future, or the
metric just rewards leakage (the watchability scorecard reads watch history, so a
random split lets it "predict" watches it was handed). These helpers produce the
held-out future from timestamped watch events.

PURE: stdlib only — no IO, no service/_api imports (brain-purity safe).

Event shape: each event is a mapping with at least a user key, an item key, and a
sortable timestamp key (epoch int/float or ISO string both sort correctly). Key
names are parameters so this binds to whatever the data adapter emits.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Hashable, Mapping, Sequence

Event = Mapping
Item = Hashable
User = Hashable


def temporal_leave_last_out(
    events: Sequence[Event], *, user_key: str = "user", item_key: str = "item", time_key: str = "ts"
) -> tuple[list[Event], dict[User, Item]]:
    """Per-user leave-last-out: each user's chronologically LAST item becomes the test
    target; everything earlier is train. Users with <2 events can't be tested — all
    their events stay in train and they contribute no test query.

    Returns ``(train_events, {user: held_out_item})``."""
    by_user: dict[User, list[Event]] = defaultdict(list)
    for e in events:
        by_user[e[user_key]].append(e)

    train: list[Event] = []
    test: dict[User, Item] = {}
    for user, evs in by_user.items():
        evs_sorted = sorted(evs, key=lambda e: e[time_key])
        if len(evs_sorted) < 2:
            train.extend(evs_sorted)
            continue
        *head, last = evs_sorted
        train.extend(head)
        test[user] = last[item_key]
    return train, test


def temporal_holdout_fraction(
    events: Sequence[Event], *, frac: float = 0.2, time_key: str = "ts"
) -> tuple[list[Event], list[Event]]:
    """Global temporal split: the most-recent ``frac`` of events (by time) are test,
    the earlier ``1-frac`` are train. ``frac`` is clamped to [0, 1]."""
    frac = min(1.0, max(0.0, frac))
    evs = sorted(events, key=lambda e: e[time_key])
    cut = int(round(len(evs) * (1.0 - frac)))
    return evs[:cut], evs[cut:]


def temporal_holdout_by_time(
    events: Sequence[Event], split_time, *, time_key: str = "ts"
) -> tuple[list[Event], list[Event]]:
    """Split at an explicit boundary: events with ts < ``split_time`` are train, ts >=
    ``split_time`` are test. ``split_time`` must be comparable to the timestamp values."""
    train = [e for e in events if e[time_key] < split_time]
    test = [e for e in events if e[time_key] >= split_time]
    return train, test


def test_items_by_user(
    test_events: Sequence[Event], *, user_key: str = "user", item_key: str = "item"
) -> dict[User, set]:
    """Collapse a list of held-out test events into {user: {relevant item ids}} —
    the ``relevant`` argument the metrics consume, for the fractional/by-time splits."""
    out: dict[User, set] = defaultdict(set)
    for e in test_events:
        out[e[user_key]].add(e[item_key])
    return dict(out)
