# RadarrManager

- **File** — `scripts/managers/services/radarr/__init__.py`
- **One-liner** — The top-level Radarr service orchestrator: it builds and wires the whole Radarr submanager tree (instance, cache, movies, quality, monitoring, sync, storage, repair, orchestration) in dependency order, then drives them through `prepare()` and `run()`.

## 🔴 HIGH-PRIORITY TODO — Multi-instance upgrade routing (standard / ultra / anime)

> **Status:** 🟡 **PARTIAL — Phases 1–2 BUILT; add-if-absent + migration remain.** Done: (1) onboarding
> writes `radarr_instances_categorized` (resolution tiers + optional anime) + `gateway.categorized_instance`
> is service-aware; (2) `acquisition/resolver.py` routes **anime movies** to the categorized anime instance,
> **falling back to the default instance when no anime session is set** (reused the existing
> `classify_movie`/`_is_anime` classifier). NOTE: resolution-tier (4K↔standard) instance routing for NEW
> adds happens post-landing in `router_movie.py` (the file doesn't exist at add-time) — wiring *that* to
> `categorized_instance` is a separate sub-task. **Remaining (high priority):** add-if-absent / tag;
> safe make-before-break migration (the upgrade/downgrade path).
> **Pairs with:** `onboarding/README.md` → *"Planned enhancement — Radarr instance categorization"* (the role map this consumes).

When a household runs **more than one** Radarr instance (e.g. `standard` for HD, `ultra` for
4K/UHD, and optionally a dedicated `anime` instance), a movie's release-candidate **upgrade**
must be routed to the instance whose role matches the upgrade's target tier — not blindly to
the default instance.

**Routing rule:**
- **1 Radarr instance** → no routing decision; every upgrade stays on that instance. **No-op.**
- **2+ Radarr instances** → route each upgrade to the instance matching its target tier, using
  the role map onboarding records (`radarr_instances_categorized`):
  - target tier ≤ 1080p → **standard / HD** instance
  - target tier 4K / UHD → **ultra** instance
  - movie classified as anime **and** an anime instance exists → **anime** instance

**Where the target tier comes from:** the movie's watchability score → quality-profile tier
(`machine_learning/scoring/SCORING_GROUPS.md`, score→profile table; e.g. 70–79 → Remux-2160p
HDR). The upgrade's target profile sets the target tier, which selects the instance.

**Current behaviour (the gap):** Radarr movie acquisitions/upgrades always resolve to
`gateway.default_instance()`. Movies fall through to the default instance, and
`gateway.categorized_instance()` now reads only `radarr_instances_categorized` — Sonarr is
single-instance and **no longer has a categorized-instance map**, so `categorized_instance` is
effectively Radarr-only. So a 4K-worthy upgrade lands on `standard` with no path to `ultra`.

**Implementation hook points (when picked up):**
1. Onboarding must first write `radarr_instances_categorized` (see the paired onboarding TODO).
2. `acquisition/gateway.py::categorized_instance` — make service-aware
   (`f"{self.service}_instances_categorized"`) so Radarr role lookups resolve.
3. `radarr/quality/selector.py` (`RadarrQualitySelector`) — it already resolves the best-fit
   profile *per instance*; extend it to pick the **instance** from the target tier first.
4. `acquisition/resolver.py` instance selection — wire up the movie + resolution-tier case
   (today movies use `default_inst`). Note: Sonarr is now single-instance with no categorized
   map, so there is no longer a Sonarr anime-routing pattern to mirror — this is Radarr-only.

### Add-if-absent / tag-for-auto-add
When a movie's target tier selects an instance it is **not yet present in** (e.g. a 4K-worthy
movie missing from `ultra`, or an anime movie missing from the anime instance):
- **Direct add (default):** add it to the target instance via the Radarr API — monitored, with
  that instance's quality profile for the target tier and the correct root folder. Idempotent:
  skip if already present (dedup by `tmdbId` in the target instance).
  - **Profile-parity, not profile-copy:** resolve the target instance's *own* profile id for the
    tier (profile IDs differ per instance) — match by **name**, never copy the source id. Reliable
    name-parity is what the onboarding *"mirror quality profiles / Custom Formats across instances"*
    enhancement sets up (see `onboarding/README.md`).
