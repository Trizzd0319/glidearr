# challenger — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **challenger**

**Package** — `scripts.managers.machine_learning.challenger`
**Status** — ✅ Implemented · 🟢 Triple-gated shadow · 🟡 Zero tests · 🟡 A comment/code mismatch in the model cache
**Related** — [README.md](./README.md) · [`eval/DESIGN.md`](../eval/DESIGN.md) · [`foundation/DESIGN.md`](../foundation/DESIGN.md)

---

## 1. Problem statement

The A–G scorecard is hand-weighted. It is auditable, explainable, and entirely
unvalidated as a *ranker* — nobody knows whether a learned model would order the
library better.

Finding out is dangerous in a specific way. A model that silently began
influencing decisions would be almost impossible to audit afterwards: the score
column would still look like a score, the decisions would still look like
decisions, and the causal chain from a bad deletion back to a boosted tree would
be unrecoverable.

Three further constraints:

1. **The data is tiny.** `n_pos ≪ 100`
   ([`foundation/DESIGN.md`](../foundation/DESIGN.md)). A model with capacity to
   memorise will.
2. **A raw margin is not a probability.** A GBT's output is monotone in
   likelihood but not calibrated, and an uncalibrated number that *looks* like a
   probability invites misuse.
3. **A heavy dependency cannot become load-bearing.** LightGBM is a compiled
   package that fails to import on some platforms. Making it required would make
   the whole system fragile for an experiment.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | The challenger cannot affect any score, decision or `*arr` write. |
| G2 | The persisted P is an honest, calibrated probability. |
| G3 | Model capacity matched to the data regime. |
| G4 | Disagreement with the hand scorer is visible and specific. |
| G5 | LightGBM stays optional. |
| G6 | Never raises into the run. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Promotion | Nothing here promotes; that is `GLD-ML-08`, still undecided. |
| N2 | Training in-run | CLI only — *"never called from the run."* |
| N3 | Producing labels | `labels/` does; this consumes them. |
| N4 | Replacing the scorecard | *"The deterministic A–G scorecard stays the curation authority."* |

---

## 3. Architecture

### 3.1 Three independent shadow gates (G1)

```
attach_challenger_p(config, logger, base_dir, service, rows)
    │
    ├─ rows empty or scoring.ml_challenger.enabled false ──► rows unchanged   ← gate 2
    ├─ lightgbm absent ──► one log line per process, rows unchanged            ← gate 3
    ├─ no model file ──► rows unchanged
    │
    ├─ fill rows["challenger_p"]        ← the ONLY mutation
    └─ log Spearman ρ + top-10 rank disagreements
```

Three gates where one would do, and they are genuinely independent: a config
flag, an optional import, and a runtime entry point positioned *after* the score
map is already persisted. Even with the flag on, the model present, and lightgbm
installed, the function can still only write one column that nothing reads.

The log line states the guarantee to whoever reads it: *"Observe-only: nothing
reads `challenger_p` at runtime."*

This is the seventh instance of the house **byte-identical opt-in** pattern (see
[`affinity/DESIGN.md`](../affinity/DESIGN.md) §3.2) and the strictest — the others
change behaviour when enabled; this one cannot.

### 3.2 Calibration, not margins (G2)

> Calibrated with pure-numpy isotonic regression (`eval/np_metrics` PAVA) fitted
> on the **temporal validation window**, so the persisted P is an honest
> probability, not a raw margin.

Two details that matter:

**Fitted on validation, not training.** Calibrating on the training window would
fit the calibrator to predictions the model has already over-fitted, producing
probabilities that look excellent and are not.

**Both Brier scores are recorded** — `valid_brier_raw` and
`valid_brier_calibrated`. The pair makes the calibration's contribution
measurable rather than assumed.

And `valid_base_rate` is recorded alongside `valid_auc_pr`, which is exactly what
[`foundation/formulas.py`](../foundation/README.md) insists on: AP's baseline is
prevalence, not 0.5, so an AP without its base rate is unreadable. Rare
discipline — most reporting omits the floor.

