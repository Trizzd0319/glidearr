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

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-ACQS-01` | 🎯 **Port the renormalise-out pattern to the watchability scorer** — `AcquisitionScorer` already does at the **top level** exactly what `GLD-ML-04` asks for. **That item is a port, not a design task** | Turns the register's oldest open P-C item from "invent a fix" into "copy the sibling" | M | `GLD-ML-04`, `GLD-CON-03` |
| `GLD-ACQS-02` | 🟡 **Replace `_CURRENT_YEAR = 2026` with an injected `now`** — it is already 2 months stale and inflates recency pool-wide every January | §3.6; the same `now`-injection as `GLD-SCO-02`, opposite sign | S | `GLD-SCO-02`, D23 |
| `GLD-ACQS-03` | **Record the aggregator-inversion precedent** in `DOCS_CONVENTIONS.md` — *"a mean over matched items penalises additional evidence"*, with the measured symptom (top-10 dominated by single-broad-tag items) | §3.2 is a fully-measured fix with a memorable diagnostic; one instance, so a precedent not a pattern | S | `GLD-DIS-04` |
| `GLD-ACQS-04` | **Verify the all-signals-absent path** — what does a candidate with an empty denominator score? | §6 row 3 unverified | S | — |
| `GLD-ACQS-05` | **Verify the unknown-feed default** — `_SOURCE_LABEL` carries a `50: "feed"` entry with no matching `_SOURCE_SCORE` key | §6 row 4; `next_watch` explicitly contributes 0.0 for unknown feeds, so the two may differ | S | `GLD-NXW-02` |
| `GLD-ACQS-06` | **Track the component matrix over runs** — it is rendered and discarded | Would make §3.6's drift and §3.1's n/a rates visible; fourth package computing an explanation and dropping it | M | `GLD-SCO-06` |
| `GLD-ACQS-07` | **Re-measure the genre distribution** post-noisy-OR — the 131/663 and 8-of-10 figures justified the change and are unmonitored | The fix's own success metric has no watcher | S | `GLD-FND-04` |
| `GLD-ACQS-08` | **Document the remaining modules** — `__init__.py` (54.5 KB), `resolver.py` (28.9 KB), `candidates.py`, `gateway.py`, `adder.py` | ~105 KB unread this pass | L | — |
| `GLD-ACQS-09` | **Route `_GENRE_SATURATION` and the source tiers through `thresholds/registry`** | Hand-set cutoffs absent from `THRESHOLD_SPECS`, like `GLD-DIS-09` and `GLD-ACQ-09` | S | `GLD-THR-01` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is 0.80 the right saturation cap — how often does a multi-genre title actually overtake a single strong match? | `GLD-ACQS-07` |
| Q2 | What does a candidate with every signal absent score? | `GLD-ACQS-04` |
| Q3 | Do `_SOURCE_SCORE` and `INTENT_SOURCE_STRENGTH` handle an unknown feed the same way? | `GLD-ACQS-05` |
| Q4 | Should recency use an injected `now`, or be dropped for unowned candidates entirely? | `GLD-ACQS-02` |

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
