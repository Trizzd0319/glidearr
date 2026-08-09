# foundation — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **foundation**

**Package** — `scripts.managers.machine_learning.foundation`
**Status** — ✅ Implemented · ⚪ Zero runtime adopters *(by design)* · 🟡 One non-delegating mirror
**Related** — [README.md](./README.md) · [`MATH_FOUNDATION.md`](../MATH_FOUNDATION.md) · [`machine_learning/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

A statistical choice is only defensible if its justification survives the person
who made it. Glidearr contains a dozen such choices, and each carries a
non-obvious rationale tied to **this specific data regime**:

- Why average precision and not ROC-AUC? *Because at ~2 % prevalence, ROC-AUC is
  dominated by true-negative ordering and barely moves when the top of the
  ranking changes.*
- Why isotonic and not Platt calibration? *Because the hand score is a capped sum
  with no reason to be logit-linear, and Platt would impose that shape on it.*
- Why ridge on the refit? *Because a signal group that fired three times can be
  perfectly separated by chance.*

Left in call sites, those rationales are invisible. Written into a separate
document, they rot the moment the code changes. Re-implemented in a "maths
utilities" module, they become a **second implementation** that silently diverges
from what the planners actually compute — the exact P-E failure this repo already
tracks eight instances of.

`foundation/` resolves all three: the docstring lives with an executable
function, and **that function delegates** rather than reimplementing. Drift is
structurally impossible.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Every formula documented with its math, its regime justification, and its consumers. |
| G2 | **Cannot drift** — each wrapper delegates to the operative implementation. |
| G3 | Serve as the adoption point, so a new tool imports rather than re-implements. |
| G4 | Pure — numpy/stdlib over plain arrays; no I/O, no manager graph, no clock. |
| G5 | Executable index into [`MATH_FOUNDATION.md`](../MATH_FOUNDATION.md). |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Being on the runtime path | It documents; the planners compute. |
| N2 | Owning the implementations | G2 — owning them would recreate the drift risk. |
| N3 | Applying anything | `suggested_weight_multipliers` is explicitly *"Suggestions only; nothing applies them."* |
| N4 | Novel estimators | Canonical forms only. |

---

## 3. Architecture

### 3.1 Delegate, never duplicate

```
foundation/formulas.py
      │  documents + wraps
      ├──► eval/np_metrics          AP · Brier · ECE · Spearman · isotonic · IRLS
      ├──► likelihood/survival      hazard_curve · residual · blend_hazards
      └──► space/coordinator_ranker utility_per_gb
```

Eleven of the twelve functions are thin wrappers. `linear_utility` is the one
that computes — and it computes the *form*, explicitly documenting that the
operative implementations compute it inline:

> OPERATIVE IMPLEMENTATIONS (this function documents the form; **do NOT rewire
> them**): `scoring/movie_scorer.score_movie` and `scoring/show_scorer.score_show`
> compute the `f_g` terms and this sum inline.

That parenthetical is doing real work — it pre-empts the obvious "tidy-up" of
making the scorers call `foundation`, which would invert the dependency and put a
documentation package on the hot path.

### 3.2 Regime-justified choices

Each choice is argued against measured numbers rather than convention:

| Choice | Justification |
|---|---|
| **AP over ROC-AUC** | At ~2 % prevalence ROC-AUC is dominated by true-negative ordering. AP *"weights exactly the region the system acts on (what gets kept/acquired first)."* Baseline ≈ prevalence, so it is read against that floor |
| **Brier as diagnostic, not loss** | Strictly proper — *"uniquely minimized in expectation by the true probability, so it cannot be gamed by hedging."* Murphy decomposition: reliability − resolution + uncertainty. All-zeros scores ≈ 0.02 at this prevalence |
| **Isotonic over Platt** | Nonparametric; assumes *only* that P(watch) is monotone in the score — *"which is the entire premise of ranking by it."* Platt would impose logit-linearity on a capped sum |
| **Ridge on the refit** | Keeps `XᵀWX + λI` invertible under separable columns; *"a group that fired 3 times can be perfectly separated by chance"* |
| **Spearman for challenger divergence** | The 0–100 hand score and a probability *"share no scale, only an intended ordering"* |
| **7-day hazard buckets** | Trades resolution for at-risk count per cell at n ≈ 931 |
| **EB pooling with k = 5** | *"A title's own history starts outweighing the pool at about 5 events"* |
| **Density-ordered deletion** | LP relaxation makes density order **exactly** optimal (Dantzig/exchange); integral greedy differs by at most the one boundary item |

### 3.3 Three guards worth noting

**Leakage.** `standardize` documents that you must *"supply TRAIN moments to
transform a test window (fitting them on test leaks the future into the past)."*

**Overflow parity.** `logistic_probability` clips η to ±30 *"matching the
overflow guard inside `eval/np_metrics.logistic_irls` so predictions round-trip
the fit exactly."* A wrapper that guarded differently from its delegate would
break G2 silently.

**Zero-variance equivalence.** A σ ≤ 1e-12 column maps to `z = 0.0`, which is
argued to be *"fit-equivalent to `ml_weight_refit`'s policy of DROPPING
zero-variance columns"* — because under ridge an all-zero column earns exactly
β = 0 and leaves every other coefficient untouched.

### 3.4 ⚪ Zero adopters — design intent, and its risk

> Nothing at runtime imports this package; it exists so adopters (new tools,
> future policy derivations) import formulas instead of re-implementing them.

This is **not** §8 **P-A** (a signal computed every run and read by nothing). It
costs nothing per run and its stated purpose is to be available. Calling it a
dead signal would be wrong.

But it does carry a real risk, and it is the mirror image of P-A: **an adoption
point with no adopters is a hypothesis, not a fact.** Nothing verifies that the
wrappers are usable — that their signatures match how a tool would want to call
them, that the re-exports cover what an adopter needs. `test_foundation.py`
asserts delegation equivalence, which proves correctness but not ergonomics.

The cheapest disproof is one real adoption. §9 `GLD-FND-02`.

### 3.5 🟡 `downgrade_credit` is the one that does not delegate

Eleven functions wrap. This one reimplements:

> USED BY (**pure mirror — the coordinator is NOT rewired to call this**):
> `services/coordinator/space_coordinator` … `test_foundation` asserts
> equivalence against that arithmetic.

So G2's structural guarantee does not hold here. Two copies of

```
delete_target = max(0, need − clamp(ratio,0,1) · Σ max(reclaim_i, 0))
```

exist, kept in step by a **test** rather than by construction.

That is a considered choice — the coordinator's version is embedded in a
two-step (`credit = min(need, Σ·r); need −= credit`) that would be awkward to
extract, and the docstring proves the two-step and the clamped one-liner are
algebraically identical. The equivalence test is a genuine mitigation.

It is still §8 **P-E**, in the mildest form seen so far: a duplicate that is
*asserted* equivalent rather than left to drift. Worth recording because the
mitigation is only as durable as the test.

The underlying reasoning is worth preserving regardless:

> Downgrades reclaim space non-destructively but LATER. Deleting to cover the
> full deficit while re-grabs are in flight double-counts: **titles die that the
> downgrades would have paid for.**

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Wrap, never reimplement | G2 — a maths-utilities module that computes independently is a second implementation | Standalone implementations |
| D2 | Docstrings carry regime justification, not just formulas | A formula without its regime is unfalsifiable advice | Formula-only docs |
| D3 | `linear_utility` documents the form; scorers keep computing inline | Prevents inverting the dependency and putting docs on the hot path | Rewire the scorers |
| D4 | Off the runtime path | N1 — availability, not participation | Make it the compute layer |
| D5 | Clip parity with the delegate (±30) | A wrapper guarding differently breaks G2 invisibly | Independent guard |
| D6 | Zero-variance ⇒ z = 0 rather than drop | Fit-equivalent under ridge, and keeps column indices stable | Drop the column |
| D7 | `suggested_weight_multipliers` normalises to max\|β\| | Scale-free — comparable across refits with different λ or n | Raw β |
| D8 | Refit only ever suggests | *"At our n the estimates are honest but HIGH-VARIANCE"* | Auto-apply |
| D9 | `value_density` floors GB at 0.1 | Stops near-zero files jumping the queue — *"a 1 MB file is not 'free value'"* | Raw division |
| D10 | `downgrade_credit` mirrors rather than delegates | Coordinator arithmetic is embedded in a two-step; equivalence is tested | Extract and delegate |
| D11 | One `w` per survival curve from event count | *"A deliberate small-n simplification vs per-bucket at-risk weighting"* | Per-bucket weighting |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Every function except `linear_utility` and `downgrade_credit` delegates. |
| I2 | Wrapper guards match their delegate's guards exactly (clipping, falsy handling, clamps). |
| I3 | The package performs no I/O and reads no clock. |
| I4 | Nothing on the runtime path imports it. |
| I5 | `downgrade_credit` is asserted equivalent to the coordinator's arithmetic by test. |
| I6 | Refit outputs are suggestions; nothing applies them. |
| I7 | Metrics return `NaN` where undefined (no positives, <2 pairs, constant input) rather than a fabricated number. |

**I7 is quietly important**: `average_precision` is *"NaN with no positives —
undefined, and the tools SAY so."* A metric that returned 0 there would look like
a bad model rather than an absent measurement.

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| A delegate changes its guard, the wrapper does not | [`test_foundation.py`](./test_foundation.py) | Test fails | Caught | ✅ |
| **`downgrade_credit` diverges from the coordinator** | Equivalence test **only** | Two answers to one question | 🟡 Bounded by the test's continued existence | ✅ *while the test lives* |
| No positives in a metric input | Explicit | `NaN`, and tools report it | Correct | ✅ |
| ECE bins sparsely populated | Documented | Noisy estimate; *"ECE → 0 artifacts appear when bins are empty"* | 🟡 Read alongside per-bin counts | 🟡 Table only |
| Survival tail thin (small `r_b`) | At-risk counts printed | Visibly noisy hazards | Bounded | ✅ Report prints at-risk |
| Someone rewires the scorers to call `linear_utility` | Docstring warning **only** | Docs package joins the hot path | 🟡 | ❌ **None** |
| An adopter finds the signature unusable | **None** | Re-implements instead | ⚪ Silent non-adoption | ❌ **None** |

Rows 6 and 7 are the package-specific ones. Row 7 is the §3.4 risk in failure-mode
form: **non-adoption is invisible**, because the package's success condition is
that someone *chose* to import it.

---

## 7. Configuration surface

Reads no config directly. Two keys govern the behaviour its §5 formulas mirror:

| Key | Owner | Effect |
|---|---|---|
| `space_delete_ranking` | [`space/coordinator_ranker.py`](../space/README.md) | `"utility_per_gb"` mode; default-off Stage 5b. Default `"score"` ranks by score alone and is untouched |
| `space_downgrade_credit_ratio` | [`services/coordinator/`](../../services/coordinator/README.md) | The `ratio` in `downgrade_credit`. `1.0` = full credit; `0.0` = legacy delete-covers-everything |

Constants surfaced from delegates: `DEFAULT_BUCKET_DAYS` (7), `DEFAULT_K` (5),
`DEFAULT_MAX_DAYS`.

---

## 8. Implemented capabilities

- ✅ Twelve canonical formulas across five pipeline stages
- ✅ Delegation to `np_metrics`, `survival` and `coordinator_ranker` — drift-proof by construction
- ✅ Regime justification per estimator, grounded in measured library numbers
- ✅ Leakage warning on `standardize` (train moments only)
- ✅ Overflow-clip parity with the IRLS delegate
- ✅ Zero-variance handling argued fit-equivalent to the refit tool's drop policy
- ✅ Scale-free weight multipliers
- ✅ Murphy decomposition and prevalence floors documented for Brier
- ✅ Censoring-ignorability argument for the life-table hazard
- ✅ Beta-Binomial derivation of the EB shrinkage weight
- ✅ LP-optimality argument for density-ordered deletion
- ✅ Equivalence test covering the one non-delegating mirror
- ✅ Re-exports for `calibration_table`, `minmax_scale`, `isotonic_fit`/`predict`, `entity_gaps`

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-FND-01` | **Add `foundation` to `_GUARDED_SUBPACKAGES`** — it is genuinely pure, unlike `labels`, so this is a safe addition where `GLD-ML-02` as a flat list is not | Closes part of the P-B guard gap without the `GLD-LAB-05` complication | S | `GLD-ML-02` |
| `GLD-FND-02` | **Adopt it somewhere real** — port one tool (`ml_weight_refit` or `ml_survival_report`) to import from `foundation` | §3.4: an adoption point with no adopters is an untested hypothesis. One adoption disproves or confirms it | S | — |
| `GLD-FND-03` | **Delegate `downgrade_credit`** — extract the coordinator's two-step so the mirror becomes a wrapper | Restores G2's structural guarantee; removes the last P-E instance here | M | `GLD-SPA-05` |
| `GLD-FND-04` | **Assert the documented regime numbers** — ~2 % prevalence, n ≈ 931, `n_pos ≪ 100` — against live data, and warn when they move | Every estimator choice is justified by these; if prevalence doubles, several justifications weaken and nothing says so | S | `GLD-LAB-07` |
| `GLD-FND-05` | **Cross-link `MATH_FOUNDATION.md` section anchors** from each docstring (§3, §5, §6, §7, §9 are cited by number) | Makes the "executable index" claim navigable rather than aspirational | S | — |
| `GLD-FND-06` | **Guard against the scorers importing `linear_utility`** | §6 row 6 — the docstring warns; nothing enforces | S | `GLD-FND-01` |
| `GLD-FND-07` | **Surface at-risk counts and per-bin ECE counts** wherever these metrics are reported | The docstrings say to read them alongside; not every consumer does | S | `GLD-THR-04` |
| `GLD-FND-08` | **Document the Stage numbering** (1, 1b, 2, 3, 4, 5a, 5b) in one place — it is referenced across `labels`, `thresholds`, `challenger` and here | The stages are load-bearing shorthand with no single definition | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should `foundation` be the adoption target, or should the delegates simply be imported directly? A package nothing imports may be answering a question nobody asked. | `GLD-FND-02` |
| Q2 | Should `downgrade_credit` be extracted, or is the equivalence test sufficient mitigation? | `GLD-FND-03` |
| Q3 | If prevalence moves materially above ~2 %, which justifications need revisiting — AP-over-ROC certainly, but also the ridge trade and the bucket width? | `GLD-FND-04` |
| Q4 | Is `space_delete_ranking="utility_per_gb"` ready to become the default, given the LP-optimality argument? | `GLD-SPA-05` |

**Q1 deserves a view.** The delegation design is right, but "import `foundation`
instead of `np_metrics`" only pays off if the docstring is what an adopter needs
at the call site. Porting one tool (`GLD-FND-02`) answers it in an afternoon and
either validates the package or converts it into pure documentation — both useful
outcomes.

## 11. Related designs

- [`MATH_FOUNDATION.md`](../MATH_FOUNDATION.md) — §3 leakage, §5 isotonic-vs-Platt, §6 EB hierarchy, §7 expected utility, §9 threshold derivation
- [`eval/DESIGN.md`](../eval/DESIGN.md) · [`likelihood/DESIGN.md`](../likelihood/DESIGN.md) · [`space/DESIGN.md`](../space/DESIGN.md) — the delegation targets
- [`thresholds/DESIGN.md`](../thresholds/DESIGN.md) — consumes the same shrinkage form with `k = 150`
- [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8 P-E — the duplicate-implementation pattern
