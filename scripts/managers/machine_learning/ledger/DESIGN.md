# ledger — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **ledger**

**Package** — `scripts.managers.machine_learning.ledger`
**Status** — ✅ Implemented · 🟡 Column-stamp, not an append-only record — which caps four other items
**Related** — [README.md](./README.md) · [`eval/DESIGN.md`](../eval/DESIGN.md) · [`space/DESIGN.md`](../space/DESIGN.md)

---

## 1. Problem statement

Project goal G3 says a dry run must produce the **complete plan, persisted**. The
plan is the deliverable — the thing an operator reviews before letting the system
touch a library.

That needs two things a naive log line cannot give:

1. **Machine-readable persistence.** "Would delete X" in a log is not reviewable,
   sortable, or summable. The plan must survive the run as data.
2. **Correct netting.** A run that upgrades 8 GB and downgrades 3 GB has *not*
   done 11 GB of reclaim. Without signed accounting, a plan summary is
   directionally wrong for capacity planning.

There is also a migration-era requirement: the ML refactor moved decision logic
out of the services, and something had to prove the outputs were unchanged.
`PlanSummary` became **the system-level parity oracle** — the roll-up you diff
before and after a refactor.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Every planned action is persisted, including in dry run. |
| G2 | Space accounting nets correctly (signed). |
| G3 | One readable roll-up across both services and every instance. |
| G4 | The roll-up never breaks the run. |
| G5 | Serve as a byte-comparable parity oracle for refactors. |
| G6 | Pure — operates on a DataFrame cell; no HTTP, no `global_cache`. |

### Non-goals (as built)

| # | Non-goal | Consequence |
|---|---|---|
| N1 | Append-only decision history | One row holds **one** action; a later stamp overwrites an earlier one |
| N2 | Run-over-run history | Each run overwrites the previous stamps |
| N3 | Signal-level provenance | `plan_reason` is free text; no breakdown, no axis version |
| N4 | Surviving the item | A deleted file's row is the only place its decision lived |

**N1–N4 are the design's real boundary**, and §3.3 traces what they cost.

---

## 3. Architecture

### 3.1 Write path

```
planner decides
    │
    ▼
stamp(df, idx, action, reason, reclaim_gb)
    df.at[idx, "planned_action"]  = action
    df.at[idx, "plan_reason"]     = reason
    df.at[idx, "plan_reclaim_gb"] = round(float(reclaim_gb), 2) or None
    │
    ▼
service persists the Parquet   ← including in dry_run (G1)
```

`stamp_universe_plan` computes the signed impact rather than receiving it:

```
est_target = estimate_gb_for_profile(target_profile, runtime_minutes, 1)
cur_gb     = size_bytes / 1024³

downgrade → +max(0, cur_gb − est_target)      space freed
upgrade   → −max(0, est_target − cur_gb)      space consumed
```

Its docstring carries an important caution: *"Ledger-only — callers must NOT
persist `quality_profile_id` speculatively in dry_run."* The ledger records
intent; it must not leak into state.

### 3.2 Read path

`PlanSummary` iterates `(service, manager) × instances`, skipping anything it
cannot load, and aggregates `planned_action` / `plan_reclaim_gb` /
`watchability_score` into one table.

It has also become the single per-run home for two neighbouring jobs, justified
in-source by the fact that it is *"the one place that already has both service
parquets open, runs after every phase has stamped its plans, and is otherwise
read-only by contract"*:

| Job | Module |
|---|---|
| `log_thresholds()` | [`thresholds/report.run`](../thresholds/) |
| `first_run_backfill()` | [`labels/first_run`](../labels/) |

That is a reasonable placement argument. It does mean "read-only by contract" is
now approximate — `first_run_backfill` writes labels.

### 3.3 🟡 The shape caps four other items

The ledger is **columns on the item's own row**. That is cheap, simple, and
correct for the dry-run table. It also means:

