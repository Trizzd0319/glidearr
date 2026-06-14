# TraktUniverseManager

- **File** — `scripts/managers/services/trakt/universe/__init__.py`
- **One-liner** — A tiny in-memory lookup table that maps named franchise "universes" (e.g. `marvel-cinematic-universe`, `star-trek`) to lists of TVDB show IDs.

## What it does (for a senior Python engineer)

`TraktUniverseManager` is a thin, stateless reference table. It holds a hard-coded `slug -> [TVDB ids]` dictionary and exposes a few read helpers over it. It performs **none of FETCH / CACHE / APPLY** — it makes no HTTP calls, reads/writes no `global_cache` or Parquet keys, touches no Trakt API endpoints, and reads no config keys. The data is entirely static and recomputed on every call.

Key public methods:

- `get_universe_mapping() -> dict` — Returns the full static mapping of universe slugs to lists of TVDB IDs. The docstring says it can be "extended or overridden via config," but the current implementation reads no config — it just returns the literal dict. This is the single source the other methods read from.
- `get_shows_by_universe(universe_slug: str) -> list` — Looks up one slug in the mapping. On a hit, returns its list of TVDB IDs; on a miss, logs a warning (`[TraktUniverse] No universe found for slug '<slug>'`) and returns `[]`.
- `list_all_universes() -> list` — Returns the list of all available universe slugs (the mapping's keys) and logs them at info level.
- `add_custom_universe(universe_slug: str, tvdb_ids: list)` — A **placeholder/no-op**. It only logs that a custom universe was "registered"; it does not mutate the mapping or persist anything anywhere. Returns `None`.

Position in the manager tree:

- Declares `parent_name = "TraktManager"` (both as a class attribute and re-set in `__init__`).
- It is constructed by **`TraktAPIManager`** (`scripts/managers/services/trakt/api/__init__.py`), which attaches it as the `universe` attribute alongside the other Trakt sub-managers (`history`, `ratings`, `recommendations`, `watchlist`, `lookup`, `analytics`, `progress`, `lists`, `sync`). Note that `TraktAPIManager` instantiates these directly in a manual loop with a shared `init_kwargs` dict — it does **not** use `ComponentManagerMixin.load_components` for them. If construction raises, `TraktAPIManager` logs `[TraktAPI] Sub-manager 'universe' failed to load` and sets the attribute to `None`.
- It inherits `BaseManager` (so it participates in the singleton/registry/auto-link machinery — `super().__init__(...)` then `self.register()`) and `ComponentManagerMixin`, but it loads **no submanagers of its own** (it never calls `load_components`).

`__init__` details:

- Signature: `__init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)`.
- Calls `super().__init__(...)` then `self.register()`.
- `self.dry_run` is taken from `kwargs["dry_run"]`, falling back to the parent manager's `dry_run`, then `False`. It is stored but never used (the class has no APPLY path).
- `self.trakt_api` is captured from `kwargs["trakt_api"]` (the owning `TraktAPIManager`) but is also unused by any current method.

dry_run behavior: not applicable — there is no mutation or write step to suppress. `add_custom_universe` is already a log-only stub.

Singleton / concurrency: standard `BaseManager` singleton behavior applies. The class itself has no locks, no shared mutable state, and no threading concerns — every method derives its result from the freshly-constructed literal dict.

## How it functions

Lifecycle: `TraktAPIManager.__init__` builds the shared `init_kwargs` (logger, config, global_cache, validator, registry, `manager=self`, `dry_run`, `trakt_api=self`) and constructs `TraktUniverseManager(**init_kwargs)`, attaching it as `self.universe`. The universe manager's own `__init__` runs the `BaseManager` chain, registers itself, and stashes `dry_run` and `trakt_api`. There is no `load_components` step and no run/entry method — it is purely a lookup utility invoked on demand by callers that want to resolve a franchise slug to TVDB IDs.

Control flow is trivial: `get_shows_by_universe` and `list_all_universes` both delegate to `get_universe_mapping()` (which rebuilds the dict each call), then either index into it or return its keys. `add_custom_universe` only emits a log line.

It delegates **no** decision to any `machine_learning` brain module.

## Criteria & examples

The only guard in the file is the slug-lookup miss in `get_shows_by_universe`:

- `get_shows_by_universe("marvel-cinematic-universe")` -> the slug is present, so it returns `[295759, 295760, 326490]`.
- `get_shows_by_universe("star-trek")` -> returns `[74608, 79349, 261690]`.
- `get_shows_by_universe("harry-potter")` -> the slug is **not** a key in the mapping, so it logs `[TraktUniverse] No universe found for slug 'harry-potter'` and returns `[]`. (Note: the Harry Potter franchise is present under the slug `wizarding-world`, so a caller using the wrong slug silently gets nothing.)

One data caveat visible in the source: the `arrowverse` slug maps to `[295759, 295760, 326490]` — the exact same IDs as `marvel-cinematic-universe` — which looks like a copy/paste artifact rather than intentional, but the code treats them as independent entries regardless.

## In plain English

Think of this as the index card taped inside a box set: it just knows that "the Marvel universe" means a specific handful of shows, "the Star Trek universe" means another handful, and so on. Ask it "what's in the Star Wars universe?" and it hands you the list; ask for a universe it has never heard of and it shrugs ("no universe found") and hands you an empty list. It doesn't go fetch anything from the internet, it doesn't remember anything between questions, and the "add your own universe" button is currently just a sticky note that says "registered" without actually saving it. It exists so other parts of the app can group related shows together by franchise.

## Interactions

- **Parent manager:** `TraktAPIManager` (declared `parent_name = "TraktManager"`; the `TraktManager` service sits above `TraktAPIManager`). It is held as `TraktAPIManager.universe`.
- **Sibling submanagers:** the other Trakt sub-managers loaded by `TraktAPIManager` — `history`, `ratings`, `recommendations`, `watchlist`, `lookup`, `analytics`, `progress`, `lists`, `sync`. It does not call any of them.
- **Brain modules / other services:** none. No `machine_learning` delegation, no external API, no cache. It only uses the injected `logger`. The captured `trakt_api` reference is currently unused.
