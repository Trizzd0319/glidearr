"""Hand-verified unit tests for eval/metrics.py — every expected value computed by hand."""
import math

from scripts.managers.machine_learning.eval import metrics as m

# Canonical fixture used across tests:
#   ranked = [a, b, c, d], relevant = {a, c}
RANKED = ["a", "b", "c", "d"]
REL = {"a", "c"}


def approx(x, y, tol=1e-9):
    return abs(x - y) <= tol


def test_precision_at_k():
    assert approx(m.precision_at_k(RANKED, REL, 2), 0.5)      # 1 hit / 2
    assert approx(m.precision_at_k(RANKED, REL, 4), 0.5)      # 2 hits / 4
    assert approx(m.precision_at_k(RANKED, REL, 1), 1.0)      # a / 1
    assert approx(m.precision_at_k(RANKED, REL, 0), 0.0)      # guard
    assert approx(m.precision_at_k([], REL, 5), 0.0)


def test_recall_at_k():
    assert approx(m.recall_at_k(RANKED, REL, 2), 0.5)         # 1 of 2 relevant
    assert approx(m.recall_at_k(RANKED, REL, 4), 1.0)         # 2 of 2
    assert approx(m.recall_at_k(RANKED, set(), 4), 0.0)       # no relevant → 0


def test_hit_rate():
    assert approx(m.hit_rate_at_k(RANKED, REL, 1), 1.0)       # a at rank 1
    assert approx(m.hit_rate_at_k(["b", "d"], REL, 2), 0.0)   # none relevant


def test_average_precision():
    # i=1 a hit P=1/1; i=3 c hit P=2/3; sum=1.6667; denom=min(2,4)=2 → 0.8333…
    assert approx(m.average_precision(RANKED, REL), (1.0 + 2 / 3) / 2)
    assert approx(m.average_precision(RANKED, REL, k=2), (1.0) / 2)  # only a in top2 → 1/2
    assert approx(m.average_precision(RANKED, set()), 0.0)


def test_ndcg():
    # DCG: a@1 = 1/log2(2)=1 ; c@3 = 1/log2(4)=0.5 → 1.5
    # IDCG (R=2): 1/log2(2)+1/log2(3)=1+0.63092975 → 1.63092975
    expected = 1.5 / (1.0 + 1.0 / math.log2(3))
    assert approx(m.ndcg_at_k(RANKED, REL, 4), expected)
    assert approx(m.ndcg_at_k(RANKED, REL, 4), 0.9197207891, tol=1e-9)
    # perfect ranking → 1.0
    assert approx(m.ndcg_at_k(["a", "c", "b", "d"], REL, 4), 1.0)


def test_coverage():
    cov = m.catalog_coverage([["a", "b"], ["a", "c"]], {"a", "b", "c", "d"})
    assert approx(cov, 0.75)   # {a,b,c} of 4
    assert approx(m.catalog_coverage([], set()), 0.0)


def test_novelty():
    pop = {"a": 5, "b": 3, "c": 1, "d": 1}  # total 10
    # top2 [a,b]: -log2(.5)=1 ; -log2(.3)=1.736965594 → mean 1.368482797
    assert approx(m.novelty_at_k(RANKED, pop, 2), (1.0 + -math.log2(0.3)) / 2)
    # unseen item gets the rarity cap -log2(1/total)
    assert approx(m.novelty_at_k(["z"], pop, 1), -math.log2(1 / 10))


def test_evaluate_and_aggregate():
    r1 = m.evaluate_ranking(RANKED, REL, ks=(2, 4))
    assert approx(r1["precision@2"], 0.5)
    assert approx(r1["recall@4"], 1.0)
    assert approx(r1["map"], (1.0 + 2 / 3) / 2)
    # aggregate: mean of two identical dicts is itself; mean of 0.5 and 1.0 is 0.75
    agg = m.aggregate([{"x": 0.5}, {"x": 1.0}])
    assert approx(agg["x"], 0.75)
    assert m.aggregate([]) == {}
