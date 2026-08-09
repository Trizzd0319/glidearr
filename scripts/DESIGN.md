# scripts — Design

> Breadcrumb: **glidearr** › **scripts**

**Package** — `scripts`
**Status** — 🟡 Partial (core run path ✅ Implemented; web layer 🔵 Planned)
**Related** — [README.md](./README.md) · [DOCS_CONVENTIONS.md](./DOCS_CONVENTIONS.md) · [main.md](./main.md)

---

## 1. Problem statement

A large personal media library accumulates three classes of debt that no single
`*arr` tool solves:

1. **Acquisition debt** — the gap between "what the household will actually watch"
   and "what is on disk". Sonarr/Radarr can only act on lists someone else curated.
2. **Quality debt** — files that are the wrong tier for the device that will play
   them. Too low and it looks bad; too high and Plex transcodes, or the disk fills.
3. **Space debt** — finite storage against an unbounded wishlist, where the naive
   answer (delete oldest) destroys exactly the things a household re-watches.

Glidearr is the decision layer that sits above Sonarr, Radarr, Plex, Tautulli,
Trakt, MAL and MDBList and resolves all three against **observed household
behaviour** rather than against generic popularity.

The core thesis: *acquisition, quality and deletion are the same optimisation
problem viewed from three angles, and they must share one scoring model or they
will fight each other.*

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | **One scoring model.** Acquisition, upgrade, downgrade and deletion all read the same 100-point watchability score. |
| G2 | **Evidence before action.** Every destructive decision is traceable to a ledger row naming the signals that produced it. |
| G3 | **Dry-run is the primary mode.** A dry run must produce the complete plan, persisted, without touching external state. |
| G4 | **Degrade, never abort.** One dead service downgrades the run's ambition; it does not end the run. |
| G5 | **Idempotent.** Running twice back-to-back is a no-op on the second pass. |
| G6 | **Cache is a first-class citizen.** The external APIs are rate-limited and slow; the cache is the working set, not an optimisation. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Replacing Sonarr/Radarr | They own indexer/download-client mechanics. Glidearr owns *intent*. |
| N2 | Being a media server | Plex owns playback. |
| N3 | Multi-tenant SaaS | Single household, single operator. Shapes the auth and concurrency model. |
| N4 | Real-time reaction | The run is batch. Latency budget is minutes, not milliseconds. |

---

## 3. Architecture

### 3.1 The four layers

```
┌──────────────────────────────────────────────────────────────────────┐
│  ENTRY            scripts/main.py                                    │
│                   Owns the run lifecycle. Makes no value judgements. │
├──────────────────────────────────────────────────────────────────────┤
│  FACTORY          scripts/managers/factories/                        │
│                   Logger, Config, Secrets, Registry, Cache, Metrics, │
│                   Daemons, Onboarding, Mixins.                       │
│                   Infrastructure only. Knows nothing about media.    │
├──────────────────────────────────────────────────────────────────────┤
│  SERVICE          scripts/managers/services/                         │
│                   Radarr, Sonarr, Plex, Tautulli, Trakt, MAL,        │
│                   MDBList, Acquisition, Coordinator, Routing,        │
│                   Writeback, Calendar, Backup.                       │
│                   Thin adapters. FETCH / CACHE / APPLY. They ask     │
│                   the brain; they do not decide.                     │
├──────────────────────────────────────────────────────────────────────┤
│  BRAIN            scripts/managers/machine_learning/                 │
│                   Scoring, likelihood, lifecycle, space, sizing,     │
│                   thresholds, discovery, playlists, eval, ledger.    │
│                   Pure functions over feature rows. No I/O.          │
└──────────────────────────────────────────────────────────────────────┘
                   scripts/support/  — utilities, tools, daemons,
                                       notifications, config, profiles
```

**The load-bearing rule:** services never make value judgements and the brain
never performs I/O. A service that starts scoring inline, or a brain module that
starts calling an API, is a design violation — it breaks G1 (one model) and makes
the brain untestable offline, which breaks the whole `eval/` harness.

### 3.2 Control flow — the canonical run

Source of truth: [`main.py`](./main.py), documented in [`main.md`](./main.md).

