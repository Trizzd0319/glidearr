# SpaceCoordinatorManager

- **File** — `scripts/managers/services/coordinator/space_coordinator.py`
- **One-liner** — Phase-4 capstone that, when free space drops below the pressure band, runs both services' downgrades and then deletes the least-watchable movies *and* TV episodes from a single cross-service ranked pool until the shared media mount recovers.

## What it does (for a senior Python engineer)

`SpaceCoordinatorManager(BaseManager, ComponentManagerMixin)` is the unified deletion authority across Radarr (movies) and Sonarr (TV episodes). Radarr and Sonarr each still own their *upgrade* and *downgrade* stages, but when the coordinator is enabled it takes over **deletion** so movies and episodes compete in one ranked pool sorted by watchability — the least-valuable bytes go first regardless of which service owns them.

It does not perform FETCH/CACHE/APPLY directly. Instead it orchestrates other managers that do, and reads free/total capacity through them. The only API endpoints it touches are indirect, via the leaf managers it calls:
- movie deletions hit `moviefile/{id}` (delegated to `RadarrSpacePressureManager.delete_selected_movie_files`)
- episode deletions hit `episodefile/{id}` with whole-file guards (delegated to `SonarrCacheEpisodeFilesManager.delete_selected_episode_files`)
- total capacity via `radarr_api.disk_total_gb(...)` / `sonarr_api.disk_total_gb(...)`

**Place in the manager tree.** It is a top-level service manager constructed by `Main`. Its `parent_name` is `"SpaceCoordinatorManager"` (it is its own parent root). It receives `sonarr` and `radarr` references via kwargs at construction. It does **not** call `load_components` — it has no submanagers of its own. Instead it locates the managers it drives at runtime through the shared registry by key (`self._mgr(key)` → `registry.get("manager", key)`):
- `RadarrSpacePressureManager` — movie downgrades, movie candidate build, movie deletion, free-space read
- `SonarrSpacePressureManager` — Sonarr downgrades, free-space read
- `SonarrCacheEpisodeFilesManager` — episode candidate build, episode load, episode deletion, episode restore
- `RadarrRepairAnomalyManager` — movie restore (`restore_recovered_deletions`)

**Public methods.**
- `__init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)` — standard `BaseManager` injection plus explicit `dry_run` resolution (kwargs → parent manager → registry `Main`, never silently defaulting; final fallback `False`). Stashes `self.sonarr` / `self.radarr` from kwargs. Calls `self.register()`.
- `prepare(self) -> None` — no-op.
- `run(self) -> dict` — the entry point (timed via `@timeit("run")`). Returns a stats dict describing the action taken (see "How it functions").
- `_space_targets(total_gb=None) -> tuple[float, float]` — returns `(T, U)`, the pressure floor and the band-top, by delegating to the brain (`space_targets`); when `free_space_limit` is unset the floor defaults to 25% of `total_gb`.
- `_critic_sort(critic) -> float` (staticmethod) — critic-rating sort key; delegates to the brain.
- `_select_for_target(pool, need_gb, *, recency_ramp=None, now=None, tier_size=None) -> tuple[list[dict], float]` (classmethod) — ranks the combined delete pool to the target; delegates to the brain. Returns `(selected, projected_gb)`.

**Config keys read.**
- `space_coordinator_enabled` — opt-in flag.
- `free_space_limit` — pressure floor in GB; must be `> 0` for the coordinator to own deletion.
- `delete_recency_ramp` (dict, default-off) — when `enabled`, a recently-watched file sinks to the bottom of the delete order.
- `delete_tier_size` (float, default-off when unset/`<=0`) — likelihood-tier bucket size so the biggest file in the lowest tier goes first.
The gating logic (`coordinator_owns_deletion`, `space_targets`) lives in the brain module and is only consulted, not reimplemented here.

**Class constant.** `PRESSURE_FALLBACK_GB = 1000` — last-resort floor used only when `free_space_limit` is unset *and* the shared mount's total size is unreadable.

