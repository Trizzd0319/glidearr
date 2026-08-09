# calendar — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **calendar**

**Manager** — `CalendarManager`
**Run position** — Phase 3.
**Status** — ✅ Implemented · 🟡 Weak `dry_run` in a manager that writes · 🟡 Scores on a compressed range
**Existing docs** — [`README.md`](./README.md) (10.3 KB)

> **This document does not restate [`README.md`](./README.md).** It adds the
> 11-section frame and the cross-package findings.

---

## 1. Problem statement

A show airing tomorrow is worth more than the same show airing in six months, and
an *arr that is not monitoring it will not grab it. Two feeds carry that
information and neither is usable raw:

1. **Trakt calendars** — upcoming episodes, premieres, movie releases. The cache
   keys `trakt/<user>/calendar/*` were *"long-declared-but-dormant"*, so the data
   was reachable and unused.
2. **MAL seasonal charts** — everything airing next season, most of which the
   household will never watch. Surfacing all of it is noise.

The MAL filter is where the design gets interesting, because an **unowned**
upcoming title has almost no signal: no watch history, no file, no affinity from
people the household has seen, no completion. Scoring it with the full A–G
scorecard produces a number in a range the scorecard was never calibrated for.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Populate the dormant Trakt calendar keys. |
| G2 | Ensure already-owned upcoming titles are monitored so the *arr grabs on air. |
| G3 | Filter MAL seasonal to what the household plausibly wants. |
| G4 | Search stays behind a flag, matching acquisition policy. |
| G5 | No-op unless `calendar.enabled`. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Adding titles | It monitors what is already in the library; adding is acquisition's job. |
| N2 | Searching by default | *"Optional search behind a flag (default off, matching the acquisition policy)."* |
| N3 | Owning the scorer | Calls `score_show` directly. |

---

## 3. Architecture

### 3.1 🟡 Scoring on a systematically sparse input

```python
score = score_show(
    {"genres": genres or ["anime"]},
    genre_affinity=genre_affinity or {},
    sonarr_rating=mean,
)
```

Two signals reach the scorer: **genres** (Group B) and the **MAL community mean**
as a rating (Group F). Groups A, C, D, E and G are absent and therefore zero.

The docstring is explicit about the consequence:

> the household-intent groups are all 0 for unowned titles, so realistic scores
> run **0–25** — hence the **low default threshold**.

This is §8 **P-C**'s shape — missing groups contribute zero, so scores come out
uniformly depressed — handled by a **third strategy** the sweep has not seen
before:

| Strategy | Where |
|---|---|
| Renormalise the missing axis out | `scoring/device_fit.py` |
| Renormalise at the top level, dynamic denominator | `services/acquisition/scorer.py` |
| **Accept the compression and lower the threshold to match** | **here** |

And it is **defensible**. The depression is uniform across the compared
population — every entry in a MAL seasonal chart is equally signal-poor — so
*ranking within the pool is preserved*, which is all this filter needs. The pool
is never compared against owned titles.

The cost is that **`20` here is not the `20` anywhere else.** A threshold on a
0–25 effective range is a ~80th-percentile cut; the same number on the persisted
0–100 axis is near the floor.

**This is already recorded.** `thresholds/registry.py`'s `mal_min_watchability`
spec is `routed=False` with the note:

> scores **UNOWNED** titles, which never enter the snapshot store — the
> calibrator is fit on owned-library rows and **does not cover them**.

So the registry and this module agree, independently, on both the value and the
reason. That is a **verified cross-document consistency**, and worth recording as
such given §8 **P-G**'s tally of the opposite.

It does mean the codebase now has **three effective watchability ranges**:

| Range | Path | Note |
|---|---|---|
| AXIS V2 | `refresh_scores` → `_score_row`, full inputs | median 8, max 58 |
| AXIS LEGACY | `repair/anomaly._score_owned`, partial inputs | median 7, max 36 — **16.5 % disagreement** with V2 |
| **Unowned / calendar** | `score_show` on two groups | **0–25 realistic** |

The third is the same *function* on sparse input rather than a different code
path, so it is not a fourth axis in the `GLD-THR-01` sense — but a reader
comparing thresholds across the repo needs to know which range each sits on.
`GLD-CAL-02`.

### 3.2 🟡 The weak `dry_run` form, in a manager that writes

```python
self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
```

Two levels, defaulting to **`False`**. Identical to
[`writeback/`](../writeback/DESIGN.md) §3.4, and materially weaker than
[`coordinator/`](../coordinator/DESIGN.md) §3.6:

```python
# dry_run resolution (kwargs → parent → Main); never silently default.
```

Calendar **does write** — it PUTs `monitored=true` to Sonarr and Radarr, and
optionally triggers searches. Less destructive than a delete, but a monitored
flag flipped across a season's worth of titles is a real change, and a search
spends indexer budget.