```
BOOTSTRAP (outside the class, in __main__)
  0.1  LoggerManager
  0.2  OnboardingManager.run_if_needed()      ← before ConfigManager, so
                                                 SecretBootstrap sees a
                                                 provisioned keyring
  0.3  daemons.enrich.enabled?
         └─ write MAIN_ACTIVE_SENTINEL {pid, ts}   (daemon yields rate limit)
         └─ EnrichDaemonSupervisor.restart()
  0.4  GlobalCacheManager
  0.5  Main(...)

CONSTRUCT (Main.__init__)
  1.1  ConfigManager → .reload()
  1.2  RegistryManager
  1.3  GlobalCacheManager, MetricsLogger
  1.4  BaseManager.__init__ → parent auto-link via registry
  1.5  dry_run resolved from config, propagated explicitly to every manager
  1.6  validate_all()  — ThreadPoolExecutor(3): Radarr ‖ Sonarr ‖ Trakt
                          → ONE consolidated auth line
  1.7  _initialize_managers()   (order below)
  1.8  _validate_managers()     — hard gate on 4 flags

  Manager construction order and why it matters:
    Tautulli      → writes affinity + completion caches everything else reads
    Trakt         → after Tautulli, before Radarr (registry visibility for
                     RadarrOrchestrationManager)
    TraktMovies   → self-registers; consumed during run_relational_pull
    MAL           → self-disables if unauthorized
    Radarr, Sonarr
    Acquisition, Writeback, Calendar        (Phase 3, config-gated)
    SpaceCoordinator                        (Phase 4 capstone)

EXECUTE (Main.run)
  2.0  SizeCalibrator.load_into_model()      — size estimates accurate from P1
  2.1  _start_radarr_library_prefetch()      — daemon thread, ~39s GET /movie
                                                overlapped with phases below
  2.2  PREPARE   tautulli → trakt → radarr → sonarr
  2.3  RUN       tautulli → trakt → mal → sonarr
                 └─ prefetch.join(timeout=90)
                 → radarr
                 Each wrapped: failure records summary.add_error() and continues
  2.4  SizeCalibrator.refresh()              — TTL-guarded, ≈weekly
  2.5  SpaceCoordinator.run()                — unified movie+TV delete plan,
                                                AFTER both libraries scored,
                                                BEFORE the plan summary
  2.6  PlanSummary(...).log()                — read-only ledger roll-up
  2.7  PHASE 3   calendar → acquisition → writeback   (each config-gated)
  2.8  Stats roll-up: {tautulli,radarr,sonarr,trakt}/run_stats
  2.9  DiscordNotifier.send_run_summary()
  2.10 dump_profile() → clear_all_service_flags()
```

**Why Sonarr runs before Radarr:** Sonarr's ~15s wall-clock overlaps the movie
prefetch. Radarr then joins a warm snapshot instead of paying the cold fetch.

**Why SpaceCoordinator is Phase 2.5, not Phase 3:** it needs both libraries
scored and downgraded, but its deletes must land in the Parquet ledger *before*
`PlanSummary` reads it — otherwise a dry run under-reports the plan, defeating G3.

### 3.3 Data contracts

| Contract | Shape | Producer | Consumer |
|---|---|---|---|
| **Feature row** | `contracts/feature_rows.py` | `machine_learning/features/` | all scorers |
| **Score** | `float 0–100`, or `None` | `scoring/` | acquisition, quality, space |
| **Plan** | `contracts/plans.py` | `machine_learning/space/*_planner` | service APPLY layers |
| **Context** | `contracts/context.py` | services | brain (read-only) |
| **Ledger row** | Parquet | `ledger/decision_ledger.py` | `PlanSummary`, eval, web |
| **Run stats** | `<service>/run_stats` in `global_cache` | each service manager | `Main`, Discord |