- **Tag-for-auto-add (fallback / looser coupling):** apply a Radarr tag (e.g.
  `glidearr:promote-ultra`) so an existing import list / automation pulls it in. Tag-based
  is also the safe **dry-run-first** default — tag for a few runs, review, then enable direct add.

### Safe make-before-break migration (upgrade ↔ downgrade)
When a movie's tier changes so it should live in a **different** instance, the old-instance copy
is **marked for deletion — but the delete only fires once a valid candidate has been located,
acquired, and INGESTED (present with `hasFile` at the target tier) in the target instance.**
Until then the original is retained, so there is never a coverage gap.
- **Stateful, multi-run:** the replacement may take days to appear, so this is a durable
  **pending-migration stamp** in the decision ledger (`machine_learning/ledger`, the same
  parity-oracle the dry-run summary reads) — not a single-run op. The delete fires on a later
  run, when ingestion is confirmed.
- **"Ingested"** = present in the target instance with `hasFile` **and** quality meeting the
  target tier — never merely "added / monitored" (that would break-before-make).
- **Reuse the guarded delete path:** dry_run gate, the `free_space_limit` floor (deletions are
  **hard-disabled when `free_space_limit` is unset**), whole-file guards, restorability — never a
  new unguarded delete. The in-flight new acquisition must be **protected from space-pressure**
  during the transition (the movie is briefly ~2× on disk).
- **Hysteresis + timeout:** require the new tier to persist (score band / N-run dwell) before
  migrating, so a movie oscillating around the 4K threshold doesn't thrash; if the replacement
  never appears within a grace window, **keep the current copy and clear the pending stamp**.
- **Downgrade shortcut:** on a downgrade, if the lower-tier instance already holds the movie,
  just delete the high-tier copy (no re-acquire); only acquire if absent.
- Prefer letting the **Phase-2.5 space coordinator** own the confirmed delete so it unifies with
  other guarded deletions. Anime routing needs an **anime-movie classifier** (Radarr has none
  today; mirror the show-side `library_classifier`).

## What it does (for a senior Python engineer)

`RadarrManager(BaseManager, ComponentManagerMixin)` is the single entry point for everything Radarr-related. It is constructed by `Main` in `scripts/main.py` (alongside `SonarrManager`, `TraktManager`, etc.) with the shared process-wide deps (`logger`, `config`, `global_cache`, `validator`, `registry`, `dry_run`). As a `BaseManager` it is a singleton, self-registers under the registry `"manager"` category, and auto-links to its parent (`Main`), inheriting the shared logger/config/cache/validator.

It is itself an **orchestrator**, not a FETCH/CACHE/APPLY leaf — it owns no API endpoints, config keys, or cache keys of its own. All of those live in the submanagers it constructs. It does, however, perform two pieces of real wiring work:

- It builds a `CacheKeyBuilder` (`self.key_builder`) and threads it into every downstream component via `init_args`.
- It eagerly constructs the two foundational submanagers — `RadarrInstanceManager` and `RadarrCacheManager` — and exposes `self.radarr_api` as an **alias for `instance_manager`** (the same `radarr_api` naming convention used by Sonarr). Per project convention this alias is always `radarr_api`, never a generic `api`.

### Submanager tree (the dependency graph)

`self.component_dependencies` declares both the set of enabled components and their load order:

