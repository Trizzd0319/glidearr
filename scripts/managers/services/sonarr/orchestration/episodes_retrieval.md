# SonarrOrchestrationEpisodeRetrievalManager

**File** — `scripts/managers/services/sonarr/orchestration/episodes_retrieval.py`
**One-liner** — Runs episode retrieval in sharded batches per Sonarr instance, and warms the episode cache across all instances.

## What it does (for a senior Python engineer)

`SonarrOrchestrationEpisodeRetrievalManager(BaseManager, ComponentManagerMixin)` with `parent_name = "SonarrOrchestration"`. Resolves `manager` (top-level `SonarrManager`), `sonarr_api`, `sonarr_cache`, `dry_run`, then two submodules off `manager.episodes`:
- `self.retrieval` = `episodes.retrieval`
- `self.sharding` = `episodes.sharding`

**Self-disabling:** if either `retrieval` or `sharding` is unavailable, it sets `self.active = False` and `self._inactive_reason = "Retrieval or Sharding managers unavailable — episode retrieval orchestration disabled."` and returns early. The parent `SonarrOrchestrationManager` checks `active` and, if `False`, sets its `episodes_retrieval` slot to `None` (recorded as `⏭️ Inactive`) rather than raising. Otherwise `self.active = True`.

Key public methods:
- `orchestrate_episode_retrieval(instance, shard_size=10, limit=None)` — builds a shard plan via `self.sharding.generate_shard_plan(instance, shard_size=shard_size)` (`{shard_index: [series_ids]}`), then for each shard (stopping early when `limit and shard_index >= limit`) fetches episodes per series via `self.retrieval.fetch.fetch_episodes_for_series(sid, instance)`. Per-series failures are caught and warned. Returns `{series_id: episodes}` merged across shards.
- `warm_all_episodes_cache(shard_size=10)` — enumerates instances via `self.sonarr_api.get_all_sonarr_apis().keys()` and runs `orchestrate_episode_retrieval` for each, logging the cached episode count per instance.

FETCH: yes — episode list GETs per series. CACHE: the warmups populate the episode cache through the retrieval leaf. APPLY: none.

External API endpoints: none called directly here; `fetch_episodes_for_series` (leaf) issues the Sonarr episode call.

Config keys: none read directly.

global_cache / Parquet keys: indirectly via the retrieval leaf's episode cache.

dry_run: captured; no mutating APPLY to gate.

Concurrency: none here — sharding sequences the work to avoid hammering Sonarr; parallelism (if any) is the cache child's job.

## How it functions

Lifecycle: `__init__` resolves submodules and sets the `active` flag (the soft-disable contract with the parent orchestrator). At runtime, `warm_all_episodes_cache` loops instances; for each it calls `orchestrate_episode_retrieval`, which walks the shard plan and fetches episodes series-by-series, tolerating per-series fetch errors.

Brain delegation: none.

## Criteria & examples

- **Shard batching:** `shard_size=10` → each shard holds up to 10 series. With 95 series there are 10 shards (last one partial).
- **`limit`:** `orchestrate_episode_retrieval(instance, limit=3)` processes only shards with index `< 3` (the first 3 shards, ~30 series) and stops — useful for a quick partial warmup.
- **Soft-disable:** if `episodes.sharding` failed to initialise (so `self.sharding is None`), the manager flips `active=False` at construction; the parent skips it and the rest of the enrichment pipeline continues. No exception propagates.

## In plain English

This is the "download the episode lists, but in polite batches" worker. Rather than asking Sonarr for every episode of every show all at once (which could overwhelm it), it splits your shows into groups of ten and works through them group by group, across every Sonarr instance you run. If one show's lookup hiccups, it shrugs and moves on. And if the tools it depends on aren't available, it quietly bows out instead of breaking the whole refresh. The result: the app has a fresh, complete picture of every episode you own — think of it as re-checking the full episode guide for all your shows.

## Interactions

- **Parent manager:** `SonarrManager` (resolved as `manager`); constructed by `SonarrOrchestrationManager` as its `episodes_retrieval` child (which honours this manager's `active` flag).
- **Leaf submodules driven:** `episodes.sharding` (`generate_shard_plan`) and `episodes.retrieval.fetch` (`fetch_episodes_for_series`); instance enumeration via `sonarr_api.get_all_sonarr_apis()`.
- **Brain modules:** none.
