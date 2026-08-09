# radarr/storage — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [radarr](../README.md) › **storage**

**Manager** — `RadarrStorageManager`
**Status** — ✅ Loaded and critical · 🎯 Holds the correct version of a bug Sonarr has
**Existing docs** — [`README.md`](./README.md) + a `.md` per module

> **Coverage:** `__init__.py` read in full. `deletion.py` (11.2 KB),
> `cross_instance_move.py` (18.4 KB), `cross_instance_dedup_apply.py`,
> `shared_storage.py`, `space.py`, `selection.py`, `library.py`, `relocation.py`
> are **unread**.

---

## 1. Shape — richer than its Sonarr twin

| Component | Radarr | Sonarr |
|---|---|---|
| `library` · `selector`/`selection` · `space` | ✅ | ✅ |
| **`deletion`** | ✅ 11.2 KB | ❌ |
| **`relocation`** | ✅ 2.5 KB | ❌ |

Plus three non-component modules Sonarr has no equivalent of:
[`cross_instance_move.py`](./cross_instance_move.py) (18.4 KB, **15.8 KB of
tests**), [`cross_instance_dedup_apply.py`](./cross_instance_dedup_apply.py)
(8.8 KB, 6.8 KB tests), [`shared_storage.py`](./shared_storage.py) (5.0 KB,
3.2 KB tests).

---

## 2. 🎯 `GLD-RQ-08` answered — Radarr's `warm_cache` is **correct**

```python
# radarr/storage/__init__.py
key = CacheKeyPaths.radarr.SPACE_ESTIMATES.replace("<instance>", instance or "default")
cache.get_or_generate_cache(key=key, generator_function=…, expiration_time=300)
```

```python
# sonarr/storage/__init__.py — the SAME method
cache.get_or_generate_cache(
    key=CacheKeyPaths.sonarr.SPACE_ESTIMATES,      # ← raw template, never substituted
    …
)
```

**Radarr substitutes the placeholder; Sonarr does not.** Same method name, same
purpose, same `expiration_time=300`, same generator shape — and one of them writes
a key its reader will find.

So `GLD-STO-01` is **not** a systemic pattern that happens to appear in Sonarr. It
is a **divergence between twins**, and Radarr is the reference implementation.
The Sonarr fix is one line, and it is written out two folders away.

It also narrows `GLD-STO-08`: the "grep every unformatted `CacheKeyPaths` use"
sweep is still worth doing, but this particular pair is now A/B'd rather than
suspected.

### 2.1 🟡 Though Radarr uses two substitution mechanisms in one class

```python
# get_root_folders — the builder
cache_key = self.key_builder.format_cache_key(CacheKeyPaths.radarr.SPACE_ESTIMATES,
                                              instance=resolved_instance)

# warm_cache — a hand-rolled replace
key = CacheKeyPaths.radarr.SPACE_ESTIMATES.replace("<instance>", instance or "default")
```

Both produce the right key here, so nothing is broken. But two mechanisms for one
operation, forty lines apart, is exactly the condition under which a copy picks up
the wrong one — which is a plausible account of how Sonarr's ended up with
neither. `GLD-RS-01`.

Radarr's version also carries an `or "default"` fallback that Sonarr's lacks.

---

## 3. ✅ The two orphan `deletion.md` files, explained

[`storage/DESIGN.md`](../../sonarr/storage/DESIGN.md) §6 recorded two Sonarr
folders carrying a `deletion.md` with no `deletion.py`, and asked (Q3) where the
modules went.

**They did not go anywhere — Radarr has `deletion.py` and Sonarr never did.**

Radarr's storage carries a real 11.2 KB `RadarrStorageDeletionManager` in
`critical_keys`. Sonarr's storage has three components and no deletion module; its
`deletion.md` (and `episodes/deletion.md`) are almost certainly **scaffolding
copied from Radarr's folder shape** when the Sonarr tree was laid out.

So `GLD-STO-03` is not *"find the deleted modules"* — it is *"delete two `.md`
files documenting a module this service does not have,"* or write the module. The
first is a minute's work. `GLD-RS-02`.

That also reframes the Sonarr/Radarr asymmetry more broadly: Radarr's storage owns
deletion, relocation and cross-instance movement; Sonarr's owns none of them,
because Sonarr deletion lives in `cache/episode_files.py` instead.

---

