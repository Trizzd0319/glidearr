"""eval/metrics.py — pure ranking-quality metrics for the recommendation eval harness.
==============================================================================
Phase 0 of DESIGN_recommendation_enhancement.md. These are the measuring stick:
given a ranker's ordered list of item ids and the set of *relevant* (held-out /
later-watched) ids, they quantify how well the ranking surfaces what the household
actually engaged with. They are the baseline-vs-candidate comparison any learned
re-ranker must beat.

PURE: stdlib ``math`` only — no IO, no numpy, no service/_api imports, so this module
is safe under the brain-purity guard (``scripts/hooks/brain_purity.py``). Every metric
takes plain ids; ``aggregate`` averages per-query dicts across users.

Conventions
-----------
* ``ranked``   — an ordered iterable of item ids, most-relevant first.
* ``relevant`` — an iterable/set of the item ids that count as hits.
* ``k``        — rank cutoff. precision@k divides by ``k`` (textbook P@k), so
  recommending fewer than k items is reflected (not hidden) in the score.
* Empty relevant set or empty ranking → ``0.0`` (never raises / divides by zero).
* Gains are binary (watched / not). NDCG uses the standard ``1/log2(rank+1)`` discount.
"""
from __future__ import annotations

import math
from typing import Hashable, Iterable, Sequence

Item = Hashable


def _topk(ranked: Iterable[Item], k: int | None) -> list[Item]:
    seq = list(ranked)
    return seq[:k] if k else seq


def precision_at_k(ranked: Iterable[Item], relevant: Iterable[Item], k: int) -> float:
    """|relevant ∩ top-k| / k.  Divides by the cutoff k (standard P@k)."""
    if k <= 0:
        return 0.0
    rel = set(relevant)
    top = list(ranked)[:k]
    if not top:
        return 0.0
    hits = sum(1 for it in top if it in rel)
    return hits / k


def recall_at_k(ranked: Iterable[Item], relevant: Iterable[Item], k: int) -> float:
    """|relevant ∩ top-k| / |relevant|."""
    rel = set(relevant)
    if not rel:
        return 0.0
    top = list(ranked)[:k]
    hits = sum(1 for it in top if it in rel)
    return hits / len(rel)


def hit_rate_at_k(ranked: Iterable[Item], relevant: Iterable[Item], k: int) -> float:
    """1.0 if any relevant item appears in the top-k, else 0.0 (per query)."""
    rel = set(relevant)
    return 1.0 if any(it in rel for it in list(ranked)[:k]) else 0.0


def average_precision(ranked: Iterable[Item], relevant: Iterable[Item], k: int | None = None) -> float:
    """AP(@k) = (1 / min(R, k)) · Σ_i P@i · rel(i).  MAP is the mean of this across queries."""
    rel = set(relevant)
    if not rel:
        return 0.0
    top = _topk(ranked, k)
    if not top:
        return 0.0
    hits = 0
    score = 0.0
    for i, it in enumerate(top, start=1):
        if it in rel:
            hits += 1
            score += hits / i
    denom = min(len(rel), k) if k else len(rel)
    return score / denom if denom else 0.0


def dcg_at_k(ranked: Iterable[Item], relevant: Iterable[Item], k: int) -> float:
    """Discounted cumulative gain with binary gains: Σ_{rank≤k, relevant} 1/log2(rank+1)."""
    rel = set(relevant)
    return sum(1.0 / math.log2(i + 1) for i, it in enumerate(list(ranked)[:k], start=1) if it in rel)


def idcg_at_k(num_relevant: int, k: int) -> float:
    """Ideal DCG: all relevant items ranked first, truncated at k."""
    n = min(num_relevant, k)
    return sum(1.0 / math.log2(i + 1) for i in range(1, n + 1))


def ndcg_at_k(ranked: Iterable[Item], relevant: Iterable[Item], k: int) -> float:
    """DCG@k normalised by the ideal DCG@k → in [0, 1]."""
    rel = set(relevant)
    if not rel:
        return 0.0
    idcg = idcg_at_k(len(rel), k)
    if idcg <= 0:
        return 0.0
    return dcg_at_k(ranked, rel, k) / idcg


def catalog_coverage(ranked_lists: Iterable[Iterable[Item]], catalog: Iterable[Item]) -> float:
    """Fraction of the catalog that appears across all recommendation lists (aggregate diversity)."""
    cat = set(catalog)
    if not cat:
        return 0.0
    recommended: set[Item] = set()
    for rl in ranked_lists:
        recommended.update(rl)
    return len(recommended & cat) / len(cat)


def novelty_at_k(ranked: Iterable[Item], popularity: dict, k: int, total: float | None = None) -> float:
    """Mean self-information −log2(p(item)) of the top-k (higher = more novel / less popular).

    ``popularity`` maps item id → interaction count. Items unseen in ``popularity`` are
    treated as maximally novel (capped at the rarest-possible −log2(1/total))."""
    top = list(ranked)[:k]
    if not top:
        return 0.0
    tot = total if total is not None else sum(popularity.values())
    if tot <= 0:
        return 0.0
    cap = -math.log2(1.0 / tot)
    vals = []
    for it in top:
        c = popularity.get(it, 0)
        p = c / tot if c > 0 else 0.0
        vals.append(-math.log2(p) if p > 0 else cap)
    return sum(vals) / len(vals)


# ── convenience: one query → all metrics, and cross-query aggregation ────────────
def evaluate_ranking(ranked: Iterable[Item], relevant: Iterable[Item],
                     ks: Sequence[int] = (5, 10, 20)) -> dict:
    """Compute the standard metric suite for a single query at each cutoff in ``ks``."""
    ranked = list(ranked)
    rel = set(relevant)
    out: dict = {"map": average_precision(ranked, rel)}
    for k in ks:
        out[f"precision@{k}"] = precision_at_k(ranked, rel, k)
        out[f"recall@{k}"] = recall_at_k(ranked, rel, k)
        out[f"ndcg@{k}"] = ndcg_at_k(ranked, rel, k)
        out[f"hit_rate@{k}"] = hit_rate_at_k(ranked, rel, k)
    return out


def aggregate(per_query: Sequence[dict]) -> dict:
    """Mean each metric across a list of per-query metric dicts (the MAP over APs, etc.)."""
    rows = [r for r in per_query if r]
    if not rows:
        return {}
    keys: list = []
    for r in rows:
        for kk in r:
            if kk not in keys:
                keys.append(kk)
    return {kk: sum(r.get(kk, 0.0) for r in rows) / len(rows) for kk in keys}
