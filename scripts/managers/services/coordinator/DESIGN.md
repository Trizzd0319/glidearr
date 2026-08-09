# coordinator — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **coordinator**

**Manager** — `SpaceCoordinatorManager`
**Status** — ✅ Implemented · 🟢 Best-tested module in the repo · 🔴 A 40× same-named constant collision
**Related** — [README.md](./README.md) · [`space_coordinator.md`](./space_coordinator.md) · [`machine_learning/space/DESIGN.md`](../../machine_learning/space/DESIGN.md)

---

## 1. Problem statement

Radarr and Sonarr manage the same physical mount and cannot see each other. Each
reclaims against its own library, so under pressure both delete simultaneously and
neither knows what the other freed — or, worse, one deletes a rewatched episode
while the other keeps an unwatched film twenty times its size.

Unifying deletion introduces its own problems:

1. **Two currencies.** A movie is one file; an episode is one of forty. They must
   be comparable in a single ranking.
2. **Downgrades and deletes have different reversibility.** A downgrade re-grabs
   a smaller file — recoverable. A delete is not. They cannot share a trigger.
3. **Projected reclaim is not realised reclaim.** Stage-1 downgrades queue
   re-grabs that land later. Deleting to cover a deficit those downgrades will
   also cover double-counts, and *"titles die that the downgrades would have paid
   for."*
4. **The backstop must stay reachable.** A design where downgrades always cover
   the deficit on paper means deletion never fires — which sounds safe until the
   disk actually fills.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One ranked pool across both services. |
| G2 | A healthy library is never touched. |
| G3 | Non-destructive reclaim is exhausted before destructive reclaim begins. |
| G4 | Deletion is floor-gated with hysteresis, separately from downgrade. |
| G5 | Every deletion is restorable. |
| G6 | The backstop must remain reachable — §3.3. |
| G7 | `dry_run` is resolved explicitly, never silently defaulted. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Owning upgrades or downgrades | Each service owns its own; this orchestrates. |
| N2 | Ranking math | `machine_learning/space/coordinator_ranker` — `select_for_target`, `critic_sort`. |
| N3 | Running when disabled | *"each service keeps its own per-service delete loop; this manager is simply never invoked."* |

---

## 3. Architecture

### 3.1 🔵 Two triggers, not one — a correction to `space/DESIGN.md`

```
free ≥ U          → bail immediately, nothing runs                    (G2)
T ≤ free < U      → Stage 1 downgrades ONLY
free < T          → Stage 1 downgrades, then Stage 2 deletes
```

> Downgrades are non-destructive (restorable re-grabs), so they run **throughout
> the band**. Re-read free; if free ≥ the floor T — whether recovered all the way
> to U or merely holding in the band — **STOP** here: the destructive delete pool
> is **floor-gated for hysteresis**.

[`machine_learning/space/DESIGN.md`](../../machine_learning/space/DESIGN.md) §3.1
characterises the band `T ≤ free < U` as *"hold steady"*. That understates it.
The band is **downgrade-active, delete-inactive** — reclaim is happening, just
non-destructively.

The distinction matters for `GLD-SPA-05` (downgrade-vs-delete accounting): the
two stages have different triggers, so measuring them together would conflate a
band-only run with a below-floor one. `GLD-COORD-04` corrects the ML doc.

### 3.2 🎯 Why the downgrade credit is *skipped* by default

This is the subtlest reasoning in the module, and it resolves an open thread from
[`foundation/DESIGN.md`](../../machine_learning/foundation/DESIGN.md) §3.5.

> `space_exhaustive_downgrade` (**DEFAULT ON**) makes that literal… The downgrade
> **CREDIT** below is **skipped** in that mode — the invariant already keeps every
> creditable (still-downgradable) title **out of** the delete pool, so crediting it
> would **double-count the same GB** and, with a library-sized exhaustive
> projection, **permanently suppress the backstop**.

Unpacking it:

- Exhaustive mode guarantees a still-downgradable title is **not in** the delete
  pool at all.
- The downgrade credit subtracts *projected* downgrade reclaim from the deficit.
- Applying both means the same GB is counted twice — once by exclusion from the
  pool, once by subtraction from the need.
- Under an exhaustive projection the credit is **library-sized**, so
  `need − credit` clamps to zero **always**, and Stage 2 can never fire.

So the credit is not merely redundant in this mode — it would **disable the
backstop entirely** (G6). Skipping it is what keeps deletion reachable.

