# machine_learning — Design

> Breadcrumb: [glidearr](../../..) › [scripts](../../README.md) › [managers](../README.md) › **machine_learning**

**Package** — `scripts.managers.machine_learning`
**Status** — ✅ Implemented · 🟡 Migration incomplete (flat legacy modules unguarded)
**Related** — [README.md](./README.md) · [ARCHITECTURE.md](./ARCHITECTURE.md) · [ML_PIPELINE.md](./ML_PIPELINE.md) · [MATH_FOUNDATION.md](./MATH_FOUNDATION.md) · [MIGRATION.md](./MIGRATION.md)

---

## 1. Problem statement

Glidearr must decide, for tens of thousands of titles, what to acquire, what
quality to hold it at, and what to delete when the disk fills. Three properties
make this hard in ways a simple ruleset cannot address.

**The decisions are coupled.** Acquisition, upgrade, downgrade and deletion are
the same optimisation viewed from different angles. Scored independently they
fight: the acquirer grabs a title on Monday that the deleter removes on Tuesday,
forever. They must share one model — this is goal G1 of the whole project.

**The signal is indirect.** Nobody labels "I would enjoy this." The evidence is
playback completion, ratings, watchlist membership, series progress, cast
overlap with things already watched, device capability, transcode behaviour.
Turning that into one comparable number is the core modelling task.

**Every decision is destructive and unverifiable at the time.** Deleting the
wrong film is discovered months later when someone looks for it. There is no
immediate feedback signal. This is why the layer is built to be **replayable**:
the only way to improve a model with no live feedback is to re-run history under
new weights and compare.

Replayability is why brain purity is enforced mechanically rather than by
convention. A single `import requests` makes offline replay impossible.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One 100-point score shared by acquisition, quality and space. |
| G2 | Every decision replayable offline from cached inputs. |
| G3 | Every decision explainable — which signals, what weight. |
| G4 | Pure functions. No I/O, anywhere, ever. |
| G5 | Unknown is not the same as zero, and not the same as maximum. |
| G6 | Thresholds are declared and inspectable, not scattered as literals. |
| G7 | New models can be evaluated in shadow before promotion. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Online learning | Batch. Models refit deliberately, not continuously. |
| N2 | Deep learning | The feature set is small and interpretable; G3 matters more than marginal accuracy. |
| N3 | Per-title manual overrides | Belongs in the service/tag layer, not the model. |
| N4 | Cross-household generalisation | Single household. The model is deliberately overfit to it. |

---

## 3. Architecture

### 3.1 Flow

```
service layer
    │  gathers rows, joins caches
    ▼
contracts/feature_rows.py ──► features/ ──► scoring/ ──► score 0–100
                                   │                          │
                              affinity/                        │
                              people_matrix/                    │
                              likelihood/                        │
                                                                ▼
                                          ┌──────── space/ ──────────┐
                                          │  delete / downgrade /     │
                                          │  upgrade / JIT planners   │
                                          └──────────┬───────────────┘
                                                     │
                          lifecycle/ ────────────────┤
                          classification/ routing/ ──┤
                          acquisition/ discovery/ ───┤
                          playlists/ next_watch/ ────┤
                                                     ▼
                                       contracts/plans.py ──► service APPLY
                                                     │
                                                     ▼
                                              ledger/ (Parquet)
                                                     │
                                          eval/ ◄────┘  replay, forward-validate
                                          challenger/   shadow models
```

### 3.2 The scoring model

A 100-point scale, decomposed into signal groups (A–F, documented in
[`scoring/SCORING_GROUPS.md`](./scoring/SCORING_GROUPS.md)) covering household
affinity, cast/crew overlap, related-graph proximity, engagement, device fit and
critic blend.

Three thresholds are load-bearing across the whole system:

| Threshold | Value | Effect |
|---|---|---|
| 4K eligibility | score ≥ 70 | Below this, no UHD |
| Quality floor | HD-720p | SD is never a target |
| Unknown score | `None` | ⇒ **safe mid-tier** (HD Bluray + WEB) |