### 3.3 Hyperparameters chosen for the regime (G3)

```python
"num_leaves": 15,   # small — n is tiny, deep trees would memorise
"min_data_in_leaf": 10,
"metric": "average_precision",
```

The `num_leaves` comment is the important one. The default is 31; halving it is a
deliberate capacity reduction justified by `n`. Same reasoning as
`foundation`'s ridge argument — *"a group that fired 3 times can be perfectly
separated by chance"* — expressed in tree terms.

Using `average_precision` as the training metric rather than AUC or logloss is
consistent with the same regime argument: at ~2 % prevalence, AP weights the
region the system acts on.

### 3.4 The degenerate split is recorded, not hidden

```
split_date given        → train < split_date <= valid
≥2 distinct days        → last ~20% of days become validation
1 day only              → warned random 80/20   ← degenerate, labels in-sample
too few rows/positives  → warned random split, then give up
```

`degenerate_split: bool` is persisted **into the calibration JSON's metrics**. The
model carries its own untrustworthiness with it, so a later reader of the
artifact knows the validation was in-sample without having to reconstruct how it
was trained.

That is a small thing done right, and it is the pattern
[`ledger/DESIGN.md`](../ledger/DESIGN.md) §3.3 wants and lacks: provenance
travelling with the artifact.

### 3.5 🟡 Zero tests

`gbt_shadow.py` is 14.7 KB covering training, temporal splitting, isotonic
calibration, artifact persistence, mtime-cached loading, prediction, and rank
divergence. **There is no test file in the package.**

Blast radius is genuinely bounded by §3.1 — nothing it computes can affect a
decision. But three things inside it are not protected by that:

- **The temporal split logic** decides what "validation" means. A bug there
  produces optimistic metrics that would be read as evidence for promotion.
- **The isotonic calibration** is the difference between an honest probability
  and a laundered margin.
- **The degenerate fallback chain** has three branches and a give-up path.

The shadow guarantee protects the *library*. It does not protect the *conclusion*
someone draws from a divergence report. `GLD-CHL-01`.

Worth contrasting with [`acquisition/`](../acquisition/DESIGN.md) §3.6, where every
module has a matching test as large as its source — and with
[`playlists/`](../playlists/DESIGN.md) §3.4, where the spoiler invariant has none.
Test coverage in the brain is not uniform; it is per-package and uncorrelated
with risk.

### 3.6 🟡 The model cache comment does not match its code

```python
_MODEL_CACHE.clear()           # keep at most one model per service alive
_MODEL_CACHE[key] = bundle
```

The comment says *"one model per service"*. `clear()` empties the **whole** dict,
so it is one model **total**. With both Radarr and Sonarr attaching in the same
run, each service's load evicts the other's, and a second call for the first
service re-reads the Booster from disk.

In practice each service attaches once per run, so the cost is bounded — but the
stated intent and the behaviour differ, and a future reader optimising this would
be misled. `GLD-CHL-04`.

### 3.7 🟡 Missing features read as zero

```python
if v is None or (isinstance(v, float) and pd.isna(v)):
    X[i, j] = 0.0
```

Every absent feature becomes `0.0`. For a GBT this is less damaging than for a
linear model — a tree can split at zero and learn that the value is special — but
only if *absence* and *a genuine zero* are distinguishable in the training data,
and here they are not: both arrive as `0.0`.

This is §8 **P-C** in its mildest form, and it inherits the problem
`GLD-ML-04` describes: a partially-enriched row's missing signal groups already
contribute zero to the hand score, and now contribute zero to the challenger too.
Both rankers are being fed the same ambiguity, which is at least consistent —
and means a divergence report cannot surface it. `GLD-CHL-05`.

LightGBM supports NaN natively and splits on missingness, so the fix is small:
leave missing as `np.nan` rather than coercing.

### 3.8 The promotion gap, confirmed

