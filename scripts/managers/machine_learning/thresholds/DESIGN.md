# thresholds — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **thresholds**

**Package** — `scripts.managers.machine_learning.thresholds`
**Status** — ✅ Implemented · 🔴 Documents a 16.5 % cross-axis disagreement on delete-eligibility
**Related** — [README.md](./README.md) · [`scoring/DESIGN.md`](../scoring/DESIGN.md) · [`ledger/DESIGN.md`](../ledger/DESIGN.md)

---

## 1. Problem statement

Glidearr's behaviour is defined by roughly twenty hand-set cutoffs on a 0–100
scale — delete below 17, monitor above 30, acquire above 35, 4K above 75. Each was
reviewed once against a distribution that has since moved.

Three problems compound:

1. **A literal is a private opinion.** `score >= 35` embedded at a call site
   cannot be swapped for a calibrated value without editing every consumer, and
   nothing records what 35 was *meant* to select.
2. **The distribution moves under them.** Group D v2 translated the persisted
   score axis down ~13 points. Every literal that stayed put silently became a
   different decision — a delete ceiling of 20 became far more aggressive than
   the one that was reviewed.
3. **There is more than one axis.** Two different call paths produce
   "the watchability score", and they disagree about 16.5 % of movies (§3.4).
   A pass that "fixes" one family's numbers to match another's makes things worse.

This package is the answer to all three: one accessor, a full inventory with
provenance, and a calibration path that defaults to **reporting only**.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One seam every routed cutoff reads through. |
| G2 | The default mode is byte-identical to the pre-registry code. |
| G3 | Calibrated values are shrunk toward the reviewed constant by evidence strength. |
| G4 | A derived cutoff is inspectable, diffable and blamable. |
| G5 | Never a silent switch — a fallback always warns. |
| G6 | The inventory records *why* each constant is what it is. |
| G7 | "What would change?" is answerable before anything changes, every run, for free. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Deriving every cutoff | `routed=False` entries are reported but keep their literal — reasons on each spec. |
| N2 | Mid-run adaptation | Derived values come from the *previous* run's artifact (§3.3). |
| N3 | Unifying the two axes | That is a behaviour change to demote/restore/monitor, tracked separately (§3.4). |
| N4 | Multi-rung ladders | One probability per rung is the wrong shape; see `quality_ladder`. |

---

## 3. Architecture

### 3.1 The seam

```python
threshold = get_threshold("movie_monitor", self.config, threshold)
```

| Mode | Behaviour |
|---|---|
| `shadow` (default) / `off` / unknown | Returns `default` **unchanged and unconverted** — the same object |
| `derived` | Returns the effective (shrunk) value; `int` default ⇒ `int` result so downstream typing is unchanged |

At `n_pos = 0` the two branches agree **by construction**: the effective value
would equal the constant exactly, and the `None` fallback returns the caller's own
object rather than a float copy — so a cold install is byte-identical to shadow.

### 3.2 Empirical-Bayes shrinkage

```
effective = w · derived + (1 − w) · constant        w = n_pos / (n_pos + k)
```

`k = 150` is chosen as the midpoint of the "usable" (100) and "stable" (300)
evidence milestones — *"the derived value is trusted half-way exactly where the
milestones say it has stopped being a rumour and has not yet settled."*

`shrinkage_k` refuses to configure `k ≤ 0`, because that would mean *"believe the
calibrator completely at one positive"* — the exact failure the shrinkage exists
to prevent. (`derive.shrinkage_weight` still honours it if passed explicitly.)

### 3.3 Previous-run artifact, not mid-run fit

The calibrator fits once at end-of-run inside `PlanSummary`. `derived` mode reads
the newest `<cache>/ml/reports/thresholds_*.json` — last run's output.

Two properties fall out, both stated as the reason:

- **Auditable** — *"a value someone can open, diff and blame."*
- **Stable within a run** — *"it can only change between runs, not during one."*

That sidesteps a mid-run ordering dependency entirely: no consumer's cutoff
depends on whether it ran before or after the fit.

Reports written before shrinkage existed carry only `derived`/`gate_ok`; those are
read as the effective value when `effective` is absent — a documented
backward-compatibility path.

### 3.4 🔴 Two watchability axes, disagreeing on 16.5 % of movies

| Axis | Path | Missing |
|---|---|---|
| **V2** (persisted) | `refresh_scores` → `_build_score_map` → `_score_row` | — full input set |
| **LEGACY** (anomaly) | `repair/anomaly.py::_score_owned` on raw Radarr dicts | `transcode_profile` (⇒ Group D takes the v1 path), `platform_usage`, `transcode_stats`, `target_resolution`, `video_codec` (⇒ D1 = 0.0, D3 = 0.0, D2 flat +2.0), C3, C4, per-user affinity, audience split; `completion_pct` hardcoded 0.0 |

