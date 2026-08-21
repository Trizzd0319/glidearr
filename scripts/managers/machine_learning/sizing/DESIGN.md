# sizing — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **sizing**

**Package** — `scripts.managers.machine_learning.sizing`
**Status** — ✅ Implemented · 🟡 One module contradicts the package's own purity standard · 🔴 Known anime under-estimate
**Related** — [README.md](./README.md) · [`space/DESIGN.md`](../space/DESIGN.md) · [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8 P-F

---

## 1. Problem statement

"How big will this be?" is asked from at least six places: acquisition `~size`,
JIT upgrade space reservation, active-watcher upgrade anticipation, quality
file-size comparison, storage forecasting, and the coordinator's reclaim
arithmetic. Before this package, each answered independently.

Three failure modes followed:

1. **Disagreement.** Two subsystems reserving space for the same upgrade with
   different estimates produce a plan that does not add up.

2. **Unbounded garbage.** Radarr's quality definitions carry a `maxSize`
   ceiling around 2000 MiB/min. Read as a rate, that yields a **187 GB estimate
   for a 96-minute movie** — a number that then propagates into every space
   decision downstream.

3. **Cold-start guessing.** A brand-new install has no measurements, and a table
   of invented numbers is worse than no table, because it looks authoritative.

The answer is a single model with an explicit resolution order, an empirical
clamp, and a calibration table seeded from **6,914 real files** and overlaid at
runtime by live measurement.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One model — every size question resolves through it. |
| G2 | Measured beats calibrated beats default, always in that order. |
| G3 | No estimate can escape the empirical bounds. |
| G4 | Importable from any layer — dependency-free. |
| G5 | Cross-service quality names resolve without normalisation. |
| G6 | The pure math is separable from the I/O that feeds it. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Reading the caches it calibrates from | G6 — the `SizeCalibrator` bridge does that. |
| N2 | Per-title prediction | It is a per-quality *rate*, applied to runtime. |
| N3 | Persisting the overlay | The bridge persists to `size_model/calibration`. |
| N4 | Codec-level modelling | Codec is a Group-D concern in `scoring/`. |

---

## 3. Architecture

### 3.1 The pure/impure split, done right

This package is the **other half** of the P-F structure recorded in
[`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8:

```
machine_learning/size_calibration.py     ← IMPURE bridge (root tier)
    SizeCalibrator: registry walk, Parquet load, global_cache persist,
                    size_model.set_calibration side effect
        │  calls
        ▼
sizing/size_calibration.py               ← PURE math (guarded subpackage)
    fold_stats · compute_calibration_table · movie_runtime_min
    calibration_is_fresh
```

The pure module states it plainly: *"The service-io orchestrator … is the thin
**BRIDGE** that calls these functions. Pure + unit-testable with hand-built
dicts; no registry, no global_cache, no HTTP, no `set_calibration` side
effect."*

That confirms the session-5 reading: the root-tier impurity is **deliberate
architecture**, not a migration leftover. It is the strongest argument for
`GLD-ML-17` (document the two-tier structure) — the pattern is correct and only
undocumented.

### 3.2 `size_model.py` is the reference module in this repo

It is worth naming why, because the qualities are transferable:

| Property | Evidence |
|---|---|
| **Explicit dependency stance** | *"intentionally dependency-free (no logger, cache, or registry) so it is safe to import from any layer"* |
| **Bounds justified empirically** | 900 chosen against a measured 540 max and an ~850 theoretical BR-DISK |
| **Bounds justified by a real bug** | The 187 GB estimate is named |
| **Sample sizes published per tier** | `Bluray-720p 1170 … WEBDL-2160p 4` |
| **Every deviation from measurement declared** | DVD-R pinned, WEBRip-1080p conservative, Bluray-2160p nudged — each with its reason |
| **Ordering constraint stated** | Bluray-2160p nudged *"to stay ≥ WEBDL-2160p"* |
| **Cross-service aliasing** | Sonarr and Radarr spellings share a value (G5) |
| **Provenance** | The exact tool and run that produced the table |

A reader can audit every number without asking anyone.

### 3.3 🔴 Anime is knowingly under-estimated

```python
# NOTE: revisit — a flat 5 MiB/min underestimates anime 4K/remux films.
ANIMATED_MB_PER_MIN = 5.0
```

A **single flat rate** for all animated content, *"regardless of profile"* — so
an anime 4K remux and an anime 480p WEB-DL are estimated identically.

The comment justifies the conservatism (*"anime encodes … trend far smaller than
live-action at the same quality label"*) and then flags its own limit. Both are
true, and the second matters here more than it might elsewhere, because **anime
is a first-class concern in this library**: there is an `animeGenres` config
block, MAL integration, a dedicated anime routing path, and anime-specific
custom-format tiers in the TRaSH profiles.

The direction of the error is the concerning one. Under-estimating size means:

- space reservation for an anime upgrade reserves **too little**,
- the coordinator's downgrade credit over-counts projected reclaim,
- and `~size` on an anime acquisition under-reports.

All three push toward acquiring or upgrading more than the disk can hold.
`GLD-SIZ-01`.

Note that the live overlay partially rescues this: once the library has ≥
`MIN_SAMPLES` (20) anime files at a tier, *measured* wins (G2). The flat rate
only governs tiers with too few samples — which, for anime 4K remux
specifically, is exactly the thin-sample case.

### 3.4 🟡 `storage_estimator.py` contradicts the package's own standard

Its docstring says *"Pure forecasting on top of the shared `sizing.size_model` —
no HTTP, no cache writes."* But:

```python
from scripts.support.utilities.logger.logger import LoggerManager

class MLStorageForecaster:
    @LoggerManager().log_function_entry
    def __init__(self, logger, cache):
        self.logger = logger
        self.cache = cache
```

Two problems:

**It logs.** Sibling modules in the same layer state the stricter standard
explicitly — [`affinity/genre_affinity.py`](../affinity/README.md): *"PURE — no
HTTP, no Tautulli API, **no logging**, no global_cache"*;
[`features/completion_stats.py`](../features/README.md): *"PURE — … no HTTP, **no
logging**, no global_cache."* And `size_model.py`, in this very package, is
*"intentionally dependency-free (**no logger**, cache, or registry)."*

So "pure" means one thing three modules away and another here. `brain_purity.py`
does not catch it — `LoggerManager` is not an HTTP client, a service import, or a
`*_api` module — which makes this a §8 **P-B** instance: the guard's name implies
more than its scope covers.

**`self.cache` is never read.** The docstring says it is *"accepted for API
compatibility but only read-through"* — but nothing in the module reads it.
`_load_bitrate_estimates` returns `{"animated": 5.0}` without touching it. It is
a stored, unused constructor parameter: §8 **P-A** at parameter scale.

### 3.5 `file_comparison.py` — a clean absent-vs-zero distinction

```
expected <= 0   → 'keep'      (unknown ⇒ no opinion)   ← correct
actual == 0     → 'upgrade'
actual < expected × 0.6  → 'upgrade'
actual > expected × 1.4  → 'downgrade'
otherwise       → 'keep'
```

The `expected <= 0 → 'keep'` branch is exactly the P-C discipline the rest of the
codebase keeps getting wrong: **unknown expected size produces no opinion**,
rather than defaulting to a comparison against zero.

`actual == 0 → 'upgrade'` does conflate a genuinely 0-byte file with an unknown
size — but the direction is *acquisitive*, not destructive, so the failure mode
is a wasted re-grab rather than a lost file. Acceptable, and worth stating.

### 3.6 🟡 Sonarr does not yet delegate

> Pulled from `radarr/quality/file_size.compare_file_size` (the 0.6 / 1.4
> threshold decision); **`sonarr/quality/filesizes.compare_file_sizes` will
> delegate here too (Step 1d follow-on).**

So the 0.6/1.4 comparison currently exists **twice** — here, and in Sonarr's own
`filesizes.py`. That is §8 **P-E**, mid-migration and declared. Until Step 1d
completes, a change to the acceptance band must be made in two places.

Relevant that [`sonarr/DESIGN.md`](../../services/sonarr/DESIGN.md) §3.3 found
`SonarrQualityManager` is **filtered out of `component_dependencies` and never
loads** — so the Sonarr copy may not even execute. That would make the duplicate
harmless *and* mean Sonarr file-size comparison is not happening at all. The two
findings need resolving together.

### 3.7 Calibration freshness

```python
return 0 <= age_s < max_age_days * 86_400
```

A **future** timestamp (`age_s < 0`) reads as stale, so a forward clock skew
forces recalculation rather than pinning a bad table indefinitely. Tolerant of
missing and naive timestamps; any exception → `False`, i.e. recompute. Every
branch fails toward *recalibrating*, which is the cheap direction.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | One model for every size question | G1 — independent estimates made plans that did not add up | Per-subsystem estimation |
| D2 | MiB/min as the unit | Separates rate from runtime, so one table serves movies and episodes | Per-title sizes |
| D3 | Measured > calibrated > resolution > default | G2 — the library is the best evidence about itself | Fixed table |
| D4 | Hard clamp `[0.5, 900]` | G3 — bounds a units/field bug before it reaches a planner | Trust upstream |
| D5 | Bounds from measured extremes, not round numbers | 540 measured, ~850 theoretical ⇒ 900 | Arbitrary ceiling |
| D6 | Publish per-tier `n` | A reader can judge which values to trust | Table only |
| D7 | Declare every deviation from raw measurement | DVD-R at n=3 is an artifact, not a rate | Use raw means |
| D8 | Nudge `Bluray-2160p` to preserve tier ordering | A non-monotone table produces nonsensical upgrade economics | Keep the measured value |
| D9 | Share values across Sonarr/Radarr spellings | G5 — avoids a normalisation layer | Normalise names |
| D10 | `size_model` dependency-free | G4 — importable from any layer, including `support/` | Allow a logger |
| D11 | Pure calibration math split from the I/O bridge | G6 — the arithmetic is unit-testable with hand-built dicts | One module |
| D12 | Flat anime rate | Anime encodes trend smaller; a conservative single rate beat a wrong per-tier guess at the time | Per-tier anime table |
| D13 | `expected <= 0 ⇒ keep` | Unknown means no opinion (§3.5) | Compare against 0 |
| D14 | Freshness fails toward recompute | Recalculating is cheap; a pinned bad table is not | Fail toward reuse |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Every size estimate resolves through `size_model`. |
| I2 | Every returned rate is within `[MIN_MB_PER_MIN, MAX_MB_PER_MIN]`. |
| I3 | A measured rate always beats the calibrated table. |
| I4 | `size_model` imports no logger, cache or registry. |
| I5 | The calibration overlay has exactly one copy, whichever import path is used. |
| I6 | Sonarr and Radarr spellings of a quality resolve to the same rate. |
| I7 | The calibration table is monotone across tiers. |
| I8 | Unknown expected size yields no upgrade/downgrade opinion. |
| I9 | The pure calibration math performs no I/O and no `set_calibration` side effect. |

**I4 holds for `size_model.py` and fails for `storage_estimator.py`** (§3.4).
**I7 is maintained by hand** (D8) and nothing asserts it.

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Upstream `maxSize` read as a rate | **Clamp** | Bounded at 900 | Was a 187 GB estimate | ✅ By construction |
| Quality name unrecognised | Resolution default | Coarse per-resolution rate | 🟡 Approximate | ❌ **None** |
| Tier below `MIN_SAMPLES` (20) | Calibration filter | Cold-start table value retained | Correct | 🟡 Debug |
| **Anime 4K/remux** | **Known, flagged in-source** | Flat 5 MiB/min — **under-estimates** | 🔴 Under-reserves space, over-credits reclaim | ✅ In a code comment only |
| Calibration table non-monotone after a refresh | **None** | Upgrade economics invert | 🟡 | ❌ **None** |
| Clock skew forward | `age_s < 0` | Treated stale, recomputes | Safe | ❌ None |
| `actual == 0`, size unknown | — | `'upgrade'` | 🟡 Wasted re-grab, non-destructive | ❌ None |
| Sonarr band diverges from this one | **None** | Two acceptance bands | 🟡 §3.6 | ❌ **None** |
| `storage_estimator` logs from the brain | `brain_purity` **does not check** | Passes the guard | 🟡 §3.4 | ❌ **None** |

**Row 5 is the unguarded one worth closing.** The cold-start table is hand-tuned
for monotonicity (D8), but the *live overlay* is computed from measurement and
nothing re-checks the ordering. A tier with 20 unusually small samples could land
below the tier beneath it, and every upgrade decision in that region would then
be reasoning about a table that says a better tier is smaller.

---

## 7. Configuration surface

Constants rather than config keys — this package is deliberately unconfigured:

| Constant | Value | Where |
|---|---|---|
| `MIN_MB_PER_MIN` / `MAX_MB_PER_MIN` | 0.5 / 900.0 | `size_model.py` |
| `DEFAULT_MB_PER_MIN` | 25.0 | `size_model.py` |
| `CALIBRATED_MB_PER_MIN` | ~30 tiers | `size_model.py` |
| `ANIMATED_MB_PER_MIN` | 5.0 | `storage_estimator.py` |
| `DEFAULT_LOW` / `DEFAULT_HIGH` | 0.6 / 1.4 | `file_comparison.py` |
| `MIN_SAMPLES` | 20 | `SizeCalibrator` (root bridge) |
| `MAX_AGE_DAYS` | 7 | `SizeCalibrator` (root bridge) |

Cache key written by the bridge: `size_model/calibration`.

---

## 8. Implemented capabilities

- ✅ Single MiB/min model with a four-level resolution order
- ✅ Empirical clamp bounding a known real failure
- ✅ Cold-start table seeded from 6,914 measured files, with per-tier `n`
- ✅ Every deviation from raw measurement declared and justified
- ✅ Cross-service quality-name aliasing
- ✅ Live overlay refreshed from the library each run, TTL-guarded
- ✅ Pure calibration math separable from its I/O bridge
- ✅ Weighted fold across instances and services
- ✅ Runtime resolution from `mediaInfo.runTime` (seconds or `H:MM:SS`) with TMDB fallback
- ✅ Freshness check failing toward recompute
- ✅ Size-based upgrade/downgrade classification with a no-opinion branch
- ✅ Size-anomaly detection
- ✅ Rescan-vs-regrab remediation routing (`recommend_action`) — broadcast tiers
  (`HDTV-720p`/`-1080p`) route to RESCAN because a bloated broadcast grade is a
  mis-graded disc source a re-grab cannot fix; `HDTV-2160p` deliberately excluded
- ✅ Re-grab attempt ledger (`should_attempt`/`record_attempt`/`prune_attempts`) —
  bounds the retry loop, size-change resets the budget, unknown sizes never read
  as "changed" or as 0
- ✅ Exact `size_bytes` on every anomaly row (change-detection needs more than 2dp GB)
- ✅ Storage forecasting

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SIZ-01` | 🔴 **Per-tier anime rates** — replace the flat 5 MiB/min. The in-source NOTE already flags it; anime is first-class here (`animeGenres`, MAL, anime routing, anime CF tiers) and the error direction under-reserves space | Under-estimation over-credits reclaim and under-reserves upgrades | M | `calibrate_sizes.py` split by anime classification |
| `GLD-SIZ-02` | 🟡 **Make `storage_estimator.py` match the package's stated standard** — drop the logger, drop the unused `cache` parameter | §3.4: "pure" means two different things inside one package, and `brain_purity` cannot see the difference *(P-B, P-A)* | S | — |
| `GLD-SIZ-03` | **Assert table monotonicity** after every calibration refresh | §6 row 5 — D8 maintains it by hand for the cold-start table and nothing checks the live overlay | S | — |
| `GLD-SIZ-04` | **Complete Step 1d** — make `sonarr/quality/filesizes.compare_file_sizes` delegate here *(P-E)* | Two acceptance bands today. **Resolve jointly with `GLD-SON-01`** — the Sonarr copy may never execute | S | `GLD-SON-01` |
| `GLD-SIZ-05` | **Report calibration coverage** — tiers calibrated vs falling back, and their `n` | The overlay silently covers only well-sampled tiers | S | `GLD-THR-04` |
| `GLD-SIZ-06` | **Extend `brain_purity` to flag logger imports** in guarded subpackages, or declare logging permitted | §3.4 — the standard is stated three ways across the layer | S | `GLD-ML-03`, `GLD-ML-17` |
| `GLD-SIZ-07` | **Distinguish `actual == 0` from unknown size** in `classify_file_size` | §3.5 — non-destructive, but a wasted re-grab is still a grab | S | — |
| `GLD-SIZ-08` | **Re-run `calibrate_sizes.py`** and refresh the cold-start table with current `n` | The table is dated to one measured run; the library has grown | S | — |
| `GLD-SIZ-09` | **Warn when a size estimate hits the clamp** | The clamp silently rescues a units bug that should be fixed upstream | S | — |
| `GLD-SIZ-10` | **Document the size model in `MATH_FOUNDATION.md`** and wrap the estimator in `foundation/` | It is the one production estimator absent from the formula index | S | `GLD-FND-02` |
| `GLD-SIZ-11` | Mirror of `GLD-SON-13`/`-14` — the size-anomaly remediation-loop fixes live in this package's `anomaly.py` (routing set `_MISGRADE_WHEN_BLOATED`, attempt-ledger helpers, `size_bytes` on rows); the service wiring, measurements, and open follow-ups (`GLD-SON-15`/`-16`/`-17`) are registered under §4.20 and §0.1 #57 | One brain module, one register home (P-E avoidance) | — | `GLD-SON-13` |
| `GLD-SIZ-12` | ✅ **The calibration ratchet** — `measured_stats` filtered only to `[MIN_MB_PER_MIN, MAX_MB_PER_MIN]`, which its own docstring calls a guard against CORRUPT RUNTIMES. A mislabelled disc image (the real case: 50.9 GiB "Bluray-720p" = 321.7 MiB/min) passes `[0.5, 900]` comfortably, enters the tier's plain `.mean()`, raises `expected_size_gb`, and therefore raises the `over_ratio x expected` threshold that is supposed to catch the NEXT one — a ratchet that loosens itself, with the detector calling the file an anomaly while the calibrator averages it in as a sample. Measured on a `Bluray-1080p`-shaped tier (n=18): **+20.8% mean, threshold 195 → 236 MiB/min from a single file**. Fixed with median-anchored rejection (`outlier_ratio`, `outlier_min_n=5`) — median because the mean is what the outlier is already corrupting; only at `n >= 5` because an outlier cannot be identified in a sample of one | S | ✅ **Fixed** — §0.1 #65 |
| `GLD-SIZ-13` | ✅ **Grab-time size ceiling** (`quality_caps.py`) — turns the measured MiB/min model into *arr `qualitydefinition` `maxSize` in MB/min, at the same `over_ratio` the anomaly detector uses: *if we would flag it after the grab, refuse it before*. Caps may only TIGHTEN (a poisoned rate must never widen a ceiling — second guard on `GLD-SIZ-12`); thin tiers get no cap at all (too LOW a cap starves a tier silently, and the thin-sample check precedes the tighten check); `MIB_TO_MB` applied or every cap is 4.9% too tight; unlimited (`None`) never read as 0. **Pure planner only** — the service wiring (fetch/diff/dry-run/PUT) is `GLD-RAD-34` | M | ✅ **Fixed** — §0.1 #65 |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is a per-tier anime table warranted, or should anime simply not override once the live overlay has samples? | `GLD-SIZ-01` |
| Q2 | Is logging permitted in the brain? Three modules say no; one does it. *(= D33)* | `GLD-SIZ-02`, `GLD-SIZ-06` |
| Q3 | Does the Sonarr file-size comparison execute at all, given `SonarrQualityManager` never loads? | `GLD-SIZ-04`, `GLD-SON-01` |
| Q4 | Should the acceptance band (0.6 / 1.4) be per-tier? A 4K remux 40 % under expected is a very different signal from a 480p WEB-DL 40 % under. | — |

**Q4 is worth a thought.** The band is a fixed fraction, but the *consequence* of
being under-sized scales with tier: a 720p file at 0.5× expected is probably a
bad encode; a 2160p remux at 0.5× expected is almost certainly a different cut or
a mislabelled source. Same ratio, very different action.

## 11. Related designs

- [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8 P-F — the pure/impure split this package implements correctly
- [`space/DESIGN.md`](../space/DESIGN.md) — the planners consuming these estimates
- [`ledger/DESIGN.md`](../ledger/DESIGN.md) §3.1 — `stamp_universe_plan` calls `estimate_gb_for_profile`
- [`sonarr/DESIGN.md`](../../services/sonarr/DESIGN.md) §3.3 — the unloaded quality manager relevant to §3.6
- [`foundation/DESIGN.md`](../foundation/DESIGN.md) — the formula index this model is absent from