`GLD-ML-08` was logged as *"the challenger has no promotion criteria."* This
package confirms it precisely: there is no promotion path, and the absence is
deliberate and documented — *"Nothing downstream reads `challenger_p` at
runtime."*

What is missing is not code but a **decision**: what would have to be true of
`valid_auc_pr` against `valid_base_rate`, over how many runs, before the
challenger earned any influence? The metrics needed to answer it are already
persisted. `GLD-CHL-02`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Three independent shadow gates | G1 — any single gate failing still cannot let it influence anything | One config flag |
| D2 | Entry point after the score map is persisted | Structurally cannot participate in scoring | Hook into scoring |
| D3 | Default false | An experiment must be opted into | Default on |
| D4 | LightGBM optional, one log line per process | G5 — a compiled dependency must not be load-bearing | Hard requirement |
| D5 | Isotonic on the validation window | G2 — calibrating on train fits the over-fit | Calibrate on train |
| D6 | Record raw **and** calibrated Brier | Makes the calibration's value measurable | Calibrated only |
| D7 | Record `valid_base_rate` beside AP | AP's floor is prevalence; without it the number is unreadable | AP alone |
| D8 | `num_leaves = 15` | G3 — *"n is tiny, deep trees would memorise"* | LightGBM default 31 |
| D9 | `average_precision` as the training metric | Matches the ~2 % prevalence regime | AUC / logloss |
| D10 | Persist `degenerate_split` | The artifact carries its own untrustworthiness | Warn at train time only |
| D11 | Training is CLI-only | Never surprises a run with a training pass | Train opportunistically |
| D12 | `attach_challenger_p` never raises | G6 — an experiment must not break a run | Let it propagate |
| D13 | mtime-keyed model cache | A retrained model is picked up without a restart | Path-keyed |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No score, decision or `*arr` write can change because of this package. |
| I2 | `challenger_p` is read by nothing at runtime. |
| I3 | Disabled by default; enabled + no model ⇒ no-op. |
| I4 | Missing lightgbm ⇒ no-op, one log line per process. |
| I5 | Persisted P is calibrated and clipped to `[0, 1]`. |
| I6 | Calibration is fitted on validation, never on training. |
| I7 | `attach_challenger_p` never raises. |
| I8 | Training never runs inside a pipeline pass. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| lightgbm not installed | `HAS_LIGHTGBM` | One line per process, no-op | None | ✅ Explicit, names the fix |
| Model file absent | `load_challenger` → None | Silent no-op | None | ❌ **None** |
| Calibration JSON corrupt | `except` → None | Silent no-op | None | ❌ **None** |
| Single-day dataset | `degenerate` flag | Random split, warned, flag persisted | 🟡 In-sample metrics | ✅ Flag + warning |
| Zero positives | Guard | Refuses to train | Correct | ✅ Warning |
| Prediction raises | Outer `except` | Rows unchanged, debug line | None | 🟡 Debug only |
| **Both services attach in one run** | **None** | Each evicts the other's model; extra disk read | 🟡 §3.6 | ❌ **None** |
| **Missing feature vs genuine zero** | **None** | Indistinguishable to the model | 🟡 §3.7 | ❌ **None** |
| **A split or calibration bug** | **No tests** | Optimistic metrics read as evidence | 🟡 §3.5 — affects *conclusions*, not the library | ❌ **None** |

Every runtime failure degrades to a no-op, which is exactly right for an
experiment. The residual risk is entirely in the *interpretation* of what the
experiment reports.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `scoring.ml_challenger.enabled` | **`false`** | The shadow pass |

Training parameters are CLI arguments: `split_date`, `num_boost_round` (400),
`early_stopping_rounds` (30), `params` override.

Artifacts: `<cache>/ml/models/gbt_challenger_{service}.{txt,calib.json}`.

---

## 8. Implemented capabilities

