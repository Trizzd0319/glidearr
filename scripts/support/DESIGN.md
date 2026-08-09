# support — Design

> Breadcrumb: [glidearr](../..) › [scripts](../README.md) › **support**

**Package** — `scripts.support`
**Status** — ✅ Implemented · 🟡 Mid-migration (four shims awaiting Step 10)
**Related** — [README.md](./README.md) · [`scripts/DESIGN.md`](../DESIGN.md) · [`ENHANCEMENTS.md`](../ENHANCEMENTS.md) §8

---

## 1. Problem statement

Three categories of code do not fit the manager tree, and forcing them into it
would be worse than the catch-all:

1. **Cross-cutting utilities.** The logger, timing decorators, progress wrappers
   and auth validator are used by *every* layer including `factories/`. Making
   them managers would create a circular dependency — `BaseManager` needs a
   logger before the manager tree exists.

2. **Operator tools.** ~60 scripts that run manually: rebuild profiles, retrain a
   model, reset a cache, force a pilot search, repair Trakt IDs. They are not part
   of any automated run, must be runnable standalone, and several run with only
   `scripts/` on `sys.path` rather than the repo root.

3. **Runtime data.** Config, TRaSH profile snapshots, caches and logs live
   somewhere on disk. `support/` is that somewhere.

The design tension: a folder defined by *not being* something else accumulates
whatever nobody wanted to place. The mitigation is a clear internal split
(README §Purpose) and the discipline that anything genuinely part of the run
belongs in `managers/`.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Utilities importable from any layer without circularity. |
| G2 | Operator tools runnable standalone, outside a full run. |
| G3 | Config and profile data versioned separately from runtime cache/log output. |
| G4 | Migration shims keep old import paths working with exactly one implementation behind them. |
| G5 | Daemon bodies live beside their supervisor's path constants, not inside the manager tree. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Being a plugin system | Tools are invoked, not registered. |
| N2 | Housing decision logic | Brain layer. The shims here *point at* it. |
| N3 | Test coverage parity with `managers/` | Tools are operator-run; failure is immediately visible. |

---

## 3. Architecture

### 3.1 Why utilities cannot be managers

```
BaseManager.__init__:
    self.logger = logger or LoggerManager()      ← needs a logger
                                                    BEFORE the tree exists
```

`LoggerManager` is constructed by `BaseManager`. If it *were* a `BaseManager`, it
would need a logger to construct itself. Same for `ConfigManager` (not a
`BaseManager`, for the same reason) and the timing decorators, which wrap
`BaseManager.__init__` itself.

`support/utilities/` is therefore the layer **beneath** `factories/`, and the
only one that everything may import.

### 3.2 The migration shim pattern

Four utilities are re-export shims for modules that moved to the brain:

```python
# support/utilities/size_model.py — Step 1
from scripts.managers.machine_learning.sizing.size_model import *
from scripts.managers.machine_learning.sizing.size_model import (
    CALIBRATED_MB_PER_MIN, set_calibration, get_calibration, mb_per_min, ...
)
```

Both a wildcard **and** explicit names, because the wildcard skips
underscore-prefixed symbols that callers actually use (`_cfg`, `_num`,
`_anime_match`, `_DEFAULTS`).

The critical property, stated in `size_model.py`'s own docstring: *these names are
the SAME function objects*, so the calibration overlay's module-level state stays
consistent no matter which path a caller imported through. A shim that
re-implemented rather than re-exported would fork that state silently.

**One shim differs.** [`library_classifier.py`](./utilities/library_classifier.py)
wraps the import in try/except:

```python
try:    from scripts.managers.machine_learning.classification.library_classifier import *
except ImportError:
        from managers.machine_learning.classification.library_classifier import *
```

Because [`tools/router_show.py`](./tools/router_show.py) and
[`tools/router_movie.py`](./tools/router_movie.py) execute standalone from inside
`scripts/`, with only `scripts/` on `sys.path` — so `scripts.managers...` does not
resolve. This is a real constraint on G2, and a real constraint on deleting the
shim.

### 3.3 Tool execution context

Tools split into two groups by how they resolve imports:

| Group | `sys.path` | Import style |
|---|---|---|
| Repo-root tools (most) | repo root | `from scripts.managers...` |
| Standalone routers (`router_show.py`, `router_movie.py`) | `scripts/` only | bare `managers...`, via the shim fallback |

