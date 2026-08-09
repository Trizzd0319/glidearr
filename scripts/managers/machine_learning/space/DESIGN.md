# space — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **space**

**Package** — `scripts.managers.machine_learning.space`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [`machine_learning/DESIGN.md`](../DESIGN.md) · [`services/coordinator/`](../../services/coordinator/README.md)

---

## 1. Problem statement

A finite disk against a library that only grows. Four properties make naive
answers actively harmful:

1. **Deletion is irreversible and the signal is indirect.** A title nobody has
   touched in three years may be the one someone rewatches annually. Getting it
   wrong is discovered months later.

2. **Hardcoded thresholds don't scale.** 25 GB is generous on a 500 GB drive and
   meaningless on 40 TB. The original code had 25/50/100 GB constants scattered
   across Radarr, Sonarr and the coordinator, each independently maintained.

3. **Reclamation oscillates.** Reclaim to exactly the floor and the next
   download drops you back below it, so the system thrashes — delete, acquire,
   delete — burning indexer budget and destroying files for nothing.

4. **Shrinking is almost always better than deleting.** A 2160p Remux stepped
   down to 1080p Bluray frees most of the space and keeps the title watchable.
   The system should exhaust that option entirely before removing anything.

This package is the answer to all four, and its central policy is worth stating
plainly: **deletion is the true last resort, behind an informed-consent gate that
defaults to off.**

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One user setting (`free_space_limit`) drives every gate. |
| G2 | Thresholds scale with the drive, not with hardcoded GB. |
| G3 | Hysteresis everywhere a band edge exists — no oscillation. |
| G4 | Downgrade is exhausted before deletion is reachable. |
| G5 | No media is deleted without explicit, informed operator consent. |
| G6 | Acquisition tightens *before* deletion territory. |
| G7 | Pure — config reads only, no I/O. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Predicting future growth | The band reacts to current free space. |
| N2 | Per-library quotas | One pool, ranked globally. |
| N3 | Executing anything | Planners emit `DeletePlan` / `QualityPlan`; services apply. |
| N4 | Recovering deleted files | `restore_key` records what was removed; restoration is elsewhere. |

---

## 3. Architecture

### 3.1 The band

```
                          reclaim target
                                │
   free space  ────────────────┼──────────────────────────►
                    T          U = T×(1+headroom)
                    │          │
     free < T       │  T≤free<U│      free ≥ U
   ┌────────────────┼──────────┼────────────────────────┐
   │ BELOW FLOOR    │ PRESSURE │ COMFORTABLE            │
   │ downgrade +    │ hold     │ upgrades + acquisition │
   │ delete up to U │ steady   │ no reclamation         │
   └────────────────┴──────────┴────────────────────────┘
```

`headroom` = `space_pressure_headroom_ratio`, default **0.10**.

The pressure band *is* the hysteresis (G3): reclamation triggers below `T` but
runs until `U`, so the next download does not immediately re-trigger it.

**Fallback chain when `free_space_limit` is unset:**

```
T = 0.25 × total_gb          ← PRESSURE_FALLBACK_FRACTION, scales with the disk (G2)
  ↓ total also unknown
T = fallback_gb (25.0)       ← last resort constant only
     → returns (T, T): no headroom band, matching prior behaviour
```

The sweep to pass `total_gb` (mount-deduped via `instance_manager.disk_total_gb`)
is **complete** — no gate respects a hardcoded GB floor. `UPGRADE_MIN_FREE_GB`
(100) and `DEFAULT_UPGRADE_GB` (50) were deleted.

Two deliberate exceptions remain, justified inline at their call sites:

| Exception | Value | Why |
|---|---|---|
| `universe.DEFAULT_DOWNGRADE_GB` | 10 | Deep low-disk **emergency** trigger for the never-deleted universe class — intentionally a fixed floor, not total-derived |
| `repair/anomaly.py` | `fallback_gb=0.0`, no `total_gb` | A **sentinel** selecting the legacy time-based owned-movie prune when no `free_space_limit` is configured |

### 3.2 Two bands, deliberately different widths (G6)

| Band | Width above floor | Purpose |
|---|---|---|
| Deletion pressure | ~10 % (`space_pressure_headroom_ratio`) | When to reclaim |
| Acquisition tightness | ~30 % (`band`) | When to get selective |

Acquisition becomes choosy well before deletion is on the table. By the time
space is tight enough to delete, acquisition has already been prioritising
high-demand titles for a while.

