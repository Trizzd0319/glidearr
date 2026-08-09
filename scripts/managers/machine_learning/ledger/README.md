# ledger

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **ledger**

**Package** — `scripts.managers.machine_learning.ledger`
**Run position** — `stamp()` during Radarr/Sonarr phases; `PlanSummary.log()` at Phase 2.6, after the coordinator.
**One-liner** — The dry-run decision record: three stamped columns per file row, rolled up into the "what I'd do" table that is the headline value of a dry run — and the migration's parity oracle.

---

## Purpose

A dry run's entire deliverable is the plan. This package is where the plan lives.

| Half | Module |
|---|---|
| **Write** — stamp a planned action onto a file's row | [`decision_ledger.py`](./decision_ledger.py) |
| **Read** — roll every stamp into one readable table | [`plan_summary.py`](./plan_summary.py) |

`PlanSummary` is also described in-source as **the system-level parity oracle for
the ML migration** — the thing that proves a refactor changed nothing.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`decision_ledger.py`](./decision_ledger.py) | `stamp(df, idx, action, reason, reclaim_gb)` · `stamp_universe_plan(df, idx, action, target_profile)` | ✅ Implemented |
| [`plan_summary.py`](./plan_summary.py) | `PlanSummary(...).log()` — reads both service Parquets across instances, aggregates, logs. Also hosts `log_thresholds` and `first_run_backfill` | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_next_watch_surface.py`](./test_next_watch_surface.py) | Next-watch surface |

---

## What a ledger row actually is

**Three columns on the existing file row** — not a separate append-only record:

| Column | Contents |
|---|---|
| `planned_action` | `delete` · `downgrade` · `upgrade` · … |
| `plan_reason` | Free-text string (e.g. `"universe downgrade"`) |
| `plan_reclaim_gb` | **Signed.** `+` GiB freed (delete/downgrade), `−` GiB consumed (upgrade) |

The service ensures the columns exist and persists the Parquet; this package only
writes the cell. Rows live in the Sonarr episode-files and Radarr movie-files
caches, per instance.

`stamp_universe_plan` derives the signed impact itself: `runtime_minutes` ×
the target profile's top quality (via `estimate_gb_for_profile`) versus the
current `size_bytes`.

---

## The roll-up

```
PlanSummary(registry, logger, config, global_cache).log()
    │
    ├─ SonarrCacheEpisodeFilesManager  → per instance → df
    ├─ RadarrCacheMovieFilesManager    → per instance → df
    │       (skips a manager with no `load`, an unloadable instance, an empty frame)
    │
    ├─ aggregate planned_action / plan_reclaim_gb / watchability_score
    ├─ log the "what the system would do" table
    │
    ├─ first_run_backfill()   ← one-shot fresh-install label reconstruction
    └─ log_thresholds()       ← calibrated-threshold shadow report
```

Read-only and best-effort throughout — it never breaks the run.

`PlanSummary` hosts the threshold shadow report and the first-run label backfill
because it is *"the one place that already has both service parquets open, runs
after every phase has stamped its plans, and is otherwise read-only by
contract."*

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Writers:** [`space/`](../space/README.md) planners · [`lifecycle/`](../lifecycle/README.md) grace · Radarr/Sonarr storage + quality
- **Related:** [`thresholds/`](../thresholds/) · [`labels/`](../labels/) · [`eval/`](../eval/README.md) · [`sizing/`](../sizing/)