| Component | Class | Depends on |
| --- | --- | --- |
| `instance_manager` | `RadarrInstanceManager` | — (built first) |
| `radarr_cache` | `RadarrCacheManager` | `instance_manager` |
| `storage` | `RadarrStorageManager` | `radarr_cache`, `instance_manager` |
| `movies` | `RadarrMoviesManager` | `radarr_cache`, `instance_manager` |
| `quality` | `RadarrQualityManager` | `movies`, `instance_manager` |
| `monitoring` | `RadarrMonitoringManager` | `movies`, `instance_manager` |
| `sync` | `RadarrSyncManager` | `instance_manager` |
| `repair` | `RadarrRepairWrapperManager` | `movies`, `storage`, `instance_manager` |
| `orchestration` | `RadarrOrchestrationManager` | `movies`, `storage`, `quality`, `monitoring` |

`RadarrValidatorManager` is also listed in the `full_components` class map (as `validator_manager`) but is **not** in `component_dependencies`, so it is not auto-loaded, prepared, or run by the dependency-driven loops below. The submanagers themselves live in this directory's subdirectories (`instance/`, `cache/`, `movies/`, `quality/`, `monitoring/`, `sync/`, `storage/`, `repair/`, `orchestration/`, `validator/`) and are documented as separate work items.

### Critical vs. noncritical split

`self.critical_keys = {instance_manager, movies, quality, storage, orchestration}`. The class map is passed through `split_components(...)` (`scripts/support/utilities/managers/component_splitter.py`), which separates the map into a critical dict (names in `critical_keys`) and a noncritical dict. For noncritical candidates it instantiates a throwaway instance and keeps only those whose `parent_name` matches `"RadarrManager"`, logging an introspection warning on failure. `RadarrManager.parent_name` is the literal string `"RadarrManager"`.

### Key public methods

- `__init__(...)` — wires the graph (Steps 1–5 in source): defines `component_dependencies`, eagerly builds `instance_manager` and `radarr_cache`, sets `self.radarr_api`, assembles the shared `self.init_args` dict, filters `full_components` to the enabled keys, and runs `split_components`. Reads `dry_run` from `kwargs` (default `False`). Logs `🧩 RadarrManager initialized with filtered components: [...]`.
- `prepare()` — (timed via `@timeit("prepare")`) ensures every component in `component_dependencies` is loaded (via `_load_component`), then calls each component's `prepare()` if it has one. It explicitly back-fills the `load_summary` row to `✅` for the eagerly-built components (`instance_manager`, `radarr_cache`) that bypass `_load_component`, so they don't render `❌` despite being healthy. A `prepare()` failure flips that component's summary to `❌` and is collected into a `failed` list (previously such failures were silently swallowed). Emits a colour-coded `n_ok/total components prepared` summary.
- `run()` — (timed via `@timeit("run")`) iterates `component_dependencies` in order, loading any missing component, and calls each component's `run()`. Per-component success/failure is recorded into `results` and merged into `load_summary`, then a filtered component summary is logged via `log_filtered_component_summary(service_name="Radarr", ...)`. Exceptions in a child `run()` are caught and logged as `❌ <name>.run()`, so one failing submanager does not abort the others.
- `_load_component(name, auto_load_deps=True, log_dependencies=True)` — lazy loader: returns an already-set attribute or a registry-resident instance if present; otherwise resolves the class from the critical/noncritical maps, recursively loads its declared dependencies first (when `auto_load_deps`), then builds it via `self._singleton(...)` and records `✅` / `❌` in `load_summary`. Returns `None` for unknown names.

### dry_run / concurrency

`self.dry_run` is captured from kwargs and propagated into `instance_manager`, `radarr_cache`, and every downstream component through `init_args`. `RadarrManager` itself mutates nothing — actual "would …" dry-run behavior is implemented by the leaf managers (e.g. movies/storage/repair). As a `BaseManager`, instances are cached as singletons keyed by `(class, singleton_key)`; `_load_component` also consults the registry, so a component already built elsewhere is reused rather than duplicated.

## How it functions

