# acquisition — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **acquisition**

**Manager** — `AcquisitionManager` (`scripts/managers/services/acquisition/__init__.py`)
**Status** — ✅ Implemented · 🟢 Best test ratio in `services/` · 🟡 A hardcoded year in the recency signal
**Existing docs** — [`README.md`](./README.md) (12.8 KB)

> **This document does not restate [`README.md`](./README.md).** It adds the
> 11-section frame the [conventions](../../../DOCS_CONVENTIONS.md) require, and
> records the cross-package findings the `machine_learning/` sweep set up.

---

## 1. Problem statement

Acquisition is the only subsystem that **spends** — disk, indexer budget, and the
household's patience. Every other decision reallocates what is already there.

Four problems specific to spending:

1. **Candidates arrive from nine feeds of wildly different trustworthiness.** A
   Plex watchlist entry is the household saying *"watch this."* A MAL seasonal
   listing is *"this exists."* Treating them alike wastes the budget on things
   nobody asked for.

2. **Signals are unevenly present.** An unowned candidate may have a Trakt
   rating, or vote counts, or neither. A scorer that reads a missing signal as
   zero systematically under-ranks sparse candidates — which correlates with
   obscurity, not with undesirability.

3. **Aggregating genre evidence is subtly easy to get backwards.** §3.2 records a
   measured instance where it was.

4. **Every add must be explainable.** *"Why did it grab that?"* is the first
   question, and the answer has to survive the run.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | A 0–100 total plus a per-component matrix — every add is transparent. |
| G2 | Missing signals are dropped from the average, never counted as zero. |
| G3 | More matched evidence can only raise a score. |
| G4 | Adding a new signal cannot perturb candidates that lack it. |
| G5 | Explicit intent outranks suggestion, and both outrank mere existence. |
| G6 | A scorer built without a config is byte-identical to the pre-config one. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Deciding *whether* to spend | `space/tightness` and `acquisition/demand` set the regime. |
| N2 | Scoring owned titles | That is the A–G watchability scorecard. |
| N3 | Executing the add | `adder.py` applies; the scorer ranks. |

---

## 3. Architecture notes

### 3.1 🎯 The renormalise-out pattern is already shipping here

From the module docstring:

> Components degrade gracefully: **any signal that's unavailable for a candidate
> is marked "n/a" and dropped from the weighted average rather than counted as
> zero.**

This is **exactly** what `GLD-ML-04` proposes for the watchability scorer, and it
materially changes that item.

The register's §8 **P-C** entry reads: *"Incomplete enrichment → missing signal
groups contribute zero → scores come out uniformly lower, indistinguishable from
'the household likes this less.'"* Session 6 refined it — `scoring/device_fit.py`
renormalises a missing **axis** out of the weighted risk, so the fix was
"extend an existing pattern, not invent one."

This is stronger still. `AcquisitionScorer` renormalises at the **top level**,
across all six components, with a dynamic denominator. So the pattern exists in
this repo at exactly the scope `GLD-ML-04` asks for — just in the sibling scorer.

`GLD-ML-04` is therefore not a design task. It is **porting a working
implementation across.** `GLD-ACQS-01`.

### 3.2 ✅ A measured aggregator inversion, fixed

The genre component was an unweighted mean. The docstring records what that did:

> dividing by the match count meant **every additional genre the household liked
> dragged the score DOWN**. Measured on the live watchlist, `[adventure]` scored
> **100.0** while `[adventure, action, drama]` scored **86.1** — a title matching
> the #1, #3 and #5 household genres ranked **BELOW** one matching only the #1,
> and **131 of 663 movies won purely by carrying a single broad tag. The top 10
> was 8 drama-only films as a result.**

Replaced with noisy-OR, `1 − Π(1 − wᵢ·S)`, treating each matched genre as
independent evidence. Two properties named explicitly:

- **Monotonic** — an extra matched genre can only raise the score (G3).
- **No penalty for breadth** — a genre the household has no weight for never
  enters `hits`, so *"a film tagged with six genres of which two match is scored
  on the two."*