## 4. ✅ `GLD-RAD-01` is substantially stale — the migration half is **built**

Session 56 flagged this on file sizes alone. Reading
[`cross_instance_move.py`](./cross_instance_move.py)'s header settles it.

`CrossInstanceMove` is the **dual-version actuator** for `4k_policy=='both'`
(2160p on the 4K instance + a ≤1080p baseline on standard), with four operations:

| Operation | What it does |
|---|---|
| **`relocate`** | Shared storage: import the standard instance's **existing** 2160p into the 4K instance with `importMode=copy` — hardlinked when `copyUsingHardlinks` is on. **No re-download.** |
| **`acquire`** | No shared storage: clone the record onto the 4K instance, monitored, **search on**. Source untouched; tags carried across by label |
| **`retune_baseline`** | Retune the standard side **down** to ≤1080p **in place** via `movie/editor` — Radarr id, grab history and added-date preserved |
| **`ensure_acquiring`** | Drives an existing-but-fileless 4K record (profile + monitor + search) |

That is precisely the *"add-if-absent + migration"* work `GLD-RAD-01` records as
remaining. **It exists, with 15.8 KB of tests.**

### 4.1 What actually remains is a Q9 question

The docstring is explicit that this is an actuator, not a decider:

> **PURE orchestration over an `ArrGateway`** — no config, no registry; **the
> caller** supplies the instances, the 4K root + 2160p profile, and the standard
> ≤1080p baseline profile.

and the mode is *"chosen by the caller from a shared-storage probe"*, with
*"the caller owns eligibility (proactive_4k gate + watchability…)"*.

So `GLD-RAD-01` re-scopes from **"build the migration"** (L) to **"find or wire
the caller"** (S/M) — the same shape as `GLD-REP-07`, and the same checklist Q9
applies. `GLD-RS-03`.

### 4.2 🎯 Make-before-break, stated twice

> Uses **copy, NOT move**, so the SOURCE file is untouched — **make-before-break**
> — until the standard record later retunes to 1080p and drops its 2160p.

> The 2160p **stays on disk until the 1080p lands**, so the title is **never
> file-less**.

Both halves of the dual-version transition are ordered so a title always has a
playable file. That is the same instinct as
[`sonarr/series/space_pressure.py`](../../sonarr/series/VERIFICATION_space_pressure.md)
§4's *"a title is never traded for an empty indexer result"* — the transition is
shaped around the failure case rather than the happy path.

### 4.3 ✅ A removed implementation, and why

> The earlier `importMode=Move` relocation was **removed because a Move dropped
> the source immediately and jammed under load**; this copy/hardlink variant is
> safe + only imports once the 4K instance **proves it can read the file**.

A shipped failure recorded at the site of its fix, with the replacement's
rationale attached — the same discipline as the phantom-reclaim note
(`GLD-SP-04`) and the `dry_run` footgun comment.

### 4.4 The probe refuses to assume shared storage

> A per-title `manualimport` probe first confirms the 4K instance can actually
> **SEE** the source folder; if it can't (→ not really shared), `relocate` returns
> **`not-visible`** and the caller falls back to `acquire`.

Rather than trusting a config flag or a path comparison, it **asks the instance**.
And the failure is a named return value the caller branches on, not an exception
or a silent no-op — confirmed-vs-assumed, applied to filesystem topology.

### 4.5 🎯 `dry_run` defaults to **`True`**

```python
def __init__(self, gw, logger=None, *, dry_run: bool = True):
```

**The only safe-by-default constructor found in the sweep.** Every other manager
examined defaults to `False` (live) or refuses to construct without an explicit
value.

That matters beyond this file. [`writeback/DESIGN.md`](../../writeback/DESIGN.md)
§10 Q4 asks *"should `dry_run` default to `True` rather than `False`?"* for a
push-only sync with no undo. **There is now in-repo precedent**, in the module
that moves 60 GB files between instances. `GLD-RS-07`.

---

## 5. Structural notes

**`critical_keys` covers all five components** — so Radarr's storage, like
Sonarr's, routes entirely around the `parent_name` filter. That is now a **sixth**
all-critical caller and the tally spans both services:

| | `critical_keys` | Filter runs? |
|---|---|---|
| Sonarr `sync` · `quality` · `orchestration.quality` · `storage` · `monitoring` | all | ❌ |
| **Radarr `storage`** | **all** | ❌ |
| Sonarr `repair` · `episodes` · `validator` | partial | ✅ (two with known problems) |

