# acquisition — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **acquisition**

**Package** — `scripts.managers.machine_learning.acquisition`
**Status** — ✅ Implemented · 🟢 Full test coverage · 🟡 One cross-package dependency worth naming
**Related** — [README.md](./README.md) · [`DESIGN_series_saga_resumption.md`](../DESIGN_series_saga_resumption.md) · [`space/DESIGN.md`](../space/DESIGN.md)

---

## 1. Problem statement

Acquisition has a different economics from every other decision in this system,
and the difference is stated in one line:

> A downloaded file is **SHARED**.

Deletion, quality and retention all reason about *one title's worth to the
household*. Acquisition reasons about **cost per person served**. A 60 GB film one
person will watch and a 60 GB film four people will watch cost the same disk and
deliver very different value — and watchability, being a household aggregate,
cannot distinguish them.

Three further problems:

1. **Breadth only matters when space is scarce.** With 8 TB free, grabbing a
   niche favourite costs nothing. At the floor, every grab displaces another.
   A fixed weighting is wrong at both ends.

2. **A new account looks like a disinterested one.** Summing per-user interest
   penalises a household member with no watch history yet — their zero drags the
   breadth signal down exactly as if they had actively disliked everything.

3. **Timing is a signal nobody was using.** *"the codebase had no countdown/ramp
   logic before this."* Re-acquiring a series' prior seasons is worth far more in
   the fortnight before a new season drops than at any other time — and worth
   nothing eighteen months later.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Value a grab by how many people it serves, not just how much. |
| G2 | Breadth's influence scales with scarcity, smoothly. |
| G3 | A no-history account never penalises a candidate. |
| G4 | Re-acquisition peaks *before* the moment, not at it. |
| G5 | Undated content never floats to the top. |
| G6 | Pure — no clock, no I/O. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Monitoring or searching | *"Services do the monitor/search APPLY."* |
| N2 | Enumerating candidates | Service-side; the brain ranks what it is given. |
| N3 | Computing affinity | Passed in from `affinity/`. |
| N4 | Computing tightness | Passed in from `space/tightness`. |

---

## 3. Architecture

### 3.1 `watchability × demand^t` — one exponent, two regimes

```
demand   = Σ_users  P(user watches)
priority = watchability × demand^t
```

| `t` | `demand^t` | Behaviour |
|---|---|---|
| 0 | 1 | Demand inert — pure watchability, *"grab broadly"* |
| 1 | `demand` | Full breadth weighting — *"a 3-user title outranks a 1-user title 3:1"* |

The exponent is doing something a weighted sum cannot: at `t = 0` demand vanishes
**exactly**, not approximately, so the roomy regime is byte-identical to
watchability-only ranking. And the transition is continuous — there is no
threshold at which behaviour jumps.

One consequence is stated and worth keeping: *"A 0-demand candidate is neutral
when roomy but falls to 0 as space tightens — grabbed only in genuine
abundance."* Since `0^0 = 1` and `0^t = 0` for `t > 0`, a title nobody is
predicted to watch is grabbable only while `t` is exactly zero. That is a sharp
edge, and it is the right one: the moment tightening begins at all, unwanted
titles stop competing.

This is the same exponent design as [`space/DESIGN.md`](../space/DESIGN.md) §3.2
describes from the other side. The two packages agree by construction because
`t` has one definition.

### 3.2 ✅ Cold start contributes the prior, not zero (G3)

```python
if aff:
    m = genre_match(genres, aff, **gm_opts)
    total += m if m >= threshold else 0.0
else:
    total += pop        # cold-start: no taste signal → the popularity prior
```

> a user with NO affinity (cold start) contributes the `popularity` prior (0–1)
> instead of a flat zero, so a no-history account doesn't drag the breadth signal
> down

Another §8 **P-C** instance handled correctly, and a subtle one: the naive
implementation is a single `genre_match` call per user, which returns ~0 for an
empty affinity dict. That would make every household with a new account
systematically under-value every candidate — and it would look exactly like
lower demand.

