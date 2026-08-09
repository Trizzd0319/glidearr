# challenger

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **challenger**

**Package** — `scripts.managers.machine_learning.challenger`
**Run position** — Shadow hook inside `labels/snapshots`, after the score map is built and persisted. **Default off.**
**One-liner** — ML Stage 4: a LightGBM watch-probability model that runs alongside the hand-weighted scorer, logs where the two disagree, and can change nothing.

---

## Purpose

A hand-weighted A–G scorecard is auditable but unproven. The only way to know
whether a learned model would rank better is to run one — and the only *safe* way
to run one is to make it structurally incapable of affecting anything.

From [`gbt_shadow.py`](./gbt_shadow.py):

> A LightGBM classifier trained **OFFLINE** on the Stage-1 labeled snapshots:
> `sig_*` signal-group contributions + a few context columns → P(watched within
> horizon). Calibrated with pure-numpy isotonic regression fitted on the temporal
> validation window, **so the persisted P is an honest probability, not a raw
> margin**.

---

## SHADOW-ONLY, BY CONSTRUCTION

Three independent guarantees, any one of which is sufficient:

| # | Guarantee |
|---|---|
| 1 | **Observe-only entry point.** `attach_challenger_p` runs *after* the score map is built and persisted. It can only log divergence and fill the `challenger_p` snapshot column. *"Nothing downstream reads `challenger_p` at runtime; no score, decision, or `*arr` write can change."* |
| 2 | **Config gate.** `scoring.ml_challenger.enabled` — **default false**. Even when true, a missing model file → silent no-op. |
| 3 | **Optional dependency.** lightgbm absent → one log line per process, then no-op. *"Nothing hard-requires it."* |

---

## Script inventory

| Script | Size | Role | Tests |
|---|---|---|---|
| [`gbt_shadow.py`](./gbt_shadow.py) | 14.7 KB | Training, calibration, persistence, and the runtime shadow path | ❌ **none** |
| [`__init__.py`](./__init__.py) | 0.4 KB | Package exports | — |

**The package has no test files.** See [`DESIGN.md`](./DESIGN.md) §3.5.

---

## Artifacts

Under `<cache>/ml/models/`:

| File | Contents |
|---|---|
| `gbt_challenger_{service}.txt` | LightGBM Booster, text format |
| `gbt_challenger_{service}.calib.json` | `features`, `calib_bx`, `calib_by`, `trained_at`, `metrics`, `n_train`, `n_valid` |

---

## Training

**CLI-only** — `scripts/support/tools/ml_train_challenger.py`. Never called from a run.

```
temporal split:  train < split_date <= valid
no split date →  last ~20% of DISTINCT snapshot days become validation
single day    →  warned random 80/20  (degenerate — labels are in-sample)
```

Guards before training: ≥20 train rows, ≥5 validation rows, ≥1 positive. Failing
those falls back to a warned random split, then gives up with a logged reason.

```python
"num_leaves": 15,        # small — n is tiny, deep trees would memorise
"metric": "average_precision",
"learning_rate": 0.05,  "min_data_in_leaf": 10,
"feature_fraction": 0.8, "bagging_fraction": 0.8,
```

Recorded metrics: `valid_auc_pr`, **`valid_base_rate`**, `valid_brier_raw`,
`valid_brier_calibrated`, `best_iteration`, `degenerate_split`.

---

## Divergence reporting

`divergence_report` returns Spearman ρ between the hand score and the challenger
P, plus the top-N entities the two rankers disagree about hardest — largest
`|Δrank|` first.

Each logged line names the title, both ranks, and the delta, closing with:

> Observe-only: nothing reads `challenger_p` at runtime.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Trains on:** [`labels/`](../labels/README.md) Stage-1 snapshots
- **Uses:** [`eval/np_metrics`](../eval/README.md) — `isotonic_fit`, `average_precision`, `brier_score`, `spearman_rho`
- **Tool:** `support/tools/ml_train_challenger.py`