`GLD-STO-04`'s case strengthens: six callers avoid the machinery, three use it, two
of those three malfunction.

**Per-component registry flags on both branches** — `radarr.storage.{name}_initialized`,
matching Sonarr's storage and repair. Consistent across services.

**`self.parent_name = __class__.__name__`** rather than `self.__class__.__name__`.
Identical here (no subclassing), but the two differ under inheritance — the
closure form binds the *defining* class. A seventh minor `parent_name` variation
for `GLD-REP-02`'s tally.

**The `inf → 0.0` clamp** appears verbatim, comment and all, in both services'
storage managers — so `GLD-STO-05`'s "unreadable reads as zero free" applies to
Radarr identically. And Radarr **is** multi-instance, so the latent
`get_minimum_free_space()` hazard is live here in a way it is not on the
single-instance Sonarr side.

---

## 6. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-RS-01` | 🟡 **Two key-substitution mechanisms in one class** — `format_cache_key` in `get_root_folders`, a hand-rolled `.replace("<instance>", …)` in `warm_cache`. Both correct here, but it is the condition that plausibly produced Sonarr's `GLD-STO-01` | S | `GLD-STO-01`, `GLD-STO-08` |
| `GLD-RS-02` | ✅ **`GLD-STO-03` resolved in principle** — Radarr has `deletion.py`; Sonarr never did. The two orphan `deletion.md` files are **scaffolding copied from Radarr's folder shape**. Delete them, or write the module | S | `GLD-STO-03` |
| `GLD-RS-03` | ✅ **`GLD-RAD-01`'s migration half is BUILT** — `CrossInstanceMove` implements `relocate` (shared-storage hardlink import), `acquire` (clone + search on the 4K instance), `retune_baseline` (in-place `movie/editor` downshift preserving id/history/added-date) and `ensure_acquiring`. **Re-scope `GLD-RAD-01` from L "build the migration" to S/M "find or wire the caller"** — the module is a pure actuator; *"the caller owns eligibility"* | S | `GLD-RAD-01` |
| `GLD-RS-07` | 🎯 **`CrossInstanceMove.__init__` defaults `dry_run: bool = True`** — the **only safe-by-default constructor** found in the sweep. Answers `writeback/DESIGN.md` §10 Q4 with in-repo precedent, from the module that moves 60 GB files between instances | S | `GLD-WB-03`, `GLD-SP-01` |
| `GLD-RS-04` | **`GLD-STO-05` is live here, not latent** — the `inf → 0.0` clamp is identical, and Radarr **is** multi-instance, so one misconfigured instance drags `get_minimum_free_space()` to zero for real | S | `GLD-STO-05` |
| `GLD-RS-05` | **Read `deletion.py`** — 11.2 KB, `critical`, and the Radarr counterpart to a path Sonarr keeps in `cache/episode_files.py` | M | `GLD-BKP-07` |
| `GLD-RS-06` | **Add tests for the five components** — `cross_instance_*` and `shared_storage` are well covered; `deletion`, `library`, `selection`, `space`, `relocation` have none | M | — |

## 7. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | ✅ **ANSWERED** — Radarr's `warm_cache` substitutes correctly. `GLD-STO-01` is a Sonarr-only divergence | ✅ `GLD-RQ-08` |
| Q2 | ✅ **ANSWERED session 57** — yes. `CrossInstanceMove` implements relocate / acquire / retune_baseline / ensure_acquiring. What remains is **the caller**, not the mechanism | ✅ `GLD-RS-03` |
| Q3 | Does `deletion.py` read the backup gate? | `GLD-RS-05`, `GLD-BKP-07` |

## 8. Related designs

- [`sonarr/storage/DESIGN.md`](../../sonarr/storage/DESIGN.md) §2, §6 — the twin, and the bug this folder gets right
- [`radarr/DESIGN.md`](../DESIGN.md) §3.3 — `GLD-RAD-01`'s multi-instance routing
- [`backup/DESIGN.md`](../../backup/DESIGN.md) §9 — `GLD-BKP-07`, which `deletion.py` bears on
- [`machine_learning/space/DESIGN.md`](../../../machine_learning/space/DESIGN.md) — the `(T, U)` band these managers report free space against