This is worth recording as a **precedent** rather than a pattern — I have found
exactly one instance, and having just written §8 **P-G** about unverified claims
I am not going to manufacture a pattern from a single case. But the shape is
distinctive and worth watching for: **an aggregator that penalises additional
evidence.** A mean over matched items has that property whenever "more matches"
should mean "more confident."

The symptom is diagnostic: *the top of the ranking fills with items carrying one
broad tag.* Eight drama-only films in a top ten is what it looks like from the
outside.

### 3.3 🎯 P-H, fifth instance — and this one is about the *fix*

```python
_GENRE_SATURATION = 0.80
```

> Keeps a SINGLE matched genre **under 100** so a multi-genre match can actually
> outrank it; at 0.80 the household's #1 genre alone scores 80.0 and **leaves
> headroom** for corroborating genres to push toward 100.

Noisy-OR with an uncapped per-genre term would saturate: one strong genre match
drives the product to ~0 and the score to ~100, and every multi-genre title ties
with it at the ceiling. The 0.80 cap is what keeps the ceiling reachable-but-not-
reached by a single signal.

That is §8 **P-H** — *a signal that saturates carries no information* — now found
in five subsystems (`likelihood`, `next_watch`, `people_matrix`, and here twice
over: the saturation cap and the anti-flooding motive behind it).

Notably, this instance is the **fix to §3.2's fix**: replacing the mean with
noisy-OR solved anti-monotonicity but introduced saturation, and the cap solves
that. Both moves are in the same docstring.

### 3.4 ✅ `_SOURCE_SCORE` — the duplicate confirmed in sync

`next_watch.INTENT_SOURCE_STRENGTH` is documented as *"THE SAME RANKING as
`services/acquisition/scorer._SOURCE_SCORE`, divided by 100."* Checked against
the live table, all nine agree:

| Feed | `_SOURCE_SCORE` | `INTENT_SOURCE_STRENGTH` |
|---|---|---|
| `plex_watchlist` · `trakt_watchlist` · `mal_plantowatch` | 100 | 1.00 |
| `trakt_recommendations` · `mal_suggestions` | 65 | 0.65 |
| `plex_playlist` · `people_cooccurrence` | 60 | 0.60 |
| `plex_hubs` | 58 | 0.58 |
| `mal_seasonal` | 55 | 0.55 |

So **D40 is a preventive fix, not a bug fix** — the copies are currently correct.
That is the best moment to unify them: moving the table into the brain now costs
nothing and closes the divergence before it happens. `GLD-NXW-01`.

### 3.5 Explicit intent suppresses the recency penalty

```python
_EXPLICIT_INTENT_SOURCE = 100   # used to suppress production-year recency
```

If the household put a 1974 film on a watchlist, its age is not evidence against
it — they know how old it is. Recency is a prior about *unrequested* candidates,
and the prior is correctly discarded once there is direct evidence.

### 3.6 🟡 A hardcoded year

```python
_CURRENT_YEAR = 2026  # repo "today" is 2026-06-06; recency is a soft signal only.
```

The comment dates itself to **2026-06-06**; the current date is **2026-08-04**, so
the constant is already two months stale — harmless now, because recency is soft
and the drift is sub-year.

But it does not self-correct. In January 2027 every candidate scores as one year
newer than it is, and the recency component quietly inflates across the whole
candidate pool. Nothing detects it; the symptom would be a slow drift in what the
recency term contributes, invisible without the per-component matrix being
tracked over time.

This is the mirror image of `GLD-SCO-02`, where F3/G4 **do** read the wall clock
and that breaks replay determinism. Here the signal **doesn't** read it and should
— the same `now`-injection that fixes the scorer would fix this, with the opposite
sign. `GLD-ACQS-02`.

### 3.7 Byte-identical opt-in, eighth instance — with a twist

```python
"people_affinity": 0.0,   # MODULE default — a scorer built WITHOUT a config
                          # stays byte-identical (tests / back-compat)
_PEOPLE_AFFINITY_WEIGHT_DEFAULT = 0.08   # LIVE default when a config IS present
```

Two different defaults for the same weight, deliberately: the module constant
preserves the pre-feature behaviour for any caller constructing a bare scorer,
while the live path enables it.

And §3.1's renormalisation is what makes the weight safe to introduce at all:

