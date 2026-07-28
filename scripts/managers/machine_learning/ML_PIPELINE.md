# ML_PIPELINE — snapshots, labels, validation, refit, challenger, survival, utility ranking

Five-stage measurement-first ML layer over the hand-weighted watchability score.
**Nothing here changes any live score, decision, or \*arr write by default.** The
only default-ON piece is pure logging (snapshot appends); every behavioral surface
is config-gated DEFAULT-OFF, consistent with the dry-run decision-ledger parity
oracle (see MIGRATION.md).

The math behind every stage — formulas, derivations, estimator properties, and
the constants-to-derived-quantities roadmap — is formalized in
[MATH_FOUNDATION.md](MATH_FOUNDATION.md) (canonical implementations: `foundation/`).

All artifacts live under the global cache base dir (`scripts/support/cache/`):

```
ml/
  snapshots/{radarr,sonarr}/{YYYY-MM}.parquet    Stage 1 — append-only score snapshots
                                                 (rows carry source= prospective|backfill)
  reports/forward_validation_{date}.json         Stage 2
  reports/suggested_weights_{date}.json          Stage 3 (informational — never applied)
  reports/backfill_snapshots_{date}.json         Stage 1 backfill (truncated replay)
  reports/survival_{date}.json                   Stage 5a
  models/gbt_challenger_{service}.txt|.calib.json  Stage 4 (observe-only model)
```

---

## Stage 1 — Label/feature snapshot pipeline (`labels/`)

**What it produces.** After each score-map build (`RadarrSpacePressureManager.refresh_scores`
and `SonarrEpisodeFilesCacheManager.refresh_scores`), one Parquet row per scored
entity: score, every signal-group contribution flattened to `sig_*` columns
(from the persisted `watchability_breakdown`), size/resolution, `watched_before`,
the decision-ledger `planned_action` on the row, and `in_up_next` (movie tmdb in
`plex/playlists/protected_movie_tmdbs/{movie,combined}` at snapshot time).
Month-partitioned, deduped on `(snapshot_date, instance, entity_id)` keeping
last, atomic writes. Shows are one row per SERIES (episode rows aggregated:
summed size, modal resolution, any-watched, modal planned_action).

**Labels.** `labels/labeling.build_labels(snapshots_df, history, horizon_days)` adds:

* `watched_within_h` — a qualifying Tautulli event in `(snapshot_ts, snapshot_ts+horizon]`.
  Movies join `tautulli/history/all.json` events (`date` = epoch seconds — the only
  timestamp; `tautulli/group/household/tmdb_completions.json` has **no timestamps**,
  so it only relaxes the per-event completion cut) to tmdb via
  `plex/movies/owned_inventory.json` `rating_key` (exact join; title/year matching
  is not used for movies). Shows match on normalized `grandparent_title` — the only
  join history offers for series. Cuts: movies `percent_complete >= 90` (>= 50 when
  the title is completed per tmdb_completions), episodes `>= 50` (engagement).
* `label_mature` — the horizon has fully elapsed (immature negatives are provisional).
* `recommended_not_watched` — `in_up_next` at snapshot time and not watched within horizon.

**Failure isolation.** The service hooks call `maybe_snapshot_movies/shows`, which
never raise; any failure logs one debug line and the run proceeds untouched.

### Stage 1 backfill — truncated replay (`scripts/support/tools/ml_backfill_snapshots.py`)

Reconstructs weekly historical snapshots (movies only) so the real watch history
becomes labels immediately instead of waiting a horizon per week: history events
are truncated to the prefix strictly before each grid date `t`, the household
state (title-joined watch stats, genre affinity, watched-tmdb set, C4 person
weights) is recomputed from that prefix, and every movie whose Radarr `added`
date is `<= t` is rescored through the REAL `score_movie` call path with TODAY'S
movie metadata/credits — the known, documented leakage. Every row is stamped
`source="backfill"`, `reconstruction_version`, `leakage_flags`
(`credits_today,metadata_today,deletions_unknown[,no_added_date]`); prospective
rows default `source="prospective"` and pre-provenance parquets load as
prospective. The grid is hard-capped below the earliest prospective
snapshot_date, so the two populations can never collide in the writer's dedupe.
The offline consumers (`ml_forward_validation` / `ml_weight_refit` /
`ml_train_challenger`) EXCLUDE backfill rows unless `--include-backfill` is
passed — and then print/persist source-split counts plus a leakage warning.
Writes only `ml/snapshots` + `ml/reports`.

## Stage 2 — Forward-validation harness

`scripts/support/tools/ml_forward_validation.py` (standalone; never imported by
main.py). Evaluates the EXISTING score as a ranker/classifier on a temporal test
window: AUC-PR (numpy average precision), Brier on the min-max-scaled score,
10-bin calibration table, and per-signal univariate AUC-PR so weak signals are
visible. Degenerate data (one snapshot batch, no matured labels, empty split)
degrades to a clearly-labeled in-sample evaluation with warnings — it runs
cleanly on day one. Writes `ml/reports/forward_validation_{date}.json`.

## Stage 3 — Weight refit (suggestions only)

