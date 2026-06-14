# RadarrStorageSelectorManager

- **File** — `scripts/managers/services/radarr/storage/selection.py`
- **One-liner** — Chooses which Radarr instance (and which root path) a movie should land on, by resolution, free space, or configured root.

## What it does (for a senior Python engineer)

`RadarrStorageSelectorManager(BaseManager, ComponentManagerMixin)` is the `selector` submanager under `RadarrStorageManager`. Pure decision/lookup helper — it issues no mutating API calls.

Key PUBLIC methods:
- `select_instance_by_resolution(resolution: str) -> str`. Substring-matches the resolution string against `{"2160": "4k", "1080": "1080", "720": "720"}` and returns the mapped instance label; falls back to `"720"` (with a warning) if nothing matches.
- `select_instance_by_free_space(required_gb: float = 5.0) -> str`. Constructs a `RadarrStorageSpaceManager` (passing through all shared deps), calls `get_free_space_per_instance()`, and returns the **first** instance whose free space `>= required_gb`. If none qualifies it returns the instance with the most free space (warning). If the map is empty it logs an error and returns `"720"`.
- `select_root_path_for_instance(instance: str) -> str`. Resolves the instance, returns `config.get_radarr_instance_root(instance)` if set; otherwise builds a `RadarrStorageSpaceManager`, takes the first root folder's `"path"` (default `"/tv"`), or finally falls back to `"/tv"`.
- `_resolve_instance(instance)` — `instance_manager` → `radarr_api` → literal/`"default"`.
- `warm_cache(logger, cache)` — **staticmethod**; touches `cache.get("radarr/instance/mappings", default=None)` and logs.

FETCH/CACHE/APPLY: effectively a **read/decision** layer. It triggers FETCH/CACHE indirectly (the `RadarrStorageSpaceManager` it builds will hit `rootfolder` / `disk_free_gb` and the space-estimates cache). No APPLY. `self.dry_run` is captured but unused (selection never mutates).

- External API endpoints: none directly; transitively the space manager's `GET rootfolder` and `disk_free_gb`.
- Config keys: `config.get_radarr_instance_root(instance)`.
- global_cache keys: reads `radarr/instance/mappings` in `warm_cache` (note: this literal differs from `CacheKeyPaths.radarr.INSTANCE_MAPPINGS` which is `radarr/mappings`).
- Singleton/concurrency: BaseManager singleton; self-registers; auto-links parent.

## How it functions

`__init__` mirrors the other storage leaves: set `parent_name`, `super().__init__`, `register()`, pull deps from kwargs/parent. No `load_components`. Notable internal pattern: `select_instance_by_free_space` and `select_root_path_for_instance` both **instantiate a fresh `RadarrStorageSpaceManager` inline** (rather than reusing the sibling already loaded on the parent) — functionally fine because BaseManager is a singleton, but worth noting as a coupling point.

The two-tier fallback in `select_instance_by_free_space` (first-fit ≥ required, then max-free, then `"720"`) is the only real "selection rule" and it is hard-coded here, not delegated to a `machine_learning` brain module.

## Criteria & examples

- **Resolution match**: `select_instance_by_resolution("1080p")` → `"1080"`; `select_instance_by_resolution("HDTV-480")` → no match → `"720"`.
- **Free-space first-fit**: with `required_gb=5.0` and `{4k: 3.0, 1080: 50.0}`, the `4k` instance (3.0 < 5.0) is skipped; `1080` (50.0 ≥ 5.0) is selected. The map is iterated in dict order, so the first qualifying instance wins — not necessarily the emptiest.
- **All-too-small fallback**: with `required_gb=5.0` and `{4k: 1.0, 1080: 2.5}`, none qualifies → returns `1080` (max free = 2.5).
- **Root path**: if `config.get_radarr_instance_root("4k")` returns `"/movies/4k"`, that is used; if unset and Radarr reports a root folder `{"path": "/data/movies"}`, that path is used; otherwise `"/tv"`.

## In plain English

This is the shelf-assignment clerk. When a new movie is about to be added — say a 4K copy of an *Avengers* film — this clerk first tries to put it on the matching "4K" shelf. If you've told it exactly where the 4K shelf lives, it uses that; otherwise it asks the warehouse for the shelf's address. And if you just need *any* shelf with at least 5 GB free, it grabs the first one with room, or the emptiest-but-best one if every shelf is tight.

## Interactions

- **Parent**: `RadarrStorageManager`.
- **Siblings**: builds and uses `RadarrStorageSpaceManager` for free-space and root-folder lookups.
- **Services**: `config` (instance root), `global_cache` (mappings warm), `instance_manager` / `radarr_api` (resolution). No brain-module delegation.