Nothing enforces or documents which group a tool belongs to. A new tool written
in the first style and then run in the second context fails at import.

### 3.4 Data layout

```
support/
  config/     config.json (0600, secrets stripped) · default_config.json (blank
              template, the ONE file exempt from the commit blocklist) · backups
  profiles/   trash/{radarr,sonarr}/{cf,quality-profiles,quality-size}/  ← TRaSH data
              {radarr,sonarr}/<instance>/  ← live snapshots + _pre_apply_snapshot
              blueprint/  ← desired-state definitions
              DEVICE_CODEC_MATRIX.md · REBUILD_HANDOFF.md
  cache/      🚫 runtime — every cache key resolves under here
  logs/       🚫 runtime — run log + per-daemon logs
```

`_pre_apply_snapshot/` is the rollback point: profiles are captured before a sync
applies changes.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Utilities sit beneath `factories/` | G1 — `BaseManager` needs a logger before the tree exists (§3.1) | Make them managers |
| D2 | Shims re-export, never re-implement | G4 — same function objects means shared module state stays consistent | Duplicate the code |
| D3 | Shims export explicit private names alongside `import *` | Wildcards skip `_`-prefixed symbols that callers genuinely use | Wildcard only |
| D4 | `library_classifier` shim has a dual-import fallback | G2 — the standalone routers can't resolve `scripts.` | Force repo-root execution |
| D5 | Every shim docstring names its migration step and deletion point | Makes a shim self-identifying, so a reader doesn't mistake it for drift | Bare re-export |
| D6 | Daemon bodies here, path constants in `factories/daemons/` | The constants must be importable by both the supervisor and the body without a cycle | Bodies in `factories/` |
| D7 | `cache/` and `logs/` under `support/` | Keeps all runtime output under one gitignored root | Repo-root `var/` |
| D8 | `default_config.json` is the only commit-blocklist exemption | It is the blank template and must be committable, while `config.json` never is | No exemption |
| D9 | Tools are not tested to `managers/` standard | N3 — operator-run, failures are immediate and visible | Full parity |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | `support/utilities/` imports nothing from `managers/factories/` or `managers/services/`. |
| I2 | A shim re-exports; it never re-implements. |
| I3 | `config.json` is never committed. `default_config.json` stays blank. |
| I4 | `cache/` and `logs/` contain no source. |
| I5 | Tools never run automatically as part of `main.py`. |
| I6 | `_pre_apply_snapshot/` is written before any profile sync applies. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| Shim deleted while callers remain | `ImportError` | Hard failure at import | Immediate | ✅ Loud |
| Standalone router run from repo root | Shim try-branch succeeds | Works | None | ✅ |
| Repo-root tool run from `scripts/` | `ImportError` | Hard failure | That tool | ✅ Loud, but no guidance |
| Cache root deleted | Key misses | Everything regenerates; one very slow run | Time only | 🟡 Slow run |
| Log root unwritable | Handler error | Logging degraded | Observability | 🟡 |
| TRaSH profile data stale | **None** | Custom formats drift from upstream | 🟡 Quality decisions on stale definitions | ❌ **None** |
| `_pre_apply_snapshot` not written | **None** | No rollback point for a bad sync | 🟡 Unrecoverable bad sync | ❌ **None** |
| Tool run against live services by mistake | Tool-dependent | Varies — many tools have no `dry_run` | 🔴 Potentially destructive | ❌ Inconsistent |

**Applying §8 P-D** — three rows have no signal. Row 8 is the sharp one: the
tools directory contains scripts that delete, regrab, and rewrite profiles, and
`dry_run` support across them is inconsistent. That is the same distributed-
invariant problem as `GLD-ORCH-01`, in a directory with no orchestrator to
enforce it.

---

## 7. Configuration surface

`support/` is where config *lives* rather than a consumer of it.

| Path | Contents |
|---|---|
| `config/config.json` | Live config. `0600`, secrets stripped to keyring |
| `config/default_config.json` | Blank template |
| `config/cache_keys.py` | Cache-key constants |
| `config/reference_keys.json` | Reference key set |
| `profiles/trash/` | Upstream TRaSH custom formats + quality profiles |
| `profiles/blueprint/` | Desired-state profile definitions |
| `profiles/<service>/<instance>/` | Live snapshots + pre-apply rollback point |