> `_weighted()` renormalizes on the PRESENT signals (dynamic denominator), so a
> candidate carrying **NO** `people_affinity` is untouched at any weight; **only
> co-cast candidates (which DO carry it) are re-ranked.**

That is G4 falling straight out of G2 — adding a signal cannot perturb candidates
that lack it, because the denominator moves with the numerator. A fixed
denominator would have made every non-co-cast candidate lose 8 % of its score for
a signal it never had.

---

### 3.8 🔴 The people-evidence gate is a live P-C — and the table conversion forced it

`score()` only sets `evidence["people"]` when the candidate resolves in the
people-matrix forward map:

```python
people_ev = None
fwd, pweights = self._people_data()
if fwd and pweights:
    ...
    if roles:                       # <- the gate
        matrix["people_affinity"] = ...
        people_ev = {"score": ..., "matched": ...}
...
if people_ev:
    evidence["people"] = people_ev
```

So **absent** (`roles` empty — the title is not in the matrix, the signal was
never computed) and **empty** (`roles` found, no person carries weight > 0 —
`matched=0`) are two different facts. The prose breakdown distinguished them only
by whether the `cast/crew:` line existed at all, which is invisible to anyone not
reading two stanzas side by side.

The move to tables is what made this load-bearing: **a table cell has no "absent"
rendering by default.** A blank or `0` cell silently asserts *"we checked and
found nobody"* — a claim the code never made. `breakdown.py` carries the
distinction explicitly (`people_scored` as a separate boolean, `people_matched`
and `people_affinity` left `None`), renders absent as the literal string
`not scored`, and pins nullable pandas dtypes at the frame boundary so a stray
`fillna(0)` in the website template cannot reintroduce it.

🎯 **The cohort shape is the part worth chasing.** In the 2026-08-19 sample every
MAL-sourced title was unscored and every Trakt-watchlist title was scored (10/10
vs 15/15 — not random). The people-matrix forward map is library- and
daemon-derived; the Trakt enrich daemon covers Trakt-known movies broadly and does
not cover MAL anime. Because `_weighted()` uses a **dynamic denominator** over
present signals (§3.1), the two cohorts are therefore normalised over different
signal sets and then compete for the same `acquisition.max_adds_per_run` budget.
Whether that biases anime up or down is **unmeasured** — `people_affinity` weight
is only 0.08 — but the new signal-coverage and cohort tables surface it every run
instead of hiding it inside the score. `GLD-ACQS-11`.

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Per-component matrix alongside the total | G1 — rendered in the summary table and at debug | Total only |
| D2 | Drop absent signals from the denominator | G2 — zero-filling under-ranks sparse candidates, which correlates with obscurity | Count as 0 |
| D3 | Noisy-OR over matched genres | G3 — the mean was anti-monotonic, measurably (§3.2) | Unweighted mean |
| D4 | `_GENRE_SATURATION = 0.80` | G3's fix must not saturate — one genre must leave headroom (§3.3) | Uncapped noisy-OR |
| D5 | Three-tier source ranking, 100 / 65 / 55 | G5 — asked-for ≫ suggested ≫ merely airing | Flat feed weighting |
| D6 | Explicit intent suppresses recency | Age is not evidence against a title the household chose | Apply recency uniformly |
| D7 | Module weight 0.0, live weight 0.08 | G6 — a config-less scorer is byte-identical | One default |
| D8 | `_SOURCE_LABEL` keyed on value, not feed | Several feeds share a tier; the label names the tier | Per-feed labels |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | An absent signal is `"n/a"` and leaves the denominator, never contributing 0. |
| I2 | An additional matched genre never lowers the genre score. |
| I3 | A single matched genre cannot reach 100. |
| I4 | Adding a signal does not change the score of a candidate lacking it. |
| I5 | A config-less scorer scores identically to the pre-`people_affinity` version. |
| I6 | Explicit-intent candidates receive no recency adjustment. |
| I7 | Every score carries its component matrix. |
| I8 | Under the byte budget, funded charges never exceed a pool's `max(0, free-U)` minus in-flight, in-run **and** across runs (the committed ledger nets what has not landed). |
| I9 | The byte budget's fail direction is INVERTED from every other space gate: incomplete information (unreadable free space, corrupt/raising ledger) collapses to the **bounded count cap**, never to unlimited. A configured cap of 0 falls back to 10. |
| I10 | A candidate with no usable `expected_size_gb` is priced at a conservative default, never 0 — the budget must not exempt exactly the titles it knows least about. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Candidate has no rating / votes / genres | `"n/a"` | Dropped from the average (I1) | Correct | ✅ Matrix shows n/a |
| Candidate matches no household genre | Empty `hits` | Genre component 0, not negative | Correct | ✅ |
| All signals absent | Denominator empty | ❓ Unverified — behaviour on a fully-empty candidate not read | 🟡 | ❌ |
| Unknown feed name | Not in `_SOURCE_SCORE` | ❓ Unverified — `_SOURCE_LABEL` has a `50: "feed"` fallback suggesting a default tier exists | 🟡 | ❌ |
| **Year rolls over** | **None** | Every candidate reads one year newer; recency inflates pool-wide | 🟡 Slow drift, soft signal | ❌ **None** |
| `_SOURCE_SCORE` and `next_watch` diverge | **None** | Acquire and keep grade a feed differently | 🟡 Currently in sync | ❌ **None** |