Note the two-level guard: an affinity that exists but matches below `threshold`
contributes **0** (*"a near-zero interest doesn't count"*), while an affinity that
does not exist contributes `popularity`. Absent and weak are treated differently,
which is precisely the distinction §8 P-C is about.

The `threshold = 0.15` default matches
[`quality_analytics/likely_viewers`](../quality_analytics/DESIGN.md)'s viewer
threshold exactly. Both answer *"is this genre match real?"* — worth confirming
the agreement is intentional rather than coincidental (`GLD-ACQ-04`).

### 3.3 The resumption ramp

```
d > W          → 0
R₀ < d ≤ W     → 100·(W − d)/(W − R₀)
−G ≤ d ≤ R₀    → 100
d < −G         → 100·exp(−(−d − G)/τ)
```

Three design choices, each justified in-source:

**It peaks at `R₀ = 7`, not at `d = 0`** — *"be caught up a little EARLY so the
re-grab can finish."* Peaking on release day would start the download when the
household is already sitting down. The ramp is timed to the *download*, not the
event.

**It holds maximum through `G = 14` days after release** — *"you can still catch
up if it just dropped."* A household that starts a week late is still a household
that wants the back catalogue.

**It decays rather than cliff-edging** — `τ = 30`, half-life ≈ 21 days. A season
that dropped two months ago is still mildly relevant; one from last year is not.

**Continuity check.** The docstring claims *"Boundaries are continuous"*, and it
holds at all three:

| Boundary | Left | Right |
|---|---|---|
| `d = W` | linear → `100·(W−W)/(W−R₀)` = 0 | `0` ✓ |
| `d = R₀` | linear → `100·(W−R₀)/(W−R₀)` = 100 | hold → 100 ✓ |
| `d = −G` | hold → 100 | decay → `100·exp(0)` = 100 ✓ |

A discontinuity here would make priority jump as a date ticks over, which is the
kind of thing that surfaces as "why did this suddenly appear at the top."

### 3.4 Undated content sinks (G5, and D36 again)

> A non-numeric / undated `d` → **0**, so undated content is never floated to the
> top.

Consistent with the fail-direction rule articulated in
[`discovery/DESIGN.md`](../discovery/DESIGN.md) §3.3: **on unknown input, fail
toward the outcome that changes nothing.** An undated release contributes no
proximity urgency rather than a default one — nothing is acquired on the strength
of a date nobody knows.

Same pattern in `_num`: any non-numeric input coerces to `0.0`, and `priority`
clamps to `[0, 100]` *"so weights summing > 1 can't overflow."*

### 3.5 🟡 The brain depends on `playlists`

```python
from scripts.managers.machine_learning.playlists.per_user import genre_match
```

`acquisition` imports from `playlists`. Both are guarded subpackages so
`brain_purity` is satisfied, and reusing `genre_match` rather than
re-implementing it is exactly the anti-drift discipline
[`foundation/`](../foundation/DESIGN.md) argues for.

But the dependency direction is surprising: *what to acquire* now depends on *how
to order a Plex playlist*. `genre_match` is a general affinity primitive that
happens to live in a presentation package, and two consumers now rely on it from
there.

Not a defect — but if a third consumer appears, `genre_match` belongs in
[`affinity/`](../affinity/README.md) beside the maps it consumes.
`GLD-ACQ-03`.

### 3.6 Coverage note

[`pilot_stepping.py`](./pilot_stepping.py) (24.3 KB),
[`next_episode_planner.py`](./next_episode_planner.py) (13.3 KB) and
[`enrichment_prioritizer.py`](./enrichment_prioritizer.py) (7.0 KB) were **not
read** — ~45 KB including the largest module in the package. Nothing here asserts
their behaviour.

What the inventory does show: **every module has a matching test**, and the tests
are consistently as large as or larger than their sources — `pilot_stepping`
24.3 KB against 18.4 KB of tests, `next_episode_planner` 13.3 KB against 14.0 KB.
That is the best source-to-test ratio in the brain, and a marked contrast with
[`playlists/`](../playlists/DESIGN.md) §3.4 where the spoiler invariant has no
test at all.