So the strong form now appears in **one** manager and the weak form in at least
**two**, and the split does not track how much a manager can change. That
upgrades `GLD-WB-03` from a writeback-local fix to a systemic one:
`GLD-ORCH-01`'s distributed invariant, with three data points. `GLD-CAL-01`.

### 3.3 The MAL → library join is by title

> any that are already in the Sonarr/Radarr library (**exact normalized-title
> match**) are ensured monitored

MAL carries no id that joins a Radarr/Sonarr row — the same constraint
[`next_watch`](../../machine_learning/next_watch/DESIGN.md) documents, where the
service-layer `mal/id_bridge` does *"an exact normalized-TITLE match … cached
under `mal/{user}/id_map`."*

So this is the **sixth** appearance of the identity theme, and one of only two
instances on the unguarded side:

| Module | Join | Guarded? |
|---|---|---|
| `discovery/occupancy` · `people_matrix` · `plex` · `mdblist` | ownership id · tmdb person id · uuid · separate id-space files | ✅ |
| `labels/labeling` | normalised series title | ❌ no id available |
| **`calendar` (MAL path)** | **exact normalised title** | ❌ **no id available** |

A retitled or alternately-romanised anime silently fails to be monitored. The
module does capture `title_en` from `alternative_titles`, so a second join key
exists in the data — whether the match tries it is unverified. `GLD-CAL-03`.

### 3.4 A synthetic genre default

```python
{"genres": genres or ["anime"]}
```

An entry with no genres is scored as if tagged `anime`. Worth noting alongside
[`next_watch`](../../machine_learning/next_watch/DESIGN.md) §3.2's refusal to
fabricate a timestamp — but this is a different thing and a legitimate one:
everything in a MAL seasonal chart *is* anime, so the default is a true prior
rather than an invented datum.

It does mean a genre-less entry is scored against the household's affinity for
`anime` specifically, which may be high or absent depending on how affinity keys
genres. Minor. `GLD-CAL-06`.

### 3.5 Opt-out rather than opt-in

`calendar.mal` is *"default on when MAL is configured"* — unusual in this repo,
where the house pattern is default-off with a byte-identical no-op
([`affinity/`](../../machine_learning/affinity/DESIGN.md) §3.2, nine instances).

Defensible: the MAL path only *reads* a chart and filters it, and the write half
is separately gated by `ensure_monitored`. But it is the one place a MAL-configured
install gains behaviour without asking.

### 3.6 Test coverage is MAL-only

One test file — [`test_calendar_mal.py`](./test_calendar_mal.py), 3.4 KB against
13.1 KB of source — and its name scopes it to the MAL path.

The **Trakt calendar path is the module's stated primary purpose** (G1, G2) and
appears untested: populating the dormant keys, and the monitor-ahead write into
both *arrs. `GLD-CAL-04`.

Another instance of the §8 note that **test coverage is uncorrelated with risk**:
the pure, filterable, side-effect-free MAL scorer has tests; the path that issues
PUTs to two services does not.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Monitor, never add | N1 — the calendar is about timing, not selection | Add upcoming titles |
| D2 | Search behind a flag, default off | G4 — consistent with acquisition | Search on monitor |
| D3 | Score MAL entries on the two available groups | The only signals an unowned title has | Skip scoring |
| D4 | Threshold 20 against a 0–25 range | §3.1 — matches the compression rather than fighting it | Renormalise |
| D5 | `mal_min_watchability` left `routed=False` | The calibrator is fit on owned rows and does not cover unowned ones | Route it |
| D6 | `genres or ["anime"]` | A true prior for MAL content | Score with no genres |
| D7 | Exact normalised-title join | No id exists (§3.3) | Skip unmatched |
| D8 | MAL path default-on when configured | Read-only; the write half is separately gated | Default off |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No-op unless `calendar.enabled`. |
| I2 | Titles are monitored, never added. |
| I3 | Search occurs only behind its flag. |
| I4 | Only entries clearing `mal_min_watchability` are surfaced. |
| I5 | MAL results are sorted watchability-descending. |
| I6 | Malformed or titleless entries are dropped. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Titleless MAL entry | Guard | Dropped | Correct | ❌ None |
| Non-numeric `mean` | `try/except` | `mean = None`, Group F contributes nothing | 🟡 Score depressed further | ❌ None |
| No genres | `or ["anime"]` | Scored on the anime prior | Minor | ❌ None |
| **Retitled / romanised differently** | **None** | Title join misses ⇒ **not monitored** | 🟡 Airs ungrabbed | ❌ **None** |
| Trakt calendar unavailable | ❓ Unverified | — | 🟡 | ❌ |
| **`dry_run` unresolvable** | **None** | Defaults `False` ⇒ live `monitored=true` PUTs | 🟡 §3.2 | ❌ **None** |
| Threshold compared against the wrong range | **None** | A `20` set from the 0–100 axis would pass everything | 🟡 §3.1 | ❌ **None** |

