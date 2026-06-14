# SonarrSeriesHelpersManager

- **File** — `scripts/managers/services/sonarr/series/helpers.py`
- **One-liner** — A stateless utility belt for Sonarr series operations: title sanitizing/slugifying, TVDB-id extraction/validation, and small lookup wrappers that resolve a series by id or tvdbId.

## What it does (for a senior Python engineer)

`SonarrSeriesHelpersManager(BaseManager, ComponentManagerMixin)` is a child of `SonarrSeriesManager`. It loads no submanagers and holds no state beyond the injected dependencies. It performs FETCH only (a handful of Sonarr API reads); it does no CACHE writes and no APPLY. Several lookups simply delegate to the sibling `retrieval` manager.

Key public methods:
- `run()` — explicitly a no-op (logs "no operational logic defined").
- `sanitize_series_title(title) -> str` — normalizes curly apostrophes to straight, strips, lowercases.
- `slugify_title(title) -> str` — lowercases, spaces→hyphens, drops apostrophes.
- `extract_tvdb_id_from_series(series_obj) -> int | None` — reads `tvdbId` / `tvdb_id` / `externalIds.tvdb` in order.
- `is_valid_tvdb_id(tvdb_id) -> bool` — true only for a positive `int`.
- `get_series_by_tvdb(instance, tvdb_id)` — validates the id, then `GET movies?tvdbId={id}` and returns the first hit (see caveat below).
- `get_series_title_slug(instance, series_id)` — resolves the `retrieval` manager (registry or parent), calls `retrieval.get_series_by_id(resolved_instance, series_id)`, returns `titleSlug`.
- `get_series_title(instance, series_id)` — wraps `get_series_by_id` and returns `title`.
- `get_series_by_id(instance, series_id)` — resolves `retrieval` and returns `retrieval.get_series_by_id(instance, series_id)`.
- `get_series_tags(instance)` — `GET tags` (fallback `[]`).
- `generate_series_lookup_map(instance)` — `GET movies` (fallback `[]`) (see caveat below).

Config keys read: none directly. global_cache / Parquet: none read or written here (it wires `sonarr_cache`/`global_cache` from the parent but does not use them in any method). dry_run: stored from kwargs/parent but unused (no mutating calls). Singleton/threading: standard `BaseManager` singleton; no threading concerns.

**Caveat worth flagging**: `get_series_by_tvdb` and `generate_series_lookup_map` issue requests against `movies` / `movies?tvdbId=...` rather than a Sonarr `series` endpoint. For a TV/Sonarr context this looks like a copy-from-Radarr leftover; the code does exactly that, so these two methods are unlikely to return series data against a real Sonarr instance. Documented as-is, not corrected.

## How it functions

Init wires the dual cache, `dry_run`, `sonarr_api`, and `instance_manager` from kwargs or the parent, then `register()`. There is no `prepare()` and `run()` does nothing — this manager is invoked à-la-carte by other managers calling its helper methods.

Instance resolution: every method that hits the API or retrieval first calls `self.instance_manager.resolve_instance(instance)`. The retrieval-backed lookups (`get_series_by_id`, `get_series_title_slug`) prefer `self.registry.get("manager", "retrieval")` and fall back to `getattr(self.manager, "retrieval", None)`, warning and returning `None` if neither is available.

No decisions are delegated to a `machine_learning` brain module — this is pure plumbing.

## Criteria & examples

The only guard is `is_valid_tvdb_id`: it returns `True` only when the value is an `int` greater than 0. Example: `is_valid_tvdb_id(81189)` → `True`; `is_valid_tvdb_id("81189")` → `False` (string, not int); `is_valid_tvdb_id(0)` → `False`. `get_series_by_tvdb` short-circuits to a warning + `None` whenever this guard fails, so a malformed id never reaches the API. Title examples: `slugify_title("The King's Avatar")` → `the-kings-avatar`; `sanitize_series_title("It's Always Sunny")` (with a curly apostrophe) → `it's always sunny`.

## In plain English

This is the little toolbox of odd jobs for TV shows. Need a tidy web-friendly name for "The Mandalorian"? It hands back `the-mandalorian`. Got a database ID and want to make sure it's a real number before looking it up? It checks. Want the show's record by its ID? It asks the proper records clerk (the retrieval manager) to fetch it. None of these tools change anything in your library — they just look things up and clean up names so the rest of the system can talk about a show consistently.

## Interactions

- **Parent manager**: `SonarrSeriesManager`.
- **Sibling submanagers**: depends on the `retrieval` manager (`SonarrSeriesRetrievalManager`) for `get_series_by_id` lookups, resolved via registry or the parent.
- **Brain modules**: none.
- **Other services / registry**: Sonarr HTTP API (`sonarr_api`) for `tags` / `movies` reads, and `instance_manager` for instance resolution.
