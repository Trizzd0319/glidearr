# SonarrEpisodesRetrievalManager

**File** — `scripts/managers/services/sonarr/episodes/retrieval/__init__.py`
**One-liner** — The orchestrator for the Sonarr "episode retrieval" subtree: it loads and runs the six retrieval submanagers (fetch, enrich, tvdb, sync, validate, episode_cache) that read episode data from Sonarr and the TVDB API.

## What it does (for a senior Python engineer)

`SonarrEpisodesRetrievalManager` is a thin container/orchestrator. It owns no episode logic itself; it instantiates the retrieval submanagers and provides a uniform `prepare()`/`run()` fan-out across them.

Position in the manager tree: it is a child of the `SonarrEpisodes` manager (the parent passed in via `kwargs["manager"]`), and it is the parent of the six submanagers it loads. Its `parent_name` is declared as `"SonarrEpisodesRetrieval"` at the class level but is overwritten in `__init__` to `self.__class__.__name__` (i.e. `"SonarrEpisodesRetrievalManager"`) — note this means the registry `parent_name` the submanagers were declared against (`"SonarrEpisodesRetrieval"`) and the instance value diverge after init.

Submanagers loaded via `load_components` (registry prefix `sonarr.episodes.retrieval`, API kwarg `sonarr_api`):

| Attribute | Class |
|---|---|
| `fetch` | `SonarrEpisodesRetrievalFetchManager` |
| `enrich` | `SonarrEpisodesRetrievalEnrichmentManager` |
| `tvdb` | `SonarrEpisodesRetrievalTVDBManager` |
| `sync` | `SonarrEpisodesRetrievalSyncManager` |
| `validate` | `SonarrEpisodesRetrievalValidationManager` |
| `episode_cache` | `SonarrEpisodesRetrievalCacheManager` |

Key public methods:
- `prepare()` — iterates `self.components`, calls `component.prepare()` on any submanager that defines it. Failures are caught and logged as warnings; one bad submanager does not abort the others. (None of the six submanagers in this directory currently define `prepare`, so this is effectively a no-op hook today.)
- `run()` — iterates `self.components`, calls `component.run()` on any submanager that defines it. Failures are caught and logged as errors. (Likewise, none of the six submanagers define `run`, so this currently does nothing beyond the entry log line; the submanagers are libraries of methods called on demand by higher layers, not self-running.)

FETCH / CACHE / APPLY: the orchestrator itself does none of these — it delegates. Collectively its children FETCH (Sonarr HTTP GET, TVDB GET) and CACHE (global_cache / Sonarr cache). No APPLY (no PUT/DELETE/POST) is performed anywhere in this subtree.

Dual-cache wiring: it captures both `self.global_cache` (the process-wide `GlobalCacheManager`) and `self.sonarr_cache` (the Sonarr-specific cache object, resolved from `kwargs["cache_manager"]` or the parent's `sonarr_cache`). Both are passed down to the children.

dry_run: it reads `kwargs["dry_run"]` into `self.dry_run` (default `False`) but does not itself act on it — there are no mutating operations here.

Config keys read: none directly.
global_cache / Parquet keys: none directly (children own those).

Singleton/concurrency: like all `BaseManager`s it is a process-wide singleton keyed by class + singleton_key.

## How it functions

Lifecycle: `__init__` → `super().__init__` (BaseManager injects logger/config/cache/validator/registry and auto-links the parent) → `register()` → resolve dual caches and `sonarr_api`/`instance_manager` from kwargs or the parent → `load_components(...)` builds the six submanagers and attaches them as attributes → logs the sorted component list.

Control flow at run time: higher layers call individual submanager methods directly (e.g. `episode_cache.get_episodes_by_series_id(...)`). The `prepare()`/`run()` fan-out exists as a generic lifecycle contract but is inert for this subtree because the children expose method APIs rather than `run()` entry points.

No decisions are delegated to a `machine_learning` brain module from this file.

## Criteria & examples

No thresholds or selection rules live here — it is pure plumbing. Example: at startup it logs `🧩 SonarrEpisodesRetrievalManager component load complete: ['enrich', 'episode_cache', 'fetch', 'sync', 'tvdb', 'validate']`, after which `self.fetch`, `self.enrich`, etc. are usable attributes.

## In plain English

Think of this as the manager of a small research desk whose job is to pull TV-episode details for the rest of the app. The manager doesn't look anything up personally — instead it has six specialists on staff (one to grab raw episode data, one to dress it up with extra info, one to phone the external TVDB encyclopedia, one to track what changed since last time, one to check for missing episodes, and one to keep a filing cabinet of cached answers). This file just hires those six specialists, hands them the shared phone line and filing cabinet, and stands ready to tell them all to "get ready" or "go" at once. When you ask the app about an episode of, say, The Mandalorian, the answer ultimately comes from one of these specialists — this manager is the desk they all sit at.

## Interactions

- **Parent manager:** `SonarrEpisodes` (provides `sonarr_api`, `sonarr_cache`, `instance_manager`, `global_cache`).
- **Submanagers (siblings of each other):** `fetch`, `enrich`, `tvdb`, `sync`, `validate`, `episode_cache` — see their individual docs in this directory.
- **Other services:** transitively the Sonarr API (`sonarr_api`) and the TVDB v4 API (via the tvdb child).
- **Brain modules:** none — no `machine_learning` decision is delegated from this subtree.