- ✅ Triple-gated observe-only shadow pass
- ✅ Offline LightGBM training on Stage-1 labeled snapshots
- ✅ Temporal train/validation split with a warned degenerate fallback
- ✅ Isotonic calibration fitted on the validation window
- ✅ Raw and calibrated Brier recorded side by side
- ✅ AP recorded with its base rate
- ✅ Capacity-limited hyperparameters with a stated reason
- ✅ `degenerate_split` persisted into the artifact
- ✅ mtime-keyed model cache picking up retrains without a restart
- ✅ Spearman ρ plus top-N rank disagreements with per-title detail
- ✅ Optional dependency with a single informative log line
- ✅ Never raises into a run

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-CHL-01` | 🟡 **Add tests** — the temporal split, the isotonic calibration, and the degenerate fallback chain. The shadow guarantee protects the library, not the **conclusion** drawn from a divergence report | §3.5: 14.7 KB, zero tests, and its output is meant to inform a promotion decision | M | — |
| `GLD-CHL-02` | **Define promotion criteria** — what `valid_auc_pr` against `valid_base_rate`, over how many runs, earns influence? **Closes `GLD-ML-08`**; the metrics are already persisted | The experiment has no success condition, so it can run forever without concluding | M | D41 |
| `GLD-CHL-03` | **Persist divergence reports** rather than only logging them | ρ and the top disagreements are the experiment's actual output and survive only in a log | S | `GLD-LED-08` |
| `GLD-CHL-04` | 🟡 **Fix the cache comment or the code** — `_MODEL_CACHE.clear()` keeps one model *total*, not one *per service* | §3.6: stated intent and behaviour differ | S | — |
| `GLD-CHL-05` | **Leave missing features as `np.nan`** — LightGBM splits on missingness natively; coercing to `0.0` makes absent and zero identical | §3.7 *(P-C)* | S | `GLD-ML-04` |
| `GLD-CHL-06` | **Warn when enabled but no model is present** — currently a silent no-op | §6 row 2: an operator who enabled it gets no feedback that nothing happened | S | — |
| `GLD-CHL-07` | **Report challenger coverage** — rows scored, model age, whether the split was degenerate | The shadow pass runs invisibly except for two log lines | S | `GLD-THR-04` |
| `GLD-CHL-08` | **Track ρ over time** — a challenger converging on the hand scorer means something different from one diverging | A single run's ρ is not interpretable; a trend is | M | `GLD-CHL-03` |
| `GLD-CHL-09` | **Re-verify `num_leaves = 15`** if `n` grows materially | D8's justification is regime-bound | S | `GLD-FND-04` |
| `GLD-CHL-10` | **Train per-media models** or confirm one per service is right — a movie and a series have different `sig_*` distributions | Services already split; media type may matter more | M | `GLD-CHL-02` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | What would have to be true for the challenger to earn influence? *(= D41, closes `GLD-ML-08`)* | `GLD-CHL-02` |
| Q2 | Has it ever been trained on this library, and what were the metrics? | `GLD-CHL-07` |
| Q3 | Should missing features be `NaN` rather than `0.0`? | `GLD-CHL-05` |
| Q4 | Is ρ converging or diverging across runs? | `GLD-CHL-08` |

**Q1 is the one holding the rest.** Every ingredient for a promotion decision is
present — calibrated probabilities, AP against its base rate, both Brier scores,
a degeneracy flag, rank-level divergence. What is missing is a **stated bar**.
Without one the shadow pass can run indefinitely, accumulate evidence, and never
resolve — which is the failure mode a well-built experiment with no success
condition always has.

## 11. Related designs

- [`labels/DESIGN.md`](../labels/DESIGN.md) — the Stage-1 snapshots this trains on
- [`eval/DESIGN.md`](../eval/DESIGN.md) — `np_metrics`, and the honest-measurement discipline
- [`foundation/DESIGN.md`](../foundation/DESIGN.md) §3.2 — why AP over ROC, and why ridge/small capacity at this `n`
- [`scoring/DESIGN.md`](../scoring/DESIGN.md) — the incumbent this challenges
- [`ledger/DESIGN.md`](../ledger/DESIGN.md) §3.3 — provenance-with-artifact, done here and missing there
