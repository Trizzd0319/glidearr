# eval — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **eval**

**Package** — `scripts.managers.machine_learning.eval`
**Status** — ✅ Implemented · 🟡 Uses its own watched threshold, not `lifecycle/watched_definition.py`
**Related** — [README.md](./README.md) · [`DESIGN_recommendation_enhancement.md`](../DESIGN_recommendation_enhancement.md) · [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md)

---

## 1. Problem statement

Every other package in the brain makes decisions. This one is the only thing that
can tell you whether those decisions were any good — and it operates under a hard
constraint: **there is no feedback signal.**

Nothing reports that a deletion was wrong. Nobody rates a recommendation. The
error surfaces months later as "where did that film go?", by which point the
cause is unrecoverable.

So the only measurement available is counterfactual reconstruction, and that runs
straight into **leakage**. The watchability scorer reads watch history: Group A
completion and rewatch, Group B affinity, the watched-set. Score a held-out item
with the full history and you have told the model the answer. The measured
performance is meaningless — flatteringly so.

A second, separate problem: **the watchlist cannot be measured retrospectively at
all.** It is forward-looking, items leave it once watched, and no historical
snapshots exist. `current-watchlist ∩ past-held-out ≈ 0` — not a weak signal, an
empty intersection. That blind spot needs a different method entirely.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | No leakage — a held-out item is scored against state that predates it. |
| G2 | Measure the **real** model, not a reimplementation of it. |
| G3 | Reward predicting *new* watches, not rewatches. |
| G4 | Any hit-rate is reported against a counterfactual base rate. |
| G5 | Handle the watchlist blind spot with forward validation. |
| G6 | Pure — stdlib only, brain-purity safe. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Running in the automated pipeline | Operator-invoked. Measurement is deliberate, not continuous. |
| N2 | Promoting a model | `challenger/` shadows; promotion is a human decision (`GLD-ML-08`). |
| N3 | Reimplementing scoring | G2 — the harness calls production code. |
| N4 | Statistical significance testing | Single household, small n. Lift is directional, not inferential. |

---

## 3. Architecture

### 3.1 Two measurement modes

```
RETROSPECTIVE (replay.py)              FORWARD (forward.py)
   for signals present in history         for the watchlist blind spot

   events before T ──► state at T         snapshot watchlist at T
        │                                        │
   score held-out with THAT state          wait window W
        │                                        │
   compare against                         watched_in_window(t0, t1]
   future_watched_items(T)                        │
                                           hit_rate vs base_rate → LIFT
```

### 3.2 What is and isn't replayed

| Input | Replayed? | Why |
|---|---|---|
| `watched_ids` | ✅ | Time-dependent, and the strongest leak |
| `completion` (max per item) | ✅ | Time-dependent |
| `watch_count` | ✅ | Time-dependent |
| Genre affinity | ✅ — **by calling production `compute_genre_affinity`** | G2 |
| Ratings, credits, collection, genres | ❌ | Time-invariant; replaying is effort without effect |

Calling the real `compute_genre_affinity` rather than reimplementing it is the
decision that keeps the harness honest. A local reimplementation would measure a
model that does not exist, and would drift silently the moment the real one
changed.

### 3.3 Max-completion semantics

Per item, replay keeps the **maximum** completion across plays — *"a movie is
'watched' if any single play reached the threshold"* — and counts plays
separately for rewatch. That matches production's per-play verdict aggregated to
per-item, which is the right shape.

### 3.4 Excluding rewatches from the relevant set (G3)

`future_watched_items(..., exclude=watched_before_T)` drops items already watched
before the cutoff, *"so the metric rewards predicting genuinely NEW watches, not
re-watches."*

Without it, a recommender that simply surfaces the household's favourites would
score near-perfectly while providing no discovery value at all.

### 3.5 Lift, not hit-rate (G4)

```python
hit_rate  = |predicted ∩ watched| / |predicted|
base_pool = owned ∖ predicted                       # the counterfactual
base_rate = |base_pool ∩ watched| / |base_pool|
lift      = hit_rate / base_rate
```

> *"Without the lift the hit-rate is meaningless — a household that watches
> everything 'hits' trivially."*

Degenerate cases return `None` rather than a misleading number: no predictions ⇒
all metrics `None`; no base pool ⇒ `base_rate` and `lift` `None`. `aggregate_forward`
then drops those rows entirely rather than averaging around them.