**Row 4 is the operational one**: the whole point is grabbing on air, and a
title-join miss produces exactly the outcome the module exists to prevent —
silently, on the night it airs.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `calendar.enabled` | — | Master gate (I1) |
| `calendar.mal` | on when MAL configured | MAL seasonal path (§3.5) |
| `calendar.mal_min_watchability` | **20** | Against a **0–25** realistic range (§3.1) |
| `ensure_monitored` | — | Gates the write half |
| search flag | off | G4 |

---

## 8. Implemented capabilities

- ✅ Trakt calendar → the previously dormant `trakt/<user>/calendar/*` keys
- ✅ Monitor-ahead for owned upcoming titles in both *arrs
- ✅ MAL seasonal filtering by watchability, sorted descending
- ✅ Two-group scoring appropriate to unowned titles, with a matched threshold
- ✅ Month → anime-season mapping
- ✅ `title_en` captured from `alternative_titles`
- ✅ Optional search behind a flag

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-CAL-01` | 🟡 **Adopt the strong `dry_run` form** — kwargs → parent → Main, never defaulted. Third manager found with the weak form, and it issues `monitored=true` PUTs to both *arrs | §3.2. Upgrades `GLD-WB-03` from writeback-local to systemic — the strong form exists in **one** manager, the weak in at least **two**, and the split does not track blast radius | S | `GLD-WB-03`, `GLD-ORCH-01` |
| `GLD-CAL-02` | **Document the three effective watchability ranges** — V2 (0–58 observed), LEGACY (0–36), unowned/calendar (**0–25**) — so a threshold is never read against the wrong one | §3.1. `thresholds/registry` already notes it per-spec; nothing states it centrally | S | `GLD-THR-01` |
| `GLD-CAL-03` | **Try `title_en` in the MAL→library join** — it is already captured, and a retitled anime silently goes unmonitored | §3.3 / §6 row 4: produces exactly the failure the module exists to prevent | S | `GLD-LAB-02` |
| `GLD-CAL-04` | **Test the Trakt calendar path** — the module's primary purpose, and the half that writes, is untested; only the pure MAL scorer has coverage | §3.6 — another instance of coverage not tracking risk | M | — |
| `GLD-CAL-05` | **Report monitor-ahead outcomes** — titles matched, monitored, and missed by the join | The join miss in §6 row 4 is currently invisible | S | `GLD-PLX-03` |
| `GLD-CAL-06` | **Confirm the `anime` genre default lands on a real affinity key** | §3.4 — depends on how `genre_affinity` keys genres | S | `GLD-AFF-05` |
| `GLD-CAL-07` | **Reconsider `calendar.mal` defaulting on** — the one place a MAL-configured install gains behaviour unasked | §3.5, against nine byte-identical-opt-in instances elsewhere | S | `GLD-AFF-06` |
| `GLD-CAL-08` | **Document the remaining ~10 KB** of `__init__.py` — the Trakt calendar fetch, cache write and monitor-ahead logic | Only the MAL filter was read this pass | M | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does the MAL→library join try `title_en` as a second key? | `GLD-CAL-03` |
| Q2 | Should `mal_min_watchability` be expressed as a percentile of the unowned pool rather than an absolute? | `GLD-CAL-02` |
| Q3 | How many MAL entries actually clear 20 in a typical season? | `GLD-CAL-05` |
| Q4 | Is the weak `dry_run` form a deliberate tier, or just the older pattern? | `GLD-CAL-01` |

**Q2 is worth a thought.** The threshold sits on a range whose ceiling depends on
how much signal an unowned title happens to carry — which varies with how well
the household's affinity covers anime genres. A percentile cut over the season's
own distribution would be stable against that, and would say what the operator
actually means: *"show me the top slice of next season."*

## 11. Related designs

- [`README.md`](./README.md) — the operational reference this complements
- [`machine_learning/thresholds/DESIGN.md`](../../machine_learning/thresholds/DESIGN.md) — the `mal_min_watchability` spec §3.1 confirms, and the two-axis problem it extends
- [`writeback/DESIGN.md`](../writeback/DESIGN.md) §3.4 · [`coordinator/DESIGN.md`](../coordinator/DESIGN.md) §3.6 — the two `dry_run` forms
- [`machine_learning/next_watch/DESIGN.md`](../../machine_learning/next_watch/DESIGN.md) — the MAL id bridge and the same title-join constraint
- [`machine_learning/scoring/DESIGN.md`](../../machine_learning/scoring/DESIGN.md) — `score_show`, called here on two groups
