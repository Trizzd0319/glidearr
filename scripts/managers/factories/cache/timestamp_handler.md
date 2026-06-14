# CacheTimestampManager

- **File** — `scripts/managers/factories/cache/timestamp_handler.py`
- **One-liner** — The "last updated" marker worker behind `GlobalCacheManager`; writes and reads UTC timestamp files so the app can tell how stale a cached resource is.

## What it does (for a senior Python engineer)

`CacheTimestampManager` is a plain helper (not a `BaseManager`, not a singleton) held by `GlobalCacheManager` as `self.timestamp_handler`. It manages tiny `.last_updated` files whose entire content is one ISO-8601 UTC timestamp string, used as freshness markers next to cached data.

It holds a `CacheKeyBuilder` (passed in by `GlobalCacheManager`, else constructs its own) to resolve marker paths.

### Key public methods

- `update_timestamp(service, instance, category)` — write `datetime.utcnow()` (stamped as UTC, ISO format) to the marker file; creates parent dirs. Returns `True`/`False`.
- `read_timestamp(path)` — read and `datetime.fromisoformat`-parse a marker at an explicit path; returns `datetime` or `None` (on missing file or parse error).
- `read_timestamp_by_key(service, instance, category)` — resolve the standard path then `read_timestamp` it.
- `is_fresh(service, instance, category, max_age_seconds)` — `True` if a marker exists and its age ≤ `max_age_seconds`.
- `get_age_seconds(service, instance, category)` — integer seconds since last update, or `None` if no marker.

> Note a small API mismatch worth flagging: `GlobalCacheManager.update_timestamp` calls `timestamp_handler.update_timestamp(path)` with a *single resolved path*, but this method's signature is `update_timestamp(service, instance, category)`. So when invoked via the facade, `service` receives the `Path`, `instance`/`category` are unset, and `_resolve_timestamp_path` would mis-build the path. The facade's `read_timestamp(service, instance, category)` likewise calls `timestamp_handler.read_timestamp(path)` with a single path — which matches this class's `read_timestamp(path)` signature. The write path appears inconsistent with the read path; treat the writer's exact behavior via the facade as suspect rather than assuming it works.

### FETCH / CACHE / APPLY

Pure **CACHE** (freshness metadata). No HTTP, no external API, no config keys. Path layout for a marker is `{service}/{instance}/{category}/last_updated.last_updated` under the cache root (`_resolve_timestamp_path` builds `service/instance/category/last_updated` then applies the `.last_updated` suffix).

- **dry_run:** not applicable — local writes always occur.
- **Concurrency:** no locking; a one-line file write.

## How it functions

No lifecycle beyond `__init__`. The single internal helper `_resolve_timestamp_path(service, instance, category)` builds the key string and asks the key builder for the suffixed path. Freshness is computed with timezone-aware UTC subtraction (`datetime.now(timezone.utc) - ts`). It delegates no decision to any `machine_learning` brain module — callers decide what to do with the age.

## Criteria & examples

- **Freshness gate.** With a marker written 3,600 s ago, `is_fresh(service, instance, category, max_age_seconds=7200)` returns `True` (3600 ≤ 7200); the caller skips a refetch. With `max_age_seconds=1800` it returns `False` and the caller refetches.
- **No marker.** `get_age_seconds(...)` for a never-stamped resource returns `None`, and `is_fresh(...)` returns `False` — the caller treats "never updated" as "not fresh," forcing a fetch.

## In plain English

This is the little date sticker on each fridge container. Before reheating last night's dinner the app peeks at the sticker: "was this made within the last two hours?" If yes, eat it; if the sticker is old (or there is no sticker at all), make it fresh. The sticker itself holds nothing but the time it was written.

## Interactions

- **Parent:** `GlobalCacheManager` (`self.timestamp_handler`).
- **Collaborator:** `CacheKeyBuilder`, `CacheSuffix.LAST_UPDATED` (`constants.py`).
- **Brain modules:** none.
