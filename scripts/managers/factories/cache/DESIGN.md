# cache — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **cache**

**Package** — `scripts.managers.factories.cache`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [`factories/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Glidearr's inputs are slow and rate-limited. A cold Radarr `GET /movie` costs
~39 seconds. Trakt enforces a hard rate limit shared across the main run *and*
the enrichment daemon. Tautulli history for a large library is thousands of rows.

Against that, the brain layer needs the **whole library in memory** to score it,
and it needs that on every run.

So the cache is not a latency optimisation — it is the working set. The design
question is not "should we cache?" but "what do we do when a refresh fails?"

The answer that shapes everything here: **serving stale data is almost always
better than serving none.** A 25-hour-old movie list produces slightly outdated
scores. An *empty* movie list produces a plan to delete nothing and acquire
everything — actively destructive. The cache therefore biases hard toward
last-known-good.

That single principle produces the `None`-vs-`[]` rule (§4 D3), which is the most
important behaviour in this package.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Serve stale before serving nothing. |
| G2 | A failed refresh never destroys good data. |
| G3 | A genuinely empty API response is cached, not re-fetched forever. |
| G4 | One cache object process-wide. |
| G5 | Key → path is a pure, predictable function. |
| G6 | DataFrames persist in a columnar format the brain can re-read cheaply. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Eviction policy | Disk is cheap; [`audit.py`](./audit.py) handles cleanup on demand. |
| N2 | Distributed cache | Single process. |
| N3 | Transactions across keys | Each key is independent. |
| N4 | Thread-safe memory cache | `MemoryManager` is a plain dict. See §6. |

---

## 3. Architecture

### 3.1 Component map

`GlobalCacheManager` is a facade. It deliberately does **not** use
`ComponentManagerMixin` — its subcomponents are plain helpers built by hand in
`__init__`, not registered singletons.

| Attribute | Class | File | Role |
|---|---|---|---|
| `key_builder` | `CacheKeyBuilder` | [`key_builder.py`](./key_builder.py) | Sanitise key parts, resolve on-disk path |
| `json_handler` | `CacheJsonManager` | [`json_handler.py`](./json_handler.py) | JSON read/write/delete |
| `parquet_handler` | `CacheParquetManager` | [`parquet_handler.py`](./parquet_handler.py) | DataFrame save/load, CSV fallback |
| `timestamp_handler` | `CacheTimestampManager` | [`timestamp_handler.py`](./timestamp_handler.py) | `.last_updated` markers |
| `memory` | `MemoryManager` | [`memory.py`](./memory.py) | In-process TTL cache |
| `differ` | `CacheDiffer` | [`differ.py`](./differ.py) | DataFrame delta |
| `audit` | `CacheAuditManager` | [`audit.py`](./audit.py) | Enumerate / wipe |
| `compressor` | `CacheCompressor` | [`compressor.py`](./compressor.py) | (de)compression |

`cache_root` = `key_builder.base_dir` → `<repo>/scripts/support/cache`.

### 3.2 Control flow — `get_or_generate_cache`

The heart of the package.

```
1. expiration_time set AND file exists?
        file_age within TTL → return cached                    ← fast path
        else → expired = True, log

2. file exists AND NOT (expired AND regenerate_on_expiry)?
        → return the on-disk copy                              ← SERVE STALE (G1)
          The generator never runs. This is the default for
          most callers and is deliberate, not an oversight.

3. log miss → generator_function()

4. generator returned None → FAILURE (e.g. Trakt 429)
        prior file exists? → serve last-good, DO NOT overwrite  ← G2
        no prior file?     → return None (legitimate miss signal)

5. generator returned data (INCLUDING [] or {})
        → make_json_safe → write                               ← G3
```

Step 2 is the one that surprises people: **without `regenerate_on_expiry=True`,
an expired key is still served from disk and the generator never runs.** TTL
alone does not trigger a refresh. Callers that genuinely need freshness must opt
in explicitly.

### 3.3 Data contracts

| Kind | Convention | Example |
|---|---|---|
| JSON key | slash-delimited | `"radarr/standard/library"` → `support/cache/radarr/standard/library.json` |
| Parquet key | `CacheKeyTemplate` + `EnrichedSuffix` | `series`→`_series_enriched`, `episodes`→`_episodes_enriched`, `movies`→`_movies_enriched`, `people`→`_people_enriched` |
| Timestamp | `CacheKeyTemplate.TIMESTAMP` | `.last_updated` |
| Delta | `{"added", "removed", "changed"}` DataFrames | from `get_delta_diff` |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Serve stale by default (step 2) | G1. The alternative starves the brain on any upstream hiccup | Always regenerate on expiry |
| D2 | `regenerate_on_expiry` is opt-in | Only a few callers (e.g. the Trakt watched-set) genuinely need freshness; defaulting to refresh would multiply API load | Opt-out |
| D3 | **`None` ⇒ failure, `[]` ⇒ valid empty** | The single most important rule here. `None` preserves last-good (G2); `[]` is cached so a legitimately empty response doesn't re-miss every run (G3) | Treat both as empty |
| D4 | Facade over hand-built helpers, not `load_components` | The helpers are not `BaseManager`s and must not be registry singletons — they belong to this one cache instance | Make them managers |
| D5 | Parquet with CSV fallback | Columnar and typed (G6), but a Parquet engine failure must not lose the data | Parquet only |
| D6 | Empty-collection writes are intentional | Prevents permanent cache-miss on a valid empty result | Skip empty writes |
| D7 | `dry_run` does **not** suppress cache writes | `dry_run` governs *external* mutation. The local cache is not external state, and a dry run must still produce a usable plan | Gate cache on dry_run |
| D8 | No eviction | N1 | LRU |

**D3 restated, because it is the rule most likely to be broken by a well-meaning
refactor:** a generator that returns `[]` on a rate-limit error will cause the
cache to record "this library is empty," and every downstream consumer will act
on that. Generators must return `None` on failure.

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | A `None` generator result never overwrites an existing cache entry. |
| I2 | An `[]` / `{}` generator result **is** written. |
| I3 | Key → path is deterministic and collision-free (sanitised by `CacheKeyBuilder`). |
| I4 | One `GlobalCacheManager` per process. |
| I5 | Cache writes are **not** gated on `dry_run`. |
| I6 | The cache performs no FETCH and no APPLY — only the caller's generator may FETCH. |
| I7 | A zero-byte or corrupt file is treated as a miss, never as empty data. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Generator raises | Propagates | Caller handles; cache untouched | Caller |
| Generator returns `None` | Explicit check | Last-good served; no write (I1) | None |
| Corrupt JSON | Parse error | Treated as miss, regenerated | One key |
| Zero-byte file | [`test_json_handler_zero_byte.py`](./test_json_handler_zero_byte.py) | Treated as miss (I7) | One key |
| Key collision | [`test_cache_key_collision.py`](./test_cache_key_collision.py) | Sanitised by `CacheKeyBuilder` | — |
| Parquet engine missing | Write raises | CSV fallback | Format only |
| Disk full | Write raises | Caller sees the error; free-space check fails **open** elsewhere | One write |
| **Concurrent `MemoryManager` writes** | **None** | Plain dict, not thread-safe. The Radarr prefetch thread and the main thread both touch the cache | Race, unguarded |
| Stale-forever key | None | A key whose caller never sets `regenerate_on_expiry` is served from disk indefinitely | Silently outdated scores |
| Unbounded growth | None | `support/cache` grows without limit | Disk |

**Row 8 is the real one.** [`main.py`](../../../main.py) starts a background
`radarr-movie-prefetch` daemon thread that warms the cache while the main thread
runs Tautulli/Trakt/Sonarr phases. Both touch `GlobalCacheManager`. The on-disk
handlers are effectively serialised by the filesystem, but `MemoryManager`'s dict
is not guarded. §9 P1.

---

## 7. Configuration surface

Reads no config key. It *defines* the key→path convention rather than consuming
configuration.

| Surface | Where | Effect |
|---|---|---|
| `cache_root` | `key_builder.base_dir` | `<repo>/scripts/support/cache` |
| `expiration_time` | per-call arg | TTL in seconds |
| `regenerate_on_expiry` | per-call arg | Opt into actual refresh on expiry |
| `compressed` / `pretty` | per-call arg | Write format |

---

## 8. Implemented capabilities

- ✅ JSON get/set/exists/delete with slash-delimited keys
- ✅ `get_or_generate_cache` with serve-stale, TTL, and `None`-vs-`[]` semantics
- ✅ Parquet enriched-DataFrame save/load with suffix mapping, CSV fallback
- ✅ Timestamp markers (`update_timestamp` / `read_timestamp`)
- ✅ In-process TTL memory cache
- ✅ `get_delta_diff` — added / removed / changed, degrading to "all new" on missing prior
- ✅ `deduplicate_entries` newest-wins merge with `{total, new, updated, skipped}` stats
- ✅ Cache audit (enumerate / wipe)
- ✅ Compression helper
- ✅ Zero-byte and key-collision hardening (tested)

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Thread-safe `MemoryManager`** (`RLock` or `cachetools`) | Closes the live race between the prefetch thread and the main thread (§6 row 8) | S | — |
| P2 | **Cache schema versioning** — stamp a version, invalidate on mismatch | A shape change today is silently mis-parsed rather than detected | M | — |
| P3 | **Size accounting + optional LRU eviction** | `support/cache` grows unbounded | M | [`audit.py`](./audit.py) |
| P4 | **Stale-key report** — list keys served past TTL and how far past | Makes §6 row 9 visible; likely to surface genuinely rotten data | S | — |
| P5 | **Per-key metrics** (hit / miss / stale / regen) | Cache effectiveness is currently unmeasured | S | `metrics.py` |
| P6 | **Atomic JSON writes** (`mkstemp` + `os.replace`), matching the config layer | A crash mid-write currently truncates a key | S | — |
| P7 | **Cache warm command** — pre-populate every key out of band | Faster first run after a wipe | S | — |
| P8 | **Compression by default** for large snapshots | Disk + read time on the biggest keys | S | [`compressor.py`](./compressor.py) |
| P9 | **Cache browser in the web UI** — keys, sizes, ages, contents | Replaces filesystem archaeology | M | [`web/`](../web/DESIGN.md) |
| P10 | **Typed cache accessors** returning a declared shape | Catches consumer/producer drift | M | P2 |
| P11 | **Negative-result caching with a short TTL** | A known-404 currently re-fetches every run | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should `regenerate_on_expiry` default to `True`, with opt-out? Current default is safest but produces silently-stale keys. | P4 |
| Q2 | Should there be a global TTL default, or stay fully per-call? | P3 |
| Q3 | Is CSV a genuinely useful Parquet fallback, or does it lose enough typing to be worse than failing loudly? | — |
| Q4 | Should `MemoryManager` be thread-safe by default, or should callers coordinate? | P1 |

## 11. Related designs

- [`factories/DESIGN.md`](../DESIGN.md) §4 D6/D7 — the same stale/`None` rules stated at layer level
- [`scripts/DESIGN.md`](../../../DESIGN.md) §6 — the Trakt `None`-vs-`[]` failure mode
- [`scripts/DESIGN_performance_audit.md`](../../../DESIGN_performance_audit.md)
- [`support/PERF_BASELINE.md`](../../../support/PERF_BASELINE.md)