**Consequence for `foundation/`.** `foundation/formulas.py::downgrade_credit` is
the one function of twelve that mirrors rather than delegates, kept in step by an
equivalence test (`GLD-FND-03`). It mirrors arithmetic that **does not execute in
the default configuration**. That does not make the mirror wrong — but it does
mean the equivalence test is guarding a path most installs never take, and anyone
reading `foundation` would not know that. `GLD-COORD-03`.

### 3.3 🎯 A partial answer to D24

**D24** asks: *"With `space_exhaustive_downgrade` on by default, is the delete
path ever actually reached? If not, the delete-gating machinery protects a path
that does not execute."*

The docstring answers the design intent directly:

> Stage 2 can therefore only ever fire **after the downgrade pool is exhausted**.

So deletion is reachable **by design** — it is the terminal state once nothing is
left to shrink. And §3.2 shows the credit-skip exists *specifically* to prevent
the backstop being *"permanently suppressed."* Someone thought about exactly this
failure and engineered against it.

What remains open is the **empirical** half: on this library, is the downgrade
pool ever actually exhausted? That is what `GLD-SPA-05` measures. D24 narrows
from *"is this reachable?"* to *"how often is it reached?"*

### 3.4 🔴 Two constants, one name, 40× apart

```python
# services/coordinator/space_coordinator.py
PRESSURE_FALLBACK_GB = 1000.0

# machine_learning/space/space_targets.py
PRESSURE_FALLBACK_GB = 25.0
```

Same name, same stated purpose — *"last resort when `free_space_limit` is unset
**and** the total drive size is unreadable"* — and a **40× difference**.

The coordinator's is passed as `space_targets(config, fallback_gb=1000.0)`, so the
mechanism is legitimate: the function takes the fallback as a parameter precisely
so callers can scale it. A whole-mount coordinator wanting a larger floor than a
per-service gate is defensible.

But two module-level constants sharing an identical name with a 40× gap is a
genuine trap. A reader who greps `PRESSURE_FALLBACK_GB` finds two answers, and
nothing indicates which applies where. Worse, 1000 GB is a **large** floor — if
this path ever fires (no `free_space_limit`, unreadable mount total), the
coordinator that owns *all* deletion would begin reclaiming on any drive with
under a terabyte free.

That combination — largest blast radius, largest fallback, ambiguous name, only
reachable when two other things have already failed — is worth a rename at
minimum. `GLD-COORD-01`.

### 3.5 🟡 The coordinator imports through a migration shim

```python
from scripts.support.utilities.space_targets import (
    coordinator_owns_deletion, exhaustive_downgrade, space_targets,
)
```

`machine_learning/space/space_targets.py` states: *"`scripts/support/utilities/space_targets.py`
is now a **re-export shim** (deleted at MIGRATION.md **Step 10**)."*

The shim is doing its job — callers keep working — but **Step 10 cannot complete
while live callers still import through it**, and this is the highest-stakes
caller in the repo.

That is a concrete blocker for `GLD-ML-15` (the shim cleanup): the six documented
shims are not deletable until their importers are repointed, and nothing
currently lists who those importers are. `GLD-COORD-02`.

Note the other brain imports in the same file go direct —
`machine_learning.space.coordinator_ranker`, `machine_learning.space.routing_targets`
— so the shim import is an inconsistency within one module, not a policy.

### 3.6 `dry_run` resolved explicitly (G7)

```python
# dry_run resolution (kwargs → parent → Main); never silently default.
```

Three-level fallback with the principle stated in the comment. This is the
distributed-invariant problem `GLD-ORCH-01` describes — *"every manager
individually remembers"* — handled carefully here, in the manager where getting
it wrong deletes files.

`effective_dry_run` from `support/utilities/backup_gate` also participates, so a
backup gate can force dry-run independently of config. Two independent paths to
"don't actually delete", in the one place that most needs them.

### 3.7 FORK-D — genuine cross-run state

| Key | Purpose |
|---|---|
| `radarr/{inst}/pending_4k_evicts` | Rehomed 4K copy awaiting eviction |
| `radarr/{inst}/space_evicted_4k` | Evicted 4K awaiting **space recovery** for re-add as a dual-version bonus |

Worth noting against [`ledger/DESIGN.md`](../../machine_learning/ledger/DESIGN.md)
§3.3, which found the decision ledger has **no run history** — a stamp is
overwritten each run and a deleted item takes its record with it.

