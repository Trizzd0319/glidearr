# SonarrStorageSelectionManager

**File** — `scripts/managers/services/sonarr/storage/selection.py`
**One-liner** — Decides *which* root path a show/episode should live on for the single `sonarr` instance, based on free space, plus the per-instance machinery that still works generically with one instance.

## What it does (for a senior Python engineer)

`SonarrStorageSelectionManager(BaseManager, ComponentManagerMixin)` is the path-selection decision surface of the storage subtree (instance- and path-selection, not the actual moves — those are in relocation/deletion). Sonarr is a **single** instance named `sonarr` (port 8990); there is no longer any resolution → instance tiering (per-episode JIT governs quality), so the old tier-picking methods have been removed.

Key public methods:
- `select_instance_by_free_space(required_gb=5.0) -> str` — spins up a local `SonarrStorageSpaceManager`, reads `get_free_space_per_instance()`, returns the first instance with `>= required_gb` free; else the instance with the most free space; else falls back to `self.manager.resolve_instance(None)` (the resolved default — `sonarr`) if none exist. FETCH (via the space manager). With N=1 this resolves to `sonarr`.
- `select_root_path_for_instance(instance) -> str` — resolves the instance (`self.manager.resolve_instance`), returns `config.get_sonarr_instance_root(...)` if configured; otherwise falls back to the first root folder reported by a local space manager (`folders[0]["path"]`, default `"/tv"`), and finally to literal `"/tv"`. FETCH.
- `warm_cache(logger, cache)` — `@staticmethod`; touches cache key `sonarr/instance/mappings`.

Position in tree: child of `SonarrStorageManager` (registry parent name `"SonarrStorage"`). Loads no submanagers; it lazily instantiates `SonarrStorageSpaceManager` and `SonarrStorageLibraryManager` inline when it needs them.

FETCH / CACHE / APPLY: FETCH only (free space, root folders, series cache) plus pure in-memory decisions. **No APPLY** — `relocate_episode` lives on the *relocation* manager, not here. The canonical `SonarrStorageRelocationManager.relocate_mismatched_resolutions` calls `selection.select_root_path_for_instance` and `selection.select_instance_by_free_space`, both of which **are** defined on this class. (The old dead duplicate `library.relocate_mismatched_resolutions`, which mistakenly called the non-existent `selection.relocate_episode`, has been deleted in favour of the canonical relocation path.)

Config keys read: `sonarr_instances`, per-instance root (`get_sonarr_instance_root`).
Cache keys: reads the series cache via the library manager and `sonarr/instance/mappings` (warm only); root folders / free space via the space manager's `SPACE_ESTIMATES`.
dry_run: captured from kwargs/parent (default `False`); none of this class's own methods mutate state, so it is effectively unused here.

## How it functions

`__init__` derives `parent_name` from the class name, calls `super().__init__` + `register()`, looks up the parent in the registry, and back-fills `sonarr_api`, `logger`, `manager`, `dry_run`. Raises without a logger.

Selection flow is request-driven (no `run()`):
1. Capacity → instance walks the live free-space map and prefers the first instance over the `required_gb` threshold, with a graceful "most free space" fallback, and a final fallback to the resolved default instance.
2. Path selection prefers explicit config, then the live first-root-folder, then `/tv`.

No `machine_learning` brain module is consulted; placement is a free-space heuristic. (The broader ML brain migration moves value-judgements into `machine_learning/`; this particular file still encodes its rules locally.)

## Criteria & examples

- Free-space pick with `required_gb=5.0`: with a single `sonarr` instance, free map `{"sonarr": 40.0}` → `sonarr` (40.0) passes the threshold → returns `"sonarr"`. If the only entry were below threshold (e.g. `{"sonarr": 1.0}`), it returns the max → still `"sonarr"`. With an empty map it falls back to `self.manager.resolve_instance(None)` → `"sonarr"`.
- Root path fallback: instance has no `get_sonarr_instance_root` value and its first root folder is `/data/tv` → returns `/data/tv`; with no folders at all → `"/tv"`.
- Resolved gap: `relocate_episode` is defined on `SonarrStorageRelocationManager`, not on this class. The former dead caller `SonarrStorageLibraryManager.relocate_mismatched_resolutions` — which called the non-existent `selection.relocate_episode` and would have raised `AttributeError` — has been deleted. The live relocation path only reaches `select_root_path_for_instance` / `select_instance_by_free_space`, which do exist here.

## In plain English

This is the seating host at the restaurant of your TV drives. There's now just one room (the single `sonarr` instance), so the host's job is simpler: when an episode shows up it picks the right root path inside that room, and if it's getting cramped it seats things wherever there's the most elbow room. The old tier-checking — "4K guests go in the 4K room, that one's in the wrong section, someone should move them" — is gone, because there's only one room now.

## Interactions

- **Parent:** `SonarrStorageManager`.
- **Siblings:** lazily creates `SonarrStorageSpaceManager` (free space, root folders) and `SonarrStorageLibraryManager` (series cache). Its selection methods are called by `SonarrStorageRelocationManager` and `SonarrStorageLibraryManager` during relocation sweeps.
- **Services touched:** Sonarr HTTP API indirectly (through the space manager); `global_cache` for the series cache and `sonarr/instance/mappings`.
- **Brain modules:** none (placement rules are local to this file).
