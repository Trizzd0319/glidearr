# daemons — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **daemons**

**Package** — `scripts.managers.factories.daemons`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [`support/daemons/`](../../../support/daemons/README.md)

---

## 1. Problem statement

Two workloads are structurally incompatible with a batch run that should finish
in minutes.

**Trakt enrichment.** Trakt's hard limit is 1000 calls per 5-minute window. Each
movie costs `len(scope)` endpoint calls — with the default 7-item scope, one
movie is seven calls. Enriching a library of ~18k titles is therefore hours of
wall-clock spent almost entirely asleep. It cannot live in the run.

**Pilot interactive search.** Large batches previously ran on an in-process
worker thread that was **not** a daemon thread. Non-daemon threads block
interpreter exit, so a 9k-stub spree hung the entire run until every indexer
search returned.

Detaching both introduces four new problems this package exists to solve:

1. **Rate-limit contention.** The daemon and the main run share one Trakt budget.
   Uncoordinated, they starve each other.
2. **Path divergence.** Two processes writing "the cache" must agree byte-for-byte
   on where it is. They previously did not — the runtime manager used a
   CWD-relative path and the daemon an absolute one.
3. **Process survival on Windows.** `DETACHED_PROCESS` alone does **not** remove a
   child from an IDE's Job Object, so a "detached" daemon died seconds after the
   PyCharm run window closed.
4. **Log clobbering.** A daemon that incidentally builds a `LoggerManager` would
   rotate and overwrite the orchestrator's run log.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | The daemon yields the Trakt rate window to an active run, and resumes automatically. |
| G2 | A crashed main can never pause the daemon forever. |
| G3 | One definition of every shared path. Divergence must be impossible by construction. |
| G4 | A spawned daemon genuinely outlives the parent, including under an IDE. |
| G5 | Stopping is cooperative first, forceful only after a bounded grace window. |
| G6 | Never signal an unrelated process that reused a PID. |
| G7 | Restarting the enrichment daemon is always safe; restarting the pilot daemon mid-batch is not. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | A general job scheduler | Two known daemons with different semantics. |
| N2 | Cross-host coordination | Single host. |
| N3 | IPC beyond files | pid files, sentinels and JSON job files are sufficient and survive a crash. |
| N4 | Guaranteed-once job delivery | Pilot jobs are idempotent; newest enqueue wins. |

---

## 3. Architecture

### 3.1 Component map

```
daemon_paths.py  ─── imports only pathlib ───────────────┐
   every path, sentinel, bucket, scope, rate constant    │  G3
        ▲                    ▲                    ▲      │
        │                    │                    │      │
   supervisor.py      enrich_daemon.py    pilot_search_daemon.py
        │                (support/)            (support/)
        │
   _BaseDaemonSupervisor
     ├── EnrichDaemonSupervisor        → restart()
     └── PilotSearchDaemonSupervisor   → ensure_running()
```

`daemon_paths.py` importing nothing but `pathlib` is deliberate: it can be
imported from anywhere, at any point in startup, with zero cycle risk.
[`services/trakt/movies/cache.py`](../../services/trakt/movies/cache.py) imports
`MOVIE_BUCKETS["people"]` from it for exactly that reason.

### 3.2 Control flow — rate-limit handshake (G1, G2)

```
main.py __main__:
    daemons.enrich.enabled?
        write MAIN_ACTIVE_SENTINEL  {pid, ts}
        EnrichDaemonSupervisor.restart()
        …run…
    finally:
        remove MAIN_ACTIVE_SENTINEL          ← always, even on exception

enrich_daemon loop:
    sentinel present?
        AND pid alive?
        AND age < MAIN_ACTIVE_MAX_AGE_S (1800)?
            → pause, re-check every MAIN_ACTIVE_POLL_S (5s)
    else → work
```

Three independent conditions must all hold for the daemon to stay paused. Any
one failing releases it:

- **File removed** — the normal path, via `finally`.
- **PID dead** — main crashed hard without unwinding.
- **Age > 30 min** — backstop against a sentinel whose PID got reused by a live
  process, which would otherwise pass the liveness check indefinitely.

That is G2: three layers, so no single failure pauses the daemon forever.

### 3.3 Control flow — detached spawn (G4)

```
Windows:  CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB
POSIX:    start_new_session=True

stdin  = DEVNULL
stdout = open(log_path, "a")     ← handle leaked to the child on purpose
stderr = STDOUT
close_fds = True
cwd    = REPO_ROOT               ← so `scripts.*` imports resolve as in main.py
env    = {**os.environ, "GLIDEARR_DAEMON": "1"}
```

