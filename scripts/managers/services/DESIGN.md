# services — Design

> Breadcrumb: [glidearr](../../..) › [scripts](../../README.md) › [managers](../README.md) › **services**

**Package** — `scripts.managers.services`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [`managers/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Glidearr integrates seven external systems, each with a different API shape, auth
model, rate limit, latency profile and failure mode. Three problems follow.

**Rate and latency.** A cold Radarr `GET /movie` is ~39s. Trakt allows 1000 calls
per 5 minutes, shared with a background daemon. Tautulli history for a large
library is thousands of rows. The brain needs *all* of it, every run. Naive
fetching makes the run unusable.

**Impedance mismatch.** Radarr thinks in movies and quality profiles. Sonarr
thinks in series, seasons and episodes. Trakt thinks in slugs and watch events.
Plex thinks in rating keys. Tautulli thinks in sessions. The brain must think in
one feature-row shape, or every scorer re-implements the joins.

**Destructive asymmetry.** Reads are safe and retryable. Writes delete files.
Both flow through the same managers, and the difference must be structural, not
a matter of care.

The service layer resolves all three: it caches aggressively, normalises to a
common shape, and isolates APPLY behind explicit gates — while holding **zero**
decision logic.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One manager per external system. Nothing else talks to that API. |
| G2 | Services normalise; the brain never sees a vendor payload shape. |
| G3 | Every APPLY is gated on `dry_run`. |
| G4 | A dead service degrades scope, not the run. |
| G5 | Multi-instance (two Radarrs, two Sonarrs) is first-class. |
| G6 | The cache is the working set — a run against warm caches does minimal I/O. |
| G7 | Radarr and Sonarr share one internal shape, so either is navigable from the other. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Owning indexers or download clients | Sonarr/Radarr do that well. |
| N2 | Real-time reaction | Batch. Minutes, not milliseconds. |
| N3 | Generic API abstraction across vendors | The systems differ too much; a shared abstraction would leak everywhere. |
| N4 | Any scoring or ranking | Brain layer. Non-negotiable. |

---

## 3. Architecture

### 3.1 The service contract

```
        ┌──────────────────────────────────────────────┐
        │  Service manager                             │
        │                                              │
        │  FETCH ──► normalise ──► CACHE               │
        │                            │                 │
        │                            ▼                 │
        │                    feature rows              │
        │                            │                 │
        │                            ▼                 │
        │              ┌─────── machine_learning ──────┼──► Plan
        │              │        (pure, no I/O)         │
        │              ▼                               │
        │           APPLY ◄── dry_run gate ────────────┤
        └──────────────────────────────────────────────┘
```

A service is allowed to know *how* to delete a movie file. It is not allowed to
know *which* movie should be deleted.

### 3.2 Control flow across the run

```
PHASE 1  prepare
    tautulli → trakt → radarr → sonarr
    materialise critical subcomponents, warm caches, one summary line each

PHASE 2  run
    tautulli   watch history, affinity, completions      ← everything reads this
    trakt      watchlist, ratings, progress, people
    mal        anime ID bridge (self-disabled if unauthorized)
    sonarr     series/episode sync, quality, repair, space   ~15s
       └─ join radarr-movie-prefetch (timeout 90s)
    radarr     movie sync, quality, repair, space

PHASE 2.5  coordinator
    unified movie+TV delete plan → Parquet ledger
    (before PlanSummary reads it — otherwise a dry run under-reports)

PHASE 3  gated capabilities
    calendar → acquisition → writeback
```

Each Phase-2 manager is wrapped in try/except that records
`summary.add_error(...)` and continues — G4.

### 3.3 The uniform Radarr/Sonarr shape (G7)

Both decompose identically:

| Subfolder | Verb | Responsibility |
|---|---|---|
| `api/` | FETCH | HTTP client, auth |
| `cache/` | CACHE | Snapshots, enrichment, relational tables |
| `instance/` | — | Multi-instance resolution (G5) |
| `monitoring/` | APPLY | Monitor flags, scheduling, rules |
| `quality/` | APPLY | Profile selection, custom formats, sizes |
| `repair/` | APPLY | Anomalies, orphans, metadata, tags |
| `storage/` | APPLY | Space, selection, deletion, relocation |
| `sync/` | APPLY | Push profiles/formats/naming/folders/tags to the *arr |
| `validator/` | — | Auth, health, keys, cache validity |
| `orchestration/` | — | Intra-service sequencing |

The parallel is deliberate: learn one, navigate both. Where they diverge —
Sonarr has `series/` + `episodes/` and an extra sharding layer; Radarr has
`movies/` — the divergence is the domain, not the design.

### 3.4 Data contracts

| Contract | Producer | Consumer |
|---|---|---|
| Enriched Parquet (`_series_enriched`, `_movies_enriched`, `_episodes_enriched`, `_people_enriched`) | `cache/` | brain features |
| Feature rows | `machine_learning/features/` | scorers |
| Plans | `machine_learning/space/*_planner` | service APPLY layers |
| `<service>/run_stats` | each manager | `Main`, Discord |
| Intent keys | [`_intent_index.py`](./_intent_index.py) | acquisition + quality, for idempotence |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | One manager per external system | G1 — a single place to change when an API does | Shared HTTP layer |
| D2 | Services normalise to feature rows | G2 — otherwise every scorer re-implements vendor joins | Pass raw payloads |
| D3 | Tautulli first | It writes the affinity/completion caches every downstream scorer reads | Alphabetical / arbitrary |
| D4 | Trakt before Radarr | `RadarrOrchestrationManager` looks Trakt up in the registry during `run_relational_pull` | Any order |
| D5 | Sonarr before Radarr | Overlaps Sonarr's ~15s with the background `GET /movie` prefetch | Radarr first |
| D6 | SpaceCoordinator at 2.5, not Phase 3 | Needs both libraries scored, but must write the ledger before `PlanSummary` reads it | Phase 3 |
| D7 | Per-service try/except in `Main.run()` | G4 — a Trakt outage must not block deleting from a full disk | Fail fast |
| D8 | Sonarr **not** in the critical-flag set | A Radarr-only run is useful; a Sonarr failure should not abort | Gate on all services |
| D9 | Radarr/Sonarr share a folder shape | G7 — halves the navigation cost of the two largest subsystems | Shape each to its API |
| D10 | Phase-3 managers config-gated | Acquisition/writeback/calendar are opt-in capabilities, not core | Always on |
| D11 | MAL self-disables when unauthorized | Anime enrichment is a nice-to-have; a missing MAL token must not warn every run | Hard failure |
| D12 | Intent index for idempotence | Re-running must not re-issue the same acquisition or quality change | Timestamp comparison |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No service contains scoring, ranking or threshold logic. |
| I2 | Every APPLY checks `dry_run` first. |
| I3 | Only one manager talks to any given external API. |
| I4 | A service failure records an error and returns; it never raises past `Main`. |
| I5 | A rate-limited FETCH returns `None`, never `[]`. |
| I6 | HD-720p is the floor; no service targets SD. |
| I7 | 4K requires score ≥ 70; `score is None` gets the safe mid-tier profile. |
| I8 | `keep-universe` is never deleted. Bare `universe` is last-resort only. |
| I9 | A second consecutive run is a no-op. |
| I10 | Cursors persist after each pool operation, in `finally`. |

**I5 is the one most easily broken by a well-meaning refactor.** An API wrapper
that returns `[]` on a 429 tells the cache "this is empty," which the cache
faithfully stores, and every downstream consumer then acts on an empty library.

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Radarr unreachable | `validate_all` auth probe | Movie phases skipped | Movies |
| Sonarr unreachable | `validate_all` | TV phases skipped, **run continues** (D8) | TV |
| Trakt 429 | API returns `None` | Cache serves last-good; no false prune | Watchlist freshness |
| Tautulli unreachable | `validate_all` | Watch history falls back to cache — scores stale, not wrong | Scoring freshness |
| Plex unreachable | Per-call | Collections/playlists skipped | Presentation |
| MAL unauthorized | Construction | Self-disables silently | Anime metadata |
| MDBList unreachable | Per-call | Age/cert data absent; gates fall back to safe defaults | Certification gating |
| Prefetch times out | `join(timeout=90)` | Radarr does its own live fetch | +39s |
| Mid-run disk full | Write error | Free-space check fails open | Cache write |
| Partial APPLY (some deletes land, then failure) | **None** | Ledger may not reflect actual state | 🟡 Drift until next run's repair scan |
| Two instances share storage | `shared_storage.py` | Cross-instance dedup and move logic | Handled |

**Row 10 is the residual risk.** There is no transaction across a batch of
deletes. The mitigation is the `repair/` subsystem, which reconciles actual state
against expected on the next run — eventual consistency rather than atomicity.

---

## 7. Configuration surface

Per-service keys live in each service's own `DESIGN.md` §7. Cross-cutting:

| Key | Effect |
|---|---|
| `dry_run` | Master APPLY gate for every service |
| `sonarr_instances` / `radarr_instances` | Instance maps + `default_instance` |
| `free_space_limit` | Space floor driving the coordinator |
| `movieRootFolders` | 🔴 Empty — movie classification buckets discarded |
| Phase-3 gates | `acquisition`, `writeback`, `calendar` enablement |

Identities: Radarr `standard`, Sonarr `720`, Tautulli group `household`, Trakt
user `BuckITrizzd`.

---

## 8. Implemented capabilities

- ✅ Seven external systems behind dedicated managers
- ✅ Uniform Radarr/Sonarr internal shape
- ✅ Multi-instance support with per-instance caches and shared-storage handling
- ✅ Enriched Parquet snapshots feeding the brain
- ✅ Per-service failure isolation
- ✅ `dry_run` honoured across every APPLY path
- ✅ Unified movie+TV space coordination
- ✅ Acquisition pipeline (gather → resolve → score → add) with offline replay
- ✅ Writeback to Trakt collection/history and MAL lists
- ✅ Repair subsystems reconciling drift on each run
- ✅ Intent index for idempotence
- ✅ Cross-instance dedup and relocation

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **🔴 Trakt watchlist auto-pruning** from Tautulli history | Closes the loop: watched ⇒ off the watchlist. Blocked on the watched-definition decisions in §10 | M | Q1, Q2 |
| P2 | **🔴 Resolve `movieRootFolders`** so `classify_movie` buckets take effect | Movie classification is computed then discarded today | S | Config schema |
| P3 | **Service-purity hook** — AST-detect scoring/thresholds inside `services/` | I1 is review-enforced only, unlike brain purity | M | [`hooks/`](../../hooks/DESIGN.md) |
| P4 | **Transactional APPLY batching** with a rollback journal | Closes §6 row 10 beyond eventual repair | L | — |
| P5 | **Unified service health surface** — one reachability/latency/rate-budget view | Currently spread across per-service validators | M | [`web/`](../factories/web/DESIGN.md) |
| P6 | **Retry-with-backoff policy** shared across API clients | Each client handles transients differently today | M | — |
| P7 | **Circuit breaker** per service — stop hammering an API that is clearly down | Faster degradation, less log noise | M | P6 |
| P8 | **Plex playlist writeback preview** before publish | Bad playlists currently only visible after the fact | M | [`web/`](../factories/web/DESIGN.md) |
| P9 | **Per-service rate budget accounting** in run stats | Only Trakt's budget is explicitly managed | S | P5 |
| P10 | **Webhook ingestion** — Sonarr/Radarr/Plex events trigger targeted work | Event-driven instead of purely batch | L | Event bus |
| P11 | **`renamer.py` documentation + tests** — it is shared across services and currently undocumented | Shared, untested, and touches filenames | S | — |
| P12 | **Instance-level failure isolation** — one dead Radarr instance shouldn't skip the others | Failure is currently service-level, not instance-level | M | G5 |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Whose watch history counts as "watched" for auto-prune — any household member, or a designated primary? | P1 |
| Q2 | What fraction of a series is "watched" for prune purposes — last aired episode, 90%, or all? | P1 |
| Q3 | Should `movieRootFolders` be derived from classification or configured per bucket? | P2 |
| Q4 | Should Sonarr join the critical-flag set now that TV is a large share of the library? | — |
| Q5 | Is eventual repair sufficient for partial-APPLY drift, or is a journal warranted? | P4 |
| Q6 | Should services expose a uniform `health()` contract, or keep per-service validators? | P5 |

## 11. Related designs

- [`managers/DESIGN.md`](../DESIGN.md) — the layering rule
- [`machine_learning/ARCHITECTURE.md`](../machine_learning/ARCHITECTURE.md) — the brain they call
- [`coordinator/*.md`](./coordinator/) — cross-service design notes
- [`plex/DESIGN_plex_service.md`](./plex/DESIGN_plex_service.md) · [`plex/DESIGN_personal_playlists.md`](./plex/DESIGN_personal_playlists.md)
- [`scripts/DESIGN_performance_audit.md`](../../DESIGN_performance_audit.md)
