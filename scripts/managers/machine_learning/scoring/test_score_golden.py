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


def test_group_d_v1_is_byte_identical_when_device_fit_v2_is_off():
    """THE FLAG-OFF GUARANTEE, proved on the golden corpus rather than on a handful of
    hand-built cases.

    ``scoring.device_fit_v2`` is default-ON, so the shipped scores are the v2 ones. What
    an operator who sets it to ``false`` is promised is that they get the PREVIOUS
    behaviour back exactly — and the fixture above, frozen before the v2 work, is the
    only artefact that can prove that. The corpus never passes a ``transcode_profile``,
    which is precisely the state ``build_transcode_profile`` returns when the flag is
    off, so ``test_score_movie_golden_byte_identical`` passing IS the proof. This test
    pins the equivalence explicitly: the flag-off resolution really does yield the None
    profile the corpus assumes, and the v2 term really is inert on that path."""
    from scripts.managers.machine_learning.scoring.device_fit import (
        build_transcode_profile, resolve_device_fit,
    )
    cfg = {"scoring": {"device_fit_v2": False}}
    settings = resolve_device_fit(cfg)
    assert settings.enabled is False
    # Even with a full household's evidence in hand, flag-off yields no profile...
    assert build_transcode_profile(
        platform_usage={"Windows": 369, "Tizen": 361},
        stream_decisions={"1": {"video_decision": "transcode", "audio_decision": "copy",
                                "subtitle_decision": "burn", "container_decision": "transcode",
                                "video_codec": "hevc", "stream_video_codec": "hevc"}},
        transcode_fingerprint=[{"device": "Windows", "fingerprint": ["u", "u", "none", "u", "lan"],
                                "direct": 318, "transcode": 48, "n": 366}],
        settings=settings) is None
    # ...and a None profile leaves D4 inert while D1/D2/D3 carry the group, which is
    # exactly the shape every vector in the fixture was frozen with.
    for case in golden_corpus(25, _SEED):
        _s, bd = score_movie(**case, transcode_profile=None, return_breakdown=True)
        assert bd["D4_transcode_risk"] == 0.0
        assert bd["_total_final"] == score_movie(**case)


if __name__ == "__main__":
    import sys
    if "--write" in sys.argv:
        _write_fixture()
        print(f"wrote {_FIXTURE} ({_N} vectors)")
    else:
        print("pass --write to (re)generate the fixture")
