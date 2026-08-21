# factories — Design

> Breadcrumb: [glidearr](../../..) › [scripts](../../README.md) › [managers](../README.md) › **factories**

**Package** — `scripts.managers.factories`
**Status** — ✅ Implemented (all subpackages) · 🔵 Planned ([`web/`](./web/DESIGN.md))
**Related** — [README.md](./README.md) · [`managers/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

A tree of roughly four hundred manager modules, each needing a logger, a config
handle, a cache handle, a validator and a registry, presents a plumbing problem
that gets worse superlinearly:

- **Threading dependencies by hand** means every constructor grows a parameter
  list, and every insertion of a new shared dependency is a repo-wide edit.
- **Module-level globals** solve the plumbing but destroy testability and make
  multi-instance support (two Radarrs, two Sonarrs) impossible.
- **A full DI container** solves both but adds a configuration language, a
  lifecycle, and a debugging surface disproportionate to a single-operator app.

Layered on top: secrets must never reach disk, the external APIs are slow and
rate-limited so the cache is the working set rather than an optimisation, and
first-run setup must complete before config exists at all.

`factories/` is the answer to all of that, and it is deliberately ignorant of
media so it can never become a place where policy accumulates.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One logger, one config, one cache, one registry — process-wide. |
| G2 | Adding a shared dependency is a one-file change. |
| G3 | Secrets resolve from env → keyring → inline, and never land on disk in plaintext once a safe store exists. |
| G4 | The cache serves stale before it serves nothing. |
| G5 | Multi-instance services are first-class, not a special case. |
| G6 | Zero media knowledge in this layer. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Distributed coordination | Single process, single host. |
| N2 | Cache eviction policy | Disk is cheap; the audit tool handles cleanup on demand. |
| N3 | Config schema migration framework | Additive keys with defaults have been sufficient. |
| N4 | Thread-safe everything | Locks at the seams that actually see concurrency (registry mutation, singleton construction), not everywhere. |

---

## 3. Architecture

### 3.1 Component map

```
                    ┌───────────────────────────────┐
                    │        BaseManager            │
                    │  __new__  → singleton by       │
                    │            (cls, singleton_key)│
                    │  __init__ → inject deps        │
                    │           → self-register      │
                    │           → link parent        │
                    │  _resolve_deferred_parent()    │
                    └──┬────────┬────────┬────────┬──┘
                       │        │        │        │
                  ┌────▼──┐ ┌───▼───┐ ┌──▼───┐ ┌──▼──────┐
                  │Logger │ │Config │ │Cache │ │Registry │
                  └───────┘ └───┬───┘ └──┬───┘ └────┬────┘
                                │        │          │
                       ┌────────▼──┐  ┌──▼──────┐  ┌▼──────────┐
                       │SecretStore│  │handlers │  │core/trace/│
                       │SecretBoot │  │json     │  │cli/inject/│
                       │loader     │  │parquet  │  │health/    │
                       │resolver   │  │memory   │  │configsync │
                       │sanitizer  │  │audit    │  └───────────┘
                       │validator  │  │differ   │
                       └───────────┘  │compress │
                                      └─────────┘
   Outside the run path:
      onboarding/   ← first-run wizard, before ConfigManager
      daemons/      ← detached background workers + sentinels
      mixins/       ← ComponentManagerMixin, ordered load, queue cancel
```

### 3.2 Control flow — dependency inheritance

The central mechanism, and the one worth understanding before changing anything
here:

```
manager constructed
  │
  ├─ __new__: key = (cls, kwargs.get("singleton_key"))
  │           already in _instances? → return it   (__init__ still re-runs)
  │
  └─ __init__:
       parent_name = kwargs["parent_name"] or _infer_parent_from_path()
       registry.register("manager", self.name, self)
       parent = registry.get("manager", parent_name)
       if parent:
           inherit logger, config, global_cache, validator
           self.manager = parent.manager or parent
       …
       _resolve_deferred_parent()   ← retry if parent wasn't built yet
```

A child does not receive dependencies from its caller. It **finds its parent by
name and copies the parent's references.** That is what makes G2 true: adding a
shared dependency means editing `BaseManager.__init__` and the inheritance block,
not four hundred call sites.

`_infer_parent_from_path` supplies the name automatically from the module's
filesystem location — a class in `services/sonarr/sync/tags.py` infers
`SonarrSync`. Convenient, and a silent-failure source (§6).

### 3.3 Data contracts

| Contract | Shape | Notes |
|---|---|---|
| Cache key | slash-delimited string | `"radarr/standard/library"` → `support/cache/radarr/standard/library.json` |
| Parquet key | `CacheKeyTemplate` + `EnrichedSuffix` | `_series_enriched`, `_episodes_enriched`, `_movies_enriched`, `_people_enriched` |
| Timestamp key | `CacheKeyTemplate.TIMESTAMP` | `.last_updated` markers |
| Registry entry | `{"instance", "origin", "parent_name"}` | Under `_registry[category][name]` |
| Flag | `bool` under `_registry["flags"]` | `has_flag` → `False` if absent; `get_flag` → `None` if absent |
| Component status | `bool` under `_registry["component_status"]` | Feeds `get_all_failed_components()` |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Registry-mediated dependency inheritance | G1 + G2 without constructor plumbing | Explicit DI; module globals |
| D2 | `__init__` re-runs on singleton reuse | Python calls `__init__` after `__new__` returns; rather than fight it, every `__init__` is written idempotent | Guard flag — masks genuine re-init bugs |
| D3 | Registry `__init__` preserves `_registry` via `getattr(self, "_registry", {})` | Direct consequence of D2: a naive `self._registry = {}` would wipe the directory on every construction | — |
| D4 | Secret precedence env → keyring → inline | Env for containers/CI, keyring for interactive hosts, inline only as a legacy escape hatch | Keyring-only — breaks headless |
| D5 | Blank a secret on disk **only if** `stored or env_present` | Blanking an unstored secret destroys it irrecoverably | Always blank |
| D6 | Cache serves stale unless `regenerate_on_expiry=True` | G4 — a rate-limited Trakt fetch must not empty the working set | Always regenerate on TTL expiry |
| D7 | Generator returning `None` ⇒ failure; `[]` ⇒ valid empty | The single most important cache rule. `None` preserves the last-good copy; `[]` is cached so a genuinely empty API response doesn't re-miss forever | Treat both as empty — causes false prunes |
| D8 | Onboarding before `ConfigManager` | `SecretBootstrap` must see a provisioned keyring or it prompts twice | Lazy prompt mid-run |
| D9 | Atomic config save (`mkstemp` + `os.replace`, `0600`) | A crash mid-write must not corrupt config | In-place write |
| D10 | Resolved secrets registered with `LoggerManager.register_secrets()` | Scrubs them from *all* log output centrally, not per-call-site | Manual redaction |
| D11 | Free-space check fails **open** | A failed `disk_free` probe must not block the run — see [`test_disk_free_failopen.py`](./test_disk_free_failopen.py) | Fail closed |
| D12 | `BaseInstanceManager.prepare()` is a no-op | Instance managers are constructed ready; the base `prepare()` would emit a misleading empty summary | Inherit base `prepare()` |

**D7 deserves emphasis.** It is the same distinction as the Trakt `None` vs `[]`
rule in [`scripts/DESIGN.md`](../../DESIGN.md) §6, and it is the difference
between "the watchlist is unknown, do nothing" and "the watchlist is empty,
prune everything."

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No module under `factories/` references a movie, series, episode, or quality tier. |
| I2 | Exactly one live `RegistryManager`, `GlobalCacheManager`, `ConfigManager` per process. |
| I3 | Every `__init__` is idempotent. |
| I4 | A secret is blanked on disk only when safely stored elsewhere. |
| I5 | `config.json` is written atomically with mode `0600`. |
| I6 | Cache generator `None` never overwrites a good cache entry. |
| I7 | A resolved secret never appears in log output. |
| I8 | The free-space check fails open. |
| I9 | Registry mutations hold `_lock`; singleton construction holds `_class_lock`. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Keyring backend absent | `SecretStore` returns nothing | Falls back to env, then inline; secret stays in `config.json` (I4) | Secret remains on disk |
| Secret missing entirely | `SecretBootstrap.audit` | Reported `missing`; on an interactive TTY, a `getpass` wizard runs | Service unauthorized |
| `SecretBootstrap` raises | try/except in `ConfigManager.__init__` | `log_warning("[SecretBootstrap] skipped: …")`, construction proceeds | Headless-safe |
| Corrupt cache JSON | Handler raises on load | Treated as a miss; regenerated | One key |
| Zero-byte cache file | [`test_json_handler_zero_byte.py`](./cache/test_json_handler_zero_byte.py) | Treated as a miss | One key |
| Cache key collision | [`test_cache_key_collision.py`](./cache/test_cache_key_collision.py) | Sanitised by `CacheKeyBuilder` | — |
| Parquet write fails | Exception | CSV fallback in `CacheParquetManager` | Format only |
| Parent never registers | Deferred retry fails | Manager keeps its **own** logger/config — silently diverges | Subtle, undetected |
| `print_tree_view` called | — | **Method does not exist** in `registry/`; the call in `base_manager.py` is guarded, so it is a latent dead reference | None today |
| `load_summary` unpopulated pre-`prepare()` | — | **False `❌`** for `instance_manager`, `radarr_cache` in the summary line | Misleading output |
| Concurrent `MemoryManager` writes | None | Dict store is not thread-safe | Needs external coordination |

**Rows 8–10 are the live defects.** Row 8 is silent and undetected; rows 9 and 10
are cosmetic but actively mislead the operator reading run output.

---

## 7. Configuration surface

| Key | Type | Default | Effect | Owner |
|---|---|---|---|---|
| `dry_run` | bool | `False` | Master APPLY gate (read by `Main`, propagated) | [`config/`](./config/README.md) |
| `sonarr_instances` | dict | `{}` | Instance map incl. `default_instance` pointer | [`config/config_resolver.py`](./config/config_resolver.py) |
| `radarr_instances` | dict | `{}` | Same for Radarr | [`config/config_resolver.py`](./config/config_resolver.py) |
| `daemons.enrich.enabled` | bool | `False` | Gates sentinel + enrichment-daemon respawn | [`daemons/`](./daemons/README.md) |
| `RECOMMENDARR_*` | env | — | Highest-precedence secret source | [`config/secret_store.py`](./config/secret_store.py) |

Secret leaves are identified **by key name** (`is_secret_key`) anywhere in the
tree, not by position — so a new nested `api_key` is protected automatically.

---

## 8. Implemented capabilities

- ✅ Thread-safe singleton identity keyed `(cls, singleton_key)`
- ✅ Registry-mediated dependency inheritance with deferred parent resolution
- ✅ Path-based parent inference
- ✅ Registry: register/get/set/remove, flags, component health, origin tracing, subtree injection, config hot-swap, PrettyTable dump with stale-checkout anomaly detection
- ✅ Config: atomic `0600` save, env→keyring→inline secret overlay, redacted logging, instance resolution with legacy-format tolerance
- ✅ `SecretBootstrap` audit + interactive first-run wizard, headless-safe
- ✅ Secret scrubbing from all log output
- ✅ Cache: JSON, Parquet (CSV fallback), timestamps, TTL memory cache, audit, delta diff, compression
- ✅ `get_or_generate_cache` with serve-stale and `None`-vs-`[]` semantics
- ✅ Enriched-DataFrame save/load with suffix mapping
- ✅ `deduplicate_entries` newest-wins merge with stats
- ✅ `ComponentManagerMixin` declarative loading + ordered components + queue cancel
- ✅ Daemon supervisor with sentinel-based rate-limit yielding
- ✅ Onboarding wizard: schema, env map, OAuth, per-domain steps, validators
- ✅ Free-space check fails open

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **[`web/`](./web/DESIGN.md) — web interface** for config editing, plan review, run triggering, ledger browsing | Removes the JSON-editing barrier; makes dry-run plans reviewable | L | Read-only ledger access |
| P2 | **Warn on unresolved parent link** after deferred retry | Converts §6 row 8 from silent to visible | S | — |
| P3 | **Populate `load_summary` pre-`prepare()`** | Fixes the `instance_manager❌` / `radarr_cache❌` false negatives | S | — |
| P4 | ~~**Remove or implement `print_tree_view`**~~ — ✅ **DONE 2026-08-10** (`GLD-REG-03`) | Implemented. The dead reference was the smaller half: the call sat inside `BaseManager`'s registration/parent-linking `try`, so enabling `print_registry_tree` would have cost every manager its inherited `dry_run` | S | — |
| P5 | **Thread-safe `MemoryManager`** (`RLock` or `cachetools`) | Removes the last unguarded shared-mutable | S | — |
| P6 | **Config schema validation on load**, using [`config/validator.py`](./config/validator.py) against a declared schema | Catches typo'd keys at startup instead of at first read | M | Schema definition |
| P7 | **Cache size accounting + LRU eviction option** | `support/cache` grows unbounded today | M | [`cache/audit.py`](./cache/audit.py) |
| P8 | **Config hot-reload** — watch `config.json`, re-propagate via `RegistryConfigSync` — ⚠️ **see the note below the table** | Edit settings without restarting; prerequisite for P1's live editing | M | P1 |
| P9 | **Structured event bus** replacing ad-hoc `run_stats` keys | Live run streaming to the web layer | M | P1 |
| P10 | **Secret rotation helper** — re-prompt and re-store one secret without re-running full onboarding | Currently requires the full wizard | S | [`onboarding/`](./onboarding/README.md) |
| P11 | **Cache versioning / schema stamps** so a shape change invalidates rather than mis-parses | Prevents silent bad reads after a refactor | M | — |
| P12 | **Registry snapshot dump to disk** at run end | Post-mortem debugging of the live tree | S | P4 |
| P13 | **`Protocol` for the manager contract** so `prepare`/`run` conformance is statically checkable | Type safety at the seam | M | — |
| P14 | ~~`cache_keys` in the init summary~~ — ✅ **DONE 2026-08-10** (`GLD-MGR-11`) | Read `memory_cache`, an attribute `GlobalCacheManager` has never had (it is `.memory`), with a `{}` default — so the field reported an empty cache on every manager and **could not have reported anything else**. Existed as two byte-identical copies (inline in `__init__` + `_preview_cache_keys`); now one implementation, and a missing `.memory` warns once rather than returning a silent `[]` | S | — |

> ⚠️ **P8 note, 2026-08-10 (`GLD-REG-12`).** `RegistryConfigSync.auto_hot_swap_from_config`
> looks like the seed of P8 and is **not usable as-is**. `BaseManager.__init__` calls it
> with `config.raw_data` — a dict — while the method wants a registered object, so it has
> been a no-op on every manager init; and its own else-branch warning was gated on
> `self.registry.logger`, an attribute `RegistryManager` never defines, so the no-op was
> undetectable. Passing `self` instead would ALSO be a no-op: `BaseManager` registers the
> same object two lines earlier with identical arguments.
>
> **The harder half is not the registry write.** Managers hold DIRECT references to each
> other (`self.manager`, the parent link, everything `inject_dependencies_for_subtree`
> copies down), so replacing a registry entry updates the directory and reaches nobody
> already holding the old object. Hot-reload is a tree-rewiring problem.
>
> **And for the CACHE case specifically it is unnecessary.** `MemoryManager._cache` is bound
> once and mutated in place (`clear()` is `_cache.clear()`, not a rebind), and
> `GlobalCacheManager.memory` is constructed once — so a regenerated cache is **already**
> visible through every handle captured at init. On-demand invalidation is the correct shape
> there and already exists: `GlobalCacheManager.invalidate_cache_key(key)`.

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should an unresolved parent link be fatal? Safer, but risks aborting on benign ordering changes. | P2 |
| Q2 | Should the cache have a global TTL default, or stay per-call as today? | P7 |
| Q3 | Does the web layer read `global_cache` directly, or through a read-only service API? | P1 |
| Q4 | Is `_infer_parent_from_path` worth keeping, or should `parent_name` become mandatory? | P2 |
| Q5 | Should `MemoryManager` be thread-safe by default, or should callers coordinate? | P5 |
| Q6 | Where does the web layer's auth boundary sit — local-bind only, or real sessions? | P1 |
| Q7 | Does anything construct a SECOND `GlobalCacheManager` mid-run? If not, P8's object-swap half is unnecessary outright — in-place mutation covers every cache-reload case (`GLD-REG-12`). One grep settles it | P8 |

## 11. Related designs

- [`config/DESIGN_secrets_backend.md`](./config/DESIGN_secrets_backend.md)
- [`web/DESIGN.md`](./web/DESIGN.md) — planned web interface
- [`base_manager.md`](./base_manager.md) · [`base_instance_manager.md`](./base_instance_manager.md)
- [`mixins/component_manager.md`](./mixins/component_manager.md) · [`mixins/mixins.md`](./mixins/mixins.md)
- [`scripts/DESIGN_performance_audit.md`](../../DESIGN_performance_audit.md)
- [`support/PERF_BASELINE.md`](../../support/PERF_BASELINE.md)