`CREATE_BREAKAWAY_FROM_JOB` is the non-obvious one. IDEs run the script inside a
Job Object with `KILL_ON_JOB_CLOSE`; `DETACHED_PROCESS` does **not** remove a
child from that job. Without breakaway, the daemon dies when the IDE tears the
run down.

If the launcher's job forbids breakaway, `_popen` raises `OSError` and the
supervisor retries without the flag, logging explicit guidance to launch from a
terminal or Task Scheduler for a persistent daemon. Degraded, not broken.

`GLIDEARR_DAEMON=1` redirects any `LoggerManager` the child incidentally builds
(e.g. via `ConfigLoader`) to a daemon-owned sink, so it never rotates or
clobbers the orchestrator's run log.

### 3.4 Control flow — stop (G5, G6)

```
stop(timeout=GRACE_STOP_S):
    pid = read_pid()
    not alive? → cleanup, return True
    write stop sentinel
    poll every _poll_interval_s up to timeout
        gone?  → cleanup, return True
    _hard_kill(pid)                    ← taskkill /F /T | SIGTERM→SIGKILL
    cleanup                            ← ALWAYS; no pid/sentinel left behind
```

**PID-reuse rejection (G6):** liveness is not just "does this PID exist."

- Windows: `tasklist /FI "PID eq N"` and the row must contain `python`.
- POSIX: `os.kill(pid, 0)`, then confirm via `/proc/{pid}/cmdline` that it
  matches `_cmd_match` (`enrich_daemon` / `pilot_search_daemon`) or `python`.
- `PermissionError` is treated as **alive** — a process we can't signal is still
  a process.

### 3.5 Pilot-search job flow

```
run_pilot_search(batch):
    len(batch) > PILOT_SPILL_THRESHOLD (10)?
        → write PILOT_QUEUE_DIR/<instance>.json   (newest enqueue wins)
          PilotSearchDaemonSupervisor.ensure_running()
        → daemon claims into PILOT_PROCESSING_DIR
          orphans in processing/ are re-queued on daemon start
    else
        → run in-process
```

`PILOT_IDLE_EXIT_S` (1800) means an idle daemon exits rather than lingering; the
next enqueue re-spawns it via `ensure_running()`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | `daemon_paths.py` imports only `pathlib` | G3 — importable from anywhere, zero cycle risk. This is why the runtime Trakt cache manager can share it | Duplicate constants |
| D2 | **`restart()` for enrichment, `ensure_running()` for pilot** | G7. Enrichment is cursor-resumable, so a restart costs nothing. A pilot restart would abandon an in-flight indexer batch | One policy for both |
| D3 | Three-condition sentinel release | G2 — file removal, PID liveness and a 30-min age cap, so no single failure pauses the daemon forever | File presence alone |
| D4 | `CREATE_BREAKAWAY_FROM_JOB` with `OSError` fallback | G4 under an IDE, degrading gracefully with actionable guidance when the job forbids it | `DETACHED_PROCESS` alone |
| D5 | `GLIDEARR_DAEMON=1` | Redirects the child's incidental logger; otherwise it rotates the run log | Hope the child never builds one |
| D6 | `cwd=REPO_ROOT` | Child `scripts.*` imports resolve exactly as `main.py`'s do | Inherit CWD |
| D7 | Separate directories per daemon | The two never collide on pid / stop / log | Shared directory with prefixes |
| D8 | Stop sentinel before signals | G5 — the daemon finishes its current unit and persists its cursor | `SIGTERM` first |
| D9 | `_cleanup()` unconditionally after stop | A stale sentinel would pause the *next* daemon instantly | Clean only on success |
| D10 | PID-reuse rejection via cmdline match | G6 — hard-killing an unrelated program is unacceptable | `os.kill(pid, 0)` alone |
| D11 | `SAFE_THROUGHPUT_CALLS = 650` of Trakt's 1000/5min | 35% headroom for the main run and retries. The documented conservative floor (500) is recorded but not operative | Run at the limit |
| D12 | `SLEEP_SECONDS = 306` — just over `RATE_WINDOW_S` (300) | Guarantees the window has rolled before the next cycle | Sleep exactly 300 |
| D13 | People-matrix split into forward-map + affinity artifacts | Library-derived is stable, watched-set-derived is volatile; conflating forces a full rebuild every run | One artifact |
| D14 | `{id: mtime_ns}` incremental sidecars | ~6.3k gzipped show-credit files is ~25s cold; re-read only what moved | Full re-read |
| D15 | `PILOT_INTERACTIVE_WORKERS = 3` < `PILOT_SEARCH_WORKERS = 6` | Interactive searches hit the indexer synchronously; 6-at-once makes a single indexer time out and return false `no_results` | Match the JIT worker count |
| D16 | `PILOT_SEARCH_BATCH = 100` episodeIds per command | A 9k-stub spree posts ~91 commands to Sonarr's queue, not 9k. Profiles are still set per-series first, so each grab honours its own tier | One command per episode |
| D17 | Queue is `<instance>.json`, newest wins | N4 — jobs are idempotent; a stale batch has no value | Append-only queue |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Supervisor and daemon read every shared path from `daemon_paths.py`. |
| I2 | `MAIN_ACTIVE_SENTINEL` is removed in a `finally` block. |
| I3 | A dead or >30-min-old sentinel never pauses the daemon. |
| I4 | `stop()` leaves no pid file and no stop sentinel. |
| I5 | A PID is only signalled after confirming it is our daemon. |
| I6 | The pilot daemon is never restarted mid-batch. |
| I7 | Cursors are persisted after each pool operation, in `finally` — never only at cycle end. |
| I8 | Daemon stdout/stderr goes to its own log, never the run log. |
| I9 | Endpoint calls stay under `SAFE_THROUGHPUT_CALLS` per `RATE_WINDOW_S`. |

