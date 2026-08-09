# discovery — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **discovery**

**Package** — `scripts.managers.machine_learning.discovery`
**Status** — ✅ Implemented · 🟢 Every module tested · 🎯 Its fail-direction choice reconciles D36
**Related** — [README.md](./README.md) · [`coordinator/this_week_in_history.md`](../../services/coordinator/this_week_in_history.md) · [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8

---

## 1. Problem statement

A rotating weekly shelf of anniversary titles is deceptively hard for three
reasons that have nothing to do with picking films.

**1. There is no timer.** The application is stateless with an external
scheduler. Nothing runs at midnight on Sunday to roll the week over. Every
timing question — *has the week changed? are we in the pre-roll window?* — must
be answerable from `now` plus a persisted stamp, on a run that might fire at any
hour, or not at all for a fortnight.

**2. Grabs must be bounded and reversible.** A shelf that acquires freely fills
the disk with titles nobody asked for. Each grab is a **trial** against a
standing cap, and the purge must know exactly what it grabbed — which means
tracking slots by an identity that survives a title change.

**3. Speculative acquisition deserves a stricter bar than normal acquisition.**
A watchlist item is something the household asked for. An anniversary title is
something the system suggested. The eligibility gate should reflect that.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Every timing decision derivable from `now` + a persisted stamp. |
| G2 | Grabs bounded by a standing slot cap. |
| G3 | The purge can identify exactly what it grabbed. |
| G4 | A missed week self-heals without compounding. |
| G5 | Speculative grabs fail closed — unknown is never eligible. |
| G6 | Pure — no I/O; the manager persists. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Scoring candidates | The acquisition scorer does that; it is **injected**. |
| N2 | Persisting occupancy | Manager writes `discovery/this_week/occupancy`. |
| N3 | Executing grabs or purges | Emits decisions; the service applies. |
| N4 | Running on a schedule | N/A — stateless by design (§3.1). |

---

## 3. Architecture

### 3.1 Timing without a timer (G1)

```
week identity  = the week's SUNDAY date, isoformat      "2024-12-29"

rollover_due(now, last_rollover)
    no prior stamp            → False    (first run: nothing to roll over)
    week_stamp(now) ≠ stamp   → True

next_boundary(now)   = next Sun 00:00 STRICTLY after now, TZ preserved
in_pre_roll(now, 8h) = now ∈ [boundary − 8h, boundary)
```

Three details that show the problem was thought through rather than approximated:

**Identity is the Sunday's date, not an ISO week number.** ISO weeks start
Monday; this shelf runs Sun–Sat. Using ISO-8601 week numbers would put the
boundary in the middle of the shelf's week. The docstring names this exactly:
*"an unambiguous unique id (no ISO-week-vs-Sunday-start mismatch)."*

**TZ is preserved through the arithmetic**, *"so a DST-transition Saturday still
resolves to a well-defined Sunday 00:00."* Naive arithmetic across a DST boundary
lands an hour off, which on a Saturday-evening pre-roll window is the difference
between firing and not.

**A multi-week gap rolls over once.** *"the stale trials are purged regardless of
how many weeks were missed."* (G4) A run that fires after a fortnight's outage
does not attempt two rollovers or leave a week's trials stranded.

The strict inequality in `next_boundary` — *"today is Sunday but past 00:00 → the
NEXT one"* — is the boundary case that would otherwise make Sunday's run compute
a boundary in its own past.

### 3.2 Slot bookkeeping (G2, G3)

```
slot_key(media, ext_id) = f"{media}:{ext_id}"     movie:12345 / show:67890
```

> the ownership id, **never a title (remake/same-name collisions)**

Five states across two classes:

| Class | States | Effect |
|---|---|---|
| **On disk** | `occupied`, `deferred` | Consume a slot |
| **Freed** | `graduated`, `purged`, `cancelled` | Release it |

The three freed states are distinct outcomes, not one "gone":

- `graduated` — *"kept, tag dropped — left the trial pool"*. The trial succeeded;
  the title is now a normal library member.
- `purged` — the trial ended and the file was deleted.
- `cancelled` — *"never-completed download removed"*. It never became a file.

Distinguishing them means the shelf can report its own hit rate, which a single
terminal state would lose.

`deferred` is the subtle one: *"kept pending a torrent seed obligation."* The
trial is over, but the file cannot be deleted yet without breaching a tracker
ratio commitment — so it still consumes a slot. That is a real-world constraint
most designs would discover late.

**Copy-on-write** throughout — *"so a caller can't alias the cached table."* The
occupancy dict is read from cache, and a mutator returning the same object would
let a caller mutate the cached copy in place.

### 3.3 🎯 Fail-closed eligibility — and what it settles

```python
if total is None or total < floor:
    continue
```

> **HARD fail-closed floor**: a candidate with NO score, or a score below the
> floor, is never shelf-eligible … a missing/None/**erroring** total is treated
> as below the floor and excluded.

An exception inside the scorer is caught and the candidate dropped. A speculative
grab is never made on an absent signal (G5).

**This is the fourth distinct fail-direction I have documented, and together they
resolve D36.** The four are not inconsistent — they follow one rule that none of
them states:

| Module | On unknown input | Outcome |
|---|---|---|
| `lifecycle/watched_definition` | assume **watched** | Guard stays wide → nothing deleted |
| `sizing/file_comparison` | `expected ≤ 0` → **keep** | No opinion → nothing changed |
| `discovery/scoring` | **exclude** | Not acquired → nothing spent |
| `classification/build_franchise_file_ids` | **empty frozenset** | No protection → **things get deleted** |

The first three all fail toward **inaction** — toward not spending, not deleting,
not changing. The fourth fails toward **action**, and it is the one that caused a
real outage (`franchise.py`'s v4/v5 field rename).

So the rule is not "fail open" or "fail closed" — those framings pick opposite
answers for `watched_definition` and `discovery/scoring`, both of which are
correct. The rule is:

> **On unknown input, fail toward the outcome that changes nothing.**

Under that framing all three correct modules agree, and
`build_franchise_file_ids` is unambiguously the odd one out. Proposed as the
articulation for D36 and `GLD-CLS-02`.

### 3.4 Injected scorer (N1)

`score_and_floor` takes the scorer as an argument *"so this stays unit-testable
without a cache; the manager passes a live `AcquisitionScorer` (built once,
scores cached)."*

Same anti-drift discipline as [`foundation/`](../foundation/DESIGN.md)'s
delegation and [`likely_viewers`](../quality_analytics/DESIGN.md)'s
*"does NOT re-score"* — discovery does not get its own opinion about
watchability.

`to_scorer_input` handles the one genuine mismatch: a discovery grab has no feed
intent, so `source` defaults to a neutral marker *"(the scorer maps an unknown
source to its 50 midpoint)."* Rather than inventing a source or omitting the
field, it uses the scorer's own documented neutral.

### 3.5 An identity discipline worth contrasting

Slots are keyed by ownership id *"never a title (remake/same-name collisions)."*

Two packages away, [`labels/labeling.py`](../labels/DESIGN.md) §3.3 joins **shows
by normalised title string**, because *"history has no series id/tvdb"* — even
though `grandparent_rating_key` is present and unusable without a map.

Same repo, same failure mode understood, opposite outcomes — because one had an
id available and the other did not. That strengthens `GLD-LAB-02`: the
`rating_key → series id` map is not a nice-to-have, it is the thing that would let
`labels/` apply the discipline this package already applies.

### 3.6 Coverage note

[`gems.py`](./gems.py) (29.4 KB), [`shelf.py`](./shelf.py),
[`candidates.py`](./candidates.py) and [`window.py`](./window.py) were **not
read** this pass — roughly 48 KB including the largest module in the package.
Nothing here asserts their behaviour beyond what the modules I did read state
about them.

Notable from the inventory: **every module has a matching test**, and
`test_gems.py` is 18.1 KB. That is the most complete test-per-module coverage of
any package in the brain.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Week identified by its Sunday's date | G1 — unambiguous, and avoids the ISO-week/Sunday-start mismatch | ISO week number |
| D2 | TZ-preserving boundary arithmetic | A DST Saturday must still resolve correctly | Naive `timedelta` |
| D3 | Strict `>` in `next_boundary` | Sunday's own run must not compute a past boundary | `>=` |
| D4 | Missed weeks roll over **once** | G4 — a fortnight's outage self-heals without compounding | Roll per missed week |
| D5 | First run never rolls over | Nothing to purge | Treat absent stamp as due |
| D6 | Slots keyed by ownership id | G3 — titles collide across remakes | Key by title |
| D7 | Five states, three terminal | `graduated`/`purged`/`cancelled` are different outcomes worth reporting | One "gone" state |
| D8 | `deferred` still consumes a slot | A seed obligation means the file is genuinely still on disk | Free it on trial end |
| D9 | Copy-on-write mutators | Prevents aliasing the cached table | Mutate in place |
| D10 | Fail-closed eligibility | G5 — speculative acquisition on an absent signal is a pure cost | Neutral default |
| D11 | Catch scorer exceptions as "below floor" | An erroring scorer must not admit a candidate | Propagate |
| D12 | Scorer injected | Unit-testable without a cache; cannot drift from the real scorer | Import it |
| D13 | Neutral `source` marker | Uses the scorer's own documented midpoint rather than inventing intent | Omit, or fake a source |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | A candidate with no score is never shelf-eligible. |
| I2 | On-disk slots never exceed the cap (`open_slots` clamps at 0). |
| I3 | A slot key is an ownership id, never a title. |
| I4 | Mutators return a new dict; the cached table is never aliased. |
| I5 | Rollover fires at most once per run, regardless of weeks missed. |
| I6 | First run never triggers a rollover. |
| I7 | Boundary arithmetic preserves the household timezone. |
| I8 | This package performs no I/O. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Scorer raises | `try/except` | Candidate excluded (I1) | Safe — fails toward inaction | ❌ **None** |
| Candidate has no score | Explicit | Excluded | Safe | ❌ **None** |
| Run misses several weeks | `rollover_due` | One rollover; stale trials purged | Self-healing | 🟡 Unclear if reported |
| Run never fires on a Saturday evening | `in_pre_roll` | Pre-roll skipped; rollover still fires at the boundary | 🟡 No pre-fetch that week | ❌ **None** |
| `now` is a bare `date` | Type check | `in_pre_roll` → `False` | Safe | ❌ **None** |
| Cap reduced below current on-disk count | `max(0, …)` | `open_slots` = 0; existing trials run out naturally | Graceful | ❌ **None** |
| Seed obligation never clears | `deferred` persists | Slot consumed indefinitely | 🟡 Shelf shrinks silently | ❌ **None** |
| Slot key collides | Ownership id | Cannot — that is the point of D6 | None | ✅ By construction |
| Occupancy table lost | Manager-side | Cap resets; existing trials untracked and never purged | 🟡 Orphaned files | ❌ **None** |

**Rows 7 and 9 are the ones worth a signal.** A permanently-`deferred` slot
silently shrinks the shelf — the feature degrades to nothing with no error. And a
lost occupancy table strands already-grabbed trials that nothing will ever purge,
which is the one path where this bounded feature becomes unbounded.

---

## 7. Configuration surface

No config read directly — every parameter is passed in:

| Parameter | Default | Effect |
|---|---|---|
| `cap` | — | Standing trial slots |
| `floor` | `0` | Minimum watchability for shelf eligibility |
| `lead_hours` | `8` | Pre-roll window before the Sunday boundary |
| `sort` | `True` | Rank watchability-descending |

Cache key written by the manager: `discovery/this_week/occupancy`.

---

## 8. Implemented capabilities

- ✅ Sun–Sat week identity by Sunday date, ISO-mismatch-proof
- ✅ TZ-preserving boundary arithmetic surviving DST transitions
- ✅ Pre-roll window for reclaim-first and pre-fetch
- ✅ Multi-week gap self-healing with a single rollover
- ✅ Ownership-keyed trial slots immune to title collisions
- ✅ Five-state lifecycle distinguishing graduated / purged / cancelled
- ✅ Seed-obligation-aware `deferred` state
- ✅ Copy-on-write occupancy mutators
- ✅ Bounded-queue week scrub
- ✅ Hard fail-closed eligibility, exception-safe
- ✅ Injected scorer with neutral-source mapping
- ✅ Human-readable `why` attribution, best-effort
- ✅ Hidden-gem selection and shelf assembly
- ✅ A test module for every source module — the most complete in the brain

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-DIS-01` | **Report shelf outcomes** per rollover — graduated / purged / cancelled counts | The five-state model exists precisely to make this measurable, and nothing reports it | S | `GLD-SPA-05` |
| `GLD-DIS-02` | **Warn on a long-`deferred` slot** — a seed obligation that never clears shrinks the shelf silently | §6 row 7: the feature degrades to zero with no error | S | — |
| `GLD-DIS-03` | **Detect an orphaned occupancy table** — grabbed trials with no tracking entry are never purged | §6 row 9: the one path where a bounded feature becomes unbounded | M | `GLD-DIS-01` |
| `GLD-DIS-04` | **Articulate the fail-direction rule** — *"on unknown input, fail toward the outcome that changes nothing"* — in `DOCS_CONVENTIONS.md` §7 | §3.3 reconciles four modules and identifies `GLD-CLS-02` as the outlier. Currently implicit | S | `GLD-CLS-02`, D36 |
| `GLD-DIS-05` | **Report missed rollovers** — how many weeks were skipped when one finally fires | §6 row 3 self-heals silently; a gap is a scheduler problem worth surfacing | S | `GLD-DIS-01` |
| `GLD-DIS-06` | **Read and document `gems.py`, `shelf.py`, `candidates.py`, `window.py`** | §3.6 — ~48 KB unread, including the package's largest module | M | — |
| `GLD-DIS-07` | **Surface the shelf in the web UI** with its `why` strings | The attribution is computed and discarded, same as `scoring`'s breakdown and `profile_selector`'s reason | M | `GLD-WEB-04` |
| `GLD-DIS-08` | **Make `floor` a real threshold** — it defaults to `0`, which admits any scored candidate | With `floor=0` only *unscored* candidates are excluded; the gate is weaker than "fail-closed" implies | S | `GLD-THR-01` |
| `GLD-DIS-09` | **Route the shelf floor through `thresholds/registry`** | It is a hand-set watchability cutoff and is absent from `THRESHOLD_SPECS` | S | `GLD-THR-01` |
| `GLD-DIS-10` | **Report pre-roll misses** — weeks where the Saturday window never had a run | §6 row 4: no pre-fetch that week, silently | S | `GLD-DIS-05` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | What is the shelf's actual hit rate — how many trials graduate versus purge? | `GLD-DIS-01` |
| Q2 | Should `floor` default above 0? At 0 the "fail-closed" gate only excludes *unscored* candidates. *(= D37)* | `GLD-DIS-08` |
| Q3 | Should the shelf floor be a registered threshold, subject to calibration like the other eleven? | `GLD-DIS-09` |
| Q4 | How long may a slot stay `deferred` before it is a problem rather than a seed obligation? | `GLD-DIS-02` |

**Q2 deserves attention.** `score_and_floor` is described as a *"HARD fail-closed
floor"*, and the exclusion logic genuinely is. But with `floor=0` the only
candidates excluded are those with **no score at all** — every scored candidate,
however low, passes. The mechanism is fail-closed; the configured value makes it
close to a null gate.

## 11. Related designs

- [`coordinator/this_week_in_history.md`](../../services/coordinator/this_week_in_history.md) — the feature's own design note
- [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8 — D36 and the fail-direction rule §3.3 proposes
- [`labels/DESIGN.md`](../labels/DESIGN.md) §3.3 — the title-join this package deliberately avoids
- [`services/acquisition/README.md`](../../services/acquisition/README.md) — the injected scorer
- [`thresholds/DESIGN.md`](../thresholds/DESIGN.md) — where the shelf floor is absent