```
t = 0                          free ≥ floor × 1.30
t = (top − free)/(top − floor) linear ramp
t = 1                          free ≤ floor

demand-aware ranker:  priority = watchability × demand^t
```

The exponent is the elegant part: at `t=0`, `demand⁰ = 1` and demand is inert;
at `t=1` it multiplies directly. One knob smoothly hands control from taste to
reach.

### 3.3 Schmitt-trigger hysteresis on tightness

`tightness_with_hysteresis` widens the release band once engaged:

```
engage  when free < floor × (1 + 0.30)
release when free > floor × (1 + 0.30 + 0.10)
```

`prev_t` is the caller-persisted previous value. A download completing right at
the edge cannot flip the mode back and forth.

Note this is the **second** hysteresis mechanism in the package, independent of
the `T`/`U` band. Both exist because both edges are crossable by a single
download.

### 3.4 Exhaustive downgrade — how deletion becomes last resort (G4)

`space_exhaustive_downgrade` defaults **True**. When on:

- Downgrade planners **drop the watchability-score ceiling** as an eligibility
  filter and no longer stop at a partial `need_gb`. Every title above the 720p
  floor becomes a candidate, still ordered **ascending by watchability** so the
  least-valued shrinks first.
- Delete pools accept **only** items already at or below the 720p floor. A title
  with anything left to shrink can never be deleted.
- The step-down release picker may fall **below** 720 for a title with no ≥720
  release available.

Every other guard is untouched: keep tags, keep-universe, hot-universe credit,
recently watched, recently aired, at/below floor, multi-episode files.

Setting it false restores the historical behaviour **byte-for-byte** (score-ceiling
eligibility, spread stops at `need_gb`, delete pool ignores resolution, picker
hard-floors at 720). That byte-for-byte guarantee is what makes the default safe
to have flipped.

**`DEFAULT_REGRAB_CAP = 200`** bounds the consequence: every realized step-down
deletes a file and queues a smaller replacement, so an unbounded exhaustive pass
over a large library would ask the indexers for thousands of releases in one run.
Items over the cap **keep their files** and re-qualify next run — and, being
still above the floor, stay excluded from deletion.

### 3.5 The double deletion gate (G5)

```python
deletions_enabled(config) ==
    deletions_consented(config)  AND  free_space_limit > 0
```

Two independent conditions, both required, defaulting to off:

| Gate | Source | Answers |
|---|---|---|
| Consent | Onboarding "Media deletion" step, or `RECOMMENDARR_DELETIONS_CONSENT` / `GLIDEARR_DELETIONS_CONSENT` | *May* we delete at all? |
| Floor | `free_space_limit > 0` | *When* should we reclaim? |

A non-empty env var overrides config in **both** directions, so a container can
force consent on or off regardless of `config.json`.

When the gate is closed, every deletion path skips: per-service space-pressure
deletes, grace-marked file deletes, the stale-owned prune's delete stage, and the
cross-service coordinator. Downgrades, monitoring, grace *marking*, playlist
planning and acquisition are unaffected.

`deletions_disabled_reason` exists because of a real bug: the old hardcoded
message *"free_space_limit is not set"* was emitted for a missing-**consent**
gate too, since consent is checked first — so an install *with* a floor but no
consent was told the wrong thing. Now the reason is specific.

### 3.6 Coordinator ownership

`coordinator_owns_deletion` requires **three** conditions —
`space_coordinator_enabled` AND consent AND a floor — and defaults off. When
true, per-service legacy delete paths still **mark** candidates but must defer
the actual deleting to the coordinator's unified, ranked movie+TV pool.

