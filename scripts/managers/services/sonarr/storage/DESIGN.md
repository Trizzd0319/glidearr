# sonarr/storage — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [sonarr](../README.md) › **storage**

**Manager** — `SonarrStorageManager`
**Status** — ✅ Loaded and critical · ❓ A probable cache-key mismatch in `warm_cache`
**Existing docs** — [`README.md`](./README.md) + a `.md` per module

---

## 1. Position

`storage` is in `SonarrManager.component_dependencies` **and** `critical_keys`, so
it loads and runs. Three components, all critical:

| Component | Size |
|---|---|
| [`library.py`](./library.py) | 9.9 KB |
| [`space.py`](./space.py) | 9.2 KB |
| [`selection.py`](./selection.py) | 4.1 KB |

No tests.

---

## 2. 🔴 CONFIRMED — `warm_cache` writes a key nothing reads

```python
# scripts/support/config/cache_keys.py
SPACE_ESTIMATES = "sonarr/<instance>/storage/space_estimates"
```

The template **does** carry a placeholder. So:

| Call site | Key produced |
|---|---|
| `get_root_folders` — `format_cache_key(SPACE_ESTIMATES, instance=resolved_instance)` | `sonarr/720/storage/space_estimates` |
| `warm_cache` — raw constant | `sonarr/<instance>/storage/space_estimates` — **literally** |

```python
# get_root_folders — FORMATTED with the instance
cache_key = self.key_builder.format_cache_key(
    CacheKeyPaths.sonarr.SPACE_ESTIMATES, instance=resolved_instance
)
return self.global_cache.get_or_generate_cache(key=cache_key, ...)

# warm_cache — the RAW template, unformatted
cache.get_or_generate_cache(
    key=CacheKeyPaths.sonarr.SPACE_ESTIMATES,
    generator_function=lambda: manager.get_root_folders(instance),
    expiration_time=300,
)
```

**The two never meet.** `warm_cache` performs a full `rootfolder` fetch, stores it
under a key containing the literal string `<instance>`, and every subsequent
`get_root_folders` misses and re-fetches. The warm pass is pure cost.

The placeholder style makes it worse rather than better: `<instance>` is not
`{instance}`, so `str.format()` would not substitute it either — `format_cache_key`
must do a custom replace. There is no interpolation error to catch it, and the
resulting cache entry is a visibly malformed key that only shows up if someone
inspects the store.

### 2.1 🎯 This generalises — and is mechanically detectable

**Every** key in `CacheKeyPaths` uses an `<instance>` or `<user>` placeholder.
So *any* caller passing a raw `CacheKeyPaths.X` constant to a cache getter,
without `format_cache_key`, writes or reads a literal-placeholder key.

That is a one-pass grep: `key=CacheKeyPaths` not preceded by `format_cache_key`.

And `radarr` declares the identical constant —
`SPACE_ESTIMATES = "radarr/<instance>/storage/space_estimates"` — so if Radarr's
storage manager has the same `warm_cache` shape, the same bug is there.
`GLD-STO-08`.

### 2.2 A navigational hazard in the same file

The class nesting does not always match the key namespace:

```python
class sonarr:
    WATCHED_SHOWS = "tautulli/<instance>/watched/shows"      # sonarr class → tautulli path
class tautulli:
    TAUTULLI_SYNC_VIEWED = "sonarr/<instance>/sync/tautulli_viewed"  # tautulli class → sonarr path
```

Both directions occur. `CacheKeyPaths.sonarr.WATCHED_SHOWS` writes into the
Tautulli namespace and vice versa. Defensible — the class groups by *consumer*,
the path by *producer* — but nothing says so, and a reader tracing a key by
namespace will look in the wrong class. `GLD-STO-09`.

---

## 3. 🟡 Critical-failure policy differs from its sibling

| Manager | On a **critical** component failing |
|---|---|
| `SonarrStorageManager` | `try/except` → `load_summary[name] = "❌ Failed: {e}"`, registry flag `False`, `all_critical_loaded = False`, **continue** |
| `SonarrEpisodesManager` | **No `try/except`** — *"errors propagate (these are required)"* |

Both are defensible and they are opposites. Storage degrades and reports;
episodes fails loudly and lets `SonarrManager._load_component` record the `❌`.

