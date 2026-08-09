# foundation

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **foundation**

**Package** — `scripts.managers.machine_learning.foundation`
**Run position** — **None.** Nothing at runtime imports this package.
**One-liner** — The canonical, executable index of every statistical formula in Glidearr: one rigorous docstring per estimator, each delegating to the operative implementation so it can never drift from what the planners actually compute.

---

## Purpose

Glidearr's decisions rest on a dozen statistical estimators scattered across
`eval/np_metrics`, `likelihood/survival` and `space/coordinator_ranker`. Each is
correct, each is justified — and each justification lived only in the head of
whoever chose it.

This package is where the *why* lives, in a form that cannot rot:

> One rigorous docstring per formula: **the math**, **why it is the right
> estimator at this data regime**, and **where the codebase uses it**. Every
> function here DELEGATES to the operative implementation — the math is defined
> once and wrapped, never duplicated, so foundation can NEVER drift from what the
> tools/planners compute.

Full derivations live in [`MATH_FOUNDATION.md`](../MATH_FOUNDATION.md); these
docstrings are *"the executable index into that document."*

---

## ⚠️ Nothing imports this at runtime

Stated plainly in the module docstring:

> Nothing at runtime imports this package; it exists so adopters (new tools,
> future policy derivations) import formulas instead of re-implementing them.

That is **by design**, not neglect — it is a reference library awaiting adopters,
not an orphaned signal. See [`DESIGN.md`](./DESIGN.md) §3.4 for why that
distinction matters and where it becomes a risk.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`formulas.py`](./formulas.py) | All twelve canonical formulas, grouped by pipeline stage | ✅ Implemented |
| [`__init__.py`](./__init__.py) | Re-exports, incl. `calibration_table`, `minmax_scale`, `isotonic_fit`, `isotonic_predict`, `entity_gaps` | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_foundation.py`](./test_foundation.py) | Delegation equivalence — including asserting `downgrade_credit` matches the coordinator's own arithmetic (§3.5) |

---

## The twelve formulas

### 1 · The production model

| Function | Formula |
|---|---|
| `linear_utility` | `s(x) = Σ_g w_g · f_g(x)` — the form of the watchability score. `w_g = 1` today, hand-edited only |

### 2 · Logistic refit — Stage 3

| Function | Formula |
|---|---|
| `standardize` | `z_j = (x_j − μ_j) / σ_j` |
| `logistic_probability` | `P(y=1\|z) = σ(β₀ + Σ β_j z_j)`, η clipped to ±30 |
| `fit_logistic_irls` | L2-penalised logistic via IRLS; intercept unpenalised |
| `suggested_weight_multipliers` | `m_g = β_g / max_k \|β_k\|` |

### 3 · Evaluation metrics — Stage 2/4

| Function | Formula |
|---|---|
| `average_precision` | `AP = (1/P) Σ_k precision@k · 1[y_(k)=1]` |
| `brier_score` | `BS = (1/N) Σ (p_i − y_i)²` |
| `expected_calibration_error` | `ECE = Σ_b (n_b/N)·\|ȳ_b − p̄_b\|` |
| `spearman_rho` | `ρ = corr(rank(a), rank(b))` |
| `isotonic_calibrate` | PAVA: `f* = argmin_{f ↑} Σ (y_i − f(s_i))²` |

### 4 · Survival / recency — Stage 5a

| Function | Formula |
|---|---|
| `discrete_hazard` | Life-table `ĥ_b = d_b / r_b` |
| `residual_watch_probability` | `1 − Π_b (1 − ĥ_b · frac_b)` |
| `empirical_bayes_pool` | `ĥ = w·ĥ_child + (1−w)·ĥ_parent`, `w = n/(n+k)`, `k = 5` |

### 5 · Space decision theory — Stage 5b + coordinator

| Function | Formula |
|---|---|
| `value_density` | `ρ = v / max(GB, 0.1)` |
| `downgrade_credit` | `delete_target = max(0, need − clamp(ratio)·Σ max(reclaim_i, 0))` |

---

## The data regime these are chosen for

Every estimator choice is justified against **this household's** numbers, which
are recorded in the docstrings:

| Quantity | Value | Consequence |
|---|---|---|
| Positive prevalence | **~2 %** | AP is read against that floor, not 0.5; Brier against ≈0.02, not 0 |
| Watch events | **n ≈ 931** | 7-day hazard buckets trade resolution for at-risk count per cell |
| Positives | **n_pos ≪ 100** | Ridge is "the right trade"; Stage 3 only ever *prints* suggestions |

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Derivations:** [`MATH_FOUNDATION.md`](../MATH_FOUNDATION.md)
- **Delegation targets:** [`eval/np_metrics.py`](../eval/README.md) · [`likelihood/survival.py`](../likelihood/README.md) · [`space/coordinator_ranker.py`](../space/README.md)
