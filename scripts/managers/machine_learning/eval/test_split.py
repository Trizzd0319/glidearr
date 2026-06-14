"""Hand-verified unit tests for eval/split.py."""
from scripts.managers.machine_learning.eval import split as s


def ev(user, item, ts):
    return {"user": user, "item": item, "ts": ts}


def test_leave_last_out_basic():
    events = [ev("u1", "i1", 1), ev("u1", "i3", 3), ev("u1", "i2", 2)]  # out of order
    train, test = s.temporal_leave_last_out(events)
    # sorted by ts → i1,i2,i3 ; last (i3) held out
    assert test == {"u1": "i3"}
    assert sorted(e["item"] for e in train) == ["i1", "i2"]


def test_leave_last_out_multi_user_and_singletons():
    events = [
        ev("u1", "a", 1), ev("u1", "b", 2),
        ev("u2", "c", 5),                      # singleton → no test, stays in train
        ev("u3", "d", 1), ev("u3", "e", 9), ev("u3", "f", 4),
    ]
    train, test = s.temporal_leave_last_out(events)
    assert test == {"u1": "b", "u3": "e"}      # u2 absent (only 1 event)
    train_items = sorted(e["item"] for e in train)
    assert train_items == ["a", "c", "d", "f"]  # b, e held out; c stays (singleton)


def test_holdout_fraction():
    events = [ev("u", f"i{n}", n) for n in range(10)]  # ts 0..9
    train, test = s.temporal_holdout_fraction(events, frac=0.2)
    assert len(train) == 8 and len(test) == 2
    assert [e["item"] for e in test] == ["i8", "i9"]   # latest 20%
    # clamping
    assert len(s.temporal_holdout_fraction(events, frac=2.0)[1]) == 10
    assert len(s.temporal_holdout_fraction(events, frac=-1.0)[1]) == 0


def test_holdout_by_time():
    events = [ev("u", f"i{n}", n) for n in range(5)]   # ts 0..4
    train, test = s.temporal_holdout_by_time(events, 3)
    assert [e["item"] for e in train] == ["i0", "i1", "i2"]
    assert [e["item"] for e in test] == ["i3", "i4"]


def test_items_by_user():
    test_events = [ev("u1", "a", 1), ev("u1", "b", 2), ev("u2", "c", 3)]
    rel = s.test_items_by_user(test_events)
    assert rel == {"u1": {"a", "b"}, "u2": {"c"}}