The practical difference is **where the failure becomes visible**. A storage
critical failure surfaces as `sonarr.storage_manager_initialized = False` plus a
`load_summary` row; an episodes critical failure surfaces one level up as the
whole component failing to load.

Neither is wrong, but a reader cannot predict which they will get, and the two
produce different signals for the same class of event. `GLD-STO-02`.

Storage's version is the better of the two in one respect: it sets a
**per-component** registry flag (`sonarr.storage.{name}_initialized`) on both
branches, so an individual component's health is queryable rather than only the
aggregate.

---

## 4. 🎯 The `parent_name` filter is dead in four of five Sonarr callers

`critical_keys = {"space", "library", "selection"}` — **all three components.** So
`split_components`' non-critical loop, which is where the `parent_name` matching
lives, never executes here.

Collecting every Sonarr caller:

| Caller | `critical_keys` covers… | `parent_name` filter runs? |
|---|---|---|
| `sonarr/sync/` | all | ❌ |
| `sonarr/quality/` | all | ❌ |
| `orchestration/quality.py` | all | ❌ |
| **`sonarr/storage/`** | **all** | ❌ |
| **`sonarr/episodes/`** | **only `retrieval`** | ✅ — **and it drops `sharding`** |

So the filter is exercised in **exactly one** place in Sonarr, and that one place
has a documented bug plus a hand-written workaround
([`episodes/DESIGN.md`](../episodes/DESIGN.md) §3).

That reframes `GLD-SPLIT-02`. The question is not only *"which components does the
filter drop?"* but **"is the non-critical/`parent_name` machinery earning its
keep?"** — four callers route around it entirely by declaring everything critical,
and the fifth had to patch around it by hand. `GLD-STO-04`.

`SonarrStorageManager` also declares `parent_name = "SonarrStorageManager"` and
**does not** overwrite it in `__init__` — a third variant of the convention
[`episodes/DESIGN.md`](../episodes/DESIGN.md) §3.1 traces. Here the class
attribute is already the full name, so the outcome is right by a different route,
which is precisely why the inconsistency is hard to spot.

---

## 5. 🟡 Unreadable disk reads as zero free space

```python
# Mount-deduped free space; clamp inf (no roots/unreadable) → 0.0 to
# preserve selection/min() behavior for misconfigured instances.
_free = self.sonarr_api.disk_free_gb(resolved_instance)
result[resolved_instance] = round(_free if _free != float("inf") else 0.0, 2)
```

`disk_free_gb` returns `inf` when an instance has no root folders or its disks are
unreadable. Clamping to `0.0` makes `min()` and selection avoid that instance —
which is the right *selection* behaviour and the [D36
direction](../../../machine_learning/discovery/DESIGN.md) for acquisition (fail
toward not acquiring).

But it conflates **"this instance is full"** with **"this instance is
unreadable"**, and `get_minimum_free_space()` returns the minimum across
instances. One misconfigured instance drags the reported minimum to `0.00 GB`, and
anything gating on that reads the whole household as out of space.

Sonarr is single-instance today, so this is latent rather than live. Worth
recording because the clamp is *correct for selection* and *wrong for the
aggregate*, and the same value serves both. `GLD-STO-05`.

---

## 6. 🟡 A second `deletion.md` with no `deletion.py`

| Folder | Doc | Module |
|---|---|---|
| `sonarr/episodes/` | `deletion.md` 6.4 KB | ❌ absent |
| **`sonarr/storage/`** | **`deletion.md` 5.8 KB** | ❌ **absent** |

Two folders, same shape. That is no longer plausibly an oversight — either a
`deletion.py` was removed from both in one refactor, or the docs describe deletion
logic that now lives in `space.py` and `file.py` respectively.

Both are 6 KB of design documentation for a module a reader cannot open.
`GLD-STO-03`, and it supersedes `GLD-EPI-02` as a single joint item.

---

## 7. What it gets right

**Per-component registry flags on both branches** — `sonarr.storage.{name}_initialized`
is set `True` or `False` explicitly, so component health is individually
queryable rather than only aggregated.

**`load_summary` carries the exception text** — `f"❌ Failed: {e}"` rather than a
bare marker, so the summary is diagnostic.