**Row 5 is the actionable one.** Rows 3 and 4 are marked unverified rather than
guessed — I read the module header and `_genre_affinity`, not `AcquisitionScorer`
itself.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `acquisition.people_affinity_weight` | `0.08` with a config; `0.0` without | Cast/crew overlap weight (§3.7) |
| `acquisition.space_budget.enabled` | `false` (module) / `true` (this deployment) | Byte budget replaces the `max_adds_per_run` slice; the count cap survives only as the fallback bound (I9) |
| `acquisition.space_budget.shared_pool` | `true` | One budget = MIN headroom across routed instances (one Unraid array behind TRaSH hardlinks; per-instance pools would double-spend the same free space) |
| `acquisition.space_budget.committed_ttl_hours` | `72` | How long committed-but-unlanded bytes count against the budget; must cover add→pilot-search→download |
| `acquisition.space_budget.default_movie_gb` / `default_episode_gb` | `15.0` / `2.0` | Price for a candidate with no usable `expected_size_gb` (I10); shows charge ONE pilot episode |
| `acquisition.space_budget.hard_max_adds` | `0` (off) | Optional count ceiling on top of bytes — operator ruling 2026-08-20: bytes are the constraint |

Component weights: `genre_affinity` 0.35 · `source` 0.25 · `trakt_rating` 0.15 ·
`recency` 0.15 · `popularity` 0.10 · `people_affinity` 0.0/0.08.
Constants: `_GENRE_SATURATION` 0.80 · `_EXPLICIT_INTENT_SOURCE` 100 ·
`_CURRENT_YEAR` 2026.

---

## 8. Implemented capabilities

- ✅ Six-component explainable score with a per-candidate matrix
- ✅ Dynamic-denominator renormalisation over present signals
- ✅ Monotonic, breadth-neutral noisy-OR genre aggregation
- ✅ Saturation cap preserving multi-genre headroom
- ✅ Nine-feed source tiering shared (by copy) with `next_watch`
- ✅ Recency suppression for explicit-intent candidates
- ✅ Config-gated `people_affinity` with byte-identical no-config behaviour
- ✅ Tier-keyed short labels for the "why" column
- ✅ 13 test modules, ~90 KB — the best source-to-test ratio in `services/`
- ✅ Records-first elevation breakdown (`breakdown.py`) — one canonical record per
  acted-on title, seven boxed tables projected from it, run-constant context
  (household genre weights, profile rationale) emitted once as aggregated legends
  rather than repeated per row
- ✅ Score decomposition surfaced — per-signal contribution in score points that
  sums back to the total, with the dynamic denominator shown per row
- ✅ Single breakdown frame (`SCHEMA`, 41 columns, `SCHEMA_VERSION`) persisted to
  `acquisition/breakdown` for the website generator; every legend table is a pure
  aggregation of it, so there is no second source of truth