`scripts/support/tools/ml_weight_refit.py`. Pure-numpy IRLS logistic regression
(L2, `--l2` default 1.0, intercept unpenalised) on standardized `sig_*` columns
vs `watched_within_h`, temporal train/test. Reports per-group
`fitted_multiplier` normalized so the strongest group = 1.0 — directly
comparable to today's implicit 1.0 per group — plus per-point log-odds and
AUC-PR of current vs refit ranking. **Never applied anywhere**; the JSON says
`"applied": false` and the scorers' weights remain hand-edited only. Prints an
explicit high-variance caveat whenever `n_positive < 100`. With zero positives
(day one) it refuses to fit, with a clean message.

## Stage 4 — Shadow GBT challenger (observe-only)

`challenger/gbt_shadow.py` + `scripts/support/tools/ml_train_challenger.py`.

* lightgbm is **optional**: absent → training CLI exits with a clear message;
  the runtime path logs ONE line (only if explicitly enabled) and no-ops.
* Training (CLI only): temporal split (or last ~20% of snapshot days), early
  stopping, small trees (`num_leaves=15`) because n is tiny; probability
  calibration via pure-numpy isotonic (PAVA) fit on validation predictions.
  Persists booster + calibration JSON to `ml/models/`.
* Runtime shadow hook (`attach_challenger_p`, called from the snapshot append
  path): only when `scoring.ml_challenger.enabled` is true AND model files
  exist — predicts calibrated P(watch), logs Spearman rho(score, P) and the
  top-10 rank disagreements via the run logger, and stamps `challenger_p` into
  the snapshot rows. **Nothing at runtime reads `challenger_p`; it can never
  feed a decision.**

## Stage 5a — Survival recency (`likelihood/survival.py`)

Discrete-time hazard of the household's NEXT watch from Tautulli history:
inter-event gaps per entity (+ right-censored gap since the last event),
bucketed hazard curves, hierarchical empirical-Bayes pooling
title (>=3 events) → franchise/collection pool → household, blend weight
`n/(n+k)`, `k=5`. Pure API:

* `fit_survival_model(events_by_entity, groups_by_entity=..., now=...) -> SurvivalModel`
* `SurvivalModel.residual_watch_probability(entity, days_since_last, horizon)`
* `residual_watch_probability(entity_history, days_since_last, horizon, pooled_hazard=...)`

Report CLI: `scripts/support/tools/ml_survival_report.py` (household hazard
curve with at-risk counts, residual-probability table, top rewatched titles).
**No runtime wiring** — importable API + report only.

## Stage 5b — Utility-per-GB delete ranking (default-off mode)

`space/coordinator_ranker.select_for_target` gains `ranking_mode`
(config `space_delete_ranking`, read in `services/coordinator/space_coordinator`):

* `"score"` (DEFAULT) — the historical `(score, critic, -size)` order,
  **byte-identical when the flag is unset** (proven by smoke test against the
  pre-change git HEAD across randomized pools and every knob combination).
* `"utility_per_gb"` — ranks ascending by `score / max(size_gb, 0.1)` so the
  least watchability is destroyed per GB reclaimed (fewer, bigger, colder
  deletions). Recency ramp still feeds the score term; `uhd_first` still evicts
  baseline-backed 4K bonus copies first; critic/size tiebreaks preserved;
  `delete_tier_size` bucketing is a score-mode concept and is ignored in
  utility mode. All eligibility guards/shields live upstream in pool
  construction and are untouched.

## Simulation harness (`scripts/support/tools/ml_simulate.py`)