**`warm_cache` is a `@staticmethod`** — callable before the manager graph exists,
which is what a warm pass needs.

**Mount-deduped free space** via `disk_free_gb`, consistent with
[`series/space_pressure.py`](../series/VERIFICATION_space_pressure.md) and the
coordinator.

---

## 8. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-STO-01` | 🔴 **CONFIRMED — `warm_cache` writes a key nothing reads.** `SPACE_ESTIMATES = "sonarr/<instance>/storage/space_estimates"`; `warm_cache` passes it **raw**, `get_root_folders` **formats** it. Every warm pass does a full `rootfolder` fetch and stores it under a literal-`<instance>` key; every read misses | S | — |
| `GLD-STO-08` | 🎯 **Grep for every unformatted `CacheKeyPaths` use** — *all* keys carry `<instance>`/`<user>` placeholders, so any `key=CacheKeyPaths.X` without `format_cache_key` is the same bug. Radarr declares an identical `SPACE_ESTIMATES`, so check its `warm_cache` too | S | `GLD-STO-01` |
| `GLD-STO-09` | **Document the `CacheKeyPaths` namespace mismatch** — `sonarr.WATCHED_SHOWS` → `tautulli/…`, `tautulli.TAUTULLI_SYNC_*` → `sonarr/…`. The class groups by consumer, the path by producer; nothing says so | S | — |
| `GLD-STO-02` | 🟡 **Settle the critical-failure policy** — `storage` catches and flags; `episodes` propagates. Same event, two different signals, unpredictable from the call site | S | `GLD-EPI-04` |
| `GLD-STO-03` | 🟡 **Resolve both `deletion.md` files** — `episodes/` and `storage/` each carry ~6 KB documenting a module that does not exist. **Supersedes `GLD-EPI-02`** | S | — |
| `GLD-STO-04` | 🎯 **Decide whether the non-critical/`parent_name` machinery earns its keep** — four of five Sonarr callers declare *everything* critical and route around it; the fifth patched around it by hand. Reframes `GLD-SPLIT-02` from "fix the filter" to "is the filter wanted?" | S | `GLD-SPLIT-02`, `GLD-SPLIT-01` |
| `GLD-STO-05` | 🟡 **Separate "unreadable" from "zero free"** — the `inf → 0.0` clamp is right for selection and wrong for `get_minimum_free_space()`, where one misconfigured instance reports the household as full | S | `GLD-SPA-01` |
| `GLD-STO-06` | **Add tests** — 62 KB, three critical components, zero test files | M | — |
| `GLD-STO-07` | **Document `library.py`, `space.py`, `selection.py`** — each has a `.md`, none a `DESIGN.md` | M | — |

## 9. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | ✅ **ANSWERED** — `SPACE_ESTIMATES = "sonarr/<instance>/storage/space_estimates"`. The placeholder is real; the keys differ; the warm cache is never read | ✅ `GLD-STO-01` confirmed |
| Q2 | Should a critical component failure propagate or degrade? | `GLD-STO-02` |
| Q3 | Where did the two `deletion.py` modules go? | `GLD-STO-03` |
| Q4 | Is the non-critical path used anywhere in the repo with more than a hand-patched result? | `GLD-STO-04` |

**Q1 is a one-line check with a real payoff.** If the template is
instance-scoped, the warm pass is doing work that is thrown away every run — a
`get_or_generate_cache` write plus a full `rootfolder` fetch, cached under a key
no reader ever asks for.

**Q4 is the more interesting one.** A machinery that four of five callers avoid,
and the fifth works around, is a candidate for deletion rather than repair. That
would close `GLD-SPLIT-01` and `GLD-SPLIT-02` together.

## 10. Related designs

- [`episodes/DESIGN.md`](../episodes/DESIGN.md) §3, §3.1 — the one place the filter runs, and why it drops
- [`quality/DESIGN.md`](../quality/DESIGN.md) §3 — `GLD-SPLIT-01`, the double-construction defect in the same helper
- [`sonarr/DESIGN.md`](../DESIGN.md) §12 — `storage` in `component_dependencies` and `critical_keys`
- [`series/VERIFICATION_space_pressure.md`](../series/VERIFICATION_space_pressure.md) — the other Sonarr consumer of mount-deduped free space
