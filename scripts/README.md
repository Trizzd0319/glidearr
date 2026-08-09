# scripts

> Breadcrumb: **glidearr** › **scripts**

**Package** — `scripts`
**Run position** — Root. [`main.py`](./main.py) is the process entry point.
**One-liner** — The whole Glidearr application: a decision layer over Sonarr, Radarr, Plex, Tautulli, Trakt, MAL and MDBList that resolves acquisition, quality and space against observed household behaviour.

---

## Purpose

`scripts/` contains four layers, in strict dependency order. Read
[`DESIGN.md`](./DESIGN.md) §3 for the architecture and the rules that govern
what may call what.

| Layer | Folder | Rule |
|---|---|---|
| Entry | [`main.py`](./main.py) | Owns the lifecycle. Makes no value judgements. |
| Factory | [`managers/factories/`](./managers/factories/README.md) | Infrastructure only. Knows nothing about media. |
| Service | [`managers/services/`](./managers/services/README.md) | Thin adapters. FETCH / CACHE / APPLY. |
| Brain | [`managers/machine_learning/`](./managers/machine_learning/README.md) | Pure functions over feature rows. No I/O. |
| Support | [`support/`](./support/README.md) | Utilities, operator tools, daemons, notifications, config, profiles. |

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`main.py`](./main.py) | [`main.md`](./main.md) | Process entry point; builds the factory layer, runs parallel auth, constructs every service manager in dependency order, drives the phased run | ✅ Implemented |
| [`__init__.py`](./__init__.py) | — | Package marker (empty) | ✅ Implemented |

---

## Subpackages

| Folder | Role |
|---|---|
| [`managers/`](./managers/README.md) | Factory, service, brain and orchestration layers |
| [`support/`](./support/README.md) | Utilities, tools, daemons, notifications, config, profiles, knowledge |
| [`hooks/`](./hooks/README.md) | Git / lifecycle hooks |

---

## Entry points

| Invocation | Effect |
|---|---|
| `python scripts/main.py` | The full run. Honours `dry_run` from config. |
| `python scripts/support/setup/onboarding.py` | First-run interactive setup |
| `python scripts/support/tools/acquire_preview.py` | Offline replay of the acquisition pipeline against on-disk caches |
| `python scripts/support/daemons/enrich_daemon.py` | Enrichment daemon (normally supervisor-spawned) |

See [`support/tools/README.md`](./support/tools/README.md) for the full operator
tool catalogue.

---

## Data in / data out

| Direction | Source/Sink | Payload |
|---|---|---|
| IN | Radarr API | Movie library, quality profiles, custom formats, history, tags |
| IN | Sonarr API | Series, episodes, episode files, history, tags |
| IN | Plex API | Libraries, collections, playlists, on-deck, users, metadata |
| IN | Tautulli API | Watch history, users, devices, transcode decisions |
| IN | Trakt API | Watchlist, ratings, history, progress, recommendations, people |
| IN | MAL / MDBList | Anime IDs, list membership, age/certification data |
| OUT | Radarr / Sonarr | Adds, deletes, tag changes, quality-profile changes, searches, moves |
| OUT | Plex | Collections, playlists |
| OUT | Trakt / MAL | Collection + history writeback |
| OUT | Discord | Run summary |
| OUT | Parquet ledger | Decision rows |
| OUT | `global_cache` | Snapshots, run stats, cursors |

---

## Documentation map

| Doc | Covers |
|---|---|
| [`DOCS_CONVENTIONS.md`](./DOCS_CONVENTIONS.md) | The normative spec every README/DESIGN in this repo follows |
| [`DESIGN.md`](./DESIGN.md) | Top-level architecture, run order, invariants, roadmap |
| [`ENHANCEMENTS.md`](./ENHANCEMENTS.md) | **The backlog.** Every improvement and confirmed defect found across the repo, with global IDs, effort and blocking decisions |
| [`main.md`](./main.md) | The entry point in detail |
| [`DESIGN_auto_run_triggering.md`](./DESIGN_auto_run_triggering.md) | When and how a run is triggered |
| [`DESIGN_performance_audit.md`](./DESIGN_performance_audit.md) | Performance findings and budget |

---

## Where the work list lives

Improvements are recorded in **two** places, deliberately:

- Each folder's `DESIGN.md` **§9** — what's outstanding *in that folder*, with
  the context needed to act on it.
- [`ENHANCEMENTS.md`](./ENHANCEMENTS.md) — the **rollup** across the whole repo,
  sorted by value, with confirmed defects called out separately.

Start with [`ENHANCEMENTS.md`](./ENHANCEMENTS.md) §2 (tabled), §3 (top 15) and
§4.1 (confirmed defects).

---

## Navigation

- **Up:** repository root ([`../README.md`](../README.md))
- **Down:** [`managers/`](./managers/README.md) · [`support/`](./support/README.md) · [`hooks/`](./hooks/README.md)
- **Standards:** [`DOCS_CONVENTIONS.md`](./DOCS_CONVENTIONS.md)
- **Backlog:** [`ENHANCEMENTS.md`](./ENHANCEMENTS.md)