### 3.6 🟡 A second definition of "watched"

[`lifecycle/watched_definition.py`](../lifecycle/README.md) exists explicitly to
be *"the ONE definition of watched"*, created because two producers had inlined
their own and drifted. Its default bar is **85** (`DEFAULT_WATCHED_PERCENT = 85.0`),
and its **preferred** source is Tautulli's own `watched_status` verdict, with
`percent_complete` only as a fallback.

This package does neither:

| | `lifecycle/watched_definition.py` | `eval/replay.py` + `eval/forward.py` |
|---|---|---|
| Preferred source | Tautulli `watched_status` | — not consulted |
| Threshold | `percent_complete ≥ 85` (configurable, legacy alias) | `completion ≥ **0.9**` |
| Resolution | `resolve_watched_percent(config)` | hardcoded default parameter |

Both modules default `watched_threshold: float = 0.9`, and neither imports
`lifecycle.watched_definition`.

**Why this matters:** replay's whole purpose is reconstructing the state
production *would have had*. If it reconstructs it under a stricter bar (90 % vs
85 %) and ignores the operator's Tautulli verdict entirely, the "leakage-free
baseline" is a baseline for a household that does not exist. Measured effects
would be attributed to the model when they came from the definition.

**Caveat before treating this as a defect:** `watched_threshold` is a keyword
parameter, so callers may well pass the resolved production value. Confirming
whether the drivers do so is `GLD-EVA-01`, and it is a small read.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Rebuild state at `T` rather than masking features | G1 — masking leaves derived signals (affinity) leaking | Feature masking |
| D2 | Call production `compute_genre_affinity` | G2 — a reimplementation measures a model that does not exist and drifts silently | Reimplement locally |
| D3 | Skip replaying time-invariant inputs | Effort without effect | Replay everything |
| D4 | Max completion per item, plays counted separately | Matches production aggregation | Mean completion |
| D5 | Exclude pre-`T` watches from the relevant set | G3 — otherwise surfacing favourites scores perfectly and discovers nothing | Include rewatches |
| D6 | Report lift, not raw hit-rate | G4 — a high base rate makes any hit-rate look good | Hit-rate alone |
| D7 | Degenerate cases return `None`, not 0 | A zero would average into the aggregate as a real result | Return 0 |
| D8 | Forward validation for the watchlist | G5 — retrospective measurement is structurally impossible here | Skip the watchlist |
| D9 | Snapshots are retention-bounded | Unbounded snapshot history would grow without limit | Keep forever |
| D10 | Operator-invoked, not pipeline | N1 — measurement is a deliberate act with a chosen cutoff | Run every pass |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No event at or after `T` influences the state fed to the scorer. |
| I2 | Affinity is computed by production code, never reimplemented here. |
| I3 | The held-out relevant set excludes items watched before `T`. |
| I4 | A hit-rate is never reported without its base rate. |
| I5 | Degenerate snapshots contribute nothing to aggregates. |
| I6 | This package performs no I/O. |
| I7 | Windows are half-open `(t0, t1]`. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| **Eval threshold ≠ production threshold** | **None** | Baseline reconstructs a household that does not exist | 🟡 Model effects misattributed | ❌ **None** |
| **Tautulli `watched_status` ignored in replay** | **None** | Only the fallback leg is reproduced | 🟡 Same | ❌ **None** |
| No watchlist snapshots yet | Empty input | `aggregate_forward` → `{"n_snapshots": 0}` | Honest | ✅ |
| Snapshot aged out of retention | Missing file | That window unmeasurable | 🟡 Silent gap | ❌ **None** |
| Every owned item watched in W | `base_rate` ≈ 1 | `lift` ≈ 1 — correctly reports no signal | Correct | ✅ |
| No base pool | Guard | `None`, row dropped | Correct | ✅ |
| Scorer clock-reads (F3/G4) during replay | **None** | Replay is not byte-exact across days | 🟡 Undermines the harness | ❌ **None** |
| Scoring axis translated since the events | **None** | Replayed scores incomparable to historical ones | 🟡 | ❌ **None** |

**Rows 7 and 8 are the ones that limit this harness's reach**, and both are
already tracked elsewhere: `GLD-SCO-02` (inject `now`) and `GLD-SCO-03` /
`GLD-LIK-09` (stamp the axis version). Replay is only as trustworthy as the
determinism of the thing it replays.

---