These two keys are the counter-example: proper cross-run, per-instance,
append-and-consume ledgers with timestamps (`queued_at`, `evicted_at`). The
pattern `GLD-LED-08` asks for exists in this folder, keyed by tmdb, for exactly
the case where a decision must outlive the run that made it.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Unified delete pool across services | G1 — per-service pools cannot compare a 40 GB film to a 2 GB episode | Per-service deletion |
| D2 | Bail at `free ≥ U` | G2 — a healthy library is never touched | Always run |
| D3 | Downgrades throughout the band, deletes below the floor | G4 — different reversibility, different trigger | One threshold |
| D4 | Re-read free between stages | Stage 1's realised reclaim may end the run | Plan once |
| D5 | Sort ASC by score, then critic, then size DESC | Least-valuable first; size breaks ties toward fewer deletions | Size-first |
| D6 | Accumulate to `U`, not to `T` | Reclaiming to the trigger guarantees immediate re-trigger | Reclaim to floor |
| D7 | Skip the downgrade credit in exhaustive mode | G6 — otherwise the backstop is **permanently suppressed** (§3.2) | Always credit |
| D8 | Every deletion tracked and restorable | G5 — score recovery re-grabs | Fire and forget |
| D9 | Coordinator fallback floor 1000 GB | A whole-mount floor should exceed a per-service one | Share the 25 GB constant |
| D10 | `dry_run` kwargs → parent → Main, never defaulted | G7 — in the manager that deletes files | Default to False |
| D11 | Ranking math lives in the brain | `services` orchestrate; `ml` decides | Rank here |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Nothing runs while `free ≥ U`. |
| I2 | Stage 2 fires only when `free < T`. |
| I3 | In exhaustive mode, a still-downgradable title is never in the delete pool. |
| I4 | The downgrade credit is not applied in exhaustive mode. |
| I5 | Reclaim targets `U`, never stops at `T`. |
| I6 | Every coordinator deletion is recorded for restore. |
| I7 | `dry_run` is never silently defaulted. |
| I8 | Movies and episodes compete in one ranking. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Disabled | `coordinator_owns_deletion` | Per-service delete loops resume | Safe | ✅ |
| `free_space_limit` unset | Gate | Coordinator does not own deletion | Safe | ✅ |
| Free recovers during Stage 1 | Re-read | Stops before Stage 2 | Correct | ✅ |
| **Both `free_space_limit` unset AND mount total unreadable** | Fallback | Floor becomes **1000 GB** | 🔴 §3.4 — reclaims below 1 TB free | ❌ **None** |
| Downgrade pool never exhausts | — | Stage 2 never fires — as designed | Intended | 🟡 D24's empirical half |
| Credit applied in exhaustive mode | **Guarded** | Would suppress the backstop permanently (§3.2) | Prevented | ✅ By construction |
| Shim removed at Step 10 | `ImportError` | Loud | Immediate | ✅ |
| One service's candidates fail to build | ❓ Unverified | Pool would be single-service | 🟡 | ❌ |

**Row 4 is the one to close.** It requires two prior failures to reach, which is
exactly why it would be unexpected when it happened — and the manager it affects
is the one that deletes across both libraries.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `space_coordinator_enabled` | `false` | Master gate; plus consent + floor |
| `free_space_limit` | unset | `T`; required for the coordinator to own deletion |
| `space_exhaustive_downgrade` | **`true`** | Stage 1 plans everything to the floor; credit skipped |
| `space_downgrade_credit_ratio` | — | Only consulted when exhaustive mode is **off** |
| `space_delete_ranking` | `"score"` | `"utility_per_gb"` enables `value_density` |