- ✅ P-C-safe rendering and nullable frame dtypes — *absent* is distinguishable
  from *scored zero* in the log cell, the JSON payload, and the DataFrame

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-ACQS-01` | 🎯 **Port the renormalise-out pattern to the watchability scorer** — `AcquisitionScorer` already does at the **top level** exactly what `GLD-ML-04` asks for. **That item is a port, not a design task** | Turns the register's oldest open P-C item from "invent a fix" into "copy the sibling" | M | `GLD-ML-04`, `GLD-CON-03` |
| `GLD-ACQS-02` | 🟡 **Replace `_CURRENT_YEAR = 2026` with an injected `now`** — it is already 2 months stale and inflates recency pool-wide every January | §3.6; the same `now`-injection as `GLD-SCO-02`, opposite sign | S | `GLD-SCO-02`, D23 |
| `GLD-ACQS-03` | **Record the aggregator-inversion precedent** in `DOCS_CONVENTIONS.md` — *"a mean over matched items penalises additional evidence"*, with the measured symptom (top-10 dominated by single-broad-tag items) | §3.2 is a fully-measured fix with a memorable diagnostic; one instance, so a precedent not a pattern | S | `GLD-DIS-04` |
| `GLD-ACQS-04` | **Verify the all-signals-absent path** — what does a candidate with an empty denominator score? | §6 row 3 unverified | S | — |
| `GLD-ACQS-05` | **Verify the unknown-feed default** — `_SOURCE_LABEL` carries a `50: "feed"` entry with no matching `_SOURCE_SCORE` key | §6 row 4; `next_watch` explicitly contributes 0.0 for unknown feeds, so the two may differ | S | `GLD-NXW-02` |
| `GLD-ACQS-06` | **Track the component matrix over runs** — it is rendered and discarded | 🟡 **Partly done** — `breakdown.py` now renders the matrix as a per-signal contribution table and persists the frame to `acquisition/breakdown`, so it is no longer discarded. What remains is *cross-run* history: the key is overwritten each run, so drift (§3.6) and n/a rates (§3.1) are visible per-run but not over time | M | `GLD-SCO-06` |
| `GLD-ACQS-10` | **Split the add budget per medium** — `max_adds_per_run` caps one pool sorted by score, and `svc` is derived per candidate *after* the cap, so shows and movies compete for the same slots. A run whose top scorers are all films adds **zero** new series, and the pilot pipeline is starved by a knob that never mentions series | ⏸ **Superseded by `GLD-ACQS-13`** (operator ruling 2026-08-20: bytes are the constraint, not counts — under the byte budget the count cap no longer binds, so the medium competition it created dissolves; still latent for deployments running count-cap mode) | M | `GLD-ACQS-13` |
| `GLD-ACQS-11` | 🔴 **Measure the people-signal cohort gap** — §3.8. Every MAL-sourced candidate is scored without `people_affinity`; every Trakt-watchlist one is scored with it. Quantify whether the dynamic denominator leaves the two cohorts comparable at weight 0.08, or whether MAL titles are systematically advantaged | Two cohorts ranked on different signal sets compete for one capped budget | M | `GLD-ACQS-01`, `GLD-ACQS-04` |
| `GLD-ACQS-12` | **Give the breakdown frame a durable sink** — it is written to one global-cache key that the next run overwrites. A parquet keyed by run would make `GLD-ACQS-06`'s cross-run tracking and `GLD-ACQS-07`'s genre re-measurement fall out for free | The frame already exists and is schema-versioned; only the sink is missing | S | `GLD-ACQS-06` |
| `GLD-ACQS-07` | **Re-measure the genre distribution** post-noisy-OR — the 131/663 and 8-of-10 figures justified the change and are unmonitored | The fix's own success metric has no watcher | S | `GLD-FND-04` |
| `GLD-ACQS-08` | **Document the remaining modules** — `__init__.py` (54.5 KB), `resolver.py` (28.9 KB), `candidates.py`, `gateway.py`, `adder.py` | ~105 KB unread this pass | L | — |
| `GLD-ACQS-09` | **Route `_GENRE_SATURATION` and the source tiers through `thresholds/registry`** | Hand-set cutoffs absent from `THRESHOLD_SPECS`, like `GLD-DIS-09` and `GLD-ACQ-09` | S | `GLD-THR-01` |
| `GLD-ACQS-13` | ✅ **Byte-priced space budget** — selection funds candidates in priority order out of `max(0, free-U)` minus in-flight, skip-and-continue, with the 4K companion priced LIVE in the add loop (it is planned after selection and is the largest file class in the system). Fail direction inverted per I9; supply is now bounded by `recommendation_limit`, not the budget, on a roomy array | ✅ Done — §0.1 #58; `space_budget.py` (brain) + wiring | M | — |
| `GLD-ACQS-14` | ✅ **Committed-bytes ledger** — `acquisition/space_budget/committed`; TTL-reconciled at snapshot, committed on `added` (never `would-add`), deferred adds commit at FLUSH time (that is when their bytes become in-flight; the queue record now carries its price in `gb`), pruned-on-write, loud warning on write failure (a silent miss = over-commit next run) | ✅ Done — §0.1 #58 | M | `GLD-ACQS-13` |
| `GLD-ACQS-15` | **hasFile-based ledger reconciliation** — v1 is TTL-only (strictly conservative: early imports and dead grabs both under-grab until expiry). Clearing entries when the *arr record gains a file would tighten the budget's accuracy without changing its safety | The one wrong-direction TTL case — a download still in flight PAST the TTL — gets rarer too | M | `GLD-ACQS-14` |
| `GLD-ACQS-16` | 🟡 **Bytes committed outside the budget's view** — saga/universe walks (`ensure_owned_and_grab`, `ensure_show_owned_and_grab`) and rehome re-adds band-gate per add but never write the ledger, so their in-flight bytes are invisible to the next run's budget. Direction is WRONG (undercounted in-flight → over-commit); magnitude bounded by the band headroom each path already respects | The band absorbs it today; a big universe cold-start would not be absorbed | M | `GLD-ACQS-14` |
| `GLD-ACQS-17` | **Fairness/demand interplay under budget mode** — `_reserve_fairness` still receives the COUNT cap, so its guarantee is positional, not byte-aware; a user's reserved 40 GB pick can be funded-refused while cheap titles pass. Moot while `demand.enabled=false` (this deployment); decide before any tester enables demand + budget together | Positional fairness ≠ funded fairness | M | `GLD-ACQS-13` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is 0.80 the right saturation cap — how often does a multi-genre title actually overtake a single strong match? | `GLD-ACQS-07` |
| Q2 | What does a candidate with every signal absent score? | `GLD-ACQS-04` |
| Q3 | Do `_SOURCE_SCORE` and `INTENT_SOURCE_STRENGTH` handle an unknown feed the same way? | `GLD-ACQS-05` |
| Q4 | Should recency use an injected `now`, or be dropped for unowned candidates entirely? | `GLD-ACQS-02` |
| Q5 | Is a MAL-sourced candidate (4 signals, denominator 0.90) comparable to a Trakt-watchlist one (6 signals, denominator 1.08) when both compete for one capped budget? | `GLD-ACQS-11` |
| Q6 | Should shows and movies draw from separate add budgets, or is one score-ordered pool the intended behaviour? | `GLD-ACQS-10` |

**Q3 is worth a look despite the tables agreeing.** `next_watch` states that an
unknown feed *"contributes nothing (0.0) rather than a guessed tier"*; this module
has a `_SOURCE_LABEL[50] = "feed"` with no corresponding `_SOURCE_SCORE` entry,
which hints at a default tier of 50 somewhere in `score()`. If so, the two modules
agree on all nine known feeds and disagree on every unknown one — which is
precisely the case a new upstream feed would hit.

## 11. Related designs

- [`README.md`](./README.md) — the operational reference this complements
- [`machine_learning/acquisition/DESIGN.md`](../../machine_learning/acquisition/DESIGN.md) — the brain half: demand, tightness, the resumption ramp
- [`machine_learning/next_watch/DESIGN.md`](../../machine_learning/next_watch/DESIGN.md) §3.5 — the mirrored source table and why it cannot delegate
- [`machine_learning/scoring/DESIGN.md`](../../machine_learning/scoring/DESIGN.md) — the incumbent that lacks §3.1's renormalisation
- [`machine_learning/discovery/DESIGN.md`](../../machine_learning/discovery/DESIGN.md) §3.4 — this scorer, injected
- [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8 P-C, P-H