## 7. Configuration surface

No config keys. Every parameter is passed by the caller:

| Parameter | Default | Effect |
|---|---|---|
| `watched_threshold` | **0.9** | Completion bar. 🟡 Diverges from production's 85 (§3.6) |
| `cutoff` / `t0` / `t1` | — | Time boundaries |
| `item_key` / `time_key` / `completion_key` | `item` / `ts` / `completion` | Event field names |
| `exclude` | `None` | Items dropped from the relevant set |

---

## 8. Implemented capabilities

- ✅ Leakage-free household-state reconstruction at an arbitrary cutoff
- ✅ Production-affinity recomputation rather than reimplementation
- ✅ Rewatch-excluded held-out relevant set
- ✅ Forward validation for the watchlist blind spot
- ✅ Lift against a counterfactual base pool
- ✅ Honest degenerate-case handling (`None`, not 0)
- ✅ Multi-snapshot aggregation dropping uninformative rows
- ✅ Time-aware splits and stratified sampling
- ✅ Pure and numpy-backed metric implementations
- ✅ Five test modules

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-EVA-01` | 🔴 **Resolve the watched-threshold divergence** — confirm whether drivers pass production's resolved bar; if not, import `lifecycle.watched_definition` and consult `watched_status` | §3.6: the leakage-free baseline may be reconstructing a household that never existed. Small read to confirm | S | — |
| `GLD-EVA-02` | **Assert eval and production agree on "watched"** at harness start, and print the effective bar | Turns §6 rows 1–2 from silent into stated | S | `GLD-EVA-01` |
| `GLD-EVA-03` | **Record snapshot coverage** — which windows are measurable, which aged out | §6 row 4: retention silently creates gaps in the forward record | S | — |
| `GLD-EVA-04` | **Freeze the clock during replay** | Makes replay byte-exact; the harness cannot exceed the determinism of what it replays | S | `GLD-SCO-02` |
| `GLD-EVA-05` | **Stamp axis + anchor version on eval output** | Replayed scores are otherwise incomparable across a recalibration | S | `GLD-SCO-03`, `GLD-LIK-09` |
| `GLD-EVA-06` | **Extend forward validation beyond the watchlist** — Trakt lists, discovery shelf, next-watch | The lift method generalises; only the watchlist uses it | M | `GLD-EVA-03` |
| `GLD-EVA-07` | **Replay for deletion decisions**, not only recommendations — would the new weights have deleted differently? | The most consequential decisions are the least measured | L | `GLD-SPA-05` |
| `GLD-EVA-08` | **Confidence bands on lift** given small n | A lift of 1.4 on 12 items is not a lift | M | — |
| `GLD-EVA-09` | **Scheduled forward-validation snapshots** rather than ad-hoc | Windows are only measurable if a snapshot exists at `T` | S | `GLD-EVA-03` |
| `GLD-EVA-10` | **Publish eval results to the ledger** so they are browsable alongside decisions | Results currently live in tool output only | M | `GLD-WEB-03` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Do the drivers pass production's watched threshold, or accept the 0.9 default? | `GLD-EVA-01` |
| Q2 | Should replay honour Tautulli's `watched_status`, or is the completion leg sufficient for a historical reconstruction? | `GLD-EVA-01` |
| Q3 | What window `W` is right for forward validation — and does it differ for movies vs series? | `GLD-EVA-09` |
| Q4 | Is lift interpretable at this household's n, or does it need confidence bands first? | `GLD-EVA-08` |
| Q5 | Can deletion decisions be replayed at all, given the file is gone? | `GLD-EVA-07` |

**Q5 is the structural limit.** Recommendation replay works because the candidate
set still exists. A deleted file has no row to re-score, so measuring deletion
quality needs the pre-deletion snapshot preserved — which is a ledger question
(`GLD-ML-06`), not an eval one.

## 11. Related designs

- [`DESIGN_recommendation_enhancement.md`](../DESIGN_recommendation_enhancement.md) — Phase 0 and the §8 blind spot this package answers
- [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md) §3.1 — the production definition of "watched"
- [`scoring/DESIGN.md`](../scoring/DESIGN.md) §3.5 — clock-reading signals limiting replay
- [`likelihood/DESIGN.md`](../likelihood/DESIGN.md) §3.3 — axis coupling limiting comparability
- [`challenger/`](../challenger/) · [`ledger/`](../ledger/)