That mark/delete split is what lets one ranking span both services without
either one racing the other.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | One setting derives every threshold | G1 — scattered 25/50/100 GB constants drifted independently | Per-service constants |
| D2 | Fallback to 25 % of total drive | G2 — scales across a 500 GB and a 40 TB library | Fixed GB fallback |
| D3 | Reclaim to `U`, not to `T` | G3 — reclaiming to the trigger point guarantees immediate re-trigger | Reclaim to floor |
| D4 | Acquisition band (30 %) wider than deletion band (10 %) | G6 — get selective before getting destructive | One band |
| D5 | `priority = watchability × demand^t` | One exponent hands control from taste to reach smoothly | Weighted sum |
| D6 | Schmitt trigger on tightness release | A single download must not flip the regime | Symmetric threshold |
| D7 | Exhaustive downgrade **on by default** | G4 — shrinking preserves watchability; deleting does not | Off by default |
| D8 | Delete pool restricted to items at/below the 720p floor | Makes G4 structural rather than advisory — a shrinkable title is *unreachable* by deletion | Ordering preference |
| D9 | `exhaustive_downgrade=false` restores prior behaviour byte-for-byte | Makes flipping the default safe and reversible | Behavioural drift |
| D10 | `DEFAULT_REGRAB_CAP = 200` | Bounds the indexer storm an exhaustive pass creates | Unbounded |
| D11 | Over-cap items keep files and re-qualify next run | Partial progress each run beats a failed atomic pass | Fail the pass |
| D12 | Consent separate from the floor | G5 — "when to reclaim" and "may we delete" are different questions, and conflating them produced a real misreported error | Floor implies consent |
| D13 | Consent defaults **off**; env overrides both ways | An install can never delete media without informed opt-in; containers stay controllable | Default on |
| D14 | Coordinator ownership requires all three gates | Unified deletion is a strictly larger blast radius | Enable with the coordinator alone |
| D15 | Ascending watchability order for downgrade | Least-valued shrinks first | Largest-first |

**D8 is the structural move.** Making deletion *unreachable* for anything still
shrinkable is much stronger than ordering downgrades ahead of deletes — there is
no ordering bug that can skip it.

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No deletion without `deletions_consented` AND `free_space_limit > 0`. |
| I2 | Reclamation targets `U`, never stops at `T`. |
| I3 | With exhaustive downgrade on, a title above the 720p floor cannot be deleted. |
| I4 | `keep-universe` is never deleted. Bare `universe` is last resort. |
| I5 | Downgrade order is ascending by watchability. |
| I6 | Realized downgrades per run ≤ `downgrade_regrab_cap` (0 = unbounded). |
| I7 | Every gate derives from `free_space_limit`, or 25 % of total, or the last-resort constant — in that order. |
| I8 | This package performs no I/O. |
| I9 | `exhaustive_downgrade=false` reproduces historical behaviour byte-for-byte. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| `free_space_limit` unset | `space_targets` | 25 % of total drive; **deletion stays disabled** (I1) | Safe | ✅ `deletions_disabled_reason` |
| Consent absent | `deletions_consented` | All deletion skipped; downgrades continue | Safe | ✅ Specific reason |
| Total drive size unreadable | Fallback chain | 25 GB constant, `(T, T)` with no headroom | 🟡 Band collapses | ❌ **None** |
| Regrab cap hit | Counter | Remaining items keep files, re-qualify next run | Bounded | 🟡 Unclear if reported |
| Free space oscillates at band edge | Schmitt trigger | Mode holds | None | ✅ |
| Nothing left to downgrade, still below floor | Exhaustive pass exhausts | Delete pool becomes reachable — as designed | Intended | ✅ Ledger |
| Unscored title in a pool | [`test_unscored_deferral.py`](./test_unscored_deferral.py) | Deferred | Safe | ✅ Tested |
| Coordinator enabled without consent | `coordinator_owns_deletion` | Returns False; per-service paths keep marking but nothing deletes | Safe | ✅ |
| Env var set to a non-truthy string | `_CONSENT_TRUTHY` | Treated as **false** — overrides a config `true` | Safe direction | ❌ **None** |
| Emergency universe downgrade at 10 GB | `DEFAULT_DOWNGRADE_GB` | Fixed floor, ignores total | Intended exception | ❌ **None** |

Every failure mode here degrades toward **not deleting**, which is the correct
direction. Rows 3, 9 and 10 have no operator signal, but none of them is
destructive.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `free_space_limit` | unset → 25 % of drive | `T`, the floor. **Required for any deletion** |
| `deletions_consent` | `false` | Informed opt-in. **Required for any deletion** |
| `RECOMMENDARR_DELETIONS_CONSENT` / `GLIDEARR_DELETIONS_CONSENT` | — | Env override, both directions |
| `space_pressure_headroom_ratio` | `0.10` | `U = T × (1 + this)` |
| `space_exhaustive_downgrade` | **`true`** | Downgrade everything before anything is deleted |
| `space_downgrade_max_regrabs_per_run` | `200` | Realized downgrade cap; ≤0 unbounded |
| `space_coordinator_enabled` | `false` | Coordinator owns all deletion (plus consent + floor) |
| tightness `band` | `0.30` | Acquisition selectivity band |
| tightness `release_margin` | `0.10` | Schmitt release width |

