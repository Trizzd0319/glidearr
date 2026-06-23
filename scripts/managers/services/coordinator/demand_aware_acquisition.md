# Demand-aware acquisition

**Goal.** Make the acquisition GRAB decision value-maximizing under space pressure. A downloaded file is
**shared**, so the honest value of a grab is *utility per GB = (everyone who'll watch it) / size*. When
free space approaches the floor, weight a candidate by the **breadth of household demand** (how many users
× how likely each is to watch) so the shrinking budget fills with media many people will watch. When space
is plentiful, there's no contention — serve every individual taste, single-user matches included. The
result: maximize aggregate household value when GB are scarce, maximize individual satisfaction when they
aren't.

**Status.** Designed, NOT built. Extends the [This Week in History](this_week_in_history.md) adaptive
budget (its Phase 5) and the main acquisition pipeline; this is a general policy, not specific to the
discovery shelf. Built on signals that already exist (per-user affinity, `disk_free_gb` + `space_targets`
floor). Originates from the household's own framing: "if there's ≤30% headroom to the floor, prioritize the
number of users who'd watch a title over a title only one user wants; if space isn't tight, grab everything
that matches anyone's taste."

## Concept
- **Tightness ramp — continuous, NOT a binary switch.** A hard flip at exactly 30% thrashes: free space
  hovers on the line and acquisition oscillates `grab broadly → hit floor → delete → grab broadly`. Instead
  a *tightness* `t ∈ [0,1]` climbs smoothly as headroom shrinks: `t=0` comfortably above the floor (~30%
  above), `t=1` at the floor. Add **hysteresis** so `t` only falls once free space recovers past a *higher*
  mark than where it rose.
- **Demand = Σ per-user watch-LIKELIHOOD, not a raw head-count.** Two people weakly curious is worth less
  than one who'll definitely watch it. `demand(c) = Σ_users P(user u watches c)` from per-user genre
  affinity (`tautulli/users/<safe>/affinity`) + per-user people affinity (`people_matrix`) + the
  `watch_likelihood` signal, with a per-user interest threshold so a near-zero match doesn't count.
- **The blend.** `priority = watchability × demand^t`.
  - `t=0` (roomy): `demand^0 = 1` → demand is neutral, breadth-of-taste wins, grab widely.
  - `t=1` (at floor): `priority = watchability × demand` → a 3-user title outranks a 1-user title 3:1, so
    the scarce budget fills with broad-appeal media.
  Multiplicative (demand SCALES value, isn't additive noise); the `t` exponent makes demand inert when
  roomy. Composes with the adaptive **budget**, which sizes *how many* grabs (`C_space` shrinks with free
  space) — this picks *which ones*. Two levers, same direction.
- **Fairness floor.** Pure demand-weighting under tight space means a single user's unique genre NEVER
  wins. Reserve a small share of even the tight-space budget for each active user's TOP personal pick (if
  it clears the quality floor), so a niche taste isn't permanently starved.
- **Quality floor still gates everything.** High demand never excuses a sub-floor watchability/popularity
  title — the existing fail-closed watchability + min-popularity floor (the discovery scorer) is a hard
  precondition before demand weighting applies.

## What's reusable vs net-new
**Reusable:** per-user genre affinity (`tautulli/users/<safe>/affinity`) + the `genre_match`/`priority_score`
per-user layer (already drives every per-user playlist, incl. the anniversary shelf); per-user people
affinity (`people_matrix`); the `watch_likelihood` signal; `storage/space.disk_free_gb` + `space_targets`
(free + the `free_space_limit` floor); the `AcquisitionScorer` base watchability; the This Week adaptive
budget (`budget = clamp(min(C_consume, C_space) × Q × D_backlog, 0, HARD_MAX)`).

**Net-new — must be built:**
- **Per-user demand AGGREGATION.** `AcquisitionScorer` reads the household-AVERAGE `tautulli/affinity`
  today — there is no Σ-over-users breadth signal. Need `demand(c) = Σ_u P(watch)` from the per-user
  matrices, with a per-user threshold + a cold-start popularity fallback for no-history users.
- **The tightness ramp `t` + hysteresis** from the free/floor band (a small pure function over the space
  signals — testable at the band edges).
- **The `watchability × demand^t` blend** in the acquisition ranker (behind a flag; default off → today's
  household-average behavior, byte-identical).
- **The fairness-floor reservation** under scarcity.
- A **per-user watch-likelihood** model if `watch_likelihood` isn't already per-(user, candidate) predictive.

## Locked decisions (proposed)
- **Continuous ramp + hysteresis, never a binary 30% switch.** 30% is the configurable ramp START
  (headroom as a fraction of the buffer band), the floor is `t=1`.
- **Demand = Σ_u P(watch)**, per-user-thresholded + normalized; one grab serves all matching users (never
  double-count) — that shared-cost fact is exactly why breadth is the right currency.
- **`priority = watchability × demand^t`** (multiplicative; `t` exponent neutralizes demand when roomy).
- **Fairness floor:** ≥1 reserved budget-share per active user for their top pick under scarcity.
- **Demand feeds the GRAB ranking ONLY — never the deletion model.** A demand/discovery signal must not
  create a delete-eligibility negative (mirrors the This Week affinity isolation: the shared
  `tautulli/affinity` that `space_pressure` reads for deletions never receives a demand-derived delta).

## The tightness signal
```
t = clamp( (floor·(1+band) − free) / (floor·band),  0,  1 )      # band default 0.30
#   t = 0 when free ≥ 1.30·floor (≥30% above the floor)
#   t = 1 when free ≤ floor
```
(Or measure headroom against a configurable *comfortable target* GB rather than `floor·1.3` if the floor is
small.) Hysteresis: once `t` rises, hold it until free recovers past `floor·(1+band+margin)`, so a title
finishing downloading right at the line doesn't flip the mode back and forth.

## Worked intuition
Tight week, budget sizes to ~2 grabs from 5 candidates that each clear the quality floor:
`A` (3 users' tastes, watchability 70), `B` (3 users, 60), `C` (1 user, 90), `D` (1 user, 85), `E` (2
users, 65). With `t≈1`: `priority = watchability × demand` → A 210, B 180, E 130, C 90, D 85 → grab **A, B**
(broad appeal) even though C/D score higher in isolation. Roomy week (`t=0`): demand-neutral, the budget is
large, grab all five — every taste served. The fairness floor still guarantees the top single-user pick
(C) a reserved shot under scarcity if its owner has had nothing recently.

## Edge cases / guards
- **One grab serves all matching users** — demand is breadth, not N separate grabs of the same file.
- **Cold-start users** (no history) fall back to the popularity prior, never inflating/deflating demand
  with a phantom match.
- **Junk-but-popular** must still fail the quality floor — demand applies AFTER the floor, never instead.
- **Coherence with deletions.** Under scarcity the space coordinator may delete low-value OWNED content
  while acquisition demand-weights new grabs; keep them aligned and the demand signal isolated from the
  delete path.
- **No oscillation** — the continuous ramp + hysteresis is the defense; a stress test must prove the fixed
  point is stable, not a limit cycle.

## Phases
| # | Phase |
|---|---|
| 1 | **Tightness signal** `t` (from `disk_free_gb` + `space_targets`) + hysteresis, tested at the band edges |
| 2 | **Per-user demand aggregation** — `Σ_u P(watch)` from per-user affinity (+ people, + watch_likelihood), per-user interest threshold + cold-start popularity fallback |
| 3 | **The `demand^t` blend** in the acquisition ranker — flag-gated, default-off byte-identical |
| 4 | **Fairness-floor reservation** under scarcity |
| 5 | **Stress test** — disk full / one-user household / everyone-likes-everything / nobody-watches → sane; PROVE no mode oscillation — BEFORE it governs real grabs |
| 6 | **Wire to the This Week budget** (its Phase 5) so discovery + library acquisition share the policy |

## Risks / still-open
- Calibrating `band`, the per-user interest threshold, and the fairness share against the household's REAL
  Tautulli consumption (not the sparse `is_watched`).
- A per-user watch-likelihood that's predictive enough to weight demand (vs. a flat genre-affinity proxy).
- Keeping demand strictly out of the delete path while both run during the same tight-space window.

## See also
- [this_week_in_history.md](this_week_in_history.md) (the adaptive budget this extends + the affinity
  isolation it mirrors), [universe_acquisition.md](universe_acquisition.md) (franchise-first budget
  sharing), `space_targets` / `space_pressure` (the floor + the delete path demand must stay isolated
  from), the acquisition scorer (`services/acquisition/scorer.py`), the per-user playlist layer
  (`machine_learning/playlists/per_user.py`).