Lifecycle, as driven by `Main` in `scripts/main.py`:

1. **Construct** — `Main` builds `RadarrManager(...)` (after the parallel Radarr/Sonarr/Trakt auth check). In `__init__`, the dependency graph is declared, `instance_manager` + `radarr_cache` are built eagerly, `init_args` is assembled, and `split_components` partitions the remaining classes. `registry.set_flag("radarr_initialized")` is set by `Main`.
2. **prepare** — `Main` calls `self.radarr.prepare()` (main.py line ~317). This loads every graphed component and calls each one's `prepare()`.
3. **run** — `Main` calls `self.radarr.run()` (main.py line ~367), which executes each component's `run()` in dependency order.

Notable internal helper: `_load_component` is the *only* path that writes a `load_summary` row, which is why `prepare()` has to back-fill rows for the two eagerly-built components.

**Brain delegation:** `RadarrManager` itself delegates no decisions to `machine_learning/`. It only orchestrates the submanagers; any value-judgement delegation (e.g. monitoring/grace/storage decisions handed to `machine_learning/`) happens inside those leaf managers and is documented there — not here.

## Criteria & examples

The only selection logic at this level is the critical/noncritical partition and dependency ordering:

- **Dependency ordering example:** `quality` depends on `movies`, which depends on `radarr_cache` + `instance_manager`. If `run()` reaches `quality` and `movies` was never built, `_load_component("quality")` first recurses into `_load_component("movies")` (which in turn loads its deps), so `quality` never runs against an unbuilt movies layer.
- **Critical vs. noncritical example:** `orchestration` is in `critical_keys`, so it lands in `critical_components` directly. `sync` and `repair` are not critical, so `split_components` instantiates a probe of each and keeps it only if `probe.parent_name == "RadarrManager"`; a class that fails to instantiate is logged as a `⚠️ Failed to introspect` warning and dropped from the noncritical map (it can still be lazily loaded later if its dependencies appear).
- **Resilience example:** if `monitoring.run()` raises, `run()` records `results["monitoring"] = "❌"`, logs `❌ monitoring.run(): <error>`, and continues to `sync`, `repair`, `orchestration`. `all_ok` then becomes `False` and the final summary reflects the partial success.

## In plain English

Think of `RadarrManager` as the floor manager of a movie-library warehouse. It does not personally fetch tapes, re-shelve discs, or judge whether *The Princess Bride* should be upgraded to 4K — that's the job of the specialist crew members (the movies clerk, the quality inspector, the storage hand, etc.). The floor manager's job is to hire that crew in the right order (you can't inspect quality before someone has pulled the movie record), make sure everyone has the same clipboard (shared logger, config, and cache), do a roll-call to check everyone is ready (`prepare`), and then tell each person to start their shift in turn (`run`). If one crew member trips and drops a box, the manager notes it on the roll-call sheet and keeps the rest of the shift going rather than shutting the whole warehouse down.

## Interactions

- **Parent manager:** `Main` (`scripts/main.py`), which constructs it and calls `prepare()` then `run()`.
- **Submanagers it loads (this directory's subpackages):** `RadarrInstanceManager` (a.k.a. `radarr_api`), `RadarrCacheManager`, `RadarrMoviesManager`, `RadarrQualityManager`, `RadarrMonitoringManager`, `RadarrSyncManager`, `RadarrStorageManager`, `RadarrRepairWrapperManager`, `RadarrOrchestrationManager`. (`RadarrValidatorManager` is referenced in the class map but not wired into the dependency graph.)
- **Shared infrastructure:** `BaseManager` (singleton/registry/parent-link), `ComponentManagerMixin` (`log_filtered_component_summary`), `RegistryManager` (component lookup + `manager` category), `CacheKeyBuilder`, and `split_components`.
- **Brain modules:** none directly. Decision delegation into `machine_learning/` is the concern of the individual leaf submanagers, not this orchestrator.
