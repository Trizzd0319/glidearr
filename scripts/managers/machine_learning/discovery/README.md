# discovery

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **discovery**

**Package** — `scripts.managers.machine_learning.discovery`
**Run position** — Phase 3 acquisition (grab) and Phase 4 rollover (purge + rotate).
**One-liner** — Pure logic for the *This Week in History* anniversary shelf: which titles qualify this week, how many trial slots are free, when the week rolls over, and the fail-closed eligibility gate.

---

## Purpose

From [`__init__.py`](./__init__.py):

> pure logic for the **'This Week in History'** anniversary shelf.

Narrower than the folder name suggests. This is one feature: a rotating weekly
shelf of titles with an anniversary this week, grabbed on **trial** against a
standing slot cap, then purged or graduated at the week boundary.

The design constraint that shapes everything: **the app is stateless with an
external scheduler.** There is no resident timer, so every timing question must
be answerable from `now` plus a persisted stamp.

---

## Script inventory

| Script | Size | Role | Status |
|---|---|---|---|
| [`gems.py`](./gems.py) | 29.4 KB | Hidden-gem selection — the largest module | ✅ Implemented |
| [`shelf.py`](./shelf.py) | 8.4 KB | Shelf assembly | ✅ Implemented |
| [`window.py`](./window.py) | 5.6 KB | `week_window` — the Sun–Sat window | ✅ Implemented |
| [`candidates.py`](./candidates.py) | 4.6 KB | Candidate generation | ✅ Implemented |
| [`occupancy.py`](./occupancy.py) | 3.4 KB | Standing trial-slot bookkeeping | ✅ Implemented |
| [`scoring.py`](./scoring.py) | 2.8 KB | `score_and_floor` — the fail-closed gate | ✅ Implemented |
| [`rollover.py`](./rollover.py) | 2.6 KB | Week-boundary and pre-roll timing | ✅ Implemented |

## Test coverage

Seven test modules, ~44 KB — including [`test_gems.py`](./test_gems.py) (18.1 KB)
and [`test_shelf.py`](./test_shelf.py) (8.8 KB). Every module has a matching test.

---

## Trial slots

A slot is keyed by **ownership id** — `movie:<tmdb>` / `show:<tvdb>` — *"never a
title (remake/same-name collisions)."*

| State | Counts against cap? | Meaning |
|---|---|---|
| `occupied` | ✅ | A grabbed trial, on disk |
| `deferred` | ✅ | Kept pending a torrent seed obligation |
| `graduated` | ❌ | Kept, tag dropped — left the trial pool |
| `purged` | ❌ | Deleted |
| `cancelled` | ❌ | Never-completed download removed |

```
open_slots = max(0, cap − len(on-disk slots))
```

Every mutator is **copy-on-write** — *"so a caller can't alias the cached
table."* Persisted by the manager at `discovery/this_week/occupancy`.

---

## Week timing

The week is **Sun–Sat**, identified by its **Sunday's date** in isoformat
(`2024-12-29`) — *"an unambiguous unique id (no ISO-week-vs-Sunday-start
mismatch)."*

| Function | Behaviour |
|---|---|
| `rollover_due(now, last_rollover)` | True iff a prior stamp exists **and** the week differs. First run → `False`. *"A multi-week gap still returns True once."* |
| `next_boundary(now)` | Next Sun 00:00 **strictly after** `now`, TZ preserved — *"so a DST-transition Saturday still resolves to a well-defined Sunday 00:00."* |
| `in_pre_roll(now, lead_hours=8)` | Inside `[boundary − 8h, boundary)` — the Saturday-evening lead-in for reclaim-first and pre-fetch. A bare `date` returns `False`. |

---

## The eligibility gate

`score_and_floor` is **hard fail-closed**:

> a candidate with **NO score**, or a score below the floor, is **never
> shelf-eligible** … a missing/None/**erroring** total is treated as below the
> floor and excluded.

Survivors are ranked watchability-descending. The scorer is **injected**, so this
stays unit-testable without a cache; the manager passes a live
`AcquisitionScorer`.

`to_scorer_input` maps a discovery candidate onto the acquisition scorer's shape —
shows key people-affinity off `tvdb`, movies off `tmdb`. A discovery grab has no
feed intent, so `source` defaults to a neutral marker *"(the scorer maps an
unknown source to its 50 midpoint)."*

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Design note:** [`coordinator/this_week_in_history.md`](../../services/coordinator/this_week_in_history.md)
- **Scorer:** [`services/acquisition/scorer.py`](../../services/acquisition/README.md)
- **Sibling:** [`services/plex/discovery/gems.py`](../../services/plex/README.md)
