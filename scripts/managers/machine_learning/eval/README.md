# eval

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **eval**

**Package** — `scripts.managers.machine_learning.eval`
**Run position** — Not in the automated run. Driven by operator tools (`ml_forward_validation.py`, `eval_recommender.py`, `ml_simulate.py`).
**One-liner** — The honest-measurement harness: leakage-free replay of pre-cutoff household state, forward validation of watchlist intent, and the metric machinery for both.

---

## Purpose

Glidearr has **no live feedback signal**. Nothing tells it a deletion was wrong or
a recommendation was good. The only way to improve the model is to reconstruct
history and ask what a different model *would* have done.

That is harder than it sounds, because the scorer reads watch history. Scoring a
held-out item with the full history **leaks the answer** — Group A completion and
rewatch, Group B affinity and the watched-set all encode the outcome you are
trying to predict.

This package solves two measurement problems:

| Problem | Module |
|---|---|
| Retrospective scoring leaks the answer | [`replay.py`](./replay.py) — rebuild state as of cutoff `T` |
| The watchlist **cannot** be measured retrospectively at all | [`forward.py`](./forward.py) — snapshot now, measure later |

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`replay.py`](./replay.py) | `household_state_at(events, cutoff)` → watched_ids / completion / watch_count before `T`; `future_watched_items` → the held-out relevant set | ✅ Implemented |
| [`forward.py`](./forward.py) | `watched_in_window`, `evaluate_snapshot` (hit_rate / base_rate / **lift**), `aggregate_forward` | ✅ Implemented |
| [`split.py`](./split.py) | Time-aware splits | ✅ Implemented |
| [`stratify.py`](./stratify.py) | Stratified sampling | ✅ Implemented |
| [`metrics.py`](./metrics.py) · [`np_metrics.py`](./np_metrics.py) | Ranking metrics, pure and numpy-backed | ✅ Implemented |

## Test coverage

[`test_replay.py`](./test_replay.py) · [`test_forward.py`](./test_forward.py) ·
[`test_split.py`](./test_split.py) · [`test_stratify.py`](./test_stratify.py) ·
[`test_metrics.py`](./test_metrics.py)

---

## Leakage-free replay

```
events (timestamped plays)
        │
        ├─ before T ──► household_state_at  → watched_ids
        │                                     completion  (MAX per item)
        │                                     watch_count (plays per item)
        │                                        │
        │                                        └─► feed the scorer
        │
        └─ at/after T ──► future_watched_items → held-out relevant set
                          (excludes pre-T watches, so the metric rewards
                           predicting genuinely NEW watches, not rewatches)
```

Two deliberate choices:

- **Static inputs are not replayed.** Ratings, credits, collection and genres come
  straight from the `movie_files` row — they are time-invariant, so replaying them
  would be effort without effect.
- **Affinity is recomputed by calling the *real* production function.** The service
  adapter invokes `compute_genre_affinity` on pre-`T` entries rather than
  reimplementing it here, *"so the baseline stays faithful."*

That second point is the one that keeps the harness honest. A reimplemented
affinity would measure a model that does not exist.

---

## Forward validation — and why lift is the whole point

The watchlist cannot be evaluated retrospectively: it is forward-looking, watched
items typically **leave** it, and there are no historical snapshots. So
`current-watchlist ∩ past-held-out ≈ 0`.

```
1. snapshot the watchlist union at T   → plex/watchlist/snapshot/{ts}, retention-bounded
2. later: of the items on the list at T, how many were watched within window W?
3. is that hit-rate a LIFT over the base watch-rate of comparable
   non-watchlisted owned items?

   lift = hit_rate / base_rate       > 1 ⇒ real signal
                                     ≈ 1 ⇒ none
```

> *"Without the lift the hit-rate is meaningless — a household that watches
> everything 'hits' trivially."*

The counterfactual pool is `owned ∖ predicted`. A snapshot with no predictions or
no base pool contributes nothing to the aggregate rather than skewing it.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Drivers:** [`support/tools/ml_forward_validation.py`](../../../support/tools/ml_forward_validation.py) · [`eval_recommender.py`](../../../support/tools/eval_recommender.py) · [`ml_simulate.py`](../../../support/tools/ml_simulate.py)
- **Related:** [`challenger/`](../challenger/) · [`ledger/`](../ledger/) · [`labels/`](../labels/)