**I7 restates a hard-won lesson.** An earlier `enrich_daemon` saved state only at
cycle completion, so an interrupt lost all mid-cycle progress. Save after each
pool operation, inside `finally`.

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Main crashes without unwinding | Sentinel PID dead | Daemon resumes on next poll (5s) | ≤5s pause |
| Sentinel PID reused by a live process | Age > `MAIN_ACTIVE_MAX_AGE_S` | Daemon resumes after ≤30 min | Up to 30 min lost throughput |
| Daemon ignores the stop sentinel | Grace window expires | `taskkill /F /T` or `SIGTERM`→`SIGKILL` | Cursor may lag one unit |
| Job object forbids breakaway | `OSError` on `_popen` | Retry without the flag + warning naming the fix | Daemon dies with the IDE window |
| pid file unwritable | `OSError` | Warned; daemon runs but is unmanageable — `is_running()` returns `False`, so the next `restart()` spawns a **second** instance | 🟡 Duplicate daemons |
| Log file unopenable | `open()` raises | Spawn fails | Daemon absent |
| Daemon crashes on its own | **None** | No supervision until the next run's `restart()` / `ensure_running()` | Enrichment stalls silently |
| Orphaned pilot job in `processing/` | Daemon start scan | Re-queued | None |
| Trakt 429 despite headroom | Daemon-side | Cache generator returns `None`, last-good preserved | None |
| Two supervisors race on spawn | **None** | Both may spawn; the second overwrites the pid file | 🟡 Orphaned first daemon |

**Rows 5, 7 and 10 are the unguarded ones.** Row 7 is the most consequential: a
daemon that dies mid-enrichment goes unnoticed until the next run, and the only
symptom is scores quietly failing to improve.

---

## 7. Configuration surface

| Key | Type | Default | Effect |
|---|---|---|---|
| `daemons.enrich.enabled` | bool | `false` | Gates the sentinel write and `restart()` |
| `pilot_interactive.search_workers` | int | `3` | Overrides `PILOT_INTERACTIVE_WORKERS` |

Compile-time constants in [`daemon_paths.py`](./daemon_paths.py):

| Constant | Value | Meaning |
|---|---|---|
| `CACHE_TTL_S` | 604 800 | 7 days; matches `TraktMovieCacheManager` |
| `RATE_WINDOW_S` | 300 | Trakt's window |
| `SAFE_THROUGHPUT_CALLS` | 650 | Endpoint calls per cycle |
| `CONSERVATIVE_THROUGHPUT_CALLS` | 500 | Documented floor, **not operative** |
| `SLEEP_SECONDS` | 306 | Just over one window |
| `POLL_INTERVAL_S` | 1.5 | Stop-sentinel granularity |
| `GRACE_STOP_S` | 10 | Before hard kill |
| `MAIN_ACTIVE_MAX_AGE_S` | 1 800 | Stale-sentinel backstop |
| `MAIN_ACTIVE_POLL_S` | 5 | Paused re-check |
| `PILOT_SPILL_THRESHOLD` | 10 | Larger batches spill to the daemon |
| `PILOT_IDLE_EXIT_S` | 1 800 | Idle daemon exits |
| `PILOT_SEARCH_WORKERS` | 6 | JIT step-down workers |
| `PILOT_INTERACTIVE_WORKERS` | 3 | Interactive searches (deliberately lower) |
| `PILOT_SEARCH_BATCH` | 100 | episodeIds per Sonarr command |
| `DEFAULT_SCOPE` | 7 buckets | Movie enrichment scope |
| `SHOW_SCOPE` | 4 buckets | Show enrichment scope |

