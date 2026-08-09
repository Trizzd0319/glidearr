# machine_learning

> Breadcrumb: [glidearr](../../..) › [scripts](../../README.md) › [managers](../README.md) › **machine_learning**

**Package** — `scripts.managers.machine_learning`
**Run position** — Called synchronously by service managers throughout phases 2–3. Never runs on its own.
**One-liner** — The brain: pure functions over feature rows that produce every score, plan and policy decision in Glidearr. No I/O, enforced at commit time.

---

## Purpose

This layer answers all the questions the service layer is forbidden to answer:

| Question | Owner |
|---|---|
| How much is this title worth to this household? | [`scoring/`](./scoring/) |
| Will anyone actually watch it? | [`likelihood/`](./likelihood/) |
| What quality tier should it be? | [`quality_analytics/`](./quality_analytics/), [`sizing/`](./sizing/) |
| What should be deleted when space runs out? | [`space/`](./space/) |
| What should be acquired next? | [`acquisition/`](./acquisition/), [`discovery/`](./discovery/) |
| Should this be kept, monitored, pruned, restored? | [`lifecycle/`](./lifecycle/) |
| Which instance and root folder? | [`routing/`](./routing/), [`classification/`](./classification/) |
| What goes in the playlist, in what order? | [`playlists/`](./playlists/) |
| Was the last decision any good? | [`eval/`](./eval/), [`ledger/`](./ledger/) |

**The rule:** pure data in, pure data out. No HTTP client, no service-layer
import, no `*_api` module — mechanically enforced by
[`hooks/brain_purity.py`](../../hooks/brain_purity.py) on every commit.

---

## Subpackages

### Contracts and foundation

| Folder | Role |
|---|---|
| [`contracts/`](./contracts/) | `context.py`, `feature_rows.py`, `plans.py` — the service↔brain boundary shapes |
| [`foundation/`](./foundation/) | `formulas.py` — shared mathematical primitives |
| [`features/`](./features/) | Movie / show / episode feature construction, watched-set, completion stats |

### Scoring and likelihood

| Folder | Role |
|---|---|
| [`scoring/`](./scoring/) | `movie_scorer`, `show_scorer`, `critic`, `device_fit`, golden-corpus regression |
| [`likelihood/`](./likelihood/) | `watch_likelihood`, `survival`, saga engagement / order / progress, quality ladder |
| [`affinity/`](./affinity/) | Genre affinity, group completion, platform usage |
| [`people_matrix/`](./people_matrix/) | Person↔media co-occurrence graph |

### Decisions

| Folder | Role |
|---|---|
| [`space/`](./space/) | Delete / downgrade / upgrade / JIT planners, tightness, dedup, dual-version, targets |
| [`lifecycle/`](./lifecycle/) | Monitor, grace, restore, stale-prune, saga-retention, viewer-retention, watched-definition, auto-rater |
| [`acquisition/`](./acquisition/) | Demand, enrichment prioritiser, next-episode + resumption planners, pilot stepping |
| [`classification/`](./classification/) | Library classifier + router, franchise, keep-policy, guards |
| [`routing/`](./routing/) | Instance selection |
| [`discovery/`](./discovery/) | Candidates, gems, occupancy, rollover, scoring, shelf, window |
| [`playlists/`](./playlists/) | Caps, cert gate, engagement, expansion, grouping, ordering, per-user, rationale, spoiler, timeline |
| [`next_watch/`](./next_watch/) | Next-watch surface |

### Quality and sizing

| Folder | Role |
|---|---|
| [`quality_analytics/`](./quality_analytics/) | Codec report, legacy codec, likely viewers, profile selector, transcode analysis + fingerprinting |
| [`sizing/`](./sizing/) | Size model, calibration, anomaly detection, file comparison, storage estimation |
| [`thresholds/`](./thresholds/) | Derive, registry, report, shadow — the tunable threshold surface |

### Learning and evaluation

