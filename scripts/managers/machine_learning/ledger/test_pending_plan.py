"""ledger.pending_plan — planned actions for media with NO Parquet row.

The 2026-08-20 19:23 run is the reason this exists: the change-plan grid, whose
own title reads "every planned action this run", reported `acquire 19 / -25.2 GB`
and `TOTAL +242.7 GB` while the acquisition pass in the same run funded 97 titles
worth ~312 GB. Not one appeared — there was no `> movies` sub-row under `acquire`
at all, because `decision_ledger.stamp` writes onto Parquet ROWS and a title being
added for the first time has none. A summary that omits the largest flow in the
run is worse than no summary, because it is trusted."""
from __future__ import annotations

from scripts.managers.machine_learning.ledger.pending_plan import (
    PENDING_PLAN_KEY,
    fold_into,
    pending_rows,
    record_pending,
    reset_pending,
)


class _Cache:
    """Minimal stand-in with the two methods the module uses. ``boom`` models a
    cache that raises — the module must degrade, never propagate."""

    def __init__(self, boom: bool = False):
        self.d: dict = {}
        self.boom = boom

    def get(self, k):
        if self.boom:
            raise RuntimeError("cache down")
        return self.d.get(k)

    def set(self, k, v):
        if self.boom:
            raise RuntimeError("cache down")
        self.d[k] = v


def _seeded(n_movies: int = 97, n_shows: int = 19) -> _Cache:
    """The shape of the run that motivated this module."""
    c = _Cache()
    reset_pending(c)
    for i in range(n_movies):
        record_pending(c, service="radarr", instance="standard", action="acquire",
                       title=f"Movie {i}", gb=-3.2, ext_id=1000 + i,
                       reason="watchability 71")
    for i in range(n_shows):
        record_pending(c, service="sonarr", instance="standard", action="acquire",
                       title=f"Show S01E{i:02d}", gb=-1.3, ext_id=2000 + i,
                       reason="upcoming episode in watch window")
    return c


def test_records_and_reads_back():
    c = _seeded()
    rows = pending_rows(c)
    assert len(rows) == 116
    assert {r["service"] for r in rows} == {"radarr", "sonarr"}


def test_fold_adds_the_missing_movies_subtotal():
    """The concrete defect: `acquire` had no `> movies` line at all."""
    c = _seeded()
    agg = {"acquire": [19, -25.2], "delete": [256, 281.6]}
    detail = {"acquire": {"sonarr": {"standard": [19, -25.2]}},
              "delete": {"sonarr": {"standard": [256, 281.6]}}}
    n = fold_into(agg, detail, pending_rows(c))
    assert n == 116
    assert "radarr" in detail["acquire"]                  # was absent entirely
    assert detail["acquire"]["radarr"]["standard"][0] == 97
    assert abs(detail["acquire"]["radarr"]["standard"][1] - (-310.4)) < 0.05


def test_the_total_flips_from_freeing_to_consuming():
    """+242.7 GB read as a run that FREES space; the truth was ~-68 GB."""
    c = _seeded()
    agg = {"acquire": [19, -25.2], "delete": [256, 281.6], "upgrade": [17, -13.7]}
    detail = {"acquire": {"sonarr": {"standard": [19, -25.2]}},
              "delete": {"sonarr": {"standard": [256, 281.6]}},
              "upgrade": {"sonarr": {"standard": [12, -8.8]},
                          "radarr": {"standard": [5, -4.9]}}}
    assert sum(v[1] for v in agg.values()) > 0            # the misleading total
    fold_into(agg, detail, pending_rows(c))
    assert sum(v[1] for v in agg.values()) < 0            # the true one


def test_every_parent_row_equals_the_sum_of_its_subtotals():
    """The property `plan_summary.summarize` maintains deliberately by populating
    `agg` and `detail` in one pass — the fold must not break it."""
    c = _seeded()
    agg = {"acquire": [19, -25.2]}
    detail = {"acquire": {"sonarr": {"standard": [19, -25.2]}}}
    fold_into(agg, detail, pending_rows(c))
    for action, svcs in detail.items():
        count = sum(v[0] for s in svcs.values() for v in s.values())
        gb = sum(v[1] for s in svcs.values() for v in s.values())
        assert count == agg[action][0]
        assert abs(gb - agg[action][1]) < 0.05


def test_a_reentrant_pass_overwrites_instead_of_double_counting():
    """Same discipline as `space/reclaim_ledger`, which keys by pass for this reason."""
    c = _seeded(n_movies=3, n_shows=0)
    before = len(pending_rows(c))
    record_pending(c, service="radarr", instance="standard", action="acquire",
                   title="Movie 0", gb=-3.2, ext_id=1000)
    assert len(pending_rows(c)) == before


def test_reset_clears_and_matters():
    """A stale entry ADDS last run's consumption to this run's total, making the
    array look like it is filling faster than it is — and the natural reaction to
    that is deleting media that did not need deleting."""
    c = _seeded(n_movies=2, n_shows=0)
    assert pending_rows(c)
    reset_pending(c)
    assert pending_rows(c) == []
    assert c.d[PENDING_PLAN_KEY] == {}


def test_unparseable_gb_is_dropped_not_zeroed():
    """P-C: a silent zero would understate consumption in the one table an operator
    uses to catch understated consumption."""
    c = _Cache()
    reset_pending(c)
    for bad in (None, "abc", object()):
        record_pending(c, service="radarr", instance="s", action="acquire",
                       title="X", gb=bad)
    assert pending_rows(c) == []


def test_sign_convention_is_preserved_verbatim():
    """+GiB freed, -GiB consumed — inherited from decision_ledger. Flipping it
    would invert the TOTAL's meaning."""
    c = _Cache()
    reset_pending(c)
    record_pending(c, service="radarr", instance="s", action="acquire",
                   title="A", gb=-3.25, ext_id=1)
    record_pending(c, service="radarr", instance="s", action="delete",
                   title="B", gb=8.5, ext_id=2)
    by = {r["action"]: r["gb"] for r in pending_rows(c)}
    assert by["acquire"] == -3.25 and by["delete"] == 8.5


def test_a_dead_cache_degrades_and_never_raises():
    c = _Cache(boom=True)
    reset_pending(c)
    record_pending(c, service="r", instance="s", action="acquire", title="t", gb=-1)
    assert pending_rows(c) == []


def test_garbage_rows_fold_to_nothing():
    assert fold_into({}, {}, None) == 0
    assert fold_into({}, {}, [None, {}, {"action": ""}, "x", 7]) == 0


def test_missing_service_or_instance_still_folds_under_a_marker():
    """A row with no service must not vanish — it lands under '?' so the parent
    total still reconciles."""
    agg, detail = {}, {}
    assert fold_into(agg, detail, [{"action": "acquire", "gb": -2.0}]) == 1
    assert agg["acquire"] == [1, -2.0]
    assert detail["acquire"]["?"]["?"] == [1, -2.0]