---

## 8. Implemented capabilities

- ✅ Shared, cycle-free path module consumed by both processes and the runtime Trakt cache manager
- ✅ Detached spawn on Windows and POSIX, with Job-Object breakaway and graceful fallback
- ✅ Graceful stop via sentinel, hard-kill after a bounded grace window
- ✅ PID-reuse rejection on both platforms
- ✅ Three-condition rate-limit handshake with crash and stale-PID backstops
- ✅ `restart()` vs `ensure_running()` split matching each daemon's resumability
- ✅ Child log isolation via `GLIDEARR_DAEMON=1`
- ✅ Pilot job queue with newest-wins overwrite and orphan re-queue
- ✅ Idle-exit + re-spawn-on-demand for the pilot daemon
- ✅ Per-endpoint movie (8) and show (4) cache buckets
- ✅ People-matrix forward-map / affinity split with `mtime_ns` incremental sidecars
- ✅ Bucket merge utility

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Daemon health heartbeat** — periodic timestamp file; the run warns if stale | Closes §6 row 7: a dead daemon is currently invisible until the next run | S | — |
| P2 | **Spawn lock** (`O_EXCL` pid file) | Prevents the §6 row 10 double-spawn race | S | — |
| P3 | **Fail spawn when the pid file is unwritable** rather than warning | §6 row 5 currently leads to duplicate daemons | S | P2 |
| P4 | **Daemon status in the web UI** — running, pid, last heartbeat, cursor position, throughput | Replaces `Get-CimInstance` archaeology | M | P1, [`web/`](../web/DESIGN.md) |
| P5 | **Adaptive throughput** — read Trakt's `X-RateLimit-*` headers instead of a fixed 650 | Recovers the 35% static headroom | M | — |
| P6 | **Auto-restart on daemon crash** — supervisor thread or OS service | §6 row 7 without waiting for the next run | M | P1 |
| P7 | **Structured progress reporting** (enriched / remaining / ETA) | Enrichment progress is opaque today | S | P1 |
| P8 | **Generalise the sentinel handshake** so any future daemon gets rate-limit yielding for free | Currently enrichment-specific | S | — |
| P9 | **Consume the `translations` bucket** — it is warmed by default at +1 call/movie and **no consumer reads it** | Either build the localized renderer or drop it from `DEFAULT_SCOPE` and reclaim ~14% of enrichment cost | S | Localization decision |
| P10 | **Bucket size accounting + prune** | Twelve bucket directories grow unbounded | M | [`cache/audit.py`](../cache/audit.py) |
| P11 | **Graceful pilot drain on stop** — finish the claimed job rather than re-queue | Slightly cleaner shutdown | S | — |
| P12 | **systemd unit / Windows Service definitions** | True OS-level supervision instead of run-triggered spawn | M | P6 |

**P9 is free money.** `translations` is on by default, costs one extra Trakt
call per movie — roughly 14% of the 7-call default scope — and the code comment
states plainly that no consumer reads it yet.

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should `translations` stay in `DEFAULT_SCOPE` warming a cache nothing reads, or come out until a renderer exists? | P9 |
| Q2 | Should the daemons be OS services rather than run-triggered? That changes the rate-limit handshake from "main is active" to genuine contention. | P12 |
| Q3 | Is 650 still right, or should it track live rate-limit headers? | P5 |
| Q4 | Should a stale heartbeat be a warning, or should the run refuse to start a second daemon? | P1 |
| Q5 | Does `PILOT_SPILL_THRESHOLD = 10` still match the measured in-process cost? | — |

## 11. Related designs

- [`support/daemons/`](../../../support/daemons/README.md) — the daemon bodies
- [`cache/DESIGN.md`](../cache/DESIGN.md) §4 D3 — the `None`-vs-`[]` rule these daemons depend on
- [`machine_learning/people_matrix/`](../../machine_learning/people_matrix/README.md)
- [`services/trakt/movies/`](../../services/trakt/movies/README.md)
- [`scripts/DESIGN_auto_run_triggering.md`](../../../DESIGN_auto_run_triggering.md)