Constants: `PRESSURE_FALLBACK_GB` 25.0 · `PRESSURE_FALLBACK_FRACTION` 0.25 ·
`DEFAULT_REGRAB_CAP` 200.

---

## 8. Implemented capabilities

- ✅ Single-setting space gating with a three-level fallback chain
- ✅ `(T, U)` pressure band with reclaim-to-`U` hysteresis
- ✅ Complete sweep to drive-relative thresholds — no gate respects a hardcoded GB floor
- ✅ Acquisition tightness with a deliberately wider band than deletion
- ✅ `watchability × demand^t` demand-aware ranking
- ✅ Schmitt-trigger hysteresis on tightness engagement/release
- ✅ Exhaustive downgrade on by default, byte-for-byte reversible
- ✅ Delete pool structurally restricted to at/below-floor items
- ✅ Regrab cap with re-qualify-next-run semantics
- ✅ Double deletion gate — consent AND floor — defaulting off
- ✅ Env override in both directions for headless deployments
- ✅ Specific `deletions_disabled_reason` per gate
- ✅ Coordinator ownership with mark/delete split
- ✅ Delete / downgrade / upgrade / JIT / dedup / dual-version / universe planners
- ✅ 15 test modules

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SPA-01` | **Warn when the band collapses** — `total_gb` unreadable ⇒ `(T, T)` with no headroom, which silently removes the anti-oscillation guarantee | §6 row 3 has no signal and disables I2's protection | S | — |
| `GLD-SPA-02` | **Report regrab-cap saturation** in the run summary — how many items deferred | §6 row 4: partial progress is invisible | S | — |
| `GLD-SPA-03` | **Warn on a non-truthy consent env var** overriding a config `true` | §6 row 9: silently safe, but surprising | S | — |
| `GLD-SPA-04` | **Space forecast** — at current growth, when is the floor reached? | Turns a reactive band into a planning tool | M | `GLD-WEB-16` |
| `GLD-SPA-05` | **Downgrade-vs-delete accounting** each run — GB reclaimed by each path | Proves D7/D8 are working; currently unmeasured | S | `GLD-SCO-09` |
| `GLD-SPA-06` | **Per-mount bands** rather than one global floor | Multi-drive setups currently share one threshold | M | `disk_total_gb` per mount |
| `GLD-SPA-07` | **Document `universe.DEFAULT_DOWNGRADE_GB` (10)** as a named exception in the ladder docs, not only inline | An intentional hardcoded floor is invisible to anyone reading `space_targets` | S | — |
| `GLD-SPA-08` | **Dry-run space projection** — what the plan would free, netted | The signed contract fields already support it | S | `GLD-WEB-02` |
| `GLD-SPA-09` | **Tightness `t` in run stats** so the acquisition regime is visible | The knob is invisible today | S | — |
| `GLD-SPA-10` | **Model regrab-cap re-qualification** — confirm deferred items actually converge rather than starving | Items over the cap re-qualify forever if the cap is always saturated | M | `GLD-SPA-02` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should the fallback `(T, T)` band get synthetic headroom instead of collapsing? | `GLD-SPA-01` |
| Q2 | Is 200 the right regrab cap against measured indexer tolerance? | `GLD-SPA-02` |
| Q3 | Should the 30 %/10 % band split be configurable, or stay a designed constant? | — |
| Q4 | With exhaustive downgrade on by default, is the delete path ever reached in practice? | `GLD-SPA-05` |
| Q5 | Should multi-drive setups get per-mount floors? | `GLD-SPA-06` |

**Q4 is worth measuring.** If exhaustive downgrade means deletion effectively
never fires, the elaborate delete-gating machinery is protecting a path that
doesn't execute — useful to know either way, and `GLD-SPA-05` answers it cheaply.

## 11. Related designs

- [`services/coordinator/space_coordinator.md`](../../services/coordinator/space_coordinator.md)
- [`lifecycle/`](../lifecycle/) — grace, retention, restore policies
- [`sizing/`](../sizing/) — the size estimates these plans consume
- [`scoring/DESIGN.md`](../scoring/DESIGN.md) — the watchability ordering
- [`contracts/DESIGN.md`](../contracts/DESIGN.md) §3.2 — signed space accounting
