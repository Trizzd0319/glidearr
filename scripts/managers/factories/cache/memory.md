# MemoryManager

- **File** — `scripts/managers/factories/cache/memory.py`
- **One-liner** — A tiny in-process, TTL-aware key→value cache held by `GlobalCacheManager` for avoiding repeated disk/API lookups within a single run.

## What it does (for a senior Python engineer)

`MemoryManager` is a plain helper (not a `BaseManager`, not a singleton) owned by `GlobalCacheManager` as `self.memory`. It is a RAM-only dictionary cache with optional age checks and access counting — nothing is written to disk. It backs three parallel dicts: `_cache` (values), `_timestamps` (when each key was set, `datetime.utcnow()`), and `_access_counts` (hit counter per key).

### Key public methods

- `set(key, value)` — store value, stamp the time, reset the access count to 0.
- `get(key, max_age_seconds=None)` — return the value, or `None` if absent. If `max_age_seconds` is given and the entry is older, it is invalidated and `None` is returned; on a live hit the access count increments.
- `exists(key)` — membership check.
- `age_seconds(key)` — seconds since the key was set, or `None`.
- `get_access_count(key)` — hit count for a key (0 if absent).
- `invalidate(key)` — drop a key from all three dicts.
- `clear()` — wipe everything.
- `keys()` / `size()` — list keys / count entries.
- `summary()` — log each key with its age and hit count.

### FETCH / CACHE / APPLY

Pure **CACHE** (volatile/in-memory tier). No HTTP, no external API, no config keys, no disk paths. It is the fastest, shortest-lived layer in the cache stack — gone when the process exits.

- **dry_run:** not applicable.
- **Concurrency:** plain `dict` operations with no locks — *not thread-safe*. Safe for the single-threaded run flow; concurrent writers would need external synchronization.

## How it functions

No lifecycle beyond `__init__` (logger + three empty dicts). The only real logic is the lazy-expiry inside `get`: there is no background reaper — an entry is only evicted when someone reads it past its `max_age_seconds`. `set` always resets the access count, so the counter measures hits since the most recent write, not lifetime hits. It delegates no decision to any `machine_learning` brain module.

## Criteria & examples

- **TTL eviction on read.** `set("trakt_token", tok)`; 400 s later `get("trakt_token", max_age_seconds=300)` finds the entry is 400 s old (> 300), invalidates it, and returns `None` — forcing the caller to refetch. A `get("trakt_token", max_age_seconds=600)` at the same moment would return the value (400 ≤ 600) and bump its hit count.
- **No TTL = forever.** `get("trakt_token")` with no `max_age_seconds` always returns the stored value regardless of age (until `invalidate`/`clear`).

## In plain English

This is the countertop right next to the cook, not the fridge. While preparing one meal, things used over and over — the salt, a mixing bowl — stay on the counter for instant reach instead of going back to the cupboard each time. Some items have a "use within X minutes" rule and get tossed the next time you reach for them if they've sat too long. When the cooking session ends (the program closes), the counter is cleared completely — only the fridge (disk cache) keeps things for next time.

## Interactions

- **Parent:** `GlobalCacheManager` (`self.memory`).
- **Collaborator:** `LoggerManager` only.
- **Brain modules:** none.
