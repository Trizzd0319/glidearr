# backup — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **backup**

**Manager** — `ServiceBackupManager`
**Status** — ✅ Implemented · 🎯 The D36 rule at whole-run scope · 🟡 The gate boolean conflates two states
**Related** — [README.md](./README.md) · [`coordinator/DESIGN.md`](../coordinator/DESIGN.md) · [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8

---

## 1. Problem statement

Glidearr deletes files and re-grabs releases against libraries the household has
spent years building. Every subsystem that does so has its own guard — keep tags,
franchise anchors, grace windows, consent gates, the exhaustive-downgrade
invariant.

All of those guard against *deciding wrong*. None guards against *the code being
wrong*. A bug in the delete pool, a schema change that silently disables a
protection (as
[`classification/franchise.py`](../../machine_learning/classification/DESIGN.md)
§3.3 records happening), or a mis-set config can produce a correct-looking run
that removes hundreds of files.

The only defence against that class is a **rollback point** — and a rollback
point that is not verified loadable is worse than none, because it converts *"we
have a backup"* into a false belief precisely when it matters.

Three sub-problems:

1. **Existence is not loadability.** A zero-byte file, a truncated download, or a
   zip whose CRCs fail all look like a backup on disk.
2. **The backup must not depend on the thing it protects.** If the manager graph
   or the validated API stack is broken, the backup still has to work.
3. **Backing up ~300 MB per instance every three hours is untenable**, but
   skipping it silently is the failure mode this exists to prevent.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | No destructive change without a **validated** rollback point on disk. |
| G2 | Validation proves loadability, not existence. |
| G3 | Failure degrades the run rather than aborting or proceeding. |
| G4 | The backup path depends on as little as possible. |
| G5 | Backup churn is bounded without weakening G1. |
| G6 | The operator is told what happened and what to do. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Restoring | It creates and validates; restore is a human act via the *arr UI. |
| N2 | Backing up Glidearr's own caches | The *arr DBs are what destructive ops touch. |
| N3 | Running on dry runs | Nothing destructive to guard. |
| N4 | Being mandatory | `backup_before_destructive: false` is a supported opt-out. |

---

## 3. Architecture

### 3.1 🎯 The D36 rule, applied to the whole run

[`discovery/DESIGN.md`](../../machine_learning/discovery/DESIGN.md) §3.3 derived
the rule that reconciles four modules' apparently contradictory fail-directions:

> **On unknown input, fail toward the outcome that changes nothing.**

This module is that rule at **run scope**, and it is the largest-grain instance in
the repo:

```
cannot prove a rollback point exists  →  the ENTIRE RUN becomes read-only
```

Not "skip deletion." Not "warn and proceed." Every destructive primitive across
both services reads one gate and logs *"would …"* instead. A single unverifiable
backup on one instance disarms writes for the whole run.

Worth stating plainly because it is the strongest safety property in the codebase
and it is implemented in 14 KB. `GLD-BKP-04` proposes citing it in
`DOCS_CONVENTIONS.md` alongside the rule it exemplifies.

### 3.2 Validation proves loadability (G2)

| Layer | Rejects |
|---|---|
| `MIN_BACKUP_BYTES = 64 KB` | *"below this a 'backup' is empty/garbage, not a real DB dump"* |
| Valid zip, **CRCs verified** | Truncated or corrupted downloads |
| Contains `.db` **and** `config.xml` | A zip that is not an *arr backup |

The CRC check is what separates this from the common `os.path.exists` pattern. A
half-downloaded 200 MB zip passes size and content-listing checks and fails CRC —
and that is exactly the backup someone would discover was useless while trying to
use it.

### 3.3 Dependency-light on purpose (G4)

> Talks to the *arr REST API **DIRECTLY** … rather than through the validated api
> stack, so it is dependency-light and can be exercised standalone.

A deliberate architectural exception with a stated reason. Everywhere else in the
repo, going around the validated API stack would be a finding; here it is the
design. The safety net must not share failure modes with the machinery it
protects — and it runs at the top of `Main.run`, before that machinery is proven
healthy.

It also makes the module testable and runnable on its own, which matters for the
one component an operator might want to exercise deliberately before a risky run.

### 3.4 Freshness reuse — a real tradeoff, explicitly reasoned (G5)

```python
DEFAULT_MAX_AGE_HOURS = 24.0
```

> a library barely changes between short scheduled runs (e.g. every 3h), so this
> caps backups at one per window. Picks the newest backup of **ANY** kind (our
> manual **OR** the *arr's own scheduled backup)

Two good properties: it avoids ~300 MB per instance every three hours, and it
credits the *arr's own scheduled backups rather than duplicating them.

The tradeoff is worth naming though: **a reused backup can be up to 24 hours
old.** G1 promises *"a validated rollback point sitting on disk"* — and it
delivers, but that point may predate a day of legitimate changes. Restoring after
a bad delete would also roll back everything else that happened since.

Not a defect — a 24-hour rollback window is a reasonable trade against per-run
300 MB dumps, and it is configurable. But the *age* of the point protecting a run
is not currently reported, so an operator deciding whether to proceed cannot see
whether their net is an hour old or twenty-three. `GLD-BKP-02`.

A reused backup that fails validation falls through and a fresh one is created —
so staleness never compounds with unloadability.

### 3.5 🟡 The gate boolean conflates "verified" with "not attempted"

```python
if self.dry_run:      self._set_gate(True, reason="dry_run");  return {}
if not self.enabled(): self._set_gate(True, reason="disabled"); return {}
...
self._set_gate(all_ok, reason="ok" if all_ok else "backup_failed", results=results)
```

The gate is **armed** in three distinct situations:

| Situation | Meaning |
|---|---|
| `"ok"` | A validated rollback point exists |
| `"disabled"` | The operator opted out — **no rollback point** |
| `"dry_run"` | Moot — nothing destructive will run anyway |

So `system/backup_gate == True` does **not** mean "a backup exists." It means
"writes are permitted," which is a different claim.

The `reason` field distinguishes all three, so the information is present and the
design is recoverable. But a consumer reading only the boolean — which is what
`effective_dry_run` most plausibly does — cannot tell a **verified** run from an
**opted-out** one.

This is §8 **P-C** in its structural form: two states collapsed into one truthy
value, with the distinguishing detail available but adjacent. Whether any consumer
reads `reason` is unverified. `GLD-BKP-01`.

The `"disabled"` case is legitimate — opting out of backups means accepting
deletion without a rollback point, and that is the operator's call. The issue is
that nothing downstream can *tell*, so a run that deletes 200 files under
`backup_before_destructive: false` is indistinguishable, at the gate, from one
protected by a validated 300 MB zip.

### 3.6 An edge in the `all_ok` computation

```python
all_ok = bool(results) and all(r.get("ok") for r in results.values())
```

`bool(results)` means **no configured instances ⇒ `all_ok = False` ⇒ gate
disarmed**, with the message reading `"(none created)"`.

Harmless in practice — with no Radarr or Sonarr there is nothing destructive to
guard — but it means an install without *arr instances degrades to dry-run every
run and logs a warning about it. Fail-safe, and slightly noisy. `GLD-BKP-03`.

Worth contrasting with the naive `all([])` which is `True`: had the author written
that, an instance-less install would have *armed* the gate. The `bool(results)`
guard is deliberate and errs correctly.

### 3.7 The failure message (G6)

> `[Backup] backup FAILED or not loadable for: {bad}. **DEGRADING this run to
> dry-run — NO destructive changes will be made** (every delete/re-grab logs
> 'would …' instead). **Fix the backup target and re-run for live changes.**`

Three things in one line: what happened, what the consequence is, and what to do
about it. That is the standard the rest of the repo's warnings should be measured
against — most name only the first.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Degrade the run, don't abort | G3 — the run still does useful read-only work, and the operator sees a full plan | Abort |
| D2 | Degrade the **whole** run, not just deletion | One gate, one guarantee; a partial degrade invites gaps | Per-operation gates |
| D3 | Use the *arr **native** backup command | The vendor-blessed, restorable format — not a hand-rolled dump | Copy the DB file |
| D4 | Validate CRCs, not just existence | G2 — a truncated zip is the failure that only appears at restore | Size check |
| D5 | Require `.db` **and** `config.xml` | A DB without config restores into an unconfigured service | DB only |
| D6 | Bypass the validated API stack | G4 — the net must not share failure modes with what it protects | Use the stack |
| D7 | Reuse backups under 24 h, of any origin | G5 — credits the *arr's own scheduled backups; avoids 300 MB per 3 h | Fresh every run |
| D8 | Failed reuse falls through to a fresh backup | Staleness must never compound with unloadability | Fail on stale |
| D9 | `bool(results)` in `all_ok` | `all([])` is `True` — an instance-less install must not arm the gate (§3.6) | Bare `all()` |
| D10 | Arm the gate when disabled | Opting out is the operator's call | Refuse to run |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No destructive change occurs unless the gate is armed. |
| I2 | The gate is armed on `"ok"` only after CRC-verified, content-checked validation. |
| I3 | A backup under `MIN_BACKUP_BYTES` never validates. |
| I4 | A reused backup is validated before it is trusted. |
| I5 | Failure degrades; it never aborts and never proceeds. |
| I6 | This module makes no destructive change itself. |
| I7 | It does not depend on the validated API stack. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Backup command never completes | `BACKUP_TIMEOUT_S` 300 s | Gate disarmed | Run read-only | ✅ Named instance |
| Downloaded zip fails CRC | Validation | Gate disarmed | Run read-only | ✅ |
| Zip lacks `.db` or `config.xml` | Validation | Gate disarmed | Run read-only | ✅ |
| Backup under 64 KB | `MIN_BACKUP_BYTES` | Gate disarmed | Run read-only | ✅ |
| No `base_url` / api key | Guard | That instance fails ⇒ gate disarmed | Run read-only | ✅ |
| **`backup_before_destructive: false`** | — | Gate **armed**, `reason="disabled"` | 🟡 Deletion with **no** rollback point | 🟡 `reason` only (§3.5) |
| **Reused backup is 23 h old** | — | Gate armed | 🟡 Rollback would lose a day (§3.4) | ❌ **Age not reported** |
| No *arr instances configured | `bool(results)` | Gate disarmed, warns every run | Harmless, noisy | ✅ `"(none created)"` |
| One instance fails, others succeed | `all(...)` | Gate disarmed for the **whole** run | Intentional (D2) | ✅ Names the failure |

Every row degrades toward not-writing except rows 6 and 7, and both are
*permitted* states rather than faults — the gap is that neither is visible at the
point a consumer decides whether to write.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `backup_before_destructive` | **`true`** | `false` ⇒ gate armed with no backup |
| `backup_max_age_hours` | `24.0` | Reuse window (§3.4) |
| `{service}_instances.*.base_url` / `.api` | — | Read directly, not via the api stack |

Constants: `POLL_INTERVAL_S` 3.0 · `BACKUP_TIMEOUT_S` 300.0 ·
`MIN_BACKUP_BYTES` 64 KB · `GATE_KEY` `system/backup_gate`.

---

## 8. Implemented capabilities

- ✅ Native *arr backup triggered and polled to completion per instance
- ✅ Three-layer loadability validation — size, CRC, content
- ✅ Run-scoped gate arming/disarming both services at once
- ✅ Whole-run degradation to dry-run on any failure
- ✅ Freshness reuse crediting the *arr's own scheduled backups
- ✅ Failed-reuse fallthrough to a fresh backup
- ✅ Direct REST access, independent of the manager graph
- ✅ Actionable failure message naming instance, consequence and remedy
- ✅ 7.2 KB of tests against 14.3 KB of source

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-BKP-01` | 🟡 **Distinguish "validated" from "not attempted" at the gate** — `system/backup_gate == True` means *writes permitted*, not *a backup exists*; `"disabled"` and `"ok"` are both armed | §3.5 *(P-C)*. The `reason` field already carries it; verify `effective_dry_run` reads it, or expose a second flag | S | — |
| `GLD-BKP-02` | **Report the rollback point's age and origin** in the run summary — reused vs fresh, *arr-scheduled vs ours | §3.4: an operator cannot see whether their net is an hour old or twenty-three | S | `GLD-PLX-03` |
| `GLD-BKP-03` | **Suppress the disarm warning when no *arr instances are configured** | §3.6: fail-safe but warns every run about a backup that was never needed | S | — |
| `GLD-BKP-04` | 🎯 **Cite this module in `DOCS_CONVENTIONS.md`** as the run-scope instance of the D36 rule — *cannot prove a rollback point ⇒ the entire run becomes read-only* | The strongest safety property in the codebase, implemented in 14 KB, currently documented only here | S | `GLD-DIS-04` |
| `GLD-BKP-05` | **Warn when a backup is reused close to the age limit** — a 23.9 h reuse is materially different from a 1 h one | §3.4; complements `GLD-BKP-02` | S | `GLD-BKP-02` |
| `GLD-BKP-06` | **Record gate state in the ledger** so a run's plan carries whether it was protected | A dry-run plan reviewed later cannot say whether the live run would have been guarded | S | `GLD-LED-04` |
| `GLD-BKP-07` | **Verify every destructive primitive actually reads the gate** — the guarantee is only as broad as its readers | I1 is a distributed invariant of the `GLD-ORCH-01` kind; the gate is central, the reading is not | M | `GLD-ORCH-01` |
| `GLD-BKP-08` | **Consider backing up Glidearr's own caches** — the Parquet ledger and `size_model/calibration` are not covered | A restored *arr with a stale Glidearr cache is a partially-rolled-back system | M | D48 |
| `GLD-BKP-09` | **Surface backup size trend** — a shrinking backup can indicate DB corruption before a restore reveals it | 64 KB catches garbage; nothing catches a 300 MB → 40 MB drop | S | `GLD-BKP-02` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `effective_dry_run` read `reason`, or only the boolean? | `GLD-BKP-01` |
| Q2 | Do **all** destructive primitives read the gate, or only the ones that were retrofitted? | `GLD-BKP-07` |
| Q3 | Should Glidearr's own Parquet caches be part of the rollback point? *(= D48)* | `GLD-BKP-08` |
| Q4 | Is 24 h the right reuse window given this install's run cadence? | `GLD-BKP-05` |

**Q2 is the one that determines whether G1 actually holds.** The gate is a single
central value — a genuine improvement over the distributed `dry_run` invariant
`GLD-ORCH-01` describes. But its guarantee is only as broad as the set of
primitives that consult it, and nothing enumerates that set. One unguarded delete
path means *"nothing is ever deleted without a validated rollback point"* is
false, and it would be false silently.

## 11. Related designs

- [`coordinator/DESIGN.md`](../coordinator/DESIGN.md) §3.6 — consults `effective_dry_run` as a second independent path
- [`machine_learning/discovery/DESIGN.md`](../../machine_learning/discovery/DESIGN.md) §3.3 — the fail-direction rule this implements at run scope
- [`machine_learning/classification/DESIGN.md`](../../machine_learning/classification/DESIGN.md) §3.3 — the silent protection outage this class of guard exists for
- [`writeback/DESIGN.md`](../writeback/DESIGN.md) §3.4 — the contrasting weak `dry_run` resolution
- [`machine_learning/space/DESIGN.md`](../../machine_learning/space/DESIGN.md) §3.5 — the deletion consent gate this sits above