| Folder | Role |
|---|---|
| [`labels/`](./labels/) | Labeling, first-run, recommendations, snapshots |
| [`eval/`](./eval/) | Forward validation, replay, split, stratify, metrics |
| [`challenger/`](./challenger/) | `gbt_shadow` — shadow-mode gradient-boosted challenger |
| [`ledger/`](./ledger/) | `decision_ledger`, `plan_summary` |
| [`updates/`](./updates/) | Dataset builder, feature aggregator |

---

## Top-level scripts

| Script | Role | Status |
|---|---|---|
| [`genre_predictor.py`](./genre_predictor.py) | Genre inference | ✅ Implemented |
| [`penalty.py`](./penalty.py) | Shared penalty terms | ✅ Implemented |
| [`upgrade.py`](./upgrade.py) | Upgrade decisioning | ✅ Implemented |
| [`plan_summary.py`](./plan_summary.py) | Read-only ledger roll-up, called by `Main` | ✅ Implemented |
| [`size_calibration.py`](./size_calibration.py) | `SizeCalibrator` — warm-loaded by `Main`, TTL-refreshed | ✅ Implemented |
| [`storage_estimator.py`](./storage_estimator.py) | Storage projection | 🔴 Duplicated in [`sizing/`](./sizing/) |
| [`transcode_analyzer.py`](./transcode_analyzer.py) | Transcode analysis | 🔴 Duplicated in [`quality_analytics/`](./quality_analytics/) |
| [`watchhistoryaggregator.py`](./watchhistoryaggregator.py) | Watch-history aggregation | 🔴 Legacy flat module — **unguarded** by brain purity |
| [`__init__.py`](./__init__.py) | Package marker | ✅ Implemented |

**Three of these are migration debt.** `storage_estimator.py`,
`transcode_analyzer.py` and `size_calibration.py` exist both here and inside a
subpackage; `watchhistoryaggregator.py` is a Step-9 cleanup target explicitly
named in [`brain_purity.py`](../../hooks/brain_purity.py). Flat top-level modules
are **not** covered by the purity guard. See [`DESIGN.md`](./DESIGN.md) §9 P1.

---

## Existing documentation

| Doc | Covers |
|---|---|
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Brain-layer architecture |
| [`ML_PIPELINE.md`](./ML_PIPELINE.md) | The learning pipeline |
| [`MATH_FOUNDATION.md`](./MATH_FOUNDATION.md) | Mathematical basis of the scoring model |
| [`MIGRATION.md`](./MIGRATION.md) | Service→brain migration status |
| [`scoring/SCORING_GROUPS.md`](./scoring/SCORING_GROUPS.md) | The scoring group taxonomy (A–F) |
| [`DESIGN_people_matrix.md`](./DESIGN_people_matrix.md) | Person↔media graph |
| [`DESIGN_recommendation_enhancement.md`](./DESIGN_recommendation_enhancement.md) | Recommendation improvements |
| [`DESIGN_series_saga_resumption.md`](./DESIGN_series_saga_resumption.md) | Saga resumption |
| [`DESIGN_codec_routing_build_plan.md`](./DESIGN_codec_routing_build_plan.md) | Codec routing |
| [`DESIGN_per_person_codec_profiles.md`](./DESIGN_per_person_codec_profiles.md) | Per-person codec profiles |

---

## Data in / data out

| Direction | Source/Sink | Payload |
|---|---|---|
| IN | Service managers | Feature rows, context objects — **plain data only** |
| OUT | Service managers | Scores, plans, policy decisions |
| OUT | Decision ledger | Decision rows with signal breakdown |

**No FETCH. No direct CACHE access. No APPLY.** The brain receives what it needs
and returns what it decided.

---

## Navigation

- **Up:** [`managers/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Enforcement:** [`hooks/brain_purity.py`](../../hooks/brain_purity.py)
- **Callers:** [`services/`](../services/README.md)
