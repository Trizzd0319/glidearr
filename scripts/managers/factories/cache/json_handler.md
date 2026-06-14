# CacheJsonManager

- **File** — `scripts/managers/factories/cache/json_handler.py`
- **One-liner** — The disk-JSON read/write/delete worker behind `GlobalCacheManager`; turns slash-delimited cache keys into `.json` files and loads them back, returning an empty dict on any failure.

## What it does (for a senior Python engineer)

`CacheJsonManager` is a plain helper class (not a `BaseManager`, not a singleton) owned by `GlobalCacheManager` as `self.json_handler`. It holds its own `CacheKeyBuilder` (built from the `base_dir` passed in) and uses it to resolve every key to a path.

This file also defines a small utility class `JsonSanitizer` with a single `@classmethod sanitize(obj)` that recursively walks dicts/lists and converts `datetime` to ISO strings. (Note: the actual write path uses `make_json_safe` from `json_utils`, not `JsonSanitizer`, so `JsonSanitizer` is effectively unused by `CacheJsonManager` itself.)

### Key public methods

- `get(key)` — resolve the key to a path and `load_json` it; returns a dict (`{}` on miss).
- `exists(key_or_path)` — true if the resolved JSON file exists; accepts either a string key or an already-built `Path`.
- `load_json(path)` — open and `json.load` a path. Catches `FileNotFoundError` (debug log), `json.JSONDecodeError` (warning), and any other exception (warning), and in all error cases returns `{}` — callers never see an exception.
- `load_or_initialize(key, default=None)` — load; if the result is falsy, write `default or {}` to disk and return it. Used to lazily create empty cache files.
- `save_json(path, data, compressed=False, indent=None)` — `make_json_safe` the data then write UTF-8 JSON with the given indent. Returns `True`. (The `compressed` flag is accepted but not acted on here.)
- `set(key, data, indent=None)` — resolve key then `save_json`.
- `set_with_pretty_output(path, data, compressed=False)` — `save_json` with `indent=2`.
- `delete(key_or_path)` — unlink the file if present; returns `True` on delete, `False` if absent or on error.

### FETCH / CACHE / APPLY

Pure **CACHE** (the persistence half). No HTTP, no external API, no config reads. Paths come entirely from its `CacheKeyBuilder`.

- **Keys/paths:** every key is split on `/` and resolved through `key_builder.build_cache_path(*parts, suffix=CacheSuffix.JSON.value)` → `<base_dir>/<parts...>.json`.
- **dry_run:** not applicable — local cache writes always occur.
- **Concurrency:** no locking; relies on the filesystem. Reads degrade to `{}` rather than raising.

## How it functions

There is no lifecycle beyond construction: `__init__` stores a logger (falling back to the bare `print` builtin if none is given — note this means `log_debug`/`log_warning` calls would fail if `print` were ever actually used, but in practice `GlobalCacheManager` always passes a real `LoggerManager`) and builds a `CacheKeyBuilder` over `base_dir`.

The central helper is `_resolve_path(key)`, which every key-based method funnels through: it splits the key on `/` and asks the key builder for the `.json` path (which also sanitizes each path component and creates parent directories). It delegates no decision to any `machine_learning` brain module.

## Criteria & examples

- **Resilient read.** Calling `get("radarr/main/library")` on a machine where `support/cache/radarr/main/library.json` does not yet exist logs a debug "cache not found" and returns `{}` — the caller treats an empty dict as "nothing cached," never crashes.
- **Lazy init.** `load_or_initialize("trakt/main/state", default={"seen": []})` on a fresh install writes `{"seen": []}` to `support/cache/trakt/main/state.json` and returns it; on the next run the file exists and the stored value is returned instead.
- **Pretty vs compact.** `set_with_pretty_output(path, data)` writes 2-space-indented JSON (human-diffable in git); `set(key, data)` with no indent writes compact single-line JSON.

## In plain English

This is the part of the shared fridge that actually opens and closes the JSON containers. When asked for a labeled box it tries to open it; if the box is missing or the lid is jammed (corrupt file), it quietly hands back an empty container instead of dropping everything on the floor. When asked to store something it writes a neat label and puts it on the right shelf, creating the shelf if it does not exist yet.

## Interactions

- **Parent:** `GlobalCacheManager` (holds it as `self.json_handler`).
- **Collaborator:** `CacheKeyBuilder` (key→path), and `make_json_safe` from `scripts/support/utilities/json_utils.py`.
- **Brain modules:** none.