[`test_pilot_interactive.py`](./test_pilot_interactive.py) has no source twin
here — the module it names lives in `sonarr/cache/pilot_interactive.py`. Either
it tests the interactive path *through* `pilot_stepping`, or a brain test is
covering a service module. Same shape as `playlists/test_recency.py`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Value a grab by breadth | G1 — *"a downloaded file is SHARED"*; watchability is a household aggregate and cannot express reach | Watchability alone |
| D2 | `demand^t`, not a weighted sum | G2 — at `t=0` demand vanishes exactly, so the roomy regime is unchanged | `α·w + β·d` |
| D3 | Cold start → popularity prior | G3 — a new account must not read as a disinterested one | Flat zero |
| D4 | Threshold weak matches to 0, but not absent ones | *"a near-zero interest isn't real demand"* — absent ≠ weak | One rule for both |
| D5 | Ramp peaks at `R₀`, before release | G4 — timed to the download completing, not the event | Peak at `d = 0` |
| D6 | Hold maximum through grace | A household starting a week late still wants the back catalogue | Drop at release |
| D7 | Exponential decay, not a cliff | Relevance fades; it does not end | Hard cutoff |
| D8 | Continuous at every boundary | Priority must not jump as a date ticks over | Piecewise without matching |
| D9 | Undated ⇒ 0 | G5 — nothing acquired on an unknown date | Default to mid-ramp |
| D10 | `days_to_release is None` ⇒ `d := R₀` | Trigger-1 *"return now"* — returning **is** now | Treat as undated |
| D11 | Clamp `priority` to `[0,100]` | *"so weights summing > 1 can't overflow"* | Trust the config |
| D12 | Reuse `genre_match` from `playlists` | Anti-drift — one affinity primitive | Re-implement locally |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | At `t = 0`, `demand_priority == watchability` exactly. |
| I2 | `t` is clamped to `[0, 1]`. |
| I3 | A user with no affinity contributes the popularity prior, never 0. |
| I4 | `R(d) ∈ [0, 100]`, continuous at every boundary. |
| I5 | `R(d) = 0` for undated or non-numeric `d`. |
| I6 | `priority ∈ [0, 100]` regardless of configured weights. |
| I7 | This package plans only — no monitor, no search, no I/O, no clock. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| User has no affinity | Explicit branch | Popularity prior (D3) | Correct | ✅ By design |
| Popularity unknown | Clamp to `[0,1]`, default 0 | Cold user contributes 0 | 🟡 Reverts to the penalty D3 avoids | ❌ **None** |
| All users below threshold | Sum | `demand = 0` → grabbable only at `t = 0` | Intended | ❌ None |
| Release date unknown | `_num` → 0 | Ramp 0 — never floated (G5) | Safe | ❌ None |
| `τ ≤ 0` configured | Guard | Falls to 0 past grace | Safe | ❌ None |
| Weights sum > 1 | Clamp | Capped at 100 | Safe | ✅ Documented |
| `t` never reaches 0 | — | Zero-demand candidates never acquired | 🟡 Intended, but invisible | ❌ **None** |
| `genre_match` semantics change in `playlists` | **None** | Demand shifts library-wide | 🟡 §3.5 | ❌ **None** |

**Row 2 is worth noting.** The cold-start fix depends on a usable `popularity`
value. If popularity is missing or zero, a cold user contributes 0 — reinstating
exactly the penalty D3 exists to prevent, silently. The guard is correct; its
input is unguarded.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `acquisition.resumption.ramp_window_days` | 60 | `W` |
| `acquisition.resumption.ready_by_days` | 7 | `R₀`, the peak |
| `acquisition.resumption.grace_window_days` | 14 | `G` |
| `acquisition.resumption.decay_tau_days` | 30 | `τ`, half-life ≈ 21 d |
| `acquisition.resumption.weight_affinity` | 0.5 | `w_s` |
| `acquisition.resumption.weight_proximity` | 0.5 | `w_r` |
| demand `threshold` | 0.15 | Minimum genre match counting as demand |

`t` comes from [`space/tightness`](../space/README.md); `popularity` and the
per-user affinities from the service.

---

## 8. Implemented capabilities

