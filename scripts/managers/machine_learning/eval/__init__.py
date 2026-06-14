"""machine_learning/eval/ — offline recommendation-evaluation core (Phase 0).
==============================================================================
Pure, brain-safe measuring stick for the recommendation work in
``../DESIGN_recommendation_enhancement.md``: ranking metrics, temporal train/test
splits, and popularity stratification. No IO and no service/_api imports live here —
a data adapter (next sub-chunk, in the service/tools layer) loads cached watch
history, calls a ranker, and feeds plain ids into these functions.

This is the baseline-vs-candidate yardstick: run the current A–G watchability
scorecard through it to get the number any learned re-ranker must beat.
"""
from .metrics import (
    aggregate,
    average_precision,
    catalog_coverage,
    dcg_at_k,
    evaluate_ranking,
    hit_rate_at_k,
    idcg_at_k,
    ndcg_at_k,
    novelty_at_k,
    precision_at_k,
    recall_at_k,
)
from .split import (
    temporal_holdout_by_time,
    temporal_holdout_fraction,
    temporal_leave_last_out,
    test_items_by_user,
)
from .stratify import (
    popularity_counts,
    popularity_segments,
    relevant_by_segment,
    segment_of,
)
from .forward import (
    aggregate_forward,
    evaluate_snapshot,
    watched_in_window,
)

__all__ = [
    "aggregate", "average_precision", "catalog_coverage", "dcg_at_k",
    "evaluate_ranking", "hit_rate_at_k", "idcg_at_k", "ndcg_at_k", "novelty_at_k",
    "precision_at_k", "recall_at_k",
    "temporal_holdout_by_time", "temporal_holdout_fraction",
    "temporal_leave_last_out", "test_items_by_user",
    "popularity_counts", "popularity_segments", "relevant_by_segment", "segment_of",
    "aggregate_forward", "evaluate_snapshot", "watched_in_window",
]
