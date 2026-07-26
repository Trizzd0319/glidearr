# MATH_FOUNDATION — the statistical formulas behind every Glidearr decision

The formal reference for the math the system runs on. Canonical implementations
live in `machine_learning/foundation/` (thin delegating wrappers over
`eval/np_metrics.py`, `likelihood/survival.py`, `space/coordinator_ranker.py` —
one definition per formula, proven equivalent by `foundation/test_foundation.py`).
**Contract: every statistical formula used by Glidearr is defined or re-exported
in `foundation/`; adopters import from it, never reimplement.** This document is
descriptive — nothing here changes any live score or decision; the dry-run plan
ledger remains the parity oracle (MIGRATION.md).

## 0 Notation

| symbol | meaning |
|---|---|
| `x` | an entity's feature bundle (movie / series aggregate) |
| `f_g(x)` | capped contribution of signal group `g` (the `sig_*` snapshot columns) |
| `w_g` | group weight — implicitly 1.0 in production, hand-edited only |
| `s(x)` | watchability score, `Σ_g w_g f_g(x)` clamped to [0, 100] |
| `τ` | a score threshold in the profile ladder |
| `y_i`, `t_i` | outcome label / snapshot timestamp of row `i` |
| `H` | label horizon in days (default 14) |
| `p_i`, `p̂` | true / estimated watch probability |
| `σ(η)` | logistic function `1/(1+e^−η)` |
| `β, β₀, λ` | logistic coefficients, intercept, ridge strength |
| `z_j` | standardized feature, `(x_j − μ_j)/σ_j` (train moments) |
| `W` | IRLS weight matrix `diag(p_i(1−p_i))` |
| `h_b, d_b, r_b` | hazard / events / at-risk count in day-bucket `b` |
| `g` | days since an entity's last watch (the current, censored gap) |
| `w = n/(n+k)` | empirical-Bayes blend weight; `k` = prior strength (5) |
| `U, T` | free-space recovery target / pressure floor (GB) |
| `v`, `ρ` | retained value of a file; value density `v/max(GB, 0.1)` |
| `r` | downgrade credit ratio, `space_downgrade_credit_ratio` clamped to [0,1] |
| `m_g` | suggested weight multiplier `β_g / max_k |β_k|` |
| AP, BS, ECE, `ρ_s` | average precision, Brier score, expected calibration error, Spearman rho |

## 1 The production model — hand-weighted linear utility

The live watchability score is a linear utility over independently-capped
signal groups:

    s(x) = Σ_g w_g · f_g(x),    w_g = 1 implicit,    s* = max(0, min(100, round(s)))

Capping bounds any single group's influence (the linear model's defense against
one noisy signal) and makes every score decomposable into the additive
`watchability_breakdown` the snapshots persist. Operative implementations —
referenced by `foundation.linear_utility`, **not** rewired: the movie engine
`scoring/movie_scorer.score_movie`, the show engine `scoring/show_scorer.score_show`
(same form, gentler A4 knobs), shared tables/helpers in `scoring/_shared.py`,
and the cache-row adapters `features/movie_features.py` / `features/show_features.py`.

Signal groups and per-signal caps (movie engine):

| group | role | signals (cap) |
|---|---|---|
| A Household Intent | explicit curation + observed engagement | A1 keep_policy (+15), A2 completion (±12), A3 rewatch (+8), A4 user Trakt rating (±10) |
| B Household Affinity | taste overlap with watch history | B1 actors (+8·boost), B2 directors (+6·boost), B3 writers (+4·boost), B4 genres (+4·boost), B5 studios (+3·boost) |
| C Collection / Universe | franchise continuity + collaborative graph | C1 collection completeness (+8), C2 universe siblings (+4), C3 related-graph (+4 default cap), C4 person-affinity (cap 0.0 default — inert) |
| D Device / Playback Fit | will it actually play well here | D1 primary-device capability (+6/−2), D2 transcode avoidance (+5), D3 platform ceiling share (+4) |
| E Audience Alignment | right content for the right viewers | E1 kids alignment (+6), E2 adult alignment (+4), E3 library fit (+4) |
| F Content Quality | external quality/recency evidence | F1 critic consensus (+20 — the strongest single positive), F2 popularity (+2), F3 recency (+2) |
| G Penalties | negative evidence | G1 language (−8, file-aware), G2 abandoned (−10), G3 panned (−5), G4 not-yet-available (−5) |