- ✅ Breadth-weighted acquisition priority with a scarcity-scaled exponent
- ✅ Cold-start popularity prior distinguishing absent from weak affinity
- ✅ Thresholded per-user genre match reusing the shared primitive
- ✅ Four-segment release-proximity ramp, continuous at every boundary
- ✅ Early peak timed to download completion, plus a post-release grace hold
- ✅ Exponential relevance decay with a configurable half-life
- ✅ Affinity / proximity blend, clamped against weight overflow
- ✅ Trigger-1 "return now" mapping to the ramp peak
- ✅ Undated content structurally excluded from the top
- ✅ Pilot profile stepping, next-episode budgeting, enrichment prioritisation
- ✅ A matching test module for every source module, tests ≥ source size

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-ACQ-01` | **Guard the `popularity` input** — a missing or zero popularity reinstates the cold-start penalty D3 exists to prevent | §6 row 2: the fix is correct, its input is not | S | — |
| `GLD-ACQ-02` | **Report demand and `t` per acquisition decision** | The whole breadth model is invisible in the run output; `GLD-SPA-09` covers `t` alone | S | `GLD-SPA-09` |
| `GLD-ACQ-03` | 🟡 **Move `genre_match` to `affinity/`** — two brain packages now import it from `playlists`, a presentation package | §3.5 — the dependency direction is surprising and will get more so | S | — |
| `GLD-ACQ-04` | **Confirm the two `0.15` thresholds are deliberately aligned** — `demand` and `likely_viewers` both use it for *"is this genre match real?"* | Either document the shared constant or note the coincidence | S | `GLD-ACQ-03` |
| `GLD-ACQ-05` | **Read and document `pilot_stepping`, `next_episode_planner`, `enrichment_prioritizer`** | §3.6 — ~45 KB unread, including the largest module | M | — |
| `GLD-ACQ-06` | **Resolve `test_pilot_interactive.py`'s missing source twin** | Same shape as `playlists/test_recency.py` | S | `GLD-PLY-08` |
| `GLD-ACQ-07` | **Validate the ramp constants against real release cadence** — is 7 days enough for a re-grab to finish at this library's sizes? | `R₀` is timed to download completion and never measured against actual grab times | M | `GLD-SIZ-01` |
| `GLD-ACQ-08` | **Surface resumption candidates before they are acquired** | The ramp decides re-acquisition with no preview | M | `GLD-WEB-02` |
| `GLD-ACQ-09` | **Route the demand threshold through `thresholds/registry`** | A hand-set cutoff absent from `THRESHOLD_SPECS`, like `GLD-DIS-09` | S | `GLD-THR-01` |
| `GLD-ACQ-10` | **Test the `t = 0` byte-identity claim** — I1 is the property that makes the roomy regime safe | Nothing pins it | S | `GLD-PLY-10` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is `popularity` reliably populated for unowned candidates? If not, D3's cold-start fix is inert. | `GLD-ACQ-01` |
| Q2 | Is `ready_by_days = 7` enough for a 60 GB re-grab to complete at this household's throughput? | `GLD-ACQ-07` |
| Q3 | Should `genre_match` live in `affinity/`? *(= D39)* | `GLD-ACQ-03` |
| Q4 | Are the two `0.15` thresholds one constant or two coincidences? | `GLD-ACQ-04` |

**Q1 is the one that could quietly matter.** The cold-start branch is a genuinely
good piece of design, and it depends entirely on `popularity` being present for
candidates a new household member might want. If the service passes `0` — or the
field is absent for unowned titles — the branch executes, adds nothing, and the
new account penalises every candidate exactly as if the fix were not there.

## 11. Related designs

- [`DESIGN_series_saga_resumption.md`](../DESIGN_series_saga_resumption.md) §4 — the ramp's specification
- [`space/DESIGN.md`](../space/DESIGN.md) §3.2 — the tightness `t` this consumes, described from the other side
- [`discovery/DESIGN.md`](../discovery/DESIGN.md) §3.3 — the fail-direction rule §3.4 follows
- [`playlists/DESIGN.md`](../playlists/DESIGN.md) — home of the shared `genre_match`
- [`services/acquisition/README.md`](../../services/acquisition/README.md) — the APPLY side
