# TraktMoviesManager

- **File** — `scripts/managers/services/trakt/movies/__init__.py`
- **One-liner** — The thin aggregator for the Trakt *movies* subtree: constructs the shared movie disk cache, loads the people (cast/crew) submanager, and proxies movie-credit enrichment to it.

## What it does (for a senior Python engineer)

`TraktMoviesManager(BaseManager, ComponentManagerMixin)` is the parent of the Trakt movies submanagers. It owns no HTTP and no value judgements; it wires up dependencies and forwards calls. Concretely it:

- Constructs one `TraktMovieCacheManager` (`self.cache`) and injects that single instance into its child so the parent and the people manager share one cache.
- Instantiates its critical submanager `TraktMoviePeopleManager` as `self.people`.
- Exposes `enrich_movies(...)`, a proxy onto `self.people.enrich_movies(...)`.
- Exposes `cache_stats()`, a proxy onto `self.cache.stats()`.

Manager tree: `parent_name = "TraktMoviesManager"`; its own parent is whatever Trakt-level manager constructs it (passed as the `manager` kwarg). Submanagers:
- `people` → `TraktMoviePeopleManager` (critical).
- `cache` → `TraktMovieCacheManager` (built directly, not via the component map; injected into `people`).

Component loading note: although it inherits `ComponentManagerMixin`, it does NOT call `load_components`. Instead it uses `split_components(...)` (from `support/utilities/managers/component_splitter`) to partition `{"people": TraktMoviePeopleManager}` into critical vs non-critical (critical_keys = `{"people"}`), then instantiates each critical class itself, recording outcomes in `self.load_summary` (`"✅ Loaded"` / `"❌ Failed: …"`) and setting `self.all_components_loaded`. It then emits one summary via `log_filtered_component_summary(service_name="Trakt", …)`.

FETCH/CACHE/APPLY: none directly — it is a pure aggregator/proxy. The FETCH/CACHE work happens in its children.

External API endpoints: none directly.

Config keys read: none directly. It reads `dry_run` from the parent (`kwargs["manager"].dry_run`) or kwargs, defaulting False, and propagates it into the cache and into `init_kwargs` for children.

global_cache / Parquet keys: none directly (children own those).

Public methods:
- `enrich_movies(movies, has_file_only=True, watched_titles=None, watched_tmdb_ids=None, chunk_size=500, cache_only=False) -> list[dict]` — guards that `self.people` exists (logs a warning and returns `movies` unchanged if not), else forwards to `people.enrich_movies(...)`. Note the proxy's `has_file_only` default is **True** here (vs. False on the underlying method), so by default the parent restricts enrichment to owned movies.
- `cache_stats() -> dict` — returns `self.cache.stats()` (`{"total", "fresh", "stale"}`), or `{}` if no cache.

dry_run: captured at init and threaded into the cache and people managers; this class performs no mutating I/O of its own.

Singleton / concurrency: `BaseManager` process-wide singleton keyed by `(class, singleton_key)`. No threading of its own.

## How it functions

Lifecycle: `__init__` → `super().__init__` (inject shared logger/config/global_cache/validator/registry, auto-link to parent) → `register()` → resolve `dry_run` → build `self.cache` (`TraktMovieCacheManager`) → assemble `init_kwargs` (shared deps + `manager=self` + `dry_run` + `cache_manager=self.cache`) → `split_components` → instantiate the critical `people` submanager, attaching it as `self.people` and recording `load_summary` → set `all_components_loaded` → log the filtered component summary.

There is no long-running `run` loop; the manager's "entry" surface is the two proxy methods, called by Trakt-level orchestration when movie enrichment is needed.

Brain delegation: none here. The selection/ordering decisions live one level down in `TraktMoviePeopleManager`, which calls `machine_learning/acquisition/enrichment_prioritizer` (documented with that manager, not here).

## Criteria & examples

- **Graceful degradation.** If `people` failed to construct (`load_summary["people"] = "❌ Failed: …"`, `all_components_loaded = False`), a later `enrich_movies([...])` logs `people manager not available — returning movies unchanged` and returns the input list untouched, rather than raising.
- **Default scope is owned-only.** Calling `manager.enrich_movies(movies)` with no `has_file_only` uses the proxy default `True`, so unowned movies are skipped — even though the underlying `TraktMoviePeopleManager.enrich_movies` would default to `False`.
- **Shared cache identity.** The same `TraktMovieCacheManager` instance backs both `manager.cache_stats()` and the people manager's reads/writes, so a fetch performed during `enrich_movies` is immediately reflected in the next `cache_stats()` `total`/`fresh` counts.

## In plain English

This is the front desk for "movie people" information. You hand it a stack of movies and ask "fill in who starred in and made each of these," and it quietly passes the stack to the specialist in the back room (the people manager) who does the actual looking-up, while a shared filing cabinet (the cache) keeps results so nobody re-asks. If the specialist happens to be out sick (failed to load), the front desk simply hands your stack back unchanged instead of causing a scene. And by default it only bothers looking up movies you actually own — e.g. it'll enrich your copy of *Toy Story* but won't go researching films that aren't in your library.

## Interactions

- **Parent:** the Trakt-level manager that constructs it (passed as `manager`).
- **Submanagers:** `TraktMoviePeopleManager` (`self.people`, critical) and `TraktMovieCacheManager` (`self.cache`, shared/injected).
- **Brain modules:** none directly (delegation happens in the people submanager).
- **Helpers:** `split_components` (component partitioning) and `log_filtered_component_summary` (the one-line load summary).