| Consequence | Blocks |
|---|---|
| No timestamp, run id, or version columns | `GLD-SCO-03` (axis version), `GLD-LIK-09` (anchor provenance), `GLD-EVA-05` (stamp version on eval output) — all need columns or a store that does not exist |
| A deleted file's row is where its decision lived | **`GLD-EVA-07` (replay deletion decisions) is structurally impossible today** — there is no preserved pre-deletion snapshot to re-score |
| Last write wins per row | A title downgraded then deleted retains only the delete. The *net* stays right; the *history* is lost |
| No run-over-run record | `GLD-WEB-12` (trends) has no source; `GLD-CORE-07` (run history) likewise |
| `plan_reason` is free text | `GLD-WEB-03` (ledger browser with signal breakdown) has no structured breakdown to render — even though `scoring` computes one for free via `return_breakdown=True` |

None of this is wrong for what the ledger was built to do. It is a **plan
snapshot**, and it does that well. But four registered items assume a *decision
record*, and those are different artifacts. Reconciling them is `GLD-LED-01`.

### 3.4 🟡 Silent stamp failure

`stamp_universe_plan` wraps its whole body in:

```python
except Exception:
    pass
```

A stamp that fails leaves the row unstamped and says nothing. Because
`PlanSummary` is the parity oracle and the dry-run deliverable, a silently
skipped stamp makes the plan **silently under-report** — the exact failure mode
an oracle exists to prevent.

`stamp()` is narrower: it catches only `(TypeError, ValueError)` on the
`reclaim_gb` conversion and writes `None`, which is a reasonable degradation
because `planned_action` and `plan_reason` are already written by then.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Stamp columns on the existing row | Cheapest possible persistence — the Parquet is already being written | Separate ledger table |
| D2 | Persist in dry run | G1 — the plan *is* the dry run's deliverable | Skip writes in dry run |
| D3 | Signed `plan_reclaim_gb` | G2 — a gross figure is the wrong sign for capacity planning | Unsigned + direction column |
| D4 | `stamp_universe_plan` derives its own impact | The caller has the profile, not the estimate; deriving it here keeps the arithmetic in one place | Caller computes |
| D5 | Ledger must not persist `quality_profile_id` in dry run | Intent must not leak into state | Stamp the profile too |
| D6 | `PlanSummary` best-effort throughout | G4 — a reporting failure must never fail a run | Let it raise |
| D7 | Roll-up hosts thresholds report + first-run backfill | It is the only point with both parquets open after all phases | Separate passes |
| D8 | Round to 2 decimals | GiB at 2dp is below the noise floor of size estimation | Full precision |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Stamps are written in dry run. |
| I2 | `plan_reclaim_gb` is signed: `+` freed, `−` consumed. |
| I3 | The ledger never persists `quality_profile_id` speculatively. |
| I4 | `PlanSummary` never raises into the run. |
| I5 | This package performs no HTTP and touches no `global_cache`. |
| I6 | One row holds at most one `planned_action`. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| **`stamp_universe_plan` raises** | **None** — bare `except: pass` | Row unstamped; plan under-reports | 🔴 Oracle silently wrong | ❌ **None** |
| `reclaim_gb` unconvertible | `(TypeError, ValueError)` | `None` written; action + reason survive | Minor | ❌ **None** |
| `runtime_minutes` absent | Guarded | `est_target = 0.0` ⇒ impact estimated as full current size | 🟡 Overstated reclaim | ❌ **None** |
| Manager missing / has no `load` | Guarded | Instance skipped | 🟡 Partial roll-up | ❌ **None** |
| Parquet unloadable | `except: continue` | Instance skipped | 🟡 Partial roll-up | ❌ **None** |
| Empty frame | Guarded | Skipped | Correct | ✅ |
| Two planners stamp one row | **None** | Last write wins | 🟡 Earlier decision lost | ❌ **None** |
| Item deleted after stamping | — | Row and decision gone together | 🟡 No post-hoc audit | ❌ **None** |

**Rows 1, 4 and 5 share a shape:** a partial roll-up is indistinguishable from a
small plan. The dry-run table shows *fewer actions*, and nothing says whether that
is because the system decided less or because a source was skipped. For the
artifact whose entire purpose is completeness, that is the gap worth closing
first.

---

## 7. Configuration surface

No config keys of its own. `PlanSummary` reads `{service}_instances` to enumerate
instances, and delegates to [`thresholds/`](../thresholds/) and
[`labels/`](../labels/) for their own configuration.