The `None` score is a documented hazard. See §5.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Manager tree with registry-based parent auto-linking | One logger/config/cache/validator shared process-wide without threading them through every constructor | Explicit DI everywhere — too verbose at this depth |
| D2 | Process-wide singletons keyed `(cls, singleton_key)` | Guarantees one Radarr client, one cache | Per-call construction — duplicate API clients, cache incoherence |
| D3 | Brain is pure, services do I/O | Makes the brain replayable offline; enables `eval/` forward-validation | Inline scoring in services — untestable, and G1 collapses |
| D4 | Parquet + Pandas for persistence | Columnar, typed, fast to re-read, diffable | SQLite (schema migration cost), JSON (no types, slow at scale) |
| D5 | Dry-run still writes the ledger | The plan *is* the deliverable of a dry run (G3) | Dry-run as pure no-op — produces nothing inspectable |
| D6 | Per-service failure isolation | One dead API must not cost the whole run (G4) | Fail-fast — a Trakt outage would block deletion of a full disk |
| D7 | Background prefetch of the Radarr library | ~39s cold fetch overlapped with Sonarr's ~15s + Tautulli/Trakt | Sequential — adds ~39s dead time every run |
| D8 | Onboarding before ConfigManager | `SecretBootstrap` must see a provisioned keyring or it double-prompts | Lazy secret prompt — interactive prompt mid-run |
| D9 | Score `None` ⇒ safe mid-tier, never highest | A prior bug treated `None` as 4K-eligible and grabbed UHD for unscored titles | Treat `None` as 0 — starves genuinely-unscored new content |

---

## 5. Invariants

Each is testable. Violations are bugs, not preferences.

| # | Invariant |
|---|---|
| I1 | **HD-720p is the quality floor.** No path may target SD. |
| I2 | **4K requires score ≥ 70.** |
| I3 | **`score is None` is not eligible for anything above mid-tier** (HD Bluray + WEB). |
| I4 | **`keep-universe` is never deleted** — quality change only. |
| I5 | **bare `universe` is deletable only as last resort**, after all other pools are exhausted. |
| I6 | **`dry_run=True` performs zero APPLY**, but still writes the ledger. |
| I7 | **Cursors persist after each pool operation**, inside `finally` — never only at cycle end. |
| I8 | **One summary line per manager**: `[Name] ✅ N/N: comp✅ comp✅`. Detail at `log_debug`. |
| I9 | **A second consecutive run is a no-op** (idempotence). |
| I10 | **The brain performs no I/O.** No `requests`, no filesystem writes, in `machine_learning/`. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Radarr unreachable | `validate_all` auth probe | Movie phases skipped; TV proceeds | Movies only |
| Sonarr unreachable | `validate_all` | TV phases skipped; **run continues** (Sonarr deliberately *not* in the critical-flag set) | TV only |
| Trakt 429 rate limit | API layer returns `None` (**not** `[]`) | Caller treats as "unknown", not "empty" — no false prune | Watchlist sync |
| Tautulli unreachable | `validate_all` | Watch history falls back to cache; scores go stale, not wrong | Scoring freshness |
| MAL unauthorized | Self-disable at construction | Anime enrichment skipped | Anime metadata |
| Prefetch thread hangs | `join(timeout=90)` | Radarr repair scans do their own live fetch | +39s, no correctness loss |
| Critical manager missing | `_validate_managers()` | `RuntimeError` before Phase 1 | Whole run — intentional |
| Disk full mid-write | Parquet write error | Fail-open on free-space check | Cache write skipped |

**The Trakt `None` vs `[]` distinction is load-bearing.** Returning `[]` on a rate
limit tells the pruner "the watchlist is empty" and it will happily prune
everything. Returning `None` means "unknown — do nothing."

---

## 7. Configuration surface

Top-level keys read by [`main.py`](./main.py) itself. Per-service keys live in
each service's `DESIGN.md` §7.

| Key | Type | Default | Effect |
|---|---|---|---|
| `dry_run` | bool | `False` | Master APPLY gate. Propagated explicitly to every manager. |
| `radarr_movie_library_max_age_s` | int | `900` | Snapshot freshness window for the prefetch. Younger ⇒ reuse, skip live fetch. |
| `daemons.enrich.enabled` | bool | `False` | Gates sentinel write + enrichment-daemon respawn. |
| `movieRootFolders` | list | `[]` | 🔴 **Currently empty** — `classify_movie` bucket assignments are computed then discarded; all movies land in `/data/media/movies/standard`. See §10. |

Identity constants: Radarr instance `standard`, Sonarr instance `720`,
Tautulli group `household`, Trakt user `BuckITrizzd`.

---

## 8. Implemented capabilities

