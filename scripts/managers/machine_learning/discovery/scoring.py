"""'This Week in History' watchability scoring + fail-closed floor — the shelf-eligibility gate.

The acquisition scorer (``services/acquisition/scorer.py``) scores an UNOWNED candidate unchanged; this
maps a discovery candidate onto its input shape, scores it, and applies the HARD fail-closed floor: a
candidate with NO score, or a score below the floor, is never shelf-eligible. Survivors are ranked
watchability-descending (the shelf is ordered best-first). The scorer is INJECTED so this stays unit-
testable without a cache; the manager passes a live ``AcquisitionScorer`` (built once, scores cached).
"""
from __future__ import annotations


def to_scorer_input(cand) -> dict:
    """A discovery candidate → the acquisition scorer's ``cand`` shape. ``media == "show"`` keys the
    people-affinity lookup off ``tvdb``; a movie off ``tmdb``. A discovery grab has no feed intent, so
    ``source`` defaults to a neutral marker (the scorer maps an unknown source to its 50 midpoint)."""
    ids = {}
    if cand.get("tmdb_id") is not None:
        ids["tmdb"] = cand["tmdb_id"]
    if cand.get("tvdb_id") is not None:
        ids["tvdb"] = cand["tvdb_id"]
    return {
        "type": "show" if cand.get("media") == "show" else "movie",
        "genres": cand.get("genres") or [],
        "votes": cand.get("votes"),
        "rating": cand.get("rating"),
        "year": cand.get("year"),
        "source": cand.get("source") or "discovery",
        "ids": ids,
    }


def score_and_floor(candidates, scorer, *, floor=0, sort=True) -> list:
    """Score each candidate, drop any that can't clear ``floor`` (FAIL-CLOSED: a missing/None/erroring
    total is treated as below the floor and excluded), attach the integer ``score`` (+ ``why`` from the
    scorer's ``reason``/``evidence`` when available), and return watchability-DESCENDING. ``scorer`` must
    expose ``.score(cand) -> {"total": int, "matrix": dict, "evidence": dict}``; PURE otherwise."""
    kept: list = []
    for c in candidates:
        try:
            res = scorer.score(to_scorer_input(c)) or {}
            total = res.get("total")
        except Exception:
            res, total = {}, None
        if total is None or total < floor:
            continue
        out = {**c, "score": int(total)}
        why = _why(scorer, res)
        if why:
            out["why"] = why
        kept.append(out)
    if sort:
        kept.sort(key=lambda c: c["score"], reverse=True)
    return kept


def _why(scorer, res):
    """The scorer's human-readable driver string, if it exposes ``reason(matrix, evidence=)``."""
    matrix = res.get("matrix") if isinstance(res, dict) else None
    if not matrix or not hasattr(scorer, "reason"):
        return ""
    try:
        return scorer.reason(matrix, evidence=res.get("evidence")) or ""
    except Exception:
        return ""
