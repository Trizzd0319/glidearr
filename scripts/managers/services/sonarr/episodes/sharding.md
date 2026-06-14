# SonarrEpisodesShardingManager

- **File** — `scripts/managers/services/sonarr/episodes/sharding.py`
- **One-liner** — Splits Sonarr series IDs into fixed-size shard groups so episode operations can be batched per instance (and across all instances).

## What it does (for a senior Python engineer)

`SonarrEpisodesShardingManager(BaseManager, ComponentManagerMixin)` (`parent_name = "SonarrEpisodes"`) is a small, pure-computation utility child of `SonarrEpisodesManager`. It does one thing: turn a flat list of series IDs into batches. It performs **FETCH** only to obtain the series ID lists; it does no caching or applying.

`__init__` resolves the parent (`kwargs["manager"]` or registry lookup), the dual cache (`global_cache` + `sonarr_cache`), and `sonarr_api`. Raises `ValueError` if no logger.

Public methods (all `@timeit`-decorated):

- `compute_shards(series_ids, shard_size=10)` — pure list slicing: returns `[series_ids[i:i+shard_size] for i in range(0, len, shard_size)]`. Logs how many shards from how many series.
- `get_series_ids_by_instance(instance)` — FETCH: `self.sonarr_api.get_series(instance)`, returns the list of `s["id"]` for series that have an id; returns `[]` on error (logged).
- `generate_shard_plan(instance, shard_size=10)` — combines the two above: returns `{shard_index: [series_ids]}` for one instance.
- `generate_global_shard_plan(shard_size=10)` — iterates every instance from `self.sonarr_api.get_all_sonarr_apis()` and returns a nested dict `{instance_name: {shard_index: [series_ids]}}`. Guards against a missing/invalid API ref (`get_all_sonarr_apis` absent) → logs error and returns `{}`.

- Position in the tree: parent `SonarrEpisodesManager`; loads no submanagers.
- FETCH: `get_series`, `get_all_sonarr_apis`. CACHE: none. APPLY: none.
- API endpoints: via `sonarr_api.get_series(instance)` and `get_all_sonarr_apis()`.
- Config keys: none read directly.
- global_cache / Parquet keys: none read/written.
- dry_run: not used (no mutating operations).
- Concurrency: none.

> Loading note: this manager declares `parent_name = "SonarrEpisodes"`, which does NOT match the `parent_name_match="SonarrEpisodesManager"` used by `split_components` in the parent. As documented in the parent (`README.md`), `SonarrEpisodesManager` therefore loads sharding via an **explicit fallback** block rather than the normal split path.

## How it functions

Lifecycle: built by `SonarrEpisodesManager` (via the explicit fallback) → init wires API/cache refs → callers request a shard plan. `generate_global_shard_plan` simply loops over instances calling `generate_shard_plan`, which calls `get_series_ids_by_instance` (fetch) + `compute_shards` (slice).

No machine_learning brain module is invoked.

## Criteria & examples

- **Shard size:** `compute_shards([1..25], shard_size=10)` → `[[1..10], [11..20], [21..25]]` — three shards (last one partial).
- **Per-instance plan:** an instance with 12 series and `shard_size=10` → `generate_shard_plan` returns `{0: [10 ids], 1: [2 ids]}`.
- **Missing API guard:** if `self.sonarr_api` lacks `get_all_sonarr_apis`, `generate_global_shard_plan` logs `❌ API reference missing or invalid...` and returns `{}`.

## In plain English

Imagine you have to re-check every show in a library of 250 titles, but you can only carry ten boxes at a time. This manager is the worker who divides the 250 titles into 25 neat batches of ten so the rest of the team can process them one trolley-load at a time, instead of trying to haul everything at once. It can do this for a single library or for all your libraries together — it just makes the batches; it doesn't carry them.

## Interactions

- **Parent:** `SonarrEpisodesManager` (loaded via explicit fallback due to the `parent_name` mismatch).
- **Siblings:** retrieval, file, history, monitoring, deletion.
- **Talks to:** `sonarr_api` (`get_series`, `get_all_sonarr_apis`).
- **Brain modules:** none.