**global_cache / Parquet keys.** The coordinator reads/writes nothing directly. The leaf managers it calls own the Parquet I/O: `radarr_sp.load_movie_files(...)` loads the movie files frame; `sonarr_ef.load(...)` loads the episode-files frame; each carries a persisted `watchability_score`.

**dry_run behavior.** `self.dry_run` is resolved explicitly and logged. The coordinator itself mutates nothing; under `dry_run` the leaf managers it invokes log "would ..." lines instead of issuing the actual DELETE/PUT calls. The clock/state still advances where the leaf managers note it.

**Singleton / concurrency.** As a `BaseManager` subclass it is a process-wide singleton cached in `_instances`. `run()` is single-threaded and sequential. It is defensive: every cross-manager call is wrapped in `try/except` and logs a warning on failure rather than aborting the whole pass.

## How it functions

Lifecycle: `Main` constructs it (injecting `radarr`/`sonarr`), then calls `run()`. No `load_components` step — dependencies are resolved lazily inside `run()`.

`run()` control flow:

1. **Gate.** If `coordinator_owns_deletion(self.config)` is False, bail. Special footgun warning: if `space_coordinator_enabled=true` but `free_space_limit<=0`, the coordinator is inert and per-service deletion runs at the default 25%-of-total floor the operator never chose — this is logged loudly as a warning. Otherwise a quieter debug line. Returns `{"enabled": False, "action": "disabled"}`.
2. **Resolve managers** from the registry. If both space-pressure managers are missing, return `{"enabled": True, "action": "no_managers"}`. Resolve a Radarr instance via `radarr_sp._resolve_instance(None)` and a Sonarr instance via `sonarr_ef._resolve_instance(None)`.
3. **Read targets & free.** `_read_total(...)` takes the conservative MIN of Radarr/Sonarr `disk_total_gb` (or `None`); `_space_targets(total_gb=total)` gives `(T, U)`; `_read_free(...)` takes the conservative MIN of the two services' free-space reads. If `free >= U`, no pressure — return `{"action": "none", ...}`.
4. **Stage 1 — downgrades.** Run `radarr_sp.run_downgrades(radarr_inst, free)` and `sonarr_sp.run_downgrades(sonarr_inst, free)` (each guarded). Re-read free. If now `>= U`, run restores and return `{"action": "downgrades_only", ...}`.
5. **Stage 2 — combined ranked delete pool.** Build the pool by concatenating `radarr_sp.build_delete_candidates(...)` and `sonarr_ef.build_delete_candidates(...)`. If empty, run restores and return `{"action": "no_candidates", ...}`. Compute `need = U - free`. Read the optional `delete_recency_ramp` (sets `now` when enabled) and `delete_tier_size`. Call `_select_for_target(pool, need, ...)` → `(selected, projected)`. Split `selected` by `c["service"]` into `movie` and `episode` picks; tag movie picks with `reason = "coordinator pool (score ...)"`; collect episode `fid`s. Log the selection; warn if `projected < need` ("pool exhausted"). Delete via `radarr_sp.delete_selected_movie_files(...)` and `sonarr_ef.delete_selected_episode_files(...)`. Record `free_after_deletions_gb`; `action = "deleted"`.
6. **Stage 3 — restore.** `_run_restores(...)` calls `radarr_restore.restore_recovered_deletions(radarr_inst)` and `sonarr_ef.restore_recovered_episode_deletions(sonarr_inst)` — re-acquiring anything previously coordinator-deleted whose score has since recovered.

**Internal helpers.**
- `_mgr(key)` — registry lookup, returns `None` on miss.
- `_read_free(...)` / `_read_total(...)` — conservative MIN across both services; filter out NaN/inf; `_read_free` returns `inf` when nothing is readable (so a totally-unreadable mount is treated as "no pressure" and skipped), `_read_total` returns `None`.
- `_run_restores(...)` — guarded Stage-3 restore calls.