---

## 8. Implemented capabilities

- ✅ Logger with secret scrubbing, formatters, run summary, daemon-aware sink
- ✅ Timing decorators + profiling dump
- ✅ Parallel auth validator (one consolidated line)
- ✅ Bootstrap, backup gate, space-floor alert, stepdown cooldown
- ✅ tqdm progress wrapper, registry helpers, name normaliser, JSON utils
- ✅ Rating-key crosswalk
- ✅ Four migration shims preserving object identity
- ✅ ~60 operator tools spanning profiles, ML, cache, routing, audit, repair
- ✅ Enrichment + pilot-search daemon bodies
- ✅ Onboarding, secret setup/migration, hook installation
- ✅ Discord notifier + run-summary collector
- ✅ TRaSH profile data with pre-apply snapshots

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SUP-01` | **Execute `MIGRATION.md` Step 10** — delete the four shims, updating `router_show.py` / `router_movie.py` first | Closes the migration; removes the dual-import special case *(P-E)* | S | `GLD-ML-15` |
| `GLD-SUP-02` | **Consistent `dry_run` across `tools/`** — audit which destructive tools honour it | §6 row 8: the tools directory deletes and rewrites with inconsistent gating | M | `GLD-ORCH-01` ⏸ |
| `GLD-SUP-03` | **Declare each tool's execution context** — repo-root vs standalone — in a header and in [`tools/README.md`](./tools/README.md) | §3.3 is undocumented; a new tool in the wrong style fails at import | S | — |
| `GLD-SUP-04` | **TRaSH data freshness marker** — record when `profiles/trash/` was last fetched, warn when stale | §6 row 6: quality decisions run on silently stale definitions | S | [`arr_trash_fetch.py`](./tools/arr_trash_fetch.py) |
| `GLD-SUP-05` | **Assert `_pre_apply_snapshot` written** before any profile sync applies | §6 row 7: no rollback point, no signal | S | — |
| `GLD-SUP-06` | **Cache/log root size reporting** in the run summary | Both grow unbounded and are invisible until the disk fills | S | `GLD-CACHE-03` |
| `GLD-SUP-07` | **Tool catalogue with one-line purpose + safety class** (read-only / mutating / destructive) | ~60 tools with no index; safety is discovered by reading source | S | [`tools/README.md`](./tools/README.md) |
| `GLD-SUP-08` | **Retire `plex_stresstest.py` or document it** — a stress harness at `support/` root with no obvious owner | Root-level clutter with unclear status | S | — |
| `GLD-SUP-09` | **Config backup rotation** — `config.json.bak-<timestamp>` files accumulate | Two already present; unbounded | S | `GLD-CFG-07` |
| `GLD-SUP-10` | **Shim-removal CI guard** — once Step 10 lands, forbid the old import paths | Named in `size_model.py`'s docstring as the intended follow-up | S | `GLD-SUP-01` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should the standalone routers be converted to repo-root execution, removing the dual-import special case entirely? | `GLD-SUP-01` |
| Q2 | Which `tools/` scripts are destructive, and should they share a common `--dry-run` / confirmation harness? | `GLD-SUP-02` |
| Q3 | Should `profiles/trash/` be vendored (as now) or fetched on demand? Vendored is reproducible; fetched is current. | `GLD-SUP-04` |
| Q4 | Do `cache/` and `logs/` belong under `support/`, or at repo root as `var/`? | — |

## 11. Related designs

- [`ENHANCEMENTS.md`](../ENHANCEMENTS.md) §8 P-E / P-F — shim vs impure-wrapper distinction
- [`managers/machine_learning/MIGRATION.md`](../managers/machine_learning/MIGRATION.md) — Step 10
- [`factories/daemons/DESIGN.md`](../managers/factories/daemons/DESIGN.md) — supervisor for the bodies here
- [`factories/config/DESIGN.md`](../managers/factories/config/DESIGN.md) — consumer of `config/`
- [`PERF_BASELINE.md`](./PERF_BASELINE.md) · [`profiles/REBUILD_HANDOFF.md`](./profiles/REBUILD_HANDOFF.md)
