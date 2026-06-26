# RadarrRepairAnomalyManager

- **File** — `scripts/managers/services/radarr/repair/anomaly.py`
- **One-liner** — The watchability-driven heart of Radarr repair: scores owned/missing movies and decides whether to search, re-monitor, unmonitor, demote (unmonitor→delete), or restore them — all value judgements delegated to `machine_learning` brain modules.

## What it does (for a senior Python engineer)

`RadarrRepairAnomalyManager` is a `BaseManager` + `ComponentManagerMixin` loaded by `RadarrRepairWrapperManager` under the key `anomaly`. `parent_name` derives from the class name (`RadarrRepairAnomaly`). Deps (`radarr_api`, `instance_manager`, `dry_run`) from kwargs-or-parent. This is the largest, most consequential repair component — the only one that performs destructive deletes (guarded + restorable).

It imports several brain modules (decisions live there; this file only orchestrates):
- `machine_learning.classification.keep_policy.resolve_keep_policy`
- `machine_learning.lifecycle.monitor_policy.release_available`, `triage_action`
- `machine_learning.lifecycle.stale_prune_policy.budget_delete_cohort`, `clock_age`, `expedite_dwell`, `franchise_delete_exempt`, `prune_below_floor_action`, `prune_score_gate`

Plus utilities `support/utilities/space_targets.space_targets` and `coordinator_owns_deletion`, and the scorer `services/trakt/movies/scorer.score_movie`.

- **FETCH / CACHE / APPLY.**
  - FETCH: `GET movie` (resolution/missing scans), `GET tag`/`GET qualityprofile` (fallbacks).
  - CACHE: reads many global_cache keys (see below); writes the demote clock and deleted-set keys.
  - APPLY: `PUT movie/editor` (bulk monitor/unmonitor and bulk qualityProfile adjust), `POST command` (`MoviesSearch`), `DELETE moviefile/{id}` (the destructive delete), plus queue cancels via `QueueCancelMixin`.
- **External API endpoints:** `movie` (GET), `tag` (GET fallback), `qualityprofile` (GET fallback), `movie/editor` (PUT), `command` (POST `MoviesSearch`), `moviefile/{id}` (DELETE).
- **Config keys read:** `rating_groups`, `owned_monitor_policy` (default `watchability`), `owned_monitor_score_threshold` (35), `owned_demote_enabled` (True), `owned_demote_score_threshold` (20), `owned_demote_dwell_days` (30), `owned_delete_dwell_days` (90), `owned_delete_enabled` (True), `owned_delete_min_dwell_days` (7), `owned_delete_franchise_exempt_enabled` (False), `owned_delete_franchise_watched_fraction` (0.5), `owned_delete_budget_enabled` (False), `owned_restore_score_threshold` (20); plus `free_space_limit` indirectly via `space_targets`.
- **global_cache keys read:** `radarr.movies.{instance}.full`, `tautulli/affinity`, `trakt/history/movies`, `tautulli/group/{group}/tmdb_completions`, `radarr.tags.{instance}`, `radarr.quality.{instance}`. **Written:** `radarr/{inst}/monitor_demote_clock` (per-movie below-floor clock), `radarr/{inst}/demote_deleted` (restore-tracking set).
- **dry_run.** Every APPLY logs `"[dry_run] Would …"` and mutates nothing — but the demote clock STILL advances (so dwell time is real, mirroring the deletion grace pattern), and dry-run delete candidates keep their clock entry so they keep surfacing.
- **Singleton / concurrency.** `BaseManager` singleton; sequential. Writes go through `radarr_api._make_request` (central SQLITE_BUSY/serialisation).

Public methods:

- `find_resolution_mismatches(instance) -> list[dict]` — FETCH. Derives the instance's expected resolution band from its name (`4k/2160/uhd`→[2160,9999], `1080`→[1080,2159], `720/hd`→[720,1079]; unknown→skip) and flags files outside it, suggesting the correct instance via `_suggest_instance`.
- `find_monitored_missing_files(instance) -> list[dict]` — FETCH. Movies with `monitored=True` and `hasFile=False`.
- `find_unmonitored_with_files(instance) -> list[dict]` — FETCH. Movies with a file but `monitored=False` (disk used, no upgrade protection).
- `repair_monitored_missing(instance, movie_ids=None) -> stats` — APPLY. Cancels any in-flight queue item (so a fresh search grabs the best-scored profile), then `MoviesSearch` per missing movie. `{checked, triggered, failed, queue_cancelled}`.
- `repair_unmonitored_with_files(instance) -> stats` — APPLY. Owned-movie monitor pass. Policy `owned_monitor_policy`: `off` (leave untouched), `all` (monitor every owned movie), `watchability` (default) — monitor iff keep/universe-tagged OR watched OR `score >= owned_monitor_score_threshold` (35); movies whose Trakt credits aren't cached yet are DEFERRED (left unmonitored, re-scored next run). Applies via bulk `PUT movie/editor monitored=True`.
- `demote_stale_monitored(instance) -> stats` — APPLY (destructive). Two-stage prune of owned movies below the demote floor — see "How it functions".
- `restore_recovered_deletions(instance) -> stats` — APPLY. Re-acquires movies the prune deleted whose score recovered above `owned_restore_score_threshold` (20): bulk re-monitor + `MoviesSearch`. A recovered movie is held (`cooling`) until `owned_restore_min_age_days` (default 0 = off) have elapsed since its deletion timestamp, a time floor that stops delete/re-grab thrash when a score hovers at the floor (demote and restore floors are both 20). Tracked in `radarr/{inst}/demote_deleted`; entries drop when restored / re-acquired elsewhere / gone from Radarr.
- `triage_monitored_missing(instance) -> stats` — APPLY. Scores each monitored-but-missing movie and routes it via `triage_action` — see "How it functions".
- `run(instance) -> dict` — The pass invoked by the wrapper. Runs, in order: `find_resolution_mismatches`, `triage_monitored_missing`, `repair_unmonitored_with_files` (promote), `demote_stale_monitored` (prune), `restore_recovered_deletions` (restore). Returns a combined dict.

Internal helpers: `_resolve_instance`, `_suggest_instance`, `_resolve_keep_policy` (→ brain `resolve_keep_policy`), `_build_scoring_context` (one-shot gather of all read-only scoring inputs), `_score_owned` (scores an owned movie, returns `(score, credits_present)`; returns `(0, False)` when credits uncached so callers DEFER).

## How it functions

Lifecycle: `__init__` → `register()` → resolve deps → debug log. No children loaded.

`_build_scoring_context(instance)` is the shared backbone: it gathers (read-only) `all_movies`, `movie_by_tmdb`/`movie_by_id`, `genre_affinity` (`tautulli/affinity`), the `watched_tmdb_ids` set (Trakt history **plus** per-group Tautulli completions where `pct >= threshold`), `collection_members`, the `tag_label_map`, and a `people_mgr` (from `TraktMoviesManager.people` via the registry, for credit lookups). Both triage and the monitor/demote/restore passes score from this single context.

**`demote_stale_monitored`** — two-stage, watchability-driven prune (gated on `owned_demote_enabled`):
- stage 1 **unmonitor** when below floor (`owned_demote_score_threshold`, 20) for `owned_demote_dwell_days` (30); stage 2 **DELETE** the file when below floor for `owned_delete_dwell_days` (90).
- A per-movie clock in `radarr/{inst}/monitor_demote_clock` records "continuously below floor since"; it resets the instant the score recovers ≥ floor (hysteresis: promote at 35, act only below 20 — the 20–35 band is sticky/no-flap).
- Space-pressure gating: `space_targets(config, fallback_gb=0.0)` (deliberately NO `total_gb` — a SENTINEL, so the prune only flips to pressure-gated+expedited when the operator sets `free_space_limit`). `expedite_dwell(free_gb, T, U, delete_days, min_delete_days)` shortens the delete dwell as free space approaches the floor. `delete_active = pressure_active and not coordinator_owns_deletion(config)`.
- Hard guards: keep/universe-tagged OR ever-watched movies are never touched/clocked. Data-completeness guard: a movie with uncached credits is DEFERRED (no action, clock preserved).
- Delegated decisions: `prune_score_gate` (defer/error/recovered/below_floor), `franchise_delete_exempt` (default-off; spares a below-floor movie in a substantially-watched collection from deletion), `prune_below_floor_action` (delete/unmonitor/age), `budget_delete_cohort` (default-off; under pressure, delete only enough worst/biggest to reclaim back to U). Deletes are `DELETE moviefile/{id}` after a pre-delete bulk unmonitor; deleted tmdbIds are recorded for restore.