**The `None` rule (G5) is the most important line in this document.** A prior bug
treated `score is None` as 4K-eligible, so every unscored title grabbed UHD.
`None` means *not yet scored*, which is different from *scored zero*. Zero would
starve genuinely new content; maximum wastes disk on unknowns. Mid-tier is the
only safe default.

### 3.3 Replayability (G2)

```
labels/snapshots.py   captures feature state at decision time
        │
ledger/decision_ledger.py   records decision + signal breakdown
        │
eval/replay.py        re-runs history under altered weights
eval/forward.py       forward validation on held-out time windows
eval/split.py + stratify.py   time-aware, stratified splits
challenger/gbt_shadow.py      shadow model scored alongside the champion
```

This is the whole reason for G4. If any brain module fetched, replay would need a
live service and the harness would be worthless.

### 3.4 Purity enforcement

[`hooks/brain_purity.py`](../../hooks/brain_purity.py) walks 17 guarded
subpackages with an AST parse and rejects imports of HTTP clients, the service
layer, or any `*_api` module. AST rather than grep, because the brain's own
docstrings are full of the phrase "NO HTTP".

**Two coverage gaps, both real:**

1. **Flat top-level modules are unguarded.** `watchhistoryaggregator.py`,
   `storage_estimator.py`, `transcode_analyzer.py`, `genre_predictor.py`,
   `penalty.py`, `upgrade.py`, `plan_summary.py`, `size_calibration.py` sit at
   the package root, outside the guarded-subpackage walk.
2. **Several genuinely-pure subpackages are not in the list.** `foundation`,
   `thresholds`, `discovery`, `labels`, `challenger`, `updates` are absent from
   `_GUARDED_SUBPACKAGES`.

Neither is a live violation. Both mean the guard is narrower than it appears.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Pure functions, no I/O | G2 + G4 — the only way replay works | Let the brain read cache |
| D2 | One 100-point score everywhere | G1 — independently-scored subsystems fight each other | Per-subsystem scoring |
| D3 | `score is None` ⇒ safe mid-tier | G5 — the fix for a real bug that grabbed UHD for every unscored title | `None` ⇒ 0, or ⇒ eligible |
| D4 | Signal groups A–F | G3 — a score decomposable into named contributions is debuggable; a single blended number is not | Opaque blend |
| D5 | Explicit `thresholds/` package | G6 — literals scattered through planners cannot be tuned or reported on | Inline constants |
| D6 | Ledger written even in dry run | The plan is the deliverable of a dry run | Skip on dry run |
| D7 | Shadow challenger, not A/B | G7 — no live feedback signal exists, so a shadow scored alongside the champion is the only safe comparison | Live A/B split |
| D8 | Interpretable features over deep models | G3 outweighs marginal accuracy for destructive decisions | Neural approach |
| D9 | Time-aware splits in `eval/` | Watch behaviour drifts; a random split leaks the future | Random split |
| D10 | People matrix split forward-map / affinity | Library-derived is stable, watched-set-derived is volatile | One artifact |
| D11 | `keep-universe` never deleted; bare `universe` last-resort | Franchise completeness is a household preference the model must not optimise away | Score-only deletion |
| D12 | Golden-corpus regression on scores | Catches unintended score movement from a weight change | Unit tests only |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No brain module imports an HTTP client, the service layer, or a `*_api` module. |
| I2 | No brain module performs file or network I/O. |
| I3 | Scores are `0–100` or `None`. |
| I4 | `None` ⇒ safe mid-tier (HD Bluray + WEB). Never top tier, never zero. |
| I5 | 4K requires score ≥ 70. |
| I6 | HD-720p is the floor. |
| I7 | `keep-universe` is never in a delete plan. |
| I8 | Bare `universe` appears only after all other pools are exhausted. |
| I9 | Every decision writes a ledger row naming its signals. |
| I10 | Given identical inputs, output is deterministic. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Feature row missing a field | Feature builder | Score `None` → safe mid-tier (I4) | That title, safely |
| Affinity cache stale | None | Scores computed from old behaviour | 🟡 Silently outdated |
| Enrichment incomplete | Coverage check | Group-B/C signals absent; score computed from fewer groups | 🟡 Systematically lower scores |
| Threshold misconfigured | [`thresholds/report.py`](./thresholds/report.py) | Reported | Detected |
| Size model uncalibrated | TTL check | Falls back to defaults | Estimate accuracy |
| Golden-corpus drift | [`test_score_golden.py`](./scoring/test_score_golden.py) | Test fails | Caught pre-commit |
| Ledger schema change | None | Old rows unreadable by replay | 🟡 History lost for eval |
| Purity violated in a flat module | **Not detected** | Replay silently requires a live service | 🔴 Coverage gap |
| Non-deterministic output (dict ordering, set iteration) | None | Replay diverges from the original run | 🟡 Undermines G2 |

