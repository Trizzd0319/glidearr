"""scoring/test_score_golden.py — the byte-identity gate for score_movie.
================================================================================
Freezes the CURRENT score_movie output (final score + full breakdown) for every case in
the seeded ``golden_corpus`` into ``golden_scores.json`` and asserts any future
implementation reproduces it EXACTLY. This is the safety net the deferred score_movie
vectorisation is built behind: a batch/vectorised scorer must reproduce every frozen
score before it can replace the per-row loop.

Regenerate the fixture ONLY when an intentional scoring change lands (review the JSON diff):
    python -m scripts.managers.machine_learning.scoring.test_score_golden --write
A bare run (pytest) compares against the committed fixture and never writes.
"""
from __future__ import annotations

import json
import os

from scripts.managers.machine_learning.scoring.golden_corpus import golden_corpus
from scripts.managers.machine_learning.scoring.movie_scorer import score_movie

_FIXTURE = os.path.join(os.path.dirname(__file__), "golden_scores.json")
_N = 500
_SEED = 1_234_567


def _score_all() -> list:
    """Run score_movie(return_breakdown=True) over the seeded corpus → [[score, breakdown], ...]."""
    out = []
    for case in golden_corpus(_N, _SEED):
        score, breakdown = score_movie(**case, return_breakdown=True)
        out.append([score, breakdown])
    return out


def _write_fixture() -> None:
    with open(_FIXTURE, "w", encoding="utf-8") as fh:
        json.dump(_score_all(), fh, indent=0, sort_keys=True)


def test_score_movie_golden_byte_identical():
    assert os.path.exists(_FIXTURE), (
        "golden_scores.json missing — generate it once with "
        "`python -m scripts.managers.machine_learning.scoring.test_score_golden --write`"
    )
    with open(_FIXTURE, encoding="utf-8") as fh:
        expected = json.load(fh)
    actual = _score_all()

    assert len(actual) == len(expected) == _N, (len(actual), len(expected))
    mismatches = []
    for i, ((a_score, a_bd), exp) in enumerate(zip(actual, expected)):
        e_score, e_bd = exp[0], exp[1]
        if a_score != e_score:
            mismatches.append((i, "score", e_score, a_score))
            continue
        # breakdown: every frozen key reproduced; floats within a tight tolerance to
        # absorb only JSON float-repr noise, never a real contribution change.
        for k, ev in e_bd.items():
            av = a_bd.get(k, "<<missing>>")
            if isinstance(ev, (int, float)) and isinstance(av, (int, float)):
                if abs(av - ev) > 1e-9:
                    mismatches.append((i, k, ev, av))
            elif av != ev:
                mismatches.append((i, k, ev, av))
    assert not mismatches, f"{len(mismatches)} golden mismatch(es): {mismatches[:12]}"


def test_corpus_is_deterministic():
    # Same seed → identical corpus (the oracle is only valid if inputs are reproducible).
    a = golden_corpus(20, _SEED)
    b = golden_corpus(20, _SEED)
    assert a == b


if __name__ == "__main__":
    import sys
    if "--write" in sys.argv:
        _write_fixture()
        print(f"wrote {_FIXTURE} ({_N} vectors)")
    else:
        print("pass --write to (re)generate the fixture")
