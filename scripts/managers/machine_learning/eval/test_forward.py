"""Hand-verified unit tests for eval/forward.py (watchlist forward validation)."""
from scripts.managers.machine_learning.eval import forward as f


def ev(item, ts, c=1.0):
    return {"item": item, "ts": ts, "completion": c}


def test_watched_in_window():
    events = [ev("a", 5), ev("b", 10), ev("c", 15, 0.3), ev("d", 25)]
    # window (0, 20]: a, b complete; c below threshold; d out of window
    assert f.watched_in_window(events, 0, 20) == {"a", "b"}
    assert f.watched_in_window(events, 0, 20, watched_threshold=0.2) == {"a", "b", "c"}
    assert f.watched_in_window(events, 10, 30) == {"d"}      # half-open: b at t=10 excluded


def test_evaluate_snapshot_lift():
    # watchlist predicted {a,b,c}; owned universe {a..f}; watched in window {a,b,e}
    r = f.evaluate_snapshot(predicted={"a", "b", "c"},
                            owned_universe={"a", "b", "c", "d", "e", "f"},
                            watched={"a", "b", "e"})
    assert r["n_predicted"] == 3 and r["n_hits"] == 2
    assert r["hit_rate"] == 2 / 3                       # a,b of {a,b,c}
    # base pool = owned∖predicted = {d,e,f}; base hits = {e} → 1/3
    assert r["n_base"] == 3 and abs(r["base_rate"] - 1 / 3) < 1e-9
    assert abs(r["lift"] - (2 / 3) / (1 / 3)) < 1e-9     # lift = 2.0 → watchlist predicts well


def test_evaluate_snapshot_no_signal():
    # watchlist no better than base → lift ≈ 1
    r = f.evaluate_snapshot(predicted={"a", "b"},
                            owned_universe={"a", "b", "c", "d"},
                            watched={"a", "c"})          # hit_rate .5, base {c,d}: .5
    assert r["hit_rate"] == 0.5 and r["base_rate"] == 0.5 and r["lift"] == 1.0


def test_evaluate_snapshot_edges():
    assert f.evaluate_snapshot([], {"a"}, {"a"})["hit_rate"] is None     # no predictions
    # predicted == owned → empty base pool → base_rate/lift None (can't compare)
    r = f.evaluate_snapshot({"a"}, {"a"}, {"a"})
    assert r["hit_rate"] == 1.0 and r["base_rate"] is None and r["lift"] is None


def test_aggregate():
    rows = [
        f.evaluate_snapshot({"a", "b"}, {"a", "b", "c", "d"}, {"a", "c"}),   # lift 1.0
        f.evaluate_snapshot({"a", "b", "c"}, {"a", "b", "c", "d", "e", "f"}, {"a", "b", "e"}),  # lift 2.0
    ]
    agg = f.aggregate_forward(rows)
    assert agg["n_snapshots"] == 2
    assert abs(agg["lift"] - 1.5) < 1e-9                 # mean of 1.0, 2.0
    assert agg["total_predicted"] == 5 and agg["total_hits"] == 3   # (1 hit) + (2 hits)
    assert f.aggregate_forward([])["n_snapshots"] == 0