End-to-end parameter-recovery check: plants a KNOWN ground truth (per-signal-group
weights β\*, a discrete-time rewatch hazard h\*, a synthetic ~500-title library),
simulates weeks of household behaviour from exactly those parameters (daily
snapshots through the real `labels/snapshots.py` writer; Tautulli-shaped
`history/all.json`, `owned_inventory.json`, `tmdb_completions.json`, Up Next tmdb
sets), runs the real chain (labeling → forward validation → weight refit →
survival) against a **throwaway cache dir**, and asserts recovery: fitted
multipliers rank-correlate with β\* (planted zeros fit near zero), refit AUC-PR
beats a shuffled-label baseline, the pooled hazard matches h\* within tolerance,
the calibration table is monotone-ish, and a 1-week run leaves the refit
refusing (the `n_pos < 100` power gate — MATH_FOUNDATION §4's ~87-event sketch).
It refuses to run against the real cache and defaults to a temp dir
(`--cache-base` to override); exit 0 only when every assertion passes.
When lightgbm is installed the harness also exercises the Stage-4 challenger
end-to-end — `ml_train_challenger` against the sim cache on the same temporal
split, AP/calibration floors on the forward test window (competence checks,
not a superiority test — the planted first-watch truth is linear in `sig_*`,
and any AP surplus over the refit comes from context features like
`watched_before` that the linear refit does not use), and the
`attach_challenger_p` shadow hook — and marks those assertions SKIP (still
exit 0) when it is not.

**Scope caveat:** the harness validates that the pipeline *can recover known
parameters from data shaped like ours* — it does NOT substitute for real
household data. Fitted weights for production must come from organic watches
accumulated in the real snapshots, and the deterministic hand-weighted scorer
remains the shipping default regardless of what the simulation recovers.

---

## Config flags (all of them)

| Flag | Default | Effect |
|---|---|---|
| `ml.snapshots.enabled` | **true** | Stage-1 snapshot appends (pure logging; fully fault-isolated). |
| `ml.snapshots.backfill_on_first_run` | **true** | On a FRESH install only (no matured/backfilled rows in the store), run `ml_backfill_snapshots` once at the end of the run so the household's existing Tautulli history becomes labels immediately instead of a horizon later. One-shot marker at `<cache>/ml/snapshots/first_run_backfill.json`; any failure is a logged no-op (`labels/first_run.py`). |
| `ml.snapshots.backfill_grid_days` | **7** | Grid spacing for that first-run replay (`--grid-days`). |
| `ml.snapshots.backfill_max_grid_points` | **26** | Wall-clock bound on the first-run replay: at most this many grid dates (~6 months weekly), start additionally clamped to the first real event. 0 = unbounded. |
| `ml.thresholds.mode` | **"shadow"** | `"derived"` lets consumers read the calibrated (shrunk) cutoff; `"off"` skips the end-of-run report entirely. |
| `ml.thresholds.shrinkage_k` | **150** | Prior strength of the hand-set constant: `effective = w·derived + (1−w)·constant`, `w = n_pos/(n_pos+k)`. n_pos=0 → the constant, bit-identically. |
| `ml.thresholds.include_backfill` | **true** | Threshold fitting counts reconstructed labels (the only ML entry point that does — a fresh install has no others). The report always splits `n_pos` by provenance. |
| `ml.thresholds.horizon_days` / `.max_fit_days` / `.targets.*` | 14 / 365 / see `registry.DEFAULT_TARGET_P` | Label horizon, fit-window bound, and the per-bucket target probabilities. |
| `scoring.ml_challenger.enabled` | **false** | Stage-4 shadow: log divergence + stamp `challenger_p` into snapshots. Needs lightgbm + a trained model; otherwise no-ops. |
| `space_delete_ranking` | **"score"** | `"utility_per_gb"` switches the coordinator delete ranking (Stage 5b). Unknown values warn and fall back to `"score"`. |

No other stage has a runtime surface. The CLIs are read-only over the cache
(plus their own `ml/reports/` + `ml/models/` writes) — except that the first-run
trigger above may invoke `ml_backfill_snapshots` once, which appends to
`ml/snapshots/`.

## CLI usage

```bash
python scripts/support/tools/ml_backfill_snapshots.py [--grid-days 7] \
    [--horizon-days 14] [--instance standard] [--start YYYY-MM-DD] [--end YYYY-MM-DD] \
    [--config PATH] [--dry-run] [--no-write]

python scripts/support/tools/ml_forward_validation.py [--split-date YYYY-MM-DD] \
    [--horizon-days 14] [--service radarr|sonarr|both] [--instance NAME] \
    [--include-immature] [--include-backfill] [--bins 10] [--no-write]

python scripts/support/tools/ml_weight_refit.py [--split-date YYYY-MM-DD] [--l2 1.0] \
    [--horizon-days 14] [--service ...] [--include-immature] [--include-backfill] \
    [--no-write]

python scripts/support/tools/ml_train_challenger.py [--split-date YYYY-MM-DD] \
    [--horizon-days 14] [--service ...] [--include-backfill] [--rounds 400] \
    [--early-stopping 30]

python scripts/support/tools/ml_survival_report.py [--bucket-days 7] [--max-days 364] \
    [--k 5.0] [--horizon 14] [--min-pct 50] [--top 6] [--no-write]

python scripts/support/tools/ml_simulate.py [--seed 42] [--weeks 8] [--titles 500] \
    [--target-rate 0.04] [--cache-base PATH] [--keep-cache] [--power-gate-only]
```

All five accept `--cache-base PATH` to point at a test cache (for `ml_simulate`
it is the sim's throwaway output dir — the tool hard-refuses the real cache).

## Honest data-regime caveats

* The household ground truth is **≈931 Tautulli events total** (and only ~54 are
  tmdb-resolvable movie watches). That supports classical statistics only:
  univariate AUC-PRs, an L2 logistic refit, bucketed hazard curves with strong
  empirical-Bayes pooling. Deep or even mid-sized models would memorise this
  dataset instantly — which is why the challenger is a tiny, calibrated,
  observe-only GBT and the refit is a suggestion table, not an auto-tuner.
* Labels need the horizon to elapse: on day one every row is immature, AUC-PR is
  undefined (no positives), and the tools SAY so instead of inventing numbers.
  Expect the first usable forward validation ~`horizon_days` after snapshots
  start accumulating, and treat everything before `n_positive ≈ 100` as
  directional.
* Show labels join on normalized series title (history carries no series id) —
  a renamed series breaks its own label continuity. Movie labels are exact
  (rating_key → tmdb).
* `tmdb_completions` has no timestamps; time-scoped labels always come from
  `history/all.json` `date` fields. If Tautulli history is pruned, labels
  silently lose recall — keep the history cache long.