Constants: `PRESSURE_FALLBACK_GB` **1000.0** *(≠ `space_targets`' 25.0)*.

---

## 8. Implemented capabilities

- ✅ Four-stage pipeline with independent downgrade and delete triggers
- ✅ Unified movie+TV delete pool ranked by watchability, critic, then size
- ✅ Reclaim-to-`U` hysteresis with a mid-pipeline free-space re-read
- ✅ Exhaustive-downgrade invariant keeping shrinkable titles out of the pool
- ✅ Credit-skip preserving backstop reachability
- ✅ Full restore path for coordinator-deleted items
- ✅ FORK-D cross-run 4K eviction ledgers
- ✅ Explicit three-level `dry_run` resolution plus a backup gate
- ✅ **61 KB of tests across 6 modules** — including dedicated multi-instance, rehome, UHD-evict and last-resort suites

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-COORD-01` | 🔴 **Rename one of the two `PRESSURE_FALLBACK_GB` constants** — 1000.0 here vs **25.0** in `space_targets`, same name, same stated purpose, 40× apart. The 1000 GB path reclaims on any drive under 1 TB free | §3.4: largest blast radius, largest fallback, ambiguous name, reachable only after two other failures | S | — |
| `GLD-COORD-02` | 🟡 **Repoint the `space_targets` import** from the `support/utilities` shim to `machine_learning.space` — **Step 10 cannot complete while live callers use the shims**, and nothing lists who they are | §3.5; concrete blocker for `GLD-ML-15`. Other brain imports in this same file already go direct | S | `GLD-ML-15` |
| `GLD-COORD-03` | **Note in `foundation/` that `downgrade_credit` is skipped by default** — its equivalence test guards a path most installs never take | §3.2; a reader of `foundation` cannot tell | S | `GLD-FND-03` |
| `GLD-COORD-04` | **Correct `space/DESIGN.md` §3.1** — the pressure band is *downgrade-active, delete-inactive*, not *"hold steady"* | §3.1; affects how `GLD-SPA-05` must measure | S | `GLD-SPA-05` |
| `GLD-COORD-05` | **Report which stage ran and what each reclaimed** — band-only vs below-floor, downgrade GB vs delete GB | Answers D24's empirical half and `GLD-SPA-05` in one counter | S | `GLD-SPA-05` |
| `GLD-COORD-06` | **Warn when the 1000 GB fallback is used** | §6 row 4 has no signal at all | S | `GLD-COORD-01` |
| `GLD-COORD-07` | **Surface the FORK-D ledgers** — pending and space-evicted 4K copies are cross-run state with no operator view | A title can sit evicted-awaiting-recovery indefinitely, invisibly | S | `GLD-WEB-04` |
| `GLD-COORD-08` | 🎯 **Cite FORK-D as the pattern for `GLD-LED-08`** — proper cross-run, timestamped, per-instance ledgers already exist in this repo | The run-history the decision ledger lacks is implemented here | S | `GLD-LED-08` |
| `GLD-COORD-09` | **Document `hybrid_universe_acquisition.py` and `saga_retention_producer.py`** — 33 KB unread | Two substantial modules with their own test suites | M | — |
| `GLD-COORD-10` | **Verify single-service pool degradation** — what happens if one service's `build_delete_candidates` fails? | §6 row 8; a single-service pool defeats G1 silently | S | `GLD-COORD-05` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | On this library, is the downgrade pool ever actually exhausted? *(D24's empirical half)* | `GLD-COORD-05`, `GLD-SPA-05` |
| Q2 | Should the coordinator's fallback be 1000 GB, or a fraction of the mount like `space_targets` uses? *(= D46)* | `GLD-COORD-01` |
| Q3 | Which other modules import the migration shims? | `GLD-COORD-02` |
| Q4 | If one service's candidate build fails, does the pool proceed single-service? | `GLD-COORD-10` |

**Q2 deserves a view.** `space_targets` already prefers **25 % of the total drive**
over any constant, falling back to 25 GB only when the total is unreadable. The
coordinator's 1000 GB is reached under the *same* condition — total unreadable —
so the two are alternatives for identical circumstances, and one of them scales
while the other does not. If the mount total is unreadable, a percentage is not
available either; but 1000 GB is then an arbitrary absolute on an unknown drive,
which could be most of it.

## 11. Related designs

- [`space_coordinator.md`](./space_coordinator.md) — the feature's own design note
- [`machine_learning/space/DESIGN.md`](../../machine_learning/space/DESIGN.md) — the band, the planners, and §3.1's correction
- [`machine_learning/foundation/DESIGN.md`](../../machine_learning/foundation/DESIGN.md) §3.5 — `downgrade_credit`, the mirror §3.2 contextualises
- [`machine_learning/ledger/DESIGN.md`](../../machine_learning/ledger/DESIGN.md) §3.3 — the run history FORK-D demonstrates
- [`radarr/DESIGN.md`](../radarr/DESIGN.md) · [`sonarr/DESIGN.md`](../sonarr/DESIGN.md)
