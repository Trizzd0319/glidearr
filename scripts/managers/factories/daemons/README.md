# daemons

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **daemons**

**Package** — `scripts.managers.factories.daemons`
**Run position** — `main.py` `__main__` block, **before** `Main` is constructed. The daemons themselves run detached, outliving the run.
**One-liner** — Spawn / stop / restart machinery and the single source of truth for every path, sentinel and rate constant shared between the main run and its two background daemons.

---

## Purpose

Two workloads do not belong inside the run:

| Daemon | Why it is detached |
|---|---|
| **Enrichment** ([`support/daemons/enrich_daemon.py`](../../../support/daemons/enrich_daemon.py)) | Trakt enrichment of ~18k titles at 650 endpoint calls per 5-minute window is a multi-hour job. It cannot block a run that should finish in minutes. |
| **Pilot search** ([`support/daemons/pilot_search_daemon.py`](../../../support/daemons/pilot_search_daemon.py)) | A large interactive-search spree ran on a **non-daemon** worker thread, which blocks interpreter exit — a 9k-stub batch would hang the entire run until every indexer search returned. |

This package owns the process lifecycle for both, plus
[`daemon_paths.py`](./daemon_paths.py), which exists so the supervisor and the
daemon can never disagree about where the pid file, stop sentinel, cursor, log or
cache buckets live. That disagreement has happened before — `TraktMovieCacheManager`
used a CWD-relative path while the daemon used an absolute one, and they silently
wrote to two different directories.

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`daemon_paths.py`](./daemon_paths.py) | this file | **Single source of truth**: every path, sentinel, cache bucket, scope list and rate constant. Imports only `pathlib`, so it is always safe and cycle-free to import | ✅ Implemented |
| [`supervisor.py`](./supervisor.py) | this file | `_BaseDaemonSupervisor` + `EnrichDaemonSupervisor` + `PilotSearchDaemonSupervisor` — detached spawn, graceful stop, hard-kill fallback | ✅ Implemented |
| [`pilot_jobs.py`](./pilot_jobs.py) | — | Pilot-search job file enqueue / claim / orphan recovery | ✅ Implemented |
| [`bucket_merge.py`](./bucket_merge.py) | — | Merge per-endpoint cache buckets | ✅ Implemented |
| [`__init__.py`](./__init__.py) | — | Package marker | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_pilot_jobs.py`](./test_pilot_jobs.py) | Enqueue, claim, orphan re-queue |
| [`test_bucket_merge.py`](./test_bucket_merge.py) | Bucket merge semantics |

---

## Entry points

| Symbol | Called by | Semantics |
|---|---|---|
| `EnrichDaemonSupervisor(logger).restart()` | `main.py` when `daemons.enrich.enabled` | **Always** stop-then-spawn. Safe because enrichment is resumable from its cursor. |
| `PilotSearchDaemonSupervisor(logger).ensure_running()` | `main.py` when the pilot daemon is enabled | Spawn **only if none alive**. Never interrupts an in-flight search batch. |
| `.stop(timeout=None)` | Either | Graceful via stop sentinel, hard-kill after `GRACE_STOP_S` |
| `.is_running()` | Either | pid file + liveness probe with PID-reuse rejection |

The `restart` vs `ensure_running` split is deliberate and load-bearing — see
[`DESIGN.md`](./DESIGN.md) §4 D2.

---

## The path contract

Everything below is defined once in [`daemon_paths.py`](./daemon_paths.py) and
imported by both sides.

### Enrichment daemon

| Constant | Path |
|---|---|
| `DAEMON_SCRIPT` | `support/daemons/enrich_daemon.py` |
| `PID_PATH` | `support/cache/trakt/enrich_daemon.pid` |
| `STOP_SENTINEL` | `support/cache/trakt/enrich_daemon.stop` |
| `CURSOR_PATH` | `support/cache/trakt/enrichment_cursor.json` |
| `LOG_PATH` | `support/logs/enrich_daemon.log` |
| `MAIN_ACTIVE_SENTINEL` | `support/cache/trakt/main_run.active` |

### Pilot-search daemon — separate directory on purpose

| Constant | Path |
|---|---|
| `PILOT_DAEMON_SCRIPT` | `support/daemons/pilot_search_daemon.py` |
| `PILOT_QUEUE_DIR` | `support/cache/pilot_search/queue` — `<instance>.json`, newest enqueue wins |
| `PILOT_PROCESSING_DIR` | `support/cache/pilot_search/processing` — claimed jobs; orphans re-queued on start |
| `PILOT_PID_PATH` / `PILOT_STOP_SENTINEL` | `support/cache/pilot_search/` |
| `PILOT_LOG_PATH` | `support/logs/pilot_search_daemon.log` |

### Cache buckets

`MOVIE_BUCKETS` — 8 buckets, file `{tmdb_id}.json.gz`:
`people` (at `trakt/movies`, the pre-existing bucket the watchability scorer reads),
`summary`, `ratings`, `related`, `aliases`, `studios`, `translations`, `lists`.

`SHOW_BUCKETS` — 4 buckets, file `{tvdbId}.json.gz`:
`summary` (genres → genre affinity + cross-medium next-watch), `people` (at
`trakt/shows`, Group-B cast/crew affinity), `ratings` (Group-F critic blend),
`related` (Group-C3 related graph).

`MOVIE_BUCKETS["people"]` is also imported directly by
[`services/trakt/movies/cache.py`](../../services/trakt/movies/cache.py) so the
runtime manager and the daemon provably share one directory.

### People-matrix artifacts

Split into **two** cached artifacts on purpose:

| Artifact | Derivation | Volatility |
|---|---|---|
| `PEOPLE_MATRIX_PATH` | Library-derived forward map | Stable — changes only as the daemon enriches new titles |
| `PEOPLE_AFFINITY_PATH` | Watched-set-derived weights | Volatile — changes every run |

Conflating them would force a full matrix rebuild on every watched-set change.
Incremental sidecars (`PEOPLE_SHOWS_SIDECAR`, `PEOPLE_MOVIES_SIDECAR`) store
`{id: mtime_ns}` so a rebuild re-reads only files the daemon actually rewrote —
the show half is ~6.3k individually-gzipped credit files, ~25s cold.

---

## Data in / data out

| Direction | Source/Sink | Payload |
|---|---|---|
| IN | pid files | Liveness check |
| IN | `os.environ` | Inherited, plus `GLIDEARR_DAEMON=1` for the child |
| OUT | Detached subprocess | The daemon itself |
| OUT | Stop sentinels | Cooperative shutdown signal |
| OUT | pid files | Recorded child pid |
| OUT | Daemon log files | Child stdout+stderr, handle leaked to the child intentionally |

The supervisor performs **no FETCH, no CACHE, no APPLY**. The daemons it spawns
do all three.

---

## Navigation

- **Up:** [`factories/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Daemon bodies:** [`support/daemons/`](../../../support/daemons/README.md)
- **Related:** [`services/trakt/movies/cache.py`](../../services/trakt/movies/cache.py) · [`machine_learning/people_matrix/`](../../machine_learning/people_matrix/README.md)