- ✅ Four-layer architecture with registry-based DI and parent auto-linking
- ✅ Phased run lifecycle with per-service failure isolation
- ✅ Parallel auth validation (single consolidated log line)
- ✅ Background Radarr library prefetch with bounded join
- ✅ Unified 100-point watchability scoring shared by acquisition/quality/space
- ✅ Parquet decision ledger + `PlanSummary` roll-up
- ✅ Dry-run producing a complete, persisted plan
- ✅ SpaceCoordinator producing a unified movie+TV delete plan
- ✅ Size calibration model with TTL-guarded refresh
- ✅ Enrichment daemon with sentinel-based rate-limit yielding
- ✅ Onboarding wizard + keyring-backed secret store
- ✅ Discord run summary
- ✅ Offline replay harness ([`support/tools/acquire_preview.py`](./support/tools/acquire_preview.py))

## 9. Planned additions

> **Full backlog:** [`ENHANCEMENTS.md`](./ENHANCEMENTS.md) — 195 items across every
> area, with global IDs, confirmed defects (§4.1) and blocking decisions (§5).
> The table below is the top-level slice only.

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-CORE-01` | **Web interface** — config editing, plan review, run triggering, ledger browsing | Removes the JSON-editing barrier; makes dry-run plans reviewable | L | [`factories/web/DESIGN.md`](./managers/factories/web/DESIGN.md) |
| `GLD-CORE-02` | **Trakt watchlist auto-pruning** from Tautulli history | Closes the loop: watched ⇒ off the watchlist | M | Watched-definition decisions (§10) |
| `GLD-CORE-03` | **`movieRootFolders` resolution** so movie classification buckets take effect | Classification currently computed then discarded | S | Config schema change |
| `GLD-CORE-04` | **Docs-lint in CI** — assert every `.py` has a doc row, every folder has README+DESIGN | Prevents doc rot at this scale | S | Extend [`mirror_docs.py`](./support/tools/mirror_docs.py) |
| `GLD-CORE-05` | **Logging cleanup** — `TraktRegistrar` at INFO; verbose `__init__` lines; sub-manager `load_components` firing during parent `prepare()` | Restores I8 | S | — |
| `GLD-CORE-06` | **`load_summary` population for pre-`prepare()` components** — fixes `instance_manager❌` / `radarr_cache❌` false negatives | Removes misleading run output | S | — |
| `GLD-CORE-07` | **Run history + trend view** | Detect drift in library health over time | M | `GLD-CORE-01` |
| `GLD-CORE-08` | **Structured event bus** replacing ad-hoc `run_stats` keys | Cleaner web streaming, better observability | M | `GLD-CORE-01` |
| `GLD-CORE-09` | **Per-user playlist personalisation surface** | Extends existing playlist brain to per-viewer output | M | `GLD-CORE-01` |
| `GLD-CORE-10` | **Challenger-model promotion pipeline** (GBT shadow → champion) | Currently shadow-only | L | `eval/` forward validation |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Whose watch history counts as "watched" for auto-prune — any household member, or a designated primary? | P2 |
| Q2 | What fraction of a series constitutes "watched" for prune purposes? Last episode? 90% of aired? | P2 |
| Q3 | Should `movieRootFolders` buckets be derived from classification, or explicitly configured per bucket? | P3 |
| Q4 | Does the web layer read the ledger directly, or through a service API? | P1 |
| Q5 | Auth model for the web layer — local-only bind, or real sessions? | P1 |

## 11. Related designs

- [`DESIGN_auto_run_triggering.md`](./DESIGN_auto_run_triggering.md)
- [`DESIGN_performance_audit.md`](./DESIGN_performance_audit.md)
- [`managers/machine_learning/ARCHITECTURE.md`](./managers/machine_learning/ARCHITECTURE.md)
- [`managers/machine_learning/ML_PIPELINE.md`](./managers/machine_learning/ML_PIPELINE.md)
- [`managers/machine_learning/MATH_FOUNDATION.md`](./managers/machine_learning/MATH_FOUNDATION.md)
- [`managers/factories/config/DESIGN_secrets_backend.md`](./managers/factories/config/DESIGN_secrets_backend.md)
- [`support/PERF_BASELINE.md`](./support/PERF_BASELINE.md)