Threshold policy: the score maps to a quality profile through the ladder
`QUALITY_PROFILE_THRESHOLDS` / `score_to_profile` (`scoring/_shared.py`),
i.e. a step function τ → profile:

| s* ≥ τ | profile pattern |
|---|---|
| 80 | Remux 2160p (DV tier) |
| 70 | Remux 2160p (HDR tier) |
| 60 | Remux 1080p |
| 50 | Bluray 1080p |
| 35 | WEBDL 1080p |
| 0 | HD 720p |

Every τ is hand-set today; §9 maps each to the formula that could derive it.

## 2 Labels — what the score is measured against

`labels/labeling.build_labels` (Stage 1b) defines, per snapshot row `i`:

    y_i = 1[ a qualifying watch of entity i occurred in (t_i, t_i + H] ],   H = 14d default

Qualifying: movies — Tautulli event joined `rating_key → tmdb`
(`plex/movies/owned_inventory`), `percent_complete ≥ 90` (relaxed to ≥ 50 when
`tmdb_completions` marks the title completed overall); episodes — normalized
`grandparent_title` match, `percent_complete ≥ 50` (continued engagement, not
per-episode completion).

**Maturation is right-censoring.** A snapshot younger than `H` cannot yet be a
trustworthy negative — the household may still watch it inside the window. Such
rows carry `label_mature = false` and are excluded by default (`--include-immature`
opts in, with provisional-negative semantics). This is the same censoring logic
§6 applies to inter-watch gaps: absence of an event before the observation
boundary is *no information*, not a zero.

**Implicit negatives.** `recommended_not_watched` = the entity was in the Up
Next plan at snapshot time (`in_up_next`) and was not watched within `H` — the
system recommended it and the household declined. These are the highest-value
negatives: they were shown, not merely available.

## 3 Evaluation theory

**Average precision** (`foundation.average_precision`, delegating to
`eval/np_metrics`):

    AP = (1/P) Σ_k precision@k · 1[y_(k) = 1]

— the step-function area under the precision-recall curve; a random ranking
scores ≈ prevalence, so AP is read against that floor. Why AP and not ROC-AUC at
~2% positive prevalence: ROC's false-positive rate divides by the enormous
negative count, so reordering the top of the ranking — the only region the
system acts on (what gets kept, upgraded, acquired first) — moves ROC-AUC
negligibly while it moves AP a lot. AP weights exactly the operating region.
NaN with zero positives; the tools say so rather than inventing numbers.