**`triage_monitored_missing`** — scores each monitored-missing movie (`score_movie`, default 5 if unresolvable) and routes via `triage_action`: `keep_skip` (keep/universe tag — never unmonitor), `defer` (below floor but credits uncached), `unmonitor` (score < `UNMONITOR_BELOW`=20), `adjust_and_search` (between 20 and `WATCH_THRESHOLD`=60 with wrong profile → set HD-720p then search), `search` (≥ 60, search at current quality). Household-watched movies are a hard override (always re-acquired). Release availability is gated by `release_available`. Writes are deferred into id-lists and flushed as bulk `PUT movie/editor` + a single `MoviesSearch`, with queue-cancel before searching.

All value judgements are delegated to the named brain modules; this file only fetches inputs, calls them, and applies the resulting decisions. (Per scope: the brain modules themselves are NOT documented here.)

## Criteria & examples

- **Resolution mismatch:** on the `1080` instance (band [1080,2159]) a file whose `movieFile.quality.quality.resolution=2160` is flagged, `expected_instance="4k"`. A 1080 file there is fine.
- **Owned-monitor (watchability):** an owned, unmonitored, untagged, unwatched movie scoring 41 (≥ 35) → monitored (`monitored_score`). One scoring 30 with credits cached → left unmonitored. One scoring 30 with credits NOT cached → deferred (re-scored next run). A keep-tagged movie → monitored regardless of score.
- **Demote two-stage:** a movie scoring 14 (< floor 20) continuously for 35 days (≥ 30) → unmonitored (stage 1). Still ≤ 20 after 92 days (≥ 90) and under space pressure → file deleted (stage 2) and tmdbId recorded for restore. If its score climbs to 22 on any run, the clock resets and nothing happens.
- **Expedite:** with `free_space_limit` set and free space near the floor T, `expedite_dwell` may cut the 90-day delete dwell toward `owned_delete_min_dwell_days` (7) — logged as `[expedited from 90d, …GB free]`.
- **Triage routing:** missing movie scoring 72 (≥ 60) → searched at current quality. Scoring 45 with a non-720p profile → adjusted to HD-720p then searched. Scoring 12 (< 20), no keep tag, credits cached → unmonitored; same score but credits uncached → deferred; same score but keep-tagged → `keep_skip`.
- **Restore:** a previously deleted movie now scoring 23 (> restore floor 20) and still file-less → re-monitored + searched; one still at 18 stays tracked as `still_low`.

## In plain English

This is the smart curator of your movie shelf. For movies you own but stopped tracking, it asks "is this worth keeping an eye on?" and only re-watches the ones you'd actually care about (favorites you tagged, things you've watched, or films it rates highly) — it won't blanket-re-add everything. For movies you wanted but never got, it decides whether to hunt for them now, settle for a smaller copy, or quietly give up. And for owned movies that score badly for a long stretch, it first stops tracking them, and — only if you're truly low on space and have opted in — eventually deletes the file. Crucially, it never throws away anything you've watched or tagged "keep," and if a deleted movie's appeal recovers later (say a sequel reignites interest in the original), it quietly re-downloads it. In preview mode it narrates every move and deletes nothing — but the clock keeps ticking so the wait time is honest.

## Interactions

- **Parent manager** — `RadarrRepairWrapperManager` (loads it as `anomaly`).
- **Sibling submanagers** — Consumes refreshed state the `orphans`/`metadata` passes produce; shares the keep-tag concept with `tags`; overlaps the deletion advice from `storage` (but `storage` only recommends — this one acts).
- **Brain modules (delegated, not documented here)** — `classification.keep_policy`; `lifecycle.monitor_policy` (`release_available`, `triage_action`); `lifecycle.stale_prune_policy` (`prune_score_gate`, `prune_below_floor_action`, `clock_age`, `expedite_dwell`, `franchise_delete_exempt`, `budget_delete_cohort`).
- **Other services** — `radarr_api` (movie, tag, qualityprofile, movie/editor, command, moviefile/{id}, queue cancel); `instance_manager`; `global_cache` (scoring inputs + clock/deleted-set); the `services/trakt/movies/scorer.score_movie` scorer and `TraktMoviesManager.people` for credits; `space_targets`/`coordinator_owns_deletion` utilities; `QueueCancelMixin` for queue cancellation.
