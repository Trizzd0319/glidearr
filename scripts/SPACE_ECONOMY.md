# Space Economy — zones, the upgrade ladder, and byte accounting

**Status:** design, not implemented · **Filed:** 2026-08-20
**Register:** `GLD-SPC-01` … `GLD-SPC-08` (see §8) · converges `GLD-ACQS-16`, `GLD-SPA-00`–`-04`

---

## 1. What changed, and why this doc exists

Until 2026-08-20 the array sat 300+ GB clear of the pressure band, so every space
gate was answering the same question — *"is there room?"* — and the answer was
always yes. Two things changed on the same day:

* The byte budget **bound for the first time**: 97 funded (~312 GB), 33 refused
  (~91 GB) against ~325 GB of headroom.
* The operator lowered `free_space_limit` 3000 → 2500, and stated the policy this
  document formalises: *supply will eventually run out, and the surplus should be
  spent climbing quality tiers rather than sitting idle.*

That converts a system that **acquires until full** into one that **acquires,
then improves what it has** — and it parks the array near the band by design
rather than by accident. Everything below follows from that.

---

## 2. The three zones

`U` (band top) = `free_space_limit` + 10%. At `free_space_limit: 2500`, `U = 2750`.

```
free > U (2750)          ACQUIRE      new titles; the byte budget still applies
2500 .. 2750             UPGRADE      the 250 GB fuel tank; no new acquisition
free < 2500 (floor)      DOWNGRADE    reclaim, then delete as last resort
```

**The band is 250 GB — 10% of the FLOOR, not of free space.** This is how the
code already computes it and it is the better definition: a fraction of free
space would shrink exactly as the array fills, narrowing the upgrade zone
precisely when the system most needs a stable one. At ~15 GB/movie the tank is
~16 movie upgrades or a great many episode upgrades per cycle.

**Priority inversion is now geometric, not procedural.** Acquisition stops at
2750; upgrades begin below it. The two never compete for the same byte, and no
rule has to be remembered to keep them apart.

---

## 3. Oscillation: already prevented, and NOT by the thresholds

The obvious worry is a knife edge — a run at 2501 upgrades, a run at 2499
downgrades, each cycle costing a full re-download. **This cannot happen, and the
reason is not the band.** It is that no title is eligible for both passes.

Verified in the code 2026-08-20:

| | downgrade targets | upgrade targets | gap |
|---|---|---|---|
| **Movies** | score < `WATCHABILITY_PROTECT_THRESHOLD` = **6** (widened to `space_pressure_score_ceiling` = **17** when `space_pressure_downgrade_before_delete` is on) | 4K eligibility ≥ **70** | 6→70, or 17→70 |
| **TV** | `cold_tv_reclaim`: **unwatched**, non-pilot, score < `score_floor` = **20** | JIT: **actively watched / next-up** | disjoint on score AND on watched-state |

So the populations are disjoint by construction on both sides, with a very wide
margin. An array-level threshold cannot create churn because crossing it says
nothing about *which* title moves — and the titles the two passes select do not
overlap.

**What this means for the design:** the band does not need to be wide enough to
damp oscillation, because oscillation is not possible. The band's job is only to
separate acquisition from upgrades. 250 GB is sufficient for that.

**The one real cycle is deliberate**: JIT raises an episode before you watch it
and `run_jit_quality_restores` rolls it back after. That is the feature working,
not churn — it is bounded, watch-triggered, and snapshot-backed.

**Residual risk to hold, not fix yet.** The guarantee currently rests on two
score constants that live in different packages and were never written down as a
*pair*. If either drifts toward the other, the guarantee silently weakens with
no detector. → `GLD-SPC-06`.

---

## 4. Net vs gross — the accounting that makes upgrades affordable

**Operator ruling:** *dropping below the band for a quality upgrade is acceptable
as long as the older file is removed and the result lands at the floor (not
floor +10%).*

Two changes in one, both correct for an upgrade:

* **net, not gross** — an upgrade is a swap, so the charge is `size_new − size_old`,
  not `size_new`. Charging it like an acquisition has been over-refusing upgrades.
* **against the floor, not `U`** — upgrades may consume the band; that is what the
  band is for.

### 4.1 But the DISK sees gross, concurrently

Per-upgrade net is the right *policy*; it is the wrong *ledger entry*. Twenty
concurrent 20 GB upgrades are a 400 GB transient dip no matter how small each net
is. With 189 movies marked upgrade in the 2026-08-20 run, that is not
hypothetical.

**Rule: charge GROSS at grab, credit the DELTA when the reclaim is confirmed.**
Same shape as the committed-bytes ledger (`GLD-ACQS-14`) — and the credit needs a
confirmation source, which is §4.2.

### 4.2 Three reasons the reclaim may not land

The whole policy rests on the old file actually going away.

1. **The recycle bin.** Sonarr *retires* a replaced file into the bin on import; it
   does not delete it. Under retention the space is not reclaimed for days, so
   successive upgrades stack their gross cost while the ledger believes each was
   nearly free. `bin_forecast` already models this and recycle-bin awareness
   (`GLD-SPA-00`–`-04`) is staged but **not deployed** — this policy is what makes
   it load-bearing.
2. **Hardlinks under TRaSH.** If the old file is still hardlinked from a seeding
   torrent, unlinking the media-root copy frees nothing until the torrent is
   removed. Not currently biting (recent grabs went via SABnzbd) but the
   qBittorrent path is live.