**Brier score** (`foundation.brier_score`): `BS = (1/N) Σ (p_i − y_i)²`, a
strictly proper scoring rule (in expectation minimized only by the true
probability — hedged forecasts can't game it). Murphy decomposition:

    BS = reliability − resolution + uncertainty

reliability = calibration error (drive to 0), resolution = how much the
forecasts separate outcomes (drive up), uncertainty = `ȳ(1−ȳ)` — irreducible.
At 2% prevalence the all-zeros forecast already scores ≈ 0.02: compare against
that, not 0. Forward validation computes Brier on the *min-max-scaled score* —
the score is not a probability, so this is a calibration diagnostic, not a loss.

**Expected calibration error** (`foundation.expected_calibration_error`, in
`eval/np_metrics`): over equal-width probability bins,

    ECE = Σ_b (n_b / N) · |ȳ_b − p̄_b|

— the bin-weighted mean gap between claimed and realized probability, the
one-number summary of `calibration_table`'s reliability diagram, in probability
units. Binned estimates are noisy at small n; read alongside per-bin counts.

**Temporal split, always.** Snapshots are monthly rows of the *same titles*: a
random split places one title's March row in train and its April row in test —
twin leakage that inflates every metric. Temporal splits (`train < split-date ≤ test`)
also respect that the household drifts and that deployment is forward
prediction by construction. Degenerate splits degrade to clearly-labeled
in-sample evaluation with warnings (`ml_forward_validation`, `ml_weight_refit`).

## 4 Weight refit — logistic regression on the signal groups

Stage 3 (`ml_weight_refit`) asks: with real outcomes, what relative per-group
weights would a logistic model choose? Model (`foundation.logistic_probability`):

    P(y=1 | z) = σ(β₀ + Σ_j β_j z_j)

on train-standardized signals `z_j = (x_j − μ_j)/σ_j` (`foundation.standardize`;
population moments, zero-variance columns dropped — equivalently mapped to 0,
which under ridge earns exactly β=0). Standardization makes the L2 penalty
shrink heterogeneous columns (±12 completion vs +2 recency) comparably.

Estimation maximizes the penalized log-likelihood (intercept unpenalized):

    ℓ(β) = Σ_i [ y_i log p_i + (1−y_i) log(1−p_i) ]  −  (λ/2)‖β₋₀‖²

by IRLS/Newton (`foundation.fit_logistic_irls` → `eval/np_metrics.logistic_irls`):
with `W = diag(p_i(1−p_i))`,

    β ← β + (XᵀWX + λI₋₀)⁻¹ (Xᵀ(y − p) − λβ₋₀)

algebraically the weighted-least-squares form `(XᵀWX + λI)⁻¹XᵀWz̃` with working
response `z̃ = Xβ + W⁻¹(y − p)` — hence "iteratively reweighted least squares".
The log-likelihood is concave, so this converges to the global optimum; the
ridge keeps `XᵀWX + λI` invertible under the near-separable, rarely-firing
signal columns this dataset actually has, trading a small bias toward 0 for a
large variance cut — the right trade at `n_pos ≪ 100`.

Reporting: per-point effect `w_j = β_j/σ_j` (log-odds per raw score point),
then multipliers (`foundation.suggested_weight_multipliers`):

    m_g = w_g / max_k |w_k|

normalized so the strongest group reads 1.0 — directly comparable to today's
implicit 1.0 per group; negative means the group's points correlate with *not*
watching. Scale-free in λ and n. **Never applied**; the scorers stay hand-edited.

**The `n_pos < 100` caveat, quantified.** With standardized features the
information about each β is carried by events: `SE(β_j) ≳ 1/√n_pos` (before
inflation from inter-signal correlation). Power sketch: detecting a per-point
effect of standardized size δ = 0.3 at α = 0.05 with 80% power needs
`n_pos ≈ ((1.96 + 0.84)/δ)² ≈ 87` events — the origin of the 100-positive gate.
Below it, multipliers are directional at best, and a sign flip on a group that
fired < ~30 times is noise (the tool prints exactly this caveat).

## 5 Calibration — isotonic regression (PAVA)

To turn a ranking score into a probability the challenger uses isotonic
regression (`foundation.isotonic_calibrate` → `eval/np_metrics.isotonic_fit` /
`isotonic_predict`):

    f* = argmin_{f non-decreasing} Σ_i (y_i − f(s_i))²

— the least-squares projection of outcomes onto the cone of monotone functions
of the score. Pool-adjacent-violators solves it exactly: scan ascending, pool
adjacent blocks whose means violate monotonicity; the solution is
piecewise-constant at pooled block means, so each fitted value is the empirical
positive rate over its block — a calibrated probability by construction.
Prediction interpolates between block breakpoints and clamps flat at the ends
(no extrapolated probabilities). Fit on the *temporal validation window*, never
on train (a model is optimistically miscalibrated on its own training data).

**Why not Platt scaling at this n.** Platt fits `σ(a·s + b)` — two parameters,
assuming the score's miscalibration is sigmoid-shaped (logit-linear in `s`).
The hand score is a bounded, capped sum of heterogeneous bumps with threshold
effects; nothing trained it to be logit-linear, and imposing that shape would
bias the calibrated probabilities wherever the reliability curve is stepwise.
Isotonic assumes only monotonicity — the same assumption the entire ranking
system already commits to. The cost (isotonic can overfit tiny windows, one
block per run of outcomes) is contained by the validation-window fit, end
clamping, and reading ECE next to bin counts. At our n both methods are rough;
monotonicity is the weaker, safer assumption.

## 6 Survival — when does the next watch come?

`likelihood/survival.py` (Stage 5a). Per entity, consecutive watch events give
complete inter-watch gaps; the time from the last event to `now` is a
**right-censored** gap. Discrete-time hazard over day-buckets
(`foundation.discrete_hazard` → `survival.hazard_curve`), default 7-day buckets:

    ĥ_b = d_b / r_b

`d_b` = gaps ending in bucket `b`, `r_b` = gaps (complete or censored) that
survived to `b`'s start. This is the life-table / discrete Kaplan-Meier
estimator; conditioning on `r_b` is what makes censoring ignorable — a censored
gap contributes exposure to every bucket it reached, then exits without
asserting an event. Dropping censored gaps instead would bias hazards upward.
Each `ĥ_b` is a binomial proportion: unbiased given `r_b`, variance
`ĥ_b(1−ĥ_b)/r_b` — thin tails are noisy, so the report prints at-risk counts.

Residual watch probability (`foundation.residual_watch_probability` →
`survival.residual_probability_from_hazard`), the product-limit complement:

    P(watch in (g, g+H] | survived g)  =  1 − Π_b (1 − ĥ_b · frac_b)

over buckets covered by `(g, g+H]`; `frac_b` scales a partially-covered
bucket linearly; beyond the fitted curve the last bucket's hazard extends flat.
Having survived `g` days costs nothing extra — conditioning just starts the
product at `g`, which is the point of the hazard parameterization.

**Empirical-Bayes pooling** (`foundation.empirical_bayes_pool` →
`survival.blend_hazards`): title (≥3 events) → franchise/collection pool →
household, each level blended as

    ĥ = w·ĥ_child + (1−w)·ĥ_parent,    w = n/(n+k),    k = 5

Beta-Binomial reading: a Beta prior centered on the parent with `k`
pseudo-events, `Beta(k·h̄, k(1−h̄))`, has posterior mean
`(d + k·h̄)/(n + k) = w·(d/n) + (1−w)·h̄` — exactly this blend. `k = 5` says a
title's own history starts outweighing the pool at about five events. The
implementation applies ONE `w` per curve from the entity's event count rather
than per-bucket at-risk weighting — a deliberate small-n simplification. A
sparse title borrows almost everything from its pool; a much-rewatched title
speaks for itself.

**The measured finding and what it justifies.** On the household history
(n≈931 events), ~86% of completed inter-watch gaps end within the first 7-day
bucket — the hazard mass is front-loaded (`ml_survival_report`). Three policies
follow from that shape:

* **JIT window** — the week after a watch carries most of the next-watch
  probability, so just-in-time quality upgrades / prefetch have ~7 days of
  leverage; acting later mostly spends bandwidth on titles that won't be
  watched soon.
* **Grace as a residual-P threshold** — instead of a flat `grace_days`, a
  watched file's grace should expire when `P(watch within H | gap so far)`
  falls below a threshold. The 86% front-mass makes that collapse fast for
  pool-typical titles and slower for titles whose own blended curve has heavy
  tails — per-title grace from one formula.
* **Resumption = hazard spike** — a new watch resets `g` to 0, jumping the
  entity back into the high-hazard regime; re-protection/re-upgrade should key
  on that reset rather than on score recomputation lag.

## 7 Decision theory — space reclamation as constrained optimization

Framing: each kept file contributes expected utility ≈ `p̂ · v` (probability it
gets watched times the value of having it at its quality) and costs bytes.
When free space falls below the floor `T`, the coordinator must reclaim
`need = max(0, U − free)` GB while destroying the least utility.

**Deletion is a min-value covering knapsack**: choose a subset minimizing
`Σ v_i` subject to `Σ GB_i ≥ need`. Its LP relaxation (fractional deletion
allowed) is solved exactly by sorting ascending on value density

    ρ_i = v_i / max(GB_i, 0.1)        (foundation.value_density)

and taking from the bottom — the classic Dantzig/exchange argument: swapping a
selected item for an unselected one of lower density weakly increases value
destroyed per GB. The integral greedy differs from the LP optimum by at most
the one boundary item — negligible when single files are small next to a
multi-hundred-GB target. The 0.1 GB floor keeps near-zero-size files from
dividing by ~0 and jumping the queue. Operative: `space/coordinator_ranker`
`utility_per_gb` under `ranking_mode="utility_per_gb"` (config
`space_delete_ranking`, default-off Stage 5b; the default `"score"` mode ranks
by score alone and is byte-identical to the historical order). Today `v` = the
watchability score; with a calibrated model it becomes `p̂·v` — true expected
utility per GB (§9).

**Downgrade vs delete.** A downgrade reclaims `ΔGB` (quality delta) at cost
`p̂·Δv` (utility of the lost quality tier) and is non-destructive — the title
survives and the flip is restorable. A deletion reclaims the whole file at
cost `p̂·v`. Per GB reclaimed, downgrades are typically cheaper *and*
reversible, so Stage 1 runs both downgrade passes first, and Stage-2 deletion
is floor-gated (`free < T`) for hysteresis.

**Downgrade-first credit.** Downgrade reclaim lands later (profile flip now,
smaller file when the re-grab imports). Deleting to cover the full deficit
while re-grabs are in flight over-deletes, so the coordinator credits the
ledger's projected downgrade reclaim against the target
(`foundation.downgrade_credit`, a proven pure mirror — the coordinator is not
rewired):

    delete_target = max(0,  (U − free)  −  r · Σ_i max(reclaim_i, 0))

`r = clamp(space_downgrade_credit_ratio, 0, 1)`; only positive
`planned_action='downgrade'` stamps count. Each run recomputes from actual
free space, so realized downgrades shrink the deficit itself; a stalled
re-grab keeps crediting until its stamp clears (the documented caveat on the
config knob).

## 8 The challenger — shadow GBT

Stage 4 trains an additive-tree model

    F(x) = Σ_m ν · T_m(x)

by gradient boosting on log-loss over the labeled snapshots (`sig_*` + a few
context columns), with capacity held down for the data regime: `num_leaves=15`,
early stopping on a temporal validation window. Raw margins are then
isotonic-calibrated (§5) on that window, so the persisted `challenger_p` is an
honest probability. Observe-only by construction: the runtime hook can only
log divergence and stamp `challenger_p` into snapshot rows; nothing downstream
reads it (`challenger/gbt_shadow.py`, `ml_train_challenger`).

**Promotion criterion:** forward ΔAP > 0 — the challenger must beat the
production score's average precision on matured, strictly-forward windows,
sustained across windows, before it can be *considered* for any decision
surface. There is deliberately no auto-promotion path; today the criterion is
a measurement, not a switch.

**Divergence diagnostics:** Spearman `ρ_s(score, challenger_p)`
(`foundation.spearman_rho`) plus the top-10 rank disagreements, logged per run
— where the learned model and the hand model disagree is exactly where labels
accumulate the most informative evidence.

## 9 Adoption roadmap — hand-set constants that become derived quantities

Every constant below stays hand-set until its owning formula's data gate is
met; adoption means deriving the value from `foundation/` formulas, never
bypassing the parity oracle.

| hand-set constant (today) | derived quantity (target) | owning formula |
|---|---|---|
| `grace_days` flat window (`lifecycle/grace_policy`) | grace expires when `P(watch within H \| gap) < τ_g` | §6 `residual_watch_probability` |
| JIT upgrade window (Sonarr `run_jit_quality_upgrades`) | horizon covering a target share of next-watch mass (measured: 86% inside 7d) | §6 `discrete_hazard` |
| delete score ceilings / `delete_tier_size` | density cutoff at the pool quantile that meets the reclaim target | §7 `value_density` |
| acquisition/upgrade thresholds τ (`score_to_profile` ladder) | calibrated `P̂(watch)` bands with per-tier storage-cost break-evens | §5 `isotonic_calibrate` + §7 expected utility |
| per-group weights `w_g = 1` (+ `affinity_boost`) | refit multipliers `m_g`, gated at `n_pos ≥ 100` | §4 `fit_logistic_irls` + `suggested_weight_multipliers` |
| EB prior strength `k = 5` | marginal-likelihood (empirical-Bayes proper) fit on held-out gaps | §6 `empirical_bayes_pool` |
| `delete_recency_ramp` half-life (30d exp decay) | `1 − residual-P` directly (the ramp is an exponential proxy for it) | §6 `residual_watch_probability` |
| `space_downgrade_credit_ratio = 1.0` | measured realization rate of projected downgrade reclaim | §7 `downgrade_credit` |

## 10 Data-regime constraints — why classical statistics is the ceiling

The ground truth is ≈931 Tautulli events household-wide, of which only ~54 are
tmdb-resolvable movie watches; positives per evaluation window are far fewer.
That bounds model capacity from above:

* **Events-per-parameter heuristic** (Peduzzi-style, ≥10–20 events per
  parameter): a ceiling of roughly 45–90 effective parameters *total*. The
  ~25-signal linear refit sits at the edge — viable only with ridge and only
  as a suggestion table. A mid-sized GBT is already over budget without the
  tiny-tree + early-stopping constraints of §8; any deep model (10⁴+
  parameters) memorizes this dataset outright.
* **VC sketch**: a linear model in d dimensions has VC dimension d+1;
  the generalization-gap bound scales like `√(d·ln n / n)`. With d ≈ 25,
  n ≈ 931: `√(25 · 6.8 / 931) ≈ 0.43` — enormous. Uniform-convergence
  guarantees are vacuous here; honesty comes from strong regularization,
  temporal splits, and refusing to fit when positives are absent — which is
  exactly what the tools do.
* **What changes with data**: at `n_pos ≈ 100` the refit multipliers become
  usable suggestions (§4's power gate); at ≈300, per-group calibration curves
  and threshold derivation (§9) get stable; at ≈1000, interaction terms or a
  larger challenger become defensible. Until then, bucketed hazards with
  strong EB pooling, univariate AUC-PRs, and an L2 logistic refit are not the
  fallback — they are the correct estimators for this n.

## 11 Measured results (2026-07-26) — the theory above, priced by experiment

The simulation harness (`ml_simulate.py`, seeds 42/7) and the truncated-replay
backfill turned several of the preceding sections from claims into measurements.
This section records the numbers so future readers know how much trust each
estimator has *earned*, not just what it promises.

* **Where 87 comes from, and what it buys** (§4, §10). The two-sided power
  identity `n_pos ≈ (z_α/2 + z_β)²/δ² ≈ 7.85/δ²` gives ≈87 at δ = 0.3 — the
  point where the refit gains 80% power to *detect* a large effect at α = 0.05.
  It is the absolute minimum, direction-only, largest-effects-only. The inverse
  square is the operative constraint: δ = 0.15 needs ≈350, δ = 0.10 needs ≈780.
  Hence the 100/300/1000 ladder in §10.
* **The magnitude noise floor, measured.** With planted multipliers
  [1.0, 0.75, 0.45, 0.25, 0, 0] and `n_pos ≈ 1000`, the refit returned
  [1.0, 0.98, 0.66, 0.29, 0.21, −0.10]: rank order perfect (Spearman ≥ 0.98),
  magnitudes wobbling ±0.2 — including a 0.21 "ghost" on a planted zero. SEs
  shrink as 1/√n: expect ±0.6 at n_pos ≈ 100, ±0.35 at ≈300. Power is also
  per-group: a group with 25 fires is a tiny sub-sample regardless of total
  n_pos (the real-data `B2_director −1.000` on 25 fires is the canonical
  forbidden sign-flip specimen).
* **Hazard recovery.** Planted vs recovered household hazard agreed to
  max |ĥ−h*| = 0.044 over buckets with at_risk ≥ 40; the product identity is
  directly verifiable in the report (`1 − (1−0.333)(1−0.256) = 0.504` = the
  printed residual at gap 0). Buckets with at_risk < 40 carry SE ≈ 0.09 and are
  excluded from tolerance for that reason.
* **Calibration, quantified** (§5). Treating the min-max-scaled score as a
  probability: ECE ≈ 0.39. Raw GBT margin: ≈0.019. After isotonic: **≈0.0016**.
  Ranking quality and probability honesty are separable properties; isotonic
  buys the second without touching the first.
* **The challenger's surplus is interactions** (§8). Planted first-watch truth
  was linear-logistic in sig_*, yet the GBT scored AP 0.36 vs the linear
  refit's 0.14 — because its feature set includes context columns
  (`watched_before`, which under the planted hazard marks the rewatch pool)
  whose *interaction* with the linear signals is invisible to the linear form.
  On the refit's own features the GBT could at best match it. This is the
  precise mechanism by which the challenger may beat the linear scorer on real
  data — and forward ΔAP remains the only admissible judge.
* **Backfill provenance** (§2 amendment). Truncated-replay snapshots
  (`ml_backfill_snapshots.py`) reconstruct x_t from history strictly < t with
  library membership via Radarr `added ≤ t`; credits/metadata are today's state
  (stamped `leakage_flags`), and rows carry `source="backfill"` — excluded from
  every tool unless `--include-backfill`. First real-household readings, under
  those caveats: n_pos = 38 @ 14d, current hand-tuned scorer AP ≈ 4.6× random
  lift. Directional, not evidence — the prospective store is the only judge.
