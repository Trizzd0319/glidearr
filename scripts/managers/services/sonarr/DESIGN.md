# sonarr — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **sonarr**

**Package** — `scripts.managers.services.sonarr`
**Status** — ✅ Implemented · 🔴 One subsystem present but never loaded
**Related** — [README.md](./README.md) · [`radarr/DESIGN.md`](../radarr/DESIGN.md) · [`services/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Sonarr manages the TV half of the library. It is the **largest** subsystem in
Glidearr, and the reason is granularity: Radarr's atom is a movie; Sonarr's is an
**episode**. A library of 800 series is tens of thousands of episodes, each with
its own file, quality, monitor flag and watch state.

That produces five problems Radarr does not have:

1. **Quality is per-episode, not per-series.** A pilot may be worth 720p while a
   mid-season episode of an actively-watched show warrants 1080p. Sonarr is
   deliberately **single-instance** — there is no cross-instance tier routing —
   so quality is governed per-episode by JIT (per-episode profile / resolution
   markers) rather than by routing shows across tiered instances.

2. **Volume breaks naive approaches.** Episode-file operations across tens of
   thousands of rows need sharding, memoisation and incremental fingerprints.
   A 9k-stub interactive-search spree once hung the entire run because it ran on
   a non-daemon worker thread that blocked interpreter exit.

3. **Partial ownership is the norm.** A series is rarely all-or-nothing — some
   seasons owned, some monitored, some ignored. "Watched" for a series is
   genuinely ambiguous in a way it is not for a movie.

4. **Pilots are a special case.** Acquiring a pilot at low quality is a cheap
   probe: if the household engages, climb; if not, delete. This produces its own
   subsystem (pilot stepping, interactive search, 720 upgrade, off-720 reporting).

5. **Sagas span series.** Franchise/universe membership crosses show boundaries,
   and retention decisions must respect the arc rather than the episode.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Folder shape mirrors Radarr so either is navigable from the other. |
| G2 | Per-episode quality decisions at library scale. |
| G3 | Large search batches never block the run. |
| G4 | Drift reconciled every run. |
| G5 | Every APPLY `dry_run`-gated — including episode-file operations. |
| G6 | Saga/universe membership respected across series boundaries. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Cross-instance tier routing | Sonarr is single-instance by design. JIT handles quality per-episode. |
| N2 | Owning indexers | Sonarr does that. |
| N3 | Scoring | Brain layer. |
| N4 | Per-episode metadata authority | TVDB/Trakt own it. |

---

## 3. Architecture

### 3.1 Subsystem map

| Subfolder | Verb | Responsibility |
|---|---|---|
| [`api/`](./api/) | FETCH | HTTP client + auth |
| [`cache/`](./cache/README.md) | CACHE | Series, episodes, episode files, history, tags, quality, monitoring, JIT search, pilot jobs, owned-episode fingerprints |
| [`instance/`](./instance/README.md) | — | Instance resolution, updater |
| [`series/`](./series/README.md) | FETCH/CACHE | Retrieval, enrichment, sync, helpers, quality, monitoring, space pressure |
| [`episodes/`](./episodes/README.md) | FETCH/CACHE | Retrieval, files, history, monitoring, sharding |
| [`quality/`](./quality/README.md) | APPLY | Profile selection, custom formats, file sizes, adjustment — **⚠️ see §3.3** |
| [`monitoring/`](./monitoring/README.md) | APPLY | Rules, scheduler, priority queue, audit, backfill, space thresholds |
| [`storage/`](./storage/README.md) | **APPLY** | Space, selection, deletion, library |
| [`repair/`](./repair/README.md) | APPLY | Anomalies, orphans, metadata, tags, filepaths, series, episodes, instance config/credentials/reachability |
| [`sync/`](./sync/README.md) | APPLY | Custom formats, naming, folders, media, tags |
| [`validator/`](./validator/README.md) | — | Auth, health, keys, cache, factory |
| [`orchestration/`](./orchestration/README.md) | — | Intra-service sequencing across every subsystem |

### 3.2 Control flow

```
SonarrManager.__init__
    self.cache = global_cache            (BaseManager expects `cache`)
    dry_run from kwargs (default False)
    CacheKeyBuilder() → self.key_builder
    EAGER: instance_manager (SonarrInstanceManager — the sonarr_api reference)
    EAGER: sonarr_cache (SonarrCacheManager) — NOT a loadable component
           → initialize_cache_structure(include_optionals=True)
    split_components(...) → critical / non-critical

prepare()
    mark eagerly-built components as loaded   ← so they don't render ❌
    lazily load anything still missing via _load_component
    call .prepare() on each component that has one
    a prepare() exception flips that component to ❌ and is LOGGED (no longer swallowed)
    emit colour-coded "N/M components prepared"

run()
    iterate component_dependencies IN DECLARED ORDER
    ensure loaded (lazy _load_component fallback)
    call .run() on each that has one
    record ✅/❌ per component
    log_filtered_component_summary(service_name="Sonarr", ...)
```

`_load_component` is idempotent and dependency-aware: returns an already-set
attribute, else checks the registry `"manager"` category for an existing
singleton, else looks the class up in `critical_components`/`noncritical_components`,
**recursively loads declared dependencies first**, then builds via
`self._singleton(name, cls, **self.init_args)`.

Note that `prepare()` explicitly marks the eagerly-built components as loaded so
they don't render `❌` — this is the local workaround for the `load_summary`
absent-vs-failed conflation (`GLD-MIX-01`, §8 P-C). Radarr and other managers do
not do this, which is why they still show the false negatives.

### 3.3 🔴 Two subsystems are present but never loaded

`self.component_dependencies` declares the **active subset**, and
`all_component_classes` is `full_components` filtered by its keys:

```python
enabled_keys = set(self.component_dependencies.keys())
self.all_component_classes = {k: v for k, v in full_components.items()
                              if k in enabled_keys}
```

| | Keys |
|---|---|
| `full_components` (9) | validator_manager, series, episodes, monitoring, **quality**, storage, **sync**, repair, orchestration |
| `component_dependencies` (8) | instance_manager, storage, series, episodes, monitoring, repair, validator_manager, orchestration |
| **Filtered out** | **`quality`** — `SonarrQualityManager` · **`sync`** — `SonarrSyncManager` |

Both are imported at module top, declared as typed class attributes, and dropped
from the loadable set. Neither is loaded, prepared, or run.

**The README documents only `quality`.** `sync` is an undocumented omission, and
it is the more consequential of the two:
[`sync/`](./sync/README.md) is what pushes custom formats, naming, folders, media
management and tags **into** Sonarr. Radarr's equivalent
[`sync/`](../radarr/sync/README.md) *does* run. So Sonarr-side config sync either
is not happening, or is happening somewhere else — `orchestration` is loaded and
may invoke it. **This needs confirming before it is treated as broken.**

Both subfolders are fully built and documented:
[`quality/selector.py`](./quality/selector.py),
[`quality/custom_formats.py`](./quality/custom_formats.py),
[`quality/filesizes.py`](./quality/filesizes.py),
[`quality/adjustment.py`](./quality/adjustment.py);
[`sync/custom_formats.py`](./sync/custom_formats.py),
[`sync/naming.py`](./sync/naming.py),
[`sync/folders.py`](./sync/folders.py),
[`sync/media.py`](./sync/media.py),
[`sync/tags.py`](./sync/tags.py) — each with a `.md`.

This is §8 **P-A** in its most expensive form: not a cache key nobody reads, but
two entire subsystems written, documented, then disconnected. Two readings, and
the code cannot distinguish them:

- **Superseded** — JIT per-episode quality ([`cache/jit_search.py`](./cache/jit_search.py),
  [`orchestration/quality.py`](./orchestration/quality.py)) took over for `quality`,
  and `sync` moved into `orchestration`. Dead code awaiting deletion.
- **Regressed** — they fell out of `component_dependencies` at some point and the
  work is silently not happening.

#### The `critical_keys` contradiction

```python
self.critical_keys = {
    "instance_manager", "series", "episodes",
    "quality",                       # ← filtered out of all_component_classes
    "storage", "monitoring", "repair",
    "validator_manager", "orchestration",
}
```

`"quality"` is declared **critical** while being absent from the set
`split_components` can actually load. `"sync"` is correctly absent from
`critical_keys`, which makes `quality` look like an oversight rather than a
deliberate removal — a deliberate removal would have dropped it from both.

This is also a clean §8 **P-B** instance: `critical_keys` reads as a guarantee
that quality loads. It is not one.

### 3.4 The pilot subsystem

A distinctive piece with no Radarr analogue:

```
pilot acquired at low quality (cheap probe)
    │
    ├─ household engages → pilot_720_upgrade → climb the ladder
    └─ no engagement     → delete / off-720 report

batch > PILOT_SPILL_THRESHOLD (10)?
    → JSON job file → PilotSearchDaemonSupervisor.ensure_running()
    → daemon drains it, 3 interactive workers (deliberately < 6 JIT workers)
    else → in-process
```

The spill exists because the in-process path ran on a **non-daemon** thread that
blocked interpreter exit — a 9k-stub spree hung the whole run (G3).
`PILOT_SEARCH_BATCH = 100` episodeIds per Sonarr command turns 9k grab-triggers
into ~91 commands, with per-series profiles still set individually first so each
grab honours its own tier.

### 3.5 Data contracts

| Artifact | Produced by | Consumed by |
|---|---|---|
| `_series_enriched` / `_episodes_enriched` Parquet | [`series/retrieval/`](./series/retrieval/README.md), [`episodes/retrieval/`](./episodes/retrieval/README.md) | brain features |
| `owned_episodes` + fingerprints | [`cache/owned_episodes.py`](./cache/owned_episodes.py) | playlist readiness, people matrix (show half) |
| Pilot job files | [`cache/pilot_interactive.py`](./cache/pilot_interactive.py) | pilot-search daemon |
| `sonarr/run_stats` | this manager | `Main`, Discord |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Folder shape mirrors Radarr | G1 | Shape to the API |
| D2 | **Single-instance, JIT per-episode quality** | Episodes vary in worth within one series; tiered instances can't express that | Cross-instance tier routing |
| D3 | `dry_run` propagated explicitly into `SonarrCacheManager` | **Load-bearing.** Without it, episode-file ops (acquisition, sync, JIT) ran **LIVE even in dry_run sessions.** Noted inline in the source | Rely on ambient state |
| D4 | Eager `instance_manager` + `sonarr_cache`, everything else lazy | These two are needed by every other component; the rest are conditional | All eager, or all lazy |
| D5 | `prepare()` pre-marks eager components as loaded | Local fix for absent-vs-failed in `load_summary` | Live with false `❌` |
| D6 | `prepare()` exceptions logged, not swallowed | A silent prepare failure previously hid real breakage | Swallow |
| D7 | Dependency-aware recursive `_load_component` | Declared order plus recursion avoids a hand-maintained topological list | Manual ordering |
| D8 | Pilot batches > 10 spill to a daemon | G3 — the non-daemon thread blocked interpreter exit | Always in-process |
| D9 | 3 interactive workers vs 6 JIT workers | Interactive search hits the indexer synchronously; 6-at-once makes a single indexer time out and return false `no_results` | Match JIT |
| D10 | Episode sharding | Tens of thousands of rows won't fit a naive single pass | Single pass |
| D11 | Saga retention crosses series boundaries | G6 — franchise arcs don't respect show boundaries | Per-series retention |

**D3 is the strongest existing evidence for the tabled central `dry_run` gate
(`GLD-ORCH-01`).** A real bug shipped in which a manager didn't receive `dry_run`
and applied for real during a rehearsal. That is exactly the failure a
distributed invariant produces, and it has already happened once here.

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Every APPLY checks `dry_run` — including episode-file operations. |
| I2 | HD-720p is the floor. |
| I3 | `score is None` ⇒ safe mid-tier, never top. |
| I4 | `keep-universe` never deleted; bare `universe` last-resort only. |
| I5 | Cursors persist after each pool operation, in `finally`. |
| I6 | No scoring logic in this service. |
| I7 | A second consecutive run is a no-op. |
| I8 | Sonarr failure never aborts the run — it is deliberately **not** in the critical-flag set. |
| I9 | Pilot search never runs on a thread that blocks interpreter exit. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| Sonarr unreachable | `validate_all` | TV phases skipped; **run continues** (I8) | TV | ✅ Auth line |
| A component's `prepare()` raises | Caught + logged | That component `❌`, others continue | One capability | ✅ Summary line |
| `SonarrQualityManager` / `SonarrSyncManager` filtered out | **None** | Never loaded, prepared or run | 🔴 Unknown — dead or regressed | ❌ **None** |
| Pilot daemon not running | `ensure_running()` on enqueue | Spawned | Handled | ✅ |
| Pilot job orphaned in `processing/` | Daemon-start scan | Re-queued | None | 🟡 Debug |
| Indexer times out under load | False `no_results` | Worker count capped at 3 (D9) | Mitigated | ❌ **None** — reads as a genuine miss |
| Episode file moved outside Glidearr | [`repair/filepaths.py`](./repair/filepaths.py) | Reconciled | Handled | ✅ |
| Partial APPLY batch | **None** | Ledger may not match actual state | 🟡 Drift until next repair | ❌ **None** |
| `dry_run` not propagated to a submanager | **None** | 🔴 Real APPLY during a rehearsal | 🔴 Destructive | ❌ **None** — has happened (D3) |
| Owned-episode fingerprint stale | mtime compare | Re-read only what moved | Handled | ✅ |

**Applying §8 P-D** — five rows have no operator signal. Row 6 is subtle and
worth naming: an indexer timing out under load returns *false* `no_results`,
which is indistinguishable from the release genuinely not existing. That is §8
**P-C** (absent conflated with empty) in a new location.

---

## 7. Configuration surface

| Key | Type | Default | Effect |
|---|---|---|---|
| `sonarr_instances` | dict | `{}` | Instance map + `default_instance` |
| `pilot_interactive.search_workers` | int | `3` | Overrides `PILOT_INTERACTIVE_WORKERS` |
| `dry_run` | bool | `False` | APPLY gate — propagated explicitly (D3) |

Constants in [`daemon_paths.py`](../../factories/daemons/daemon_paths.py):
`PILOT_SPILL_THRESHOLD` 10 · `PILOT_SEARCH_WORKERS` 6 ·
`PILOT_INTERACTIVE_WORKERS` 3 · `PILOT_SEARCH_BATCH` 100 · `PILOT_IDLE_EXIT_S` 1800.

Identity: Sonarr instance `720`.

---

## 8. Implemented capabilities

- ✅ Full series + episode sync with sharding for scale
- ✅ Enriched series/episode Parquet, TVDB bridging, validation
- ✅ Owned-episode set with `mtime_ns` incremental fingerprints
- ✅ JIT per-episode quality upgrades with step-down search
- ✅ Pilot subsystem: interactive search, 720 upgrade, climb/stepping, off-720 reporting
- ✅ Daemon spill for large search batches with orphan recovery
- ✅ Legacy-codec regrab
- ✅ Monitoring: rules, scheduler, priority queue, audit, backfill, space thresholds
- ✅ Space-pressure-aware deferral and realization
- ✅ Storage: space, selection, deletion, library
- ✅ Repair across anomalies, orphans, metadata, tags, filepaths, series, episodes, instance config/credentials/reachability
- ✅ Sync: custom formats, naming, folders, media, tags
- ✅ Auth/health/key/cache validators with factory
- ✅ Saga retention and viewer retention wiring
- ✅ Dependency-aware lazy component loading
- ✅ `dry_run` propagated into episode-file operations (D3)

## 9. Planned additions

> **Verified against source, session 34.** `GLD-SON-01` and `GLD-SON-11` are
> **confirmed** with the exact mechanism below; `GLD-MIX-01` is **already fixed
> here**. Evidence in §12.

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SON-20` | 🔴→✅ **FIXED 2026-08-10.** | `custom_formats`, `media_management` and `tags` each set `self.dry_run` in `__init__` and no method consults it. `media.sync_media_management_settings` issues an unconditional `PUT config/mediamanagement`; `custom_formats.sync_all_custom_formats` and `tags.sync_tags_across_instances` are the same shape. Only `naming.sync_naming_settings` (line 65) and `folders.initialize_root_folders` (line 82) actually gate. **A stored-but-unread `dry_run` is worse than none** — it makes the class LOOK disarmable, and any caller reasonably assumes a disarmed pass is safe. This is the reason `SonarrSyncManager.run` currently drives nothing, and the reason Radarr refuses the same three. Fix: gate the writes, then Sonarr's sync can be swept like Radarr's *(P-B — guard narrower than it appears)* | M | — |
| `GLD-SON-21` | 🟡 **Sonarr has no `profile_scores` equivalent** | Radarr's sync earns its `run()` by having one safe, gated, additive path (`cap_profiles_to_tier` → `sync_definitions` → `sync_uhd_profiles` → `apply_score_sync`). Sonarr has no counterpart, so even with `SON-20` fixed there is nothing for its `run()` to sweep. This is the actual remaining asymmetry between the two services | M | `GLD-SON-20` |
| `GLD-SON-17` | 🔴→✅ **FIXED 2026-08-10 via `GLD-SON-18`+`19`.** Measured 2026-08-10: Sonarr `standard` reported **302 files wildly out of size profile (192 oversized, ~371 GB reclaimable)** and remediated `116 rescanned, 0 re-grabbed, 38 skipped (unmonitored), 2 skipped (guarded)`. Radarr the same shape. A rescan corrects the RECORDED size when the cache was merely stale; it does nothing for a genuinely bloated file, so ~371 GB sat identified and untouched run after run. Root cause was the delete-first re-grab (`SON-18`) forcing an orphan guard that blocked everything; with search-in-place the same pass planned **39 re-grabs + 118 rescans**, the Dragon Ball Z files included. **Correction to the original diagnosis:** the `0 rescanned / 0 searched` summary on a DRY run is not a defect — both counters increment only on the armed path, which I misread once before checking | Root cause was the delete-first re-grab (`SON-18`) forcing an orphan guard that blocked every candidate, so "cannot fix this one" was indistinguishable from "nothing to fix" *(P-D)* | M | — |
| `GLD-SON-18` | 🔴→✅ **FIXED 2026-08-10.** | The armed path is `DELETE episodefile/{fid}` **then** `POST command EpisodeSearch`. Delete-first is what forces the orphan guard to exist: Sonarr will not search for an **unmonitored** episode, so the command is accepted and does nothing, leaving the file deleted and never replaced. Firing `EpisodeSearch` with the file STILL IN PLACE removes that window entirely — Sonarr replaces on import only if it finds something matching the profile, and if it finds nothing the household keeps what it had. This is strictly safer than the current armed path AND it unblocks the 38 skips, because searching an unmonitored episode is merely inert rather than destructive | M | — |
| `GLD-SON-19` | 🟡→✅ **FIXED 2026-08-10.** Split into three counters (`searched`, `skipped (unresolvable)`, `skipped (guarded)`), and unmonitored episodes are now SEARCHED rather than skipped — inert until monitored, and the log says so per item. | `"file backs an unmonitored or unresolved episode"` conflates a POLICY state with a DATA fault. Unmonitored is fixable: the episode exists, and owning a file for it is arguably itself the statement that it is wanted (`PUT episode/{id} monitored:true`, then remediate normally). **Unresolved means the file maps to no known episode at all** — re-grabbing cannot fix it because Sonarr has nothing to search FOR, and it needs reporting, not remediation. Today they share a counter (`skipped_unmonitored`), so the 38 could be 38 policy skips, 38 broken mappings, or any mix, and the log cannot say which *(P-B)* | S | `GLD-SON-18` |
| `GLD-SON-01` | 🔴→🟡 **PARTIALLY FIXED 2026-08-10 — and the diagnosis was half wrong.** Both were absent from `component_dependencies`, so `all_component_classes` filtered them out and neither loaded. Both are now wired (`"quality": ["instance_manager","series","episodes"]`, `"sync": ["instance_manager"]`). **`quality` was genuinely REGRESSED** — it has a real `run()`, and first armed-adjacent pass produced `[Quality] [dry_run] Active-watcher upgrades - 'standard' (7283.4 GB free)`, output this system had never emitted. ✅ done. **`sync` was NEVER FINISHED, not regressed** — it had no `run()` AND no `prepare()`, so wiring it changed nothing but the load gap. The "Radarr's equivalent *does* run" framing that motivated this entry is misleading: `RadarrSyncManager.run` drives ONLY `profile_scores` and its docstring explicitly refuses the rest — *"tags / folders / naming / media_management stay caller-driven and are NOT run here — in particular media_management.sync_quality_across_instances (a clobbering blind-POST) is never invoked."* Sonarr has no `profile_scores` equivalent, so there is currently nothing safe for it to sweep. A first draft of the Sonarr `run()` called all five leaves and was **discarded** — it would have issued that same clobbering PUT. Sonarr now has a `run()` that drives nothing and REPORTS its own inertness, because a manager indistinguishable from absent is how this stayed invisible. Remainder tracked as `GLD-SON-20` | Radarr and Sonarr are configured asymmetrically — but by a missing subsystem, not a missing registration *(P-A)* | M | `GLD-SON-20` |
| `GLD-SON-11` | 🔴 ✅ **CONFIRMED — Fix the `critical_keys` contradiction** — `critical_keys` contains **all 8 loadable components plus `"quality"`**, which is filtered out and can never load. So the `"quality"` entry matches nothing, and `noncritical_components` is **empty** (§12.2) | `critical_keys` reads as a guarantee that quality loads; it is not one *(P-B)* | S | `GLD-SON-01` |
| `GLD-SON-13` | 🔴→✅ **FIXED 2026-08-10 — and it verified itself.** The prepare summary counted `len(self.component_dependencies)`, so "8/8 prepared" was *structurally incapable* of naming `quality` or `sync` — a denominator that can only equal its numerator is not a check (§12.3). Fixed by recording `unwired_components = set(full_components) - enabled_keys` at construction and WARNING when non-empty. Fixed FIRST, deliberately, so `GLD-SON-01` would be verifiable rather than asserted: the 2026-08-10 run after wiring showed **no warning at all**, because the set had emptied. A detector that goes quiet when the defect is gone is the only kind worth having | This is the mechanism by which `GLD-SON-01` stays invisible run after run *(P-D)* | S | `GLD-SON-01` |
| `GLD-SON-14` | **`run()` silently skips a component that fails to load** — `getattr(...) or self._load_component(name)` returning `None` falls through the `if component and hasattr(...)` guard with no entry in `results`, so `all_ok = all(results.values())` stays `True` (§12.4) | A load failure at run time produces a green summary | S | `GLD-SON-13` |
| `GLD-SON-12` | **Assert `critical_keys ⊆ all_component_classes`** at construction | Turns `GLD-SON-11`'s class of bug from silent into a startup error, for every manager | S | `GLD-SON-11` |
| `GLD-SON-02` | **Signal on false `no_results`** — distinguish an indexer timeout from a genuine miss | §6 row 6 is `P-C` in a new location: a timeout currently reads as "release doesn't exist" | M | — |
| `GLD-SON-03` | **`dry_run` propagation assertion** — verify at construction that every submanager received it | D3 documents a real bug where it wasn't propagated and episode ops ran live — the inline comment at `SonarrCacheManager` construction still records it | S | `GLD-ORCH-01` ⏸ |
| `GLD-SON-04` | ✅ **VALIDATED — Adopt Sonarr's `prepare()` pre-marking across other managers** — the fix is present and correct here (§12.5); `GLD-MIX-01` is a Sonarr-solved problem awaiting propagation | Cheapest partial fix for `GLD-MIX-01`; **verify Radarr has it** | S | `GLD-MIX-01` |
| `GLD-SON-05` | **Pilot outcome reporting** — how many probes climbed vs were deleted | The probe strategy's effectiveness is currently unmeasured | S | — |
| `GLD-SON-06` | **Per-series watched-definition** for prune purposes — last aired, 90%, or all | Blocks the auto-prune loop; series ambiguity is the hard half | M | `D2` |
| `GLD-SON-07` | **Partial-APPLY drift signal** | §6 row 8 *(P-D)* | S | — |
| `GLD-SON-08` | **Document `api/`** — the only Sonarr subfolder with no README | Doc parity | S | — |
| `GLD-SON-09` | **Component load timing** in the prepare summary | The largest subsystem, and its load cost is unmeasured | S | `GLD-MIX-05` |
| `GLD-SON-10` | **Consolidate the two `size_anomaly` test pairs** — `cache/` and `quality/` each carry `test_size_anomaly_remediate.py` + `test_size_anomaly_report.py`. ⚠️ Note the `quality/` pair tests a manager that **never loads** | Possible `P-E` duplicate implementation — and one half may be testing dead code | S | `GLD-SON-01`, `GLD-ML-15` |
| `GLD-ACQ-21` | 🔴→✅ **FIXED 2026-08-06 — `cache/episode_files.py` `_recycle_to_fund_acquisition`: refusal reasons (`_why`) emitted only when NOTHING funded.** The first live partial fund (Curious George funded; Big Bang Theory + 3 others refused) computed every per-series refusal reason — drop counts per filter stage, or the planner's verbatim reason — and discarded them all. Now emitted unconditionally when non-empty; the *"0 series reached the planner"* line still gates on the fully-empty case. ✅ **Verified 2026-08-06 23:13** — all 4 reason lines emitted; every refused pool died at the `protected` drop (TBBT 37/38 owned, See 10/10, AoT 7/7, Abbott 2/2), so the blocker sits inside `build_protected_file_ids` — attribution filed as `GLD-ACQ-22` | The one diagnostic a partial fund needs *(P-A — inside the scaffold `GLD-ACQ-19` added to prevent exactly this class; full record in ENH §4.1 + §8 P-A)* | S | — |
| `GLD-ACQ-22` | 🎯 **Attribute the `protected` drop to its guard** — the 23:13 reason lines localize the recycle blocker to `_build_protected_file_ids` (37/38, 10/10, 7/7, 2/2; protected-set 4,812 fids) but the counter lumps seven guards into one number. Household-not-all-watched (`GLD-ACQ-18`'s remaining site) and the series-wide WATCHLIST shield can each explain that scale; expose fid→guard (or per-guard counts) and print the breakdown in the reason line so one run names it. ⚠️ 2026-08-06 later the same day: the `GLD-ACQ-18` narrowing shipped first, so **the next run disambiguates EXPERIMENTALLY** (TBBT `protected` collapses ⇒ household was it; persists ⇒ watchlist) — the per-guard breakdown becomes a confirmation tool rather than the only path | Turns "guard-blocked" into WHICH guard — ✅ **ANSWERED 00:51 2026-08-07: a MOSAIC** — TBBT universe 36/38; See watchlist+retention 9/10; AoT retention 6/7; Abbott 2 files both held. household ⊆ retention in every line (`GLD-ACQ-18` proof). Full record ENH §4.1 | S | — |
| `GLD-ACQ-24` | 🔴→✅ **ALREADY FIXED — leading marker was stale, corrected 2026-08-10.** Implemented 2026-08-06 (§0.1 edit 13) and the selection bug fixed in edit 14; the 🔴 was simply never updated, which cost a re-investigation today. **Unwatched TV is unreachable by every delete path** — `build_delete_candidates` feeds the coordinator marked rows only; marking is watched+grace-driven; the stale-owned prune is movies-only; downgrades shrink but never remove. A cold 100-episode unwatched pile can never be reclaimed. Fix inside the existing flow (extend stale-prune to TV, or an unwatched low-watchability marker → same marked→coordinator path); the coordinator stays the sole delete decider. Full record in ENH §4.1 | The delete pool is missing the bulk an operator expects space pressure to take first. 🟡 **Implemented 2026-08-06 (§0.1 edit 13)** — flagged `row_origin='cold_scan'` rows, self-contained mark/release lifecycle, default OFF (`cold_tv_reclaim.enabled`); coordinator remains sole decider. 🔴→✅ First enabled run ingested 0/25 — cap burned on pilot-only series; selection fixed to `episodeFileCount≥2` fattest-first (§0.1 edit 14) | M | Next enabled run |
| `GLD-ACQ-26` | ✅ **Pilot watchability gate lowered BELOW the net-new tier** (operator ruling 2026-08-07: "by nature we don't know if we'll like a pilot or not — allow slightly easier access to new media"): `pilot_interactive.min_watchability` 20 → 15 (config + class default, rationale documented at the constant). Pilots are EXPLORATION — the sampling door is deliberately cheaper than a committed add or the cold-reclaim floor (both 20-tier). Effect: a slice of the 7,497 held-back stubs re-enters interactive search on the next run (score 15–19.9 band); the gate stays no-dead-zone — held stubs keep re-grading every run | S | ✅ Done | `episode_files.py`, `config.json` |
| `GLD-ACQ-27` | 🔴→✅ **The step-down realize grabbed WRONG SHOWS — identity gate added** (Sonarr twin of `GLD-RAD-30`, live evidence 2026-08-07 16:41: deleted Space Brothers S01E06 → grabbed 'Property.Brothers.S11E06…'; S01E05 → 'Super.Giant.Robot.Brothers.S01E05…'; S01E20 → 'Space.Brothers.E81…' absolute-mismatch). `release?episodeId=` returns raw fuzzy indexer results; the SHARED ladder picker ranks size/resolution identity-blind; the POST then forces `episodeId`, importing the stranger INTO the slot — and failed imports re-open the episode, feeding the delete→wrong-grab→missing→regrab-1080p→step-down-again churn that made "the same few shows" eat the pipeline. Fix: `_release_ok_for_episode` filters BEFORE the picker — (1) the shared GLD-RAD-30 title matcher, with a masked-episode retry because the movie-side sequel-number boundary killed legitimate 'Space Brothers - 20' anime forms; (2) episode evidence: SxxEyy/SxxEyyEzz/NxM must cover the target; bare Enn/number only when season==1 AND ==episode (kills E81-for-E20); resolution tokens ignored; packs + episode-less names rejected; (3) shared language gate. Conservative: no evidence ⇒ KEPT; blind-fallback EpisodeSearch lane (Sonarr's own matching) unchanged. `identity_rejected` stat added. Truth-tabled 12/12: all three live wrong-grabs rejected, correct SxxEyy/absolute/multi-ep/zero-padded pass, packs/wrong-season/partial-title/sequel-series rejected. **`27b` addendum (operator gap-probe "Demon Slayer vs Kimetsu no Yaiba"): the gate originally matched the display title only — original-language releases would be safely rejected but step-downs would STALL on divergently-named anime. `_series_alt_titles` now feeds the matcher's `alt_titles` param (which the movie side always had): display-title SEGMENTS split on colon/parentheticals (≥2 tokens or ≥8 chars — the combined-title convention carries the romaji for free, zero API calls) + Sonarr's own `series/{id}.alternateTitles` (once per candidate series, failure-tolerant, capped 12, primary deduped); masked-episode retry alias-aware. Verified 9/9: Kimetsu-named releases pass WITH alts and are rejected WITHOUT (proving the alts do the work), 'Demon Lord Retry'/'Yaiba Samurai Legend' strangers still rejected, prior table spot-checks green.** **`27c` addendum (operator: "can anime look for exact episode numbering instead of just SxEy?"): YES — `_series_absolute_map` pulls Sonarr's own `{(season, ep) → absoluteEpisodeNumber}` for ANIME-typed series (one `series/{id}` + one `episode?seriesId=` per candidate series per pass; None for non-anime and on failure → prior conservative behaviour), and the gate's bare-number/`Enn` rules now pass on EXACT absolute equality any season ('Kimetsu no Yaiba - 33' validates as S02E07), with 4-digit widths for long-runners and absolute variants in the masked-title retry; the season-1 heuristic remains the no-data fallback. Sonarr already SEARCHES anime with absolute forms — this is the matching half. Verified 11/11: exact-absolute + E-form pass, S1's absolute-7 rejected for S02E07, no-data conservative reject, SxxEyy primary intact, 4-digit One Piece form, E81/stranger/wrong-number regressions all still dead** | M | ✅ Fixed | `series/space_pressure.py` |
| `GLD-ACQ-28` | 🔎→✅ **SOLVED: daemons were being MURDERED by the IDE's job object, with their logs buffered into oblivion.** Operator's process check returned EMPTY (dead, not hung) — reconciling everything: the 16:34 spawn genuinely created a child (Windows recycled pid 33692), breakaway from PyCharm's job failed or wasn't effective, the child processed the legacy job instantly (queue emptied ✓) and idled — with every log line sitting in its BLOCK-BUFFERED stdout — until the run window closed at 16:57 and `KILL_ON_JOB_CLOSE` hard-killed it, destroying the buffer: zero bytes to disk, log mtime frozen at the PREVIOUS session's clean exit (13:05). The supervisor's own docstring predicted the mechanism. No job was lost today (processed, logs lost) — but any LONG pilot spree launched from an IDE run truncates silently at run close. Fixes: `PYTHONUNBUFFERED=1` in the child env (every line lands immediately — deaths become visible), post-spawn 1.5s liveness poll (instant deaths now log an ERROR + clean the pidfile instead of announcing success), breakaway-rejected warning upgraded to state the kill-at-run-close consequence plainly. Residual operator guidance: launch runs that enqueue big sprees from a terminal or Task Scheduler | M | ✅ Fixed | `factories/daemons/supervisor.py` |
| `GLD-ACQ-29` | **Legacy-regrab summary counters don't account for skips**: 822→2+752 (68 unaccounted), 347→278 (69), then 67→0/0/0 instantly — an uncounted pre-search skip gate (cooldown/attempted-recently) absorbs the residue and the same 67 re-spill every run. Add a `skipped` counter to the daemon summary + stop re-spilling files whose last attempt found no modern release within the cooldown window | S | Open | `daemons/pilot_search_daemon.py`, `episode_files.py` |
| `GLD-ACQ-30` | 🔴→✅ **CLOSED 2026-08-10 — two lanes, not four, and one was hidden behind a default-true branch.** *Incident (unchanged, 2026-08-07):* the morning's purge headroom (~1.5TB) was consumed by the day's own successes (pilot-wave imports, step-down replacements, next-episode grabs) back to 729G aggregate ≈ under the ~200G/disk share Minimum Free → shfs refused allocations → SAB `complete_dir not writable` → EVERY download failed → Sonarr FDH blocklisted + ladder-burned the release catalog per episode (the ×15/hour re-grab churn; paired same-release events 2–5s apart = grab+instant-fail) → 502 connection storms at the indexer. *Audit on close:* **three of the four named lanes had ALREADY grown floors** since the incident — `_do_acquire_next_episodes` (line 4344, with `GLD-ACQ-21`'s self-funding recycle *inside* that gate, dated 2026-08-06), the upgrade lane (7812), and every universe/adder grab via `AcquisitionManager._space_ok` (`space_targets` at 101/188), which `hybrid_universe_acquisition` routes through by design. **The proposed shared `acquisition_space_ok()` gate was therefore NOT wired in** — a second, cruder floor beside a better one is worse than none, and `space_targets` is already the single source the pressure passes read. **Two genuine gaps fixed:** (1) `legacy_regrab.py` had no free-space reference anywhere in the file and issues a DIRECT `POST release` by guid — the most immediate grab in the codebase; gated at the entry point *before* the thread pool (per-item would be racy and N API calls), caller supplies `space_targets` figures. (2) **`run_pilot_search`'s DEFAULT path** — the existing floor sits inside `if pilot_best_tier:` = `(not pilot_climb) and …`, and **`pilot_climb` defaults to True**, so the default path reached dispatch with `pilot_free_gb`/`pilot_reserve_gb` still `None`. Now gated once at `if climb_items:` so the interactive, in-process and daemon routes are covered identically. `stats["skipped_space"]` was **already declared and never set** — the gate was intended and never built. This lane matters most: it dispatches to a background worker that keeps searching after the run process moves on. Boundaries verified: at-floor proceeds (`<` not `<=`, matching 4344), unreadable free does NOT freeze the lane, `stats["searched"]` assigned after the clear. *Operator remediation at the time:* space purge, SAB incomplete cleanup, Sonarr blocklist clear (the storm blocklisted the BEST releases), SAB pause-on-low-space ≈ 1TB aggregate | M | ✅ Fixed — 2 gaps closed, 3 lanes already covered | `episode_files.py` (pilot climb), `cache/legacy_regrab.py` |
| `GLD-ACQ-31` | 🔴→✅ **Series sync sent the 'keep' LABEL where Sonarr wants integer tag ids — first-run PUTs 400'd AND were logged as ✅ Synced.** Flushed out by the fresh-start first run (the wipe's cold-seed path had never executed): `$.tags[i] could not be converted to System.Int32` on every keep-tagged series, while `run_sync_jobs` counted them `applied` because `_make_request` swallows HTTP errors and returns None without raising (P-D, the DELETE-returns-None twin). Root: `ensure_keep_set` RESOLVED the keep tag ids per instance and DISCARDED them — no accessor existed, so `updated_tags.add("keep")` grew in QUADRUPLICATE (P-E: series/sync/__init__ payload builder — the live failer — + synchronize seed path + async_tasks + the run_sync_jobs forwarder). Fix: ids persisted (`keep_tag_ids[inst]`) + `ensure_keep_tag_id(instance)` accessor (persisted → live GET /tag → POST-create → None⇒skip-with-warning, never a string); all three label sites converted; run_sync_jobs gained an int-coercion belt (drops non-ids loudly) AND a PUT result check (None ⇒ raise ⇒ the honest failure path). All four files compile; accessor ladder sim-verified (persisted/GET/create/None) | M | ✅ Fixed | `sync/tags.py`, `series/sync/__init__.py`, `series/sync/synchronize.py`, `series/sync/async_tasks.py` |
| `GLD-ACQ-18` | 🟡 **Executed here 2026-08-06 — household guard narrowed to ACTIVE WATCHERS ONLY** at `cache/episode_files.py` (`_apply_grace_period` `household_blocked`, `_do_delete_marked_files` HOUSEHOLD GUARD) and `machine_learning/classification/guards.py` (`build_protected_file_ids`): raw all-members mandate ∩ row `retention_hold`; `all_household_watched` + its sync computation untouched; guard tests extended (held / released / row-level), brain branch verified in isolation 5/5. Full rationale + operator decision in ENH §4.1. ✅ **Ran clean 23:36 UTC (mtime-proven import) — but SHADOWED: TBBT stayed 37/38, set 4,812→4,800.** Household was not the live recycle blocker; suspect shifted to universe credit (floor 1.0, TBBT saga +6.0) — see `GLD-ACQ-22`/`GLD-ACQ-23` | Unfreezes the household mandate's independent holds; the recycle blocker turned out to live elsewhere | M | `GLD-ACQ-20` (horizon) |
| `GLD-ACQ-23` | 🎯 **DECISION — the recycle inherits ENGAGEMENT guards (hot-universe credit, watchlist shield) that structurally exclude exactly the hottest series** — the ones `recency_gate` walks first and leapfrogging exists for. Curious George funds because it carries neither signal. Question per guard: is recycling an ALREADY-WATCHED episode of an engaged series the loss the guard exists to prevent? If no → recycle's set = delete's set MINUS {universe, watchlist} (deliberate, documented break of recycle ⊆ deletable, gated on `GLD-ACQ-20`). If yes → the leapfrog serves only the cold tail by design. Full framing in ENH §4.1 | Determines whether `GLD-ACQ-13` can ever serve the series it was built for | M | Operator decision, `GLD-ACQ-20` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is `SonarrQualityManager` superseded by JIT, or has it regressed out of the active set? | `GLD-SON-01` |
| Q6 | **Is Sonarr-side config sync happening at all?** `SonarrSyncManager` never loads; `orchestration` is loaded and may invoke it. If not, custom formats / naming / folders / tags are not being pushed to Sonarr while they are to Radarr. | `GLD-SON-01` |
| Q2 | What fraction of a series counts as "watched" — last aired episode, 90% of aired, all? *(= D2)* | `GLD-SON-06` |
| Q3 | Should Sonarr join the critical-flag set now that TV is a large share of the library? | — |
| Q4 | Is `PILOT_SPILL_THRESHOLD = 10` still right against measured in-process cost? | — |
| Q5 | Do the duplicated `size_anomaly` tests in `cache/` and `quality/` cover the same code? | `GLD-SON-10` |

## 11. Related designs

- [`radarr/DESIGN.md`](../radarr/DESIGN.md) — the mirrored movie subsystem
- [`services/DESIGN.md`](../DESIGN.md) §3.3 — the uniform shape
- [`factories/daemons/DESIGN.md`](../../factories/daemons/DESIGN.md) §3.5 — pilot job flow
- [`machine_learning/DESIGN_series_saga_resumption.md`](../../machine_learning/DESIGN_series_saga_resumption.md)
- [`coordinator/catchup_retention.md`](../coordinator/catchup_retention.md) · [`coordinator/tv_franchise_discovery.md`](../coordinator/tv_franchise_discovery.md)

---

## 12. Source verification — session 34

Read: `sonarr/__init__.py` in full. Every claim below is quoted or line-derived.

### 12.1 `quality` and `sync` never load — confirmed on all three paths

```python
self.component_dependencies = {            # 8 keys — comment: "(active subset)"
    "instance_manager", "storage", "series", "episodes",
    "monitoring", "repair", "validator_manager", "orchestration",
}
enabled_keys = set(self.component_dependencies.keys())

full_components = {                        # 9 entries
    "validator_manager", "series", "episodes", "monitoring",
    "quality",   ← present here
    "storage",
    "sync",      ← present here
    "repair", "orchestration",
}
self.all_component_classes = {k: v for k, v in full_components.items()
                              if k in enabled_keys}     # → 8, quality/sync dropped
```

Both `prepare()` and `run()` iterate `topo_order(self.component_dependencies)` —
**not** `all_component_classes` — so the two are excluded from loading, preparing
and running. They remain imported, declared as class attributes
(`quality: Optional[SonarrQualityManager] = None`) and listed in
`full_components`, so the code reads as though they might load.

**Important nuance:** the dict is labelled **`(active subset)`** in-source. This is
a *deliberate deactivation*, not an accidental omission. What is missing is any
statement of **why**, or of what would reactivate them.

### 12.2 `critical_keys` — the contradiction, precisely

```python
self.critical_keys = {
    "instance_manager", "series", "episodes",
    "quality",          ← not in all_component_classes
    "storage", "monitoring", "repair", "validator_manager", "orchestration",
}
```

Nine entries: **all 8 loadable components, plus `"quality"`.** Two consequences:

- `"quality"` matches nothing in `all_component_classes`, so declaring it critical
  guarantees nothing.
- Since every loadable component is critical, **`noncritical_components` is
  empty** — confirmed by `run()`'s hardcoded
  `log_filtered_component_summary(..., noncritical_components=[], ...)`.

`"sync"` is correctly absent from `critical_keys`, which is what makes the
`"quality"` entry read as an oversight rather than a pair.

### 12.3 Why it stays invisible

```python
names = list(self.component_dependencies.keys())      # 8
n_ok  = sum(1 for n in names if str(self.load_summary.get(n, '')).startswith('✅'))
```

The prepare summary's denominator is `component_dependencies`, so a healthy run
reports **8/8** and the line is *structurally incapable* of naming `quality` or
`sync`. The system reports full health while two managers are absent.

### 12.4 A load failure at run time reads as success

```python
component = getattr(self, name, None) or self._load_component(name)
if component and hasattr(component, "run"):
    ...  results[name] = "✅" / "❌"
all_ok = all(str(v).startswith("✅") for v in results.values())
```

If `_load_component` returns `None`, the guard skips the component and **no key is
written to `results`** — so `all_ok` is unaffected. `prepare()` would normally
catch it first, but the run-time path has no equivalent signal.

### 12.5 ✅ `GLD-MIX-01` is fixed here

```python
# Components built eagerly in __init__ (instance_manager) bypass
# _load_component — the only thing that writes a load_summary row — so mark
# them loaded here, else they render ❌ despite being healthy.
for name in order:
    if getattr(self, name, None) is None:
        self._load_component(name)
    elif not str(self.load_summary.get(name, "")).startswith("✅"):
        self.load_summary[name] = "✅"
```

The false-`❌` for eagerly-constructed components is **solved in Sonarr**, with the
cause named in the comment. A second fix is adjacent — *"a `prepare()` failure
flips that component to ❌ (previously such failures were silently swallowed)"*.

`GLD-MIX-01` therefore becomes: **verify Radarr has the same pre-marking**, and
propagate if not (`GLD-SON-04`).

### 12.6 Two prior bugs still recorded inline

| Comment | Bug it records |
|---|---|
| `# BaseManager's param is 'global_cache' (NOT 'cache') — passing cache= left self.global_cache=None` | A kwarg-name mismatch silently nulled the cache on this manager |
| `dry_run=self.dry_run,  # without this the cache (and its episode-file ops: acquisition, sync, JIT) ran LIVE even in dry_run=True sessions` | The shipped `dry_run` propagation bug D3 documents |

Both are the strongest available evidence for `GLD-SON-03` and `GLD-ORCH-01`: the
`dry_run` invariant has already failed once, in this exact constructor.

---

## 12.7 🎯 `component_dependencies` is a CONSTRUCTION graph, not an EXECUTION graph

Auditing all eight entries for a `run()` method:

| Component | Has `run()`? | Actually invoked? |
|---|---|---|
| `orchestration` | ✅ | ✅ from `SonarrManager.run()` |
| `series` | ✅ | ✅ — but via **`orchestration/series.py`**, not this protocol |
| `storage` | ❌ | methods called ad hoc (`get_free_space_per_instance`, …) |
| `episodes` | ❌ | via `orchestration/episodes.py` |
| `monitoring` | ❌ | ❓ unknown |
| `repair` | ❌ | ❓ unknown (`GLD-REP-07`) |
| `validator_manager` | ❌ | ✅ `audit_bootstrap_instances`, *"Called early in `SonarrInstanceManager.__init__`"* |
| `instance_manager` | ❓ not read | — |

**Six of eight define no `run()`.** And `SonarrManager.run()` only writes a
`results` entry inside the capability guard:

```python
if component and hasattr(component, "run"):
    ...  results[name] = "✅" / "❌"
all_ok = all(...)
log_filtered_component_summary(critical_components=results.keys(), …)
```

So the **run summary's denominator is "components that happened to define
`run()`"** — about two of eight. Five are absent from it not because they failed
but because they were never eligible.

That is `GLD-SON-13` at a larger scale than §12.3 recorded. The **prepare** summary
over-reports (8/8 against a filtered set); the **run** summary under-reports (~2/8
against a capability-filtered set). Neither denominator answers the question a
reader actually has: *did the eight declared components do their work?*

### 12.7.1 And this mostly reframes the finding in the codebase's favour

`validator` shows the pattern is deliberate: it exposes
`audit_bootstrap_instances` and **names its caller** in the docstring. These are
**service-layer facades** whose methods are invoked by whoever needs them —
orchestration, the instance manager, a phase hook — not by a uniform `run()`
sweep.

So `component_dependencies` is doing **construction and dependency ordering**, and
the `run()` protocol is a minority convention layered on top.

That means register items of the form *"X has no `run()`, therefore X does
nothing"* (`GLD-SERQ-01`, `GLD-REP-07`) must be re-tested against a different
question: **does the facade have a caller?** Sometimes demonstrably yes
(`validator`, `series/space_pressure`), sometimes unknown (`repair`,
`monitoring`). The tell remains the one §checklist Q8 records — whether the
method or docstring names its entrypoint.

`GLD-SON-16`.

---

## 12.8 The `parent_name` filter: five callers route around it, three exercise it

Revising §4 of [`storage/DESIGN.md`](./storage/DESIGN.md) now that every Sonarr
caller is known:

| Caller | `critical_keys` | Filter runs? |
|---|---|---|
| `sync/` · `quality/` · `orchestration/quality.py` · `storage/` · **`monitoring/`** | **all components** | ❌ dead |
| `repair/` | 6 of 15 | ✅ — and `repair_cache` is mis-keyed into it (`GLD-REP-01`) |
| `episodes/` | 1 of 5 | ✅ — and it drops `sharding` (`GLD-SPLIT-02`) |
| `validator/` | 4 of 5 (`api_factory` non-critical) | ✅ — **unverified** |

**Five route around it; three exercise it, and two of those three have known
problems.** The third — `validator`'s `api_factory` — has never been checked, and
is the obvious next probe for `GLD-EPI-06`.

That sharpens `GLD-STO-04`: the machinery is avoided by the majority and
malfunctions in the minority that use it.