**Delegated decisions (brain — named, not documented here).** Ranking and gating math live in `machine_learning/space`: `coordinator_ranker.critic_sort` and `coordinator_ranker.select_for_target` (via the `_critic_sort` / `_select_for_target` thin wrappers), and `space_targets` / `coordinator_owns_deletion` (imported through the `scripts.support.utilities.space_targets` re-export shim). This manager only feeds them inputs and applies their output.

## Criteria & examples

- **Gate:** the coordinator runs only when `space_coordinator_enabled=true` AND `free_space_limit > 0`. Example: `space_coordinator_enabled=true`, `free_space_limit=200` → coordinator owns deletion. With `free_space_limit=0` → inert, loud warning, per-service deletion continues.
- **Pressure band:** floor `T` and band-top `U` come from `space_targets`. Example: floor `T = 200 GB`, band-top `U = 220 GB` (per the project's `U = T × 1.1` convention). If the shared mount reports `free = 240 GB ≥ 220 GB`, `run()` returns immediately with `action="none"` — a healthy library is never touched.
- **Reclaim need:** when `free = 150 GB` and `U = 220 GB`, `need = U − free = 70 GB`. Stage 1 downgrades run first; if free recovers to `225 GB ≥ 220 GB`, `action="downgrades_only"` and no deletion happens. If after downgrades `free = 180 GB`, `need` is recomputed and Stage 2 deletes from the combined pool.
- **Selection order:** the pool is ranked lowest `watchability_score` first, then lowest critic rating, then biggest file first, accumulating from the bottom until projected free reaches `U`. Example: an episode scoring 12 with critic 4.0 is deleted before a movie scoring 28 with critic 7.5. With `delete_tier_size` set, within a score tier the largest file is taken first to reach the target in fewer deletions.
- **Recency guard:** with `delete_recency_ramp.enabled=true`, a file watched in the last few days sinks to the bottom of the delete order, so cold titles are swept first. Default-off → the bare watchability ranking (byte-identical).
- **Pool exhausted:** if the whole eligible pool only projects `~40 GB` against a `70 GB` need, it logs a warning and deletes everything eligible (best effort).

## In plain English

Think of your media drive as a shared fridge holding both movies and TV episodes. When the fridge gets too full, this coordinator is the person who decides what to throw out — and crucially, it judges everything together instead of letting the "movies shelf" and the "TV shelf" each clean themselves separately. First it tries the cheap fix: swapping a few big high-definition files for smaller versions (downgrades). If that frees enough room, it stops there. If not, it lines up every candidate — say a long-forgotten episode of a reality show you watched once and the third sequel of a film nobody finished — and tosses the least-loved items first until there's comfortable space again. It avoids throwing out something you just watched, and it keeps a receipt for everything it removes, so if your interest in a title bounces back later, it can quietly re-acquire it. And if your fridge isn't actually full, it doesn't touch a thing.

## Interactions

- **Parent manager:** constructed and run by `Main` (the process entry point), with `radarr` and `sonarr` references injected. It is its own `parent_name` root and loads no submanagers.
- **Sibling managers it drives (via the registry):** `RadarrSpacePressureManager`, `SonarrSpacePressureManager`, `SonarrCacheEpisodeFilesManager`, `RadarrRepairAnomalyManager` — these own the actual FETCH/CACHE/APPLY (downgrades, candidate builds, deletions, restores, free/total-space reads).
- **Brain modules (delegated, not documented):** `machine_learning.space.coordinator_ranker` (`critic_sort`, `select_for_target`) for ranking, and `machine_learning.space.space_targets` (`space_targets`, `coordinator_owns_deletion`) for the gate/target math, reached through the `scripts.support.utilities.space_targets` re-export shim.
- **External services (indirect):** Radarr and Sonarr HTTP APIs, touched only through the leaf managers (`moviefile/{id}`, `episodefile/{id}`, `disk_total_gb`).
