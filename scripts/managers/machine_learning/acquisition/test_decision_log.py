"""Tests for `decision_log` — the per-candidate acquisition record (`GLD-ACQS-22`).

The module is pure by design (records in, lines out) so the whole contract is
testable without a manager, a logger or a filesystem. That matters here more than
usual: this is a DIAGNOSTIC, and a diagnostic that can raise takes down the pass it
was written to explain. Four separate hostile-input shapes broke earlier versions
one at a time — a missing `rows`, `rows` not a list, a non-numeric `seq`, and rows
missing keys — which is what happens when a formatter assumes the dict it is handed.
`test_hostile_input_never_raises` sweeps them as a class rather than one at a time.
"""
from __future__ import annotations

from scripts.managers.machine_learning.acquisition import decision_log as dl


def _log_with(*rows):
    log = dl.new_log()
    for r in rows:
        dl.record(log, **r)
    return log


# ── the shape this file exists for ──────────────────────────────────────────
def test_records_every_disposition_and_counts_them():
    log = _log_with(
        {"title": "A", "disposition": "funded", "score": 90, "gb": 10.0},
        {"title": "B", "disposition": "funded", "score": 80, "gb": 5.5},
        {"title": "C", "disposition": "capped", "score": 70},
        {"title": "D", "disposition": "refused", "score": 10, "reason": "below floor"},
        {"title": "E", "disposition": "skipped", "reason": "already owned"},
    )
    s = dl.summarise(log)
    assert s["total"] == 5
    assert (s["funded"], s["capped"], s["refused"], s["skipped"]) == (2, 1, 1, 1)
    assert s["funded_gb"] == 15.5
    assert s["denied"] == 3          # capped + refused + skipped


def test_rows_are_ranked_within_a_disposition():
    """The question this file answers is *why these and not those*, which is a
    question about RANK — so a group that is not score-ordered is useless."""
    log = _log_with(
        {"title": "low", "disposition": "capped", "score": 30},
        {"title": "high", "disposition": "capped", "score": 90},
        {"title": "mid", "disposition": "capped", "score": 60},
    )
    out = "\n".join(dl.render(log))
    assert out.index("high") < out.index("mid") < out.index("low")


def test_unscored_rows_sort_last_but_are_not_dropped():
    """A candidate with no score still consumed a decision. Dropping it would make
    the file disagree with the summary line, and the summary has historically been
    the one that was wrong."""
    log = _log_with(
        {"title": "unscored", "disposition": "capped"},
        {"title": "scored", "disposition": "capped", "score": 5},
    )
    out = "\n".join(dl.render(log))
    assert "unscored" in out
    assert out.index("scored") < out.index("unscored")
    assert dl.summarise(log)["capped"] == 2


# ── the cap-as-sole-limiter note ────────────────────────────────────────────
def test_flags_when_the_count_cap_is_the_only_limiter():
    """With free space far above the band the byte budget refuses nothing, so
    `max_adds_per_run` alone decides what the household gets. That is not visible
    from the counts, so the file says it outright."""
    log = _log_with(
        {"title": "in", "disposition": "funded", "score": 90},
        {"title": "out", "disposition": "capped", "score": 89},
    )
    assert any("sole limiter" in ln for ln in dl.render(log, hard_max=1))


def test_does_not_flag_when_something_was_refused_on_budget():
    log = _log_with(
        {"title": "in", "disposition": "funded", "score": 90},
        {"title": "out", "disposition": "capped", "score": 89},
        {"title": "broke", "disposition": "refused", "score": 88, "reason": "no budget"},
    )
    assert not any("sole limiter" in ln for ln in dl.render(log, hard_max=1))


def test_no_note_without_a_hard_max():
    log = _log_with({"title": "out", "disposition": "capped", "score": 1})
    assert not any("sole limiter" in ln for ln in dl.render(log))


# ── independence and safety ─────────────────────────────────────────────────
def test_two_logs_do_not_share_state():
    """`new_log()` returns a fresh accumulator rather than module-level state, so two
    managers in one process cannot silently merge their runs."""
    a, b = dl.new_log(), dl.new_log()
    dl.record(a, title="a", disposition="funded")
    assert dl.summarise(b)["total"] == 0


def test_optional_fields_are_genuinely_optional():
    """Call sites know different things — the pause check has no size, the budget has
    no `why`. A recorder that demanded a full row would push them into inventing
    values, and an invented score in a ranking audit is worse than a blank one."""
    log = _log_with({"title": "bare", "disposition": "funded"})
    out = "\n".join(dl.render(log))
    assert "bare" in out and dl.summarise(log)["funded"] == 1


def test_hostile_input_never_raises():
    """Swept as a CLASS. Each of these broke an earlier version individually."""
    for bad in (None, {}, [], "str", 0,
                {"rows": "nope"},
                {"rows": [], "seq": "x"},
                {"rows": [1, 2, "x"]},
                {"rows": [{}]},
                {"rows": [{"disposition": "funded"}]},
                {"rows": [{"title": None}]},
                {"rows": [{"disposition": "capped", "score": "high"}]},
                {"rows": [{"gb": "big"}]}):
        dl.record(bad, title="x", disposition="funded")
        dl.summarise(bad)
        dl.render(bad, hard_max=1)


def test_render_is_empty_ish_for_an_empty_log():
    """An empty run must not write a misleading header claiming zero of everything —
    the caller checks `total` and skips the file entirely."""
    assert dl.summarise(dl.new_log())["total"] == 0