---

## 8. Implemented capabilities

- ✅ Three-column plan stamp persisted in dry run
- ✅ Signed space accounting that nets correctly
- ✅ Self-deriving universe quality-change impact
- ✅ Explicit prohibition on speculative profile persistence
- ✅ Cross-service, cross-instance roll-up
- ✅ Best-effort throughout — never breaks a run
- ✅ Parity oracle for the ML migration
- ✅ Single per-run home for the threshold shadow report and first-run label backfill

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-LED-01` | 🔴 **Decide: plan snapshot or decision record?** Four registered items (`GLD-SCO-03`, `GLD-LIK-09`, `GLD-EVA-05`, `GLD-EVA-07`) assume an append-only decision record; the ledger is a per-row plan snapshot. Either extend it or add a separate store | Unblocks four items and settles what "the ledger" means | M | D26 |
| `GLD-LED-02` | 🔴 **Replace the bare `except: pass`** in `stamp_universe_plan` with a logged failure | §3.4: a silently skipped stamp makes the parity oracle silently under-report | S | — |
| `GLD-LED-03` | **Report roll-up coverage** — instances read vs skipped, rows scanned | §6 rows 4–5: a partial roll-up currently looks like a small plan | S | — |
| `GLD-LED-04` | **Add provenance columns** — run id, timestamp, scoring-axis version, `untouched_base` | Prerequisite for `GLD-SCO-03`, `GLD-LIK-09`, `GLD-EVA-05` | S | `GLD-LED-01` |
| `GLD-LED-05` | **Persist the scoring breakdown** alongside the plan | `return_breakdown=True` is free and already computed; without it `GLD-WEB-03`/`GLD-SCO-06` have nothing structured to show | M | `GLD-LED-04` |
| `GLD-LED-06` | **Structure `plan_reason`** — a code plus free text, rather than free text alone | Makes reasons filterable and countable | S | `GLD-LED-04` |
| `GLD-LED-07` | **Preserve a pre-deletion snapshot row** | The only way `GLD-EVA-07` (replay deletion decisions) becomes possible | M | `GLD-LED-01` |
| `GLD-LED-08` | **Run-over-run history** | Source for `GLD-WEB-12` trends and `GLD-CORE-07` run history, neither of which has one today | M | `GLD-LED-04` |
| `GLD-LED-09` | **Warn when `runtime_minutes` is missing** during a universe stamp | §6 row 3 silently overstates reclaim | S | — |
| `GLD-LED-10` | **Record superseded stamps** rather than overwriting | §6 row 7: a downgrade-then-delete loses the downgrade | M | `GLD-LED-01` |
| `GLD-LED-11` | **Split `first_run_backfill` out of `PlanSummary`** or document that "read-only by contract" has an exception | §3.2: the contract and the behaviour disagree | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is the ledger meant to be a **plan snapshot** (as built) or a **decision record** (as four items assume)? *(= D26)* | `GLD-LED-01` |
| Q2 | If a decision record is wanted, does it live in the same Parquet or a separate append-only store? | `GLD-LED-01` |
| Q3 | Should a superseded stamp be preserved, or is last-write-wins correct because the net is what matters? | `GLD-LED-10` |
| Q4 | Is the plan summary meant to be diffable run-over-run as a regression check, given it is already the migration parity oracle? | `GLD-LED-08` |

**Q4 is nearly free.** `PlanSummary` already serves as a parity oracle for
refactors. Persisting each run's roll-up would make it a *regression* oracle too —
"the plan changed and no code did" is exactly the signal `GLD-LIK-01` wants for
axis drift.

## 11. Related designs

- [`eval/DESIGN.md`](../eval/DESIGN.md) §10 Q5 — why deletion replay needs `GLD-LED-07`
- [`likelihood/DESIGN.md`](../likelihood/DESIGN.md) §3.3 — the axis drift `GLD-LED-04` would make detectable
- [`space/DESIGN.md`](../space/DESIGN.md) — the planners that stamp
- [`contracts/DESIGN.md`](../contracts/DESIGN.md) §3.2 — signed space accounting
- [`thresholds/`](../thresholds/) · [`labels/`](../labels/) — hosted by `PlanSummary`