**Row 3 deserves emphasis.** Incomplete enrichment does not produce *wrong*
scores — it produces *uniformly lower* ones, because absent signal groups
contribute nothing. That is indistinguishable from "the household likes this
less," and it silently biases every downstream decision toward deletion.

---

## 7. Configuration surface

Thresholds and weights are collected in [`thresholds/`](./thresholds/) rather
than scattered:

| Surface | Where |
|---|---|
| Threshold registry | [`thresholds/registry.py`](./thresholds/registry.py) |
| Derived thresholds | [`thresholds/derive.py`](./thresholds/derive.py) |
| Shadow thresholds | [`thresholds/shadow.py`](./thresholds/shadow.py) |
| Threshold report | [`thresholds/report.py`](./thresholds/report.py) |
| Scoring weights | Config, written by onboarding [`extras.py`](../factories/onboarding/steps/extras.py) |
| Golden scores | [`scoring/golden_scores.json`](./scoring/golden_scores.json) |

Fixed constants: 4K ≥ 70 · floor HD-720p · `None` ⇒ HD Bluray + WEB.

---

## 8. Implemented capabilities

- ✅ Unified 100-point score with A–F signal groups
- ✅ Movie, show and episode feature construction
- ✅ Household + per-user affinity, genre affinity, platform usage
- ✅ People matrix with incremental sidecars
- ✅ Watch likelihood, survival modelling, saga engagement / order / progress
- ✅ Delete, downgrade, upgrade and JIT planners with tightness and dual-version handling
- ✅ Cross-instance dedup planning
- ✅ Lifecycle policies: monitor, grace, restore, stale-prune, saga + viewer retention, watched-definition, auto-rater
- ✅ Library classification and routing
- ✅ Discovery: candidates, gems, occupancy, rollover, shelf, window
- ✅ Playlist generation: caps, cert gate, expansion, grouping, ordering, per-user, rationale, spoiler control, timeline
- ✅ Codec reporting, transcode analysis + fingerprinting, profile selection
- ✅ Size model with calibration, anomaly detection, storage estimation
- ✅ Declared threshold registry with shadow evaluation
- ✅ Decision ledger + plan summary
- ✅ Eval harness: replay, forward validation, stratified time-aware splits, metrics
- ✅ GBT shadow challenger
- ✅ Golden-corpus score regression
- ✅ Commit-time purity enforcement across 17 subpackages

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **🔴 Complete the migration** — move the flat top-level modules into subpackages, resolving the duplicate `storage_estimator` / `transcode_analyzer` / `size_calibration` pairs | Closes the §3.4 gap 1: these are the only brain modules the purity guard cannot see | M | [`MIGRATION.md`](./MIGRATION.md) |
| P2 | **Extend `_GUARDED_SUBPACKAGES`** to `foundation`, `thresholds`, `discovery`, `labels`, `challenger`, `updates` | Closes §3.4 gap 2 | S | [`hooks/`](../../hooks/DESIGN.md) |
| P3 | **Purity rule: no filesystem writes** — AST-detect `open(...,'w')`, `to_parquet`, `Path.write_*` | I2 is currently only enforced for imports | M | P2 |
| P4 | **Enrichment-coverage gate on scoring** — mark scores computed from incomplete signal groups | Closes §6 row 3, the systematic-bias failure | M | Coverage metric |
| P5 | **Determinism test** — replay twice, assert identical output | Verifies I10, which nothing currently checks | S | [`eval/replay.py`](./eval/replay.py) |
| P6 | **Ledger schema versioning** | Protects replay history against a shape change (§6 row 7) | S | — |
| P7 | **Score explainer** — per-title waterfall of every group's contribution | Delivers G3 to a human; likely to surface real scoring bugs | M | [`web/`](../factories/web/DESIGN.md) |
| P8 | **Challenger promotion pipeline** — criteria and process for shadow → champion | The challenger exists but has no promotion path | L | [`eval/forward.py`](./eval/forward.py) |
| P9 | **Threshold sandbox** — replay the ledger under altered thresholds and diff outcomes | Turns blind tuning into measurement | L | P7, [`web/`](../factories/web/DESIGN.md) |
| P10 | **Affinity decay** — weight recent viewing above historical | A film watched five years ago currently counts equally | M | — |
| P11 | **Confidence intervals on scores** — distinguish "confidently 40" from "40 ± 30" | Would let low-confidence titles avoid destructive decisions entirely | L | P4 |
| P12 | **Per-device profile selection** consuming `tautulli/device_codec_matrix` | The signal is built every run and read by nothing | M | [`tautulli/`](../services/tautulli/DESIGN.md) P1 |
| P13 | **Cross-medium next-watch** — movie recommendations from TV affinity and vice versa | Show `summary` genres are already enriched for exactly this | M | — |
| P14 | **Golden corpus expansion** with adversarial cases | Current corpus is regression-only, not edge-case coverage | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is mid-tier the right `None` default, or should unscored titles be deferred until scored? | — |
| Q2 | Should score confidence gate destructive decisions — never delete a low-confidence title? | P11 |
| Q3 | What promotes a challenger to champion, and who decides? | P8 |
| Q4 | Should affinity decay, and on what half-life? | P10 |
| Q5 | Is `score ≥ 70` for 4K empirically right, or inherited? | P9 |
| Q6 | Should the flat legacy modules be migrated or deleted? Some may be fully superseded. | P1 |

**Q2 is the most consequential.** The system currently treats a confidently-low
score and a barely-evidenced low score identically, and both can lead to
deletion. Given the asymmetry — a wrong deletion is discovered months later —
gating destructive decisions on confidence may matter more than any accuracy
improvement.

## 11. Related designs

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) · [`ML_PIPELINE.md`](./ML_PIPELINE.md) · [`MATH_FOUNDATION.md`](./MATH_FOUNDATION.md) · [`MIGRATION.md`](./MIGRATION.md)
- [`scoring/SCORING_GROUPS.md`](./scoring/SCORING_GROUPS.md)
- [`DESIGN_people_matrix.md`](./DESIGN_people_matrix.md) · [`DESIGN_recommendation_enhancement.md`](./DESIGN_recommendation_enhancement.md) · [`DESIGN_series_saga_resumption.md`](./DESIGN_series_saga_resumption.md) · [`DESIGN_codec_routing_build_plan.md`](./DESIGN_codec_routing_build_plan.md) · [`DESIGN_per_person_codec_profiles.md`](./DESIGN_per_person_codec_profiles.md)
- [`hooks/DESIGN.md`](../../hooks/DESIGN.md) — purity enforcement
- [`services/DESIGN.md`](../services/DESIGN.md) — the callers