Measured on 2,151 owned movies: LEGACY median 7 / p99 21 / max 36; V2 median 8 /
p99 34 / max 58; **r = 0.659**. At the adopted cutoffs the axes disagree on
**354 movies (16.5 %)** — 349 that LEGACY calls below-floor and V2 does not, 5 the
other way.

**Why the delete family was split rather than unified:** Group D v2 translated
V2 down ~13 and *did not move* LEGACY (whose Group D term went flat +2.0 → flat
+2.0 — the v1 branch is unreachable in the anomaly path's input set either way).
A pass re-anchoring the whole family 20 → 17 therefore tightened four thresholds
that had moved and four that had not. The two groups are split accordingly:

| Family | Value | Rationale |
|---|---|---|
| V2 delete family | **17** (re-anchored from 20) | Preserves original selectivity — computed by reconstructing the pre-Group-D distribution and matching percentiles (movies `standard`: 84.3 % below 20 pre-D ⇒ 17.0 on v2) |
| LEGACY delete family | **20** (deliberately not re-anchored) | Their axis never moved; re-anchoring would tighten a floor against an unshifted distribution |

**The stated correct end state is *not* "pass a `transcode_profile` here."**
Threading a profile into `_score_owned` would unify Group D and nothing else — the
path would still lack C3, C4, per-user affinity, audience split and real
engagement, producing a **third axis**. Real unification means making the
demote/restore legs read the persisted column, which drops the live `has_credits`
deferral and makes the prune depend on `refresh_scores` having run in-process.
*"That is a genuine behaviour change… it is tracked, not smuggled in behind a
threshold edit."*

**Existing mitigation:** with `space_coordinator_enabled`, `coordinator_owns_deletion`
makes the LEGACY delete stage inert — it can only **unmonitor** (reversible, no
data loss) while every real deletion goes through the coordinator on V2.

### 3.5 The stub population

`pilot_min_watchability` (20) and `series_monitor` (35) look like delete-family
members. They are not — they act on **stubs**: 7,600 of 11,973 series own no
episode file.

Under v1 a stub collected a flat +2.0 (no codec) while a file-owner collected +12.
Under v2 a stub scores exactly 0.0 on Group D. So the stub axis moved **−2**
against the file-owning axis's **−13**.

Measured effect: `pilot_min_watchability` stubs clearing it **134 → 96**;
`series_monitor` **0 → 0**. Both small enough that re-anchoring would be fitting
to noise.

### 3.6 Hysteresis pairing

A delete floor and its restore floor are welded: set them apart and a title in the
gap is deleted and restored every run.

| Service | Delete | Restore | Safe because |
|---|---|---|---|
| Radarr | `owned_demote_score_threshold` 20 (LEGACY) | `owned_restore_score_threshold` 20 | Restore must not drop below the floor (`owned_restore_min_age_days` defaults to 0, so nothing damps it) |
| Sonarr | `tv_space_pressure_score_ceiling` 17 (V2) | `tv_restore_score_threshold` 17 | `floor == ceiling` is thrash-free — **both comparisons are strict**, so a series at exactly 17 is neither |

`series_restore` was **split out** of `owned_restore_score_threshold`: one key was
serving a LEGACY consumer (wants 20) and a V2 consumer (wants 17). No single value
is correct for both, so the key was split rather than compromised — and the split
*restores* the family's own convention, since every other member is already
service-split.

### 3.7 Shadow comparison (G7)

`compare` counts, per threshold, how many entities sit each side of the current
and effective cutoffs and how many would **flip**.

A threshold partitions at `score >= t`. Both shapes — "act at/above" and "act
below" — induce the *same* partition, so the flip count is identical either way
and reported once. `direction` is `looser` / `stricter` / `identical`, with
`_no_effect` appended when the line moved but nothing crossed it.

All counting is on the **effective** value — *"that is the partition the household
would experience"* — with the raw derived value carried along so the reader sees
how far shrinkage pulled it back.

`n_pos` prints with provenance: `38 (12p+26b)` = 12 prospective + 26 backfilled,
*"because on a fresh install the evidence is entirely reconstructed and that must
be visible in the same glance as the value it moved."*

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | One accessor taking the caller's own resolved default | G1 + G2 — the consumer stays authoritative in shadow | Registry owns the constant |
| D2 | Shadow is the default | Calibration must prove itself before it drives | Derived by default |
| D3 | Unknown mode ⇒ default | A typo must never arm `derived` | Raise, or honour it |
| D4 | Shrink toward the reviewed constant | G3 — a calibrator on 38 positives is a rumour | Use the derived value raw |
| D5 | `k` refuses ≤ 0 via config | Prevents "believe it completely at one positive" | Allow it |
| D6 | Read the previous run's artifact | G4 + no mid-run ordering dependency | Fit per consumer |
| D7 | Warn once per threshold per process | G5 — visible without flooding | Silent, or warn every call |
| D8 | Full inventory with `consumer` file:line and `rule` | G6 — a constant without provenance cannot be reviewed | Names only |
| D9 | `routed=False` entries reported anyway | Visibility without action; each carries its reason | Omit them |
| D10 | V2 family re-anchored 20 → 17, LEGACY left at 20 | The axes moved differently; one number cannot serve both (§3.4) | Re-anchor all, or none |
| D11 | Stub thresholds left alone | Their population moved −2, not −13 (§3.5) | Move with the family |
| D12 | `series_restore` key split | One key cannot serve two axes; hysteresis partners differ | Compromise on one value |
| D13 | `include_backfill` defaults **True** here only | A fresh install has no matured prospective labels at all; a monotone score→P map needs ordering roughly right, which tolerates the known leakage far better than an accuracy claim does | Match other ML entry points |
| D14 | `DEFAULT_TARGET_P` truncated, never rounded up | A target one ulp above a plateau's top is unreachable, and the inverse clamps to 100 — "nothing qualifies" | Round to nearest |
| D15 | Counting on effective, not raw | It is the partition the household would experience | Count on derived |
| D16 | `GLIDEARR_THRESHOLDS_OFF` kill switch | Ops escape hatch matching the other consent/kill env vars | Config only |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | `shadow` and `off` return the caller's object unchanged. |
| I2 | An unrecognised mode reads as `shadow`. |
| I3 | A missing or gated derived value falls back to the literal **and warns once**. |
| I4 | At `n_pos = 0`, effective == constant exactly. |
| I5 | Derived values change only between runs. |
| I6 | `routed=False` specs always read their literal. |
| I7 | Every routed spec belongs to exactly one bucket. |
| I8 | A V2-axis threshold is never compared against a LEGACY-axis score, or vice versa. |
| I9 | A restore floor never sits below its delete partner. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| No report yet, mode `derived` | `name not in _DERIVED` | Literal + warning naming the fix ("run once in shadow") | Safe | ✅ Explicit |
| Calibrator ungated (no labels / too few positives / degenerate map) | `value is None` | Literal + warning; *"exactly what a shrinkage weight of 0 would have produced"* | Safe | ✅ |
| Report unreadable | `except` | `source: "… (unreadable)"`, all values absent | Safe | 🟡 In meta only |
| Non-numeric derived value | `float()` fails | Literal + warning | Safe | ✅ |
| **Axis confusion** — a cutoff routed onto the wrong scale | **Review only** | Silently wrong decisions | 🔴 16.5 % disagreement already exists | ❌ **None** |
| Score axis translated again | **None** | Every literal silently means something new | 🔴 Happened once (−13) | ❌ **None** |
| `k` misconfigured ≤ 0 | Accessor refuses | Default 150 | Safe | ❌ None |
| Backfill leakage inflates `n_pos` | Provenance split in the report | Visible as `(12p+26b)` | Bounded | ✅ Printed |

**Rows 5 and 6 are the standing risks**, and neither has a detector. Row 6 is the
same coupling `GLD-LIK-01` names from the likelihood side — this package is where
it would be cheapest to catch, since it already fits a calibrator to the live
distribution every run.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `ml.thresholds.mode` | `shadow` | `shadow` \| `derived` \| `off` |
| `ml.thresholds.targets.{acquire,monitor,delete,uhd}` | 0.0378 / 0.0298 / 0.0023 / 0.0378 | Target P(watch within H); out-of-range falls back |
| `ml.thresholds.horizon_days` | 14 | Label horizon |
| `ml.thresholds.max_fit_days` | 365 | Bounds the label join; 0 = unbounded |
| `ml.thresholds.shrinkage_k` | 150 | Prior strength of the constant |
| `ml.thresholds.include_backfill` | **True** | Uniquely true here (D13) |
| `GLIDEARR_THRESHOLDS_OFF` | — | Ops kill switch |

`thresholds` at top level is accepted as an alias for `ml.thresholds`.

Per-threshold overrides are the specs' own `config_key`s — `space_pressure_score_ceiling`,
`tv_restore_score_threshold`, `routing.movies.4k_dual_min_score`, etc.

---

## 8. Implemented capabilities

- ✅ Single accessor with identity-preserving shadow/off modes
- ✅ Full threshold inventory with bucket, service, consumer `file:line`, rule, config key and rationale
- ✅ Four target-probability buckets, defaults obtained by inverting today's constants
- ✅ Empirical-Bayes shrinkage toward the reviewed constant
- ✅ Previous-run artifact as the derived source — auditable, stable within a run
- ✅ Warn-once fallback naming threshold and reason
- ✅ Shadow comparison with flip counts and direction
- ✅ End-of-run grid with raw / w / effective / `n_pos (p+b)` / confidence / would-flip
- ✅ Two-axis split preserving each family's reviewed selectivity
- ✅ Percentile-matched re-anchor of the V2 delete family
- ✅ Stub-population analysis justifying non-movement
- ✅ Hysteresis-safe restore/delete pairing with a split key
- ✅ `falsy_means_default` handling for `cfg or DEFAULT` consumers
- ✅ Ops kill switch

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-THR-01` | 🔴 **Axis-tagged specs + an assertion** that a threshold is only ever compared against its own axis | §3.4: 16.5 % of movies already get different delete verdicts by path. The spec dataclass has `service` but no `axis` field | S | — |
| `GLD-THR-02` | 🔴 **Axis-drift detector** — the calibrator already fits the live distribution every run; compare its moments to the last report and warn on a shift | Closes `GLD-LIK-01` **and** §6 row 6 at near-zero marginal cost. This package is the cheapest place in the codebase to catch it | M | `GLD-LIK-01` |
| `GLD-THR-03` | **Unify the LEGACY axis** — make demote/restore/monitor read the persisted `watchability_score` | The stated correct end state; needs its own before/after count and drops the `has_credits` deferral | L | D27 |
| `GLD-THR-04` | **Surface the shadow grid in the run summary**, not only the audit JSON | G7 is delivered every run and mostly unread | S | `GLD-WEB-01` |
| `GLD-THR-05` | **Route `movie_search` (60)** once per-tier storage-cost break-evens exist | Currently `routed=False` for a documented reason | M | — |
| `GLD-THR-06` | **Ladder-shaped derivation** for `quality_ladder` — P-bands per rung, not one probability | Six rungs, one target is the wrong shape | L | `GLD-SCO-05` |
| `GLD-THR-07` | **Likelihood-scale calibrator** so `uhd_reconcile` / `uhd_universe` can be derived | They compare watch-likelihood; routing a watchability-calibrated cutoff onto that scale would be silently wrong | M | `GLD-LIK-04` |
| `GLD-THR-08` | **Re-anchor procedure as a runnable tool** — the percentile-reconstruction method is documented in a comment only | It will be needed on every axis translation | S | `GLD-LIK-08` |
| `GLD-THR-09` | **Assert restore ≥ delete** for each hysteresis pair at config load | I9 is currently maintained by the comments | S | — |
| `GLD-THR-10` | **Report cross-axis disagreement count** each run — the 354/2,151 figure recomputed | Turns a one-off measurement into a monitored metric | S | `GLD-THR-01` |
| `GLD-THR-11` | **Bound `n_pos` confidence in the grid** — "38 positives" needs an interval before it drives anything | Shrinkage handles it mathematically; the display does not say so | S | `GLD-EVA-08` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should the LEGACY axis be unified onto the persisted column, accepting the `has_credits` deferral loss and the `refresh_scores` dependency? *(= D27)* | `GLD-THR-03` |
| Q2 | Is 16.5 % cross-axis disagreement tolerable given the coordinator mitigation, or does it need closing regardless? | `GLD-THR-01`, `GLD-THR-03` |
| Q3 | What `n_pos` justifies flipping the default to `derived`? The specs cite 100 "usable" / 300 "stable". | — |
| Q4 | Should `include_backfill` stay True here once prospective labels mature? | `GLD-EVA-01` |
| Q5 | Should the target probabilities be re-derived after each axis translation, or are they axis-independent? | `GLD-THR-02` |

## 11. Related designs

- [`scoring/DESIGN.md`](../scoring/DESIGN.md) §3.3 — the axis question this package answers definitively
- [`likelihood/DESIGN.md`](../likelihood/DESIGN.md) §3.3 — the same coupling from the other side
- [`ledger/DESIGN.md`](../ledger/DESIGN.md) §3.2 — `PlanSummary` hosts `report.run`
- [`labels/`](../labels/) — the label source the calibrator fits on
- [`space/DESIGN.md`](../space/DESIGN.md) — `coordinator_owns_deletion`, the LEGACY-axis mitigation
