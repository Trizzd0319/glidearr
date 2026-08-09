# services

> Breadcrumb: [glidearr](../../..) › [scripts](../../README.md) › [managers](../README.md) › **services**

**Package** — `scripts.managers.services`
**Run position** — Constructed in `Main._initialize_managers()`, executed across phases 1–3 of `Main.run()`.
**One-liner** — The I/O adapter layer: every external API lives behind one of these managers. They FETCH, CACHE and APPLY — they never decide.

---

## Purpose

Each service manager owns exactly one external system and presents it to the rest
of Glidearr as cached, normalised data. The layering rule from
[`managers/DESIGN.md`](../DESIGN.md) applies in full:

> **Services never make value judgements.** Scoring, ranking, thresholds and
> planning live in [`machine_learning/`](../machine_learning/README.md). A service
> gathers rows, asks the brain, and applies the answer.

A service that starts ranking inline creates a second scoring model, which is the
exact failure the architecture exists to prevent.

---

## Construction and run order

Order is load-bearing. From [`main.md`](../../main.md):

| # | Manager | Why here |
|---|---|---|
| 1 | [`TautulliManager`](./tautulli/README.md) | **First** — writes the affinity and completion caches everything downstream reads |
| 2 | [`TraktManager`](./trakt/README.md) | After Tautulli, before Radarr — its registry entry must be visible to `RadarrOrchestrationManager` |
| 3 | `TraktMoviesManager` | Self-registers; consumed during `run_relational_pull` |
| 4 | [`MALManager`](./mal/README.md) | Self-disables if unauthorized |
| 5 | [`RadarrManager`](./radarr/README.md) | Movies |
| 6 | [`SonarrManager`](./sonarr/README.md) | TV |
| 7 | [`AcquisitionManager`](./acquisition/README.md) | Phase 3, config-gated |
| 8 | [`WritebackManager`](./writeback/README.md) | Phase 3, config-gated |
| 9 | [`CalendarManager`](./calendar/README.md) | Phase 3, config-gated |
| 10 | [`SpaceCoordinatorManager`](./coordinator/README.md) | Phase 4 capstone |

**Execution** order differs from construction order:

```
prepare:  tautulli → trakt → radarr → sonarr
run:      tautulli → trakt → mal → sonarr → [join prefetch] → radarr
          → SpaceCoordinator (2.5) → PlanSummary
          → calendar → acquisition → writeback (Phase 3)
```

Sonarr runs **before** Radarr so its ~15s wall-clock overlaps the background
`GET /movie` prefetch.

---

## Subpackages

### Source-of-truth services (FETCH-heavy)

| Folder | External system | Role |
|---|---|---|
| [`tautulli/`](./tautulli/README.md) | Tautulli | Watch history, users, devices, transcode decisions |
| [`trakt/`](./trakt/README.md) | Trakt | Watchlist, ratings, history, progress, people, recommendations, universe |
| [`plex/`](./plex/README.md) | Plex | Libraries, collections, playlists, on-deck, metadata, users |
| [`mal/`](./mal/README.md) | MyAnimeList | Anime ID bridging |
| [`mdblist/`](./mdblist/README.md) | MDBList | Age / certification data |

### Library-control services (APPLY-heavy)

| Folder | External system | Role |
|---|---|---|
| [`radarr/`](./radarr/README.md) | Radarr | Movies: cache, quality, repair, storage, sync, monitoring, validator |
| [`sonarr/`](./sonarr/README.md) | Sonarr | TV: series, episodes, cache, quality, repair, storage, sync, monitoring |

### Cross-cutting services

| Folder | Role |
|---|---|
| [`acquisition/`](./acquisition/README.md) | Candidate gathering → resolution → scoring → add |
| [`coordinator/`](./coordinator/README.md) | Unified movie+TV space decisions, hybrid universe acquisition, saga retention |
| [`routing/`](./routing/README.md) | Instance selection, UHD reconciliation |
| [`writeback/`](./writeback/README.md) | Push state back to Trakt collection/history and MAL lists |
| [`calendar/`](./calendar/README.md) | Upcoming-release awareness |
| [`backup/`](./backup/README.md) | Backup gating |

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`renamer.py`](./renamer.py) | — | File/folder renaming helper shared across services | ✅ Implemented |
| [`_intent_index.py`](./_intent_index.py) | — | Intent identity index — stable keys for acquisition/quality intents | ✅ Implemented |
| [`__init__.py`](./__init__.py) | — | Package marker | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_intent_identity.py`](./test_intent_identity.py) | Intent key stability in [`_intent_index.py`](./_intent_index.py) |

---

## The uniform service shape

Radarr and Sonarr are the reference implementations. Both decompose the same way,
which makes either navigable once you know the other:

| Subfolder | Responsibility |
|---|---|
| `api/` | HTTP client + auth |
| `cache/` | Snapshot persistence, enrichment, relational tables |
| `instance/` | Multi-instance handling |
| `monitoring/` | What to monitor, scheduling, rules |
| `quality/` | Profile selection, custom formats, file sizes, adjustments |
| `repair/` | Anomalies, orphans, metadata, tags, storage drift |
| `storage/` | Space, selection, deletion, relocation |
| `sync/` | Push config to the *arr: profiles, formats, naming, folders, tags |
| `validator/` | Auth, health, keys, cache validity |
| `orchestration/` | Intra-service sequencing |
| `movies/` or `series/` + `episodes/` | Domain entities |

---

## Data in / data out

| Direction | Source/Sink | Payload |
|---|---|---|
| IN | Radarr, Sonarr, Plex, Tautulli, Trakt, MAL, MDBList | Libraries, history, ratings, metadata |
| OUT | Radarr / Sonarr | Adds, deletes, tags, profile changes, searches, moves |
| OUT | Plex | Collections, playlists |
| OUT | Trakt / MAL | Collection + history writeback |
| OUT | `global_cache` | Snapshots, enriched Parquet, run stats, cursors |
| OUT | Decision ledger | Rows written by the planners these services drive |

---

## Navigation

- **Up:** [`managers/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Run order source:** [`main.md`](../../main.md)
- **The brain they call:** [`machine_learning/`](../machine_learning/README.md)
