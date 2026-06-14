"""Hand-verified unit tests for eval/replay.py."""
from scripts.managers.machine_learning.eval import replay as r


def ev(item, ts, completion):
    return {"item": item, "ts": ts, "completion": completion}


def test_household_state_no_cutoff():
    events = [ev("a", 1, 1.0), ev("a", 2, 0.3), ev("b", 3, 0.95), ev("c", 4, 0.1)]
    st = r.household_state_at(events)  # all plays
    assert st["watched_ids"] == {"a", "b"}          # a (1.0) and b (0.95) ≥ 0.9
    assert st["completion"] == {"a": 1.0, "b": 0.95, "c": 0.1}   # max per item
    assert st["watch_count"] == {"a": 2, "b": 1, "c": 1}


def test_household_state_with_cutoff_excludes_future():
    events = [ev("a", 1, 1.0), ev("b", 5, 0.95), ev("a", 6, 0.5)]
    st = r.household_state_at(events, cutoff=5)       # only ts < 5
    assert st["watched_ids"] == {"a"}                 # b is at ts=5, excluded
    assert st["watch_count"] == {"a": 1}              # the ts=6 replay of a excluded
    assert st["completion"] == {"a": 1.0}


def test_household_state_threshold():
    events = [ev("a", 1, 0.8)]
    assert r.household_state_at(events, watched_threshold=0.75)["watched_ids"] == {"a"}
    assert r.household_state_at(events, watched_threshold=0.9)["watched_ids"] == set()


def test_future_watched_items_excludes_prior():
    events = [ev("a", 1, 1.0), ev("b", 5, 0.95), ev("c", 6, 0.4), ev("a", 7, 1.0)]
    # cutoff 5: future completed = b (0.95) and a (re-watch at 7) ; exclude a (seen before)
    fut = r.future_watched_items(events, 5, exclude={"a"})
    assert fut == {"b"}                               # c didn't reach threshold; a excluded
    # without exclude, a's future completion counts too
    assert r.future_watched_items(events, 5) == {"a", "b"}