3. **`deletions_consent`.** Whether an upgrade's replacement removal is governed by
   that flag, or is an import-time retirement outside it, is **unverified**. If it
   is gated, every upgrade is pure gross consumption. → verify before `GLD-SPC-03`.

### 4.3 Concurrency bound

"Net to the floor" is satisfiable on paper while the array briefly sits well under
it. The upgrade pass therefore needs either a transient allowance beneath the
floor or a cap on simultaneous in-flight upgrade bytes. The latter is simpler and
reuses `BudgetContext`.

---

## 5. The ladder

Climb `720p → 1080p → 2160p`, **one step per run**, so a bad tier choice costs one
hop rather than a 720→2160 jump.

**Tier selection is a household constraint, not a score.** The step is the highest
tier *every* member who might watch it can actually play — remote-play capability
is the binding factor, and `can_remote_play` + `dual_min_score` already encode
this for the 4K decision. A tier no one can direct-play is a downgrade wearing an
upgrade's name.

**Priority is summed watchability across up-nexts.** A show five members are
mid-way through outranks one member's. The inputs exist: per-user affinity
matrices (now link-merged, `GLD-TAUT-15`), the next-episode planner, and the
resumption planner.

---

## 6. Invariants

| # | Invariant |
|---|---|
| I1 | Acquisition never spends below `U`; upgrades never spend below the floor. |
| I2 | An upgrade is charged GROSS at grab and credited the delta only on a CONFIRMED reclaim — never credited optimistically at plan time. |
| I3 | No title is eligible for both the upgrade and the downgrade pass, in any zone. The score gap (§3) is the guarantee; the band is not. |
| I4 | The ladder advances at most one tier per title per run. |
| I5 | A tier no household member can play is never selected, whatever the score. |
| I6 | Every byte grabbed by ANY path — acquisition, upgrade, universe, legacy regrab, JIT — passes through the committed ledger. Invisible bytes are the defect `GLD-ACQS-16` names. |

---

## 7. What this converges

This design is not new machinery so much as a budget wrapped around machinery
that already exists and currently runs unmetered:

* JIT quality upgrades (watch-driven, snapshot-backed, with restore)
* Active-watcher profile upgrades
* Universe quality (189 movies marked upgrade, 2026-08-20)
* Legacy-codec regrab (69 eligible after cooldown)
* Next-episode and resumption planners

---

## 8. Phasing

Deliberately ordered so each phase is observable before the next depends on it.

| Phase | ID | Work | Depends on |
|---|---|---|---|
| **0** | `GLD-SPC-01` | **Measure before changing anything.** One run's worth of instrumentation: total bytes grabbed per path (acquisition / universe / legacy / JIT), so the size of the unmetered flow is known rather than assumed. No behaviour change. | — |
| **0** | `GLD-SPC-02` | **Verify the reclaim path.** Does an upgrade's replacement removal honour `deletions_consent`? Does the bin hold it? Answers whether §4 is affordable at all. | — |
| **1** | `GLD-SPC-03` | **Route every path through the committed ledger** — closes `GLD-ACQS-16`. Gross charge at grab; no credit yet. Ledger becomes truthful before it becomes clever. | `-01`, `-02` |
| **1** | `GLD-SPC-04` | **Deploy recycle-bin awareness** (`GLD-SPA-00`–`-04`) as the reclaim-confirmation source. | `-02` |
| **2** | `GLD-SPC-05` | **Delta credit on confirmed reclaim** + in-flight concurrency bound (§4.3). Upgrades become net-priced. | `-03`, `-04` |
| **2** | `GLD-SPC-06` | **Pin the disjointness guarantee** (§3): assert the upgrade/downgrade score gap in a test, and log a warning if config narrows it. Cheap, and it is the only thing standing between this design and churn. | — |
| **3** | `GLD-SPC-07` | **Surplus routing** — the three zones (§2). Acquisition funds first from `free − U`; upgrades spend the remainder down to the floor. | `-05` |
| **3** | `GLD-SPC-08` | **The ladder** (§5) — one step per run, household-playable tier, summed-watchability priority. | `-07` |

**Phase 0 is not optional.** The 2026-08-20 run shows three uncapped paths active
simultaneously; nobody currently knows how many bytes they move. Designing a
budget around an unmeasured flow is how the phantom-headroom bug (`GLD-RST-05`)
happened the first time.

---

## 9. Open questions

| # | Question | Blocks |
|---|---|---|
| Q1 | Does an upgrade's replacement removal honour `deletions_consent`? | `GLD-SPC-02`, all of §4 |
| Q2 | What is the bin's retention, and does `bin_forecast` already expose reclaim timing usable as a credit signal? | `GLD-SPC-04` |
| Q3 | Should the ladder run at all while `deletions_consent: false` — i.e. can it upgrade if it cannot reclaim? | `GLD-SPC-08` |
| Q4 | Do universe upgrades respect the household-playable tier rule, or do they climb on their own logic? | `GLD-SPC-08`, I5 |
| Q5 | Is 250 GB the right tank at 2500? It was sized against a 3000 floor. | `GLD-SPC-07` |

---

## 10. Related

* [`services/acquisition/DESIGN.md`](./managers/services/acquisition/DESIGN.md) — byte budget, I8–I10
* [`machine_learning/sizing/DESIGN.md`](./managers/machine_learning/sizing/DESIGN.md) — the MiB/min model, caps, the calibration ratchet
* [`ENHANCEMENTS.md`](./ENHANCEMENTS.md) §4.46 — `GLD-ACQS-13`…`-19`
