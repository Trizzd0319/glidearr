# mal — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **mal**

**Manager** — `MALManager`
**Status** — ✅ Implemented · 🟢 The best-guarded identity join in the repo · 🟡 A second title-join implementation
**Existing docs** — [`README.md`](./README.md) (8.2 KB)

> **This document does not restate [`README.md`](./README.md).** It adds the
> 11-section frame and focuses on [`id_bridge.py`](./id_bridge.py), which two
> other packages depend on.

---

## 1. Problem statement

MAL is the only forward-intent feed that cannot be joined:

> `MALManager._norm` emits `ids {"tvdb": None, "tmdb": None, "mal": <id>}`, and
> the MAL API returns **neither a TVDb nor a TMDb id**. That single gap is why
> `plan_to_watch` sat **outside** `next_watch.build_intent_index` while
> `INTENT_SOURCE_STRENGTH` and `DATED_SOURCES` had already ranked it.

So the feed was graded, tiered and marked dated — and contributed nothing,
because nothing could match its entries to a library row. A signal fully
specified and structurally unreachable.

Closing it by title is dangerous in a specific way:

> The stake here is **higher than a monitoring flip** — a wrong match hands **A5
> points AND a delete shield** to a title nobody asked for.

A false positive does not merely surface the wrong anime. It protects it from
deletion while inflating its score.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Join MAL entries to library rows without an id. |
| G2 | A wrong match must be harder than a missed one. |
| G3 | Ambiguity resolves to nothing, never to a guess. |
| G4 | No network call on the score/deletion path. |
| G5 | The scan cost is paid rarely, and invalidates in both directions. |
| G6 | One definition of "same title" in the application. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Fuzzy matching | *"conservative by design: fuzzy matching could monitor the wrong series."* |
| N2 | Resolving every entry | *"a miss simply contributes no A5."* |
| N3 | Live lookup fallback | §3.4 — it would put a round-trip on the deletion path. |

---

## 3. Architecture

### 3.1 🎯 Three independent guards, not one

The join is exact normalised-title equality *"and deliberately nothing more"* —
then three things narrow it further:

**1. Candidate pool restriction, with measured counts.**

| Media | Pool | Size |
|---|---|---|
| Shows | `seriesType == "anime"` Sonarr rows | **1,926 of 11,986** |
| Movies | Radarr rows with an animation genre | **1,779 of 24,849** |

> MAL is an ANIME database — matching its titles against the whole library would
> put **"Sing" the anime and "Sing" the Illumination film one string comparison
> apart**, and the anime filter is the same discrimination `seriesType` already
> encodes.

The pool drops ~84 % of shows and ~93 % of movies before a single comparison.

**2. Multi-alias candidates on both sides.**

| Side | Titles tried |
|---|---|
| MAL | `title` + `alternative_titles.en` + `alternative_titles.synonyms` |
| Library | `title` + `cleanTitle` + each `alternateTitles[].title` |

**3. Ambiguity drops to nothing** (G3).

### 3.2 🎯 The worked example that makes the design legible

> A normalized title claimed by **two different library ids resolves to NOTHING**.
> This is not theoretical: `hunter x hunter` is both **tvdb 79076 (1999)** and
> **252322 (2011)** in this library, and MAL's English title for entry 11061 is
> exactly "Hunter x Hunter". Picking either would be a **coin flip**; its MAL
> `title` (**"Hunter x Hunter (2011)"**) matches the 2011 row unambiguously, so
> **the right answer is reached by the unambiguous alias and the ambiguous one is
> discarded.**

This is the clearest justification of a design decision in the codebase. The two
guards are not independent safeguards — they **compose**: multi-alias gives
several chances to match, ambiguity-drop ensures only the *discriminating* alias
counts, and the correct answer falls out with no tie-break heuristic anywhere.

A design that picked "the first match" or "the most recent year" would resolve
Hunter x Hunter correctly about half the time and be undebuggable when it did
not.

### 3.3 🟡 This revises `GLD-CAL-03`

Last session I logged: *"Try `title_en` in the MAL→library join — already
captured."* Reading the bridge, **it already tries `title_en` and synonyms and
three library fields.**

The item was based on [`calendar/`](../calendar/DESIGN.md)'s docstring
(*"exact normalized-title match"*), which is accurate but understates what the
bridge does. And the bridge names calendar as its **precedent**, not its
implementation:

> `services/calendar._library_ids_by_title` **set the precedent and states the
> reason** … so the same bar applies, **plus two guards**

So there are **two title-join implementations**: calendar's earlier
`_library_ids_by_title`, and this richer one that cites it and adds the pool
restriction and ambiguity-drop. They share the **normaliser** —

> `writeback._util.norm_title` does the normalising — **one definition of "same
> title" in the app**, not a second one here (G6)

— but not the **matching strategy**. That is §8 **P-E** in a narrow form: one
shared primitive, two policies built on it.

`GLD-CAL-03` should therefore be re-aimed: not *"add aliases to the join"* but
*"should calendar use the bridge's matcher?"* — and if not, why the same problem
warrants two answers. `GLD-MAL-01`.

### 3.4 🎯 Refusing to put a network call on the deletion path (G4)

> `trakt.lookup.search_show_by_title_and_year` would resolve some misses, but
> this bridge is built from inside the **SCORE PASS**, and a live search there
> would put a Trakt round-trip **per unresolved MAL entry on the deletion path**
> — with its own cache key, its own refresh policy and its own failure mode.
> **Deliberately out; a miss simply contributes no A5.**

Three reasons compressed into one sentence, and the third is the sharpest: a
lookup is not just latency, it is **a new failure mode on the path that deletes
files**. A Trakt outage would become a scoring perturbation, which would become a
deletion perturbation.

And the fallback is the [D36 rule](../../machine_learning/discovery/DESIGN.md) —
a miss contributes nothing rather than a guess.

### 3.5 Two-directional cache invalidation (G5)

The scan is *"~0.7s to read the Sonarr letter buckets plus ~1.0s for the Radarr
library, and `gather_intent_index` runs **twice per pass per instance**."*

```
rebuild when:  plan-to-watch fingerprint changes   ← MAL side changed
           or  map older than ID_MAP_TTL_S (1 week) ← LIBRARY side changed
```

> this bound only covers the other direction: a MAL entry that was unowned and
> **has since been added to the library**

A fingerprint alone would miss library additions forever; a TTL alone would lag
MAL edits by up to a week. Two triggers because there are two sources, each with
its own change signal — and the TTL is aligned to `sonarr/sync/media`'s, *"the
same TTL … for library-shaped data."*

### 3.6 🟡 `animeGenres` is inert on the movie side

```python
_ANIMATION_GENRES = frozenset({"animation", "anime"})
```

> Unioned with the operator's `animeGenres` (which on this install is
> `["anime"]` — **a Sonarr-shaped value that never appears on a TMDb movie**,
> hence the explicit "animation" here).

So the configured `animeGenres` contributes **nothing** to the movie pool; only
the hardcoded `"animation"` does. The workaround is correct and documented — but
an operator editing `animeGenres` expecting it to affect movie matching would see
no change, and nothing says so outside this comment.

Worth checking whether `animeGenres` is similarly Sonarr-only elsewhere it is
consumed. `GLD-MAL-03`.

### 3.7 The seventh identity instance — and the best-guarded

| Module | Join key | Guards |
|---|---|---|
| `discovery/occupancy` | ownership id | Structural |
| `people_matrix` | `person_tmdb_id` | Structural + bool rejection |
| `plex` | Plex uuid | Collision map, fail-closed |
| `mdblist` | tmdb + separate id-space files | Structural |
| **`mal/id_bridge`** | **title (no id exists)** | **Pool restriction + multi-alias + ambiguity-drop** |
| `calendar` (MAL path) | title | ❓ §3.3 |
| `labels/labeling` | series title | ❌ none |

The first four had an id. This one **did not** and built the guards anyway — which
makes it the direct model for `labels/`, where the same "no id available"
constraint currently has no guards at all.

`GLD-LAB-02` proposes building a `rating_key → series id` map, which is the right
long-term fix. But **ambiguity-drop is available today** and would convert
`labels/`'s silent mis-attribution into a silent *omission* — strictly better,
and the same one-line change. `GLD-MAL-02`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Exact normalised equality, never fuzzy | G2 — *"a wrong match hands A5 points AND a delete shield"* | Fuzzy / ratio matching |
| D2 | Restrict the pool to anime rows | *"Sing" the anime vs "Sing" the Illumination film* | Match the whole library |
| D3 | Multi-alias on both sides | Maximises legitimate matches without loosening the comparison | Primary title only |
| D4 | Ambiguity resolves to nothing | G3 — *"picking either would be a coin flip"* | First match / newest year |
| D5 | No network fallback | G4 — a lookup on the deletion path adds a failure mode, not just latency | Trakt search for misses |
| D6 | Share `norm_title` with writeback | G6 — one definition of "same title" | Local normaliser |
| D7 | Fingerprint **and** TTL invalidation | Two sources, two change signals (§3.5) | Either alone |
| D8 | TTL = 1 week, matching `sonarr/sync/media` | Library-shaped data, library-shaped TTL | Independent value |
| D9 | Hardcode `"animation"` | The operator's `animeGenres` is Sonarr-shaped (§3.6) | Rely on config |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | A title claimed by two library ids resolves to nothing. |
| I2 | Shows match only anime-typed rows; movies only animation-genre rows. |
| I3 | No network call occurs during bridge construction. |
| I4 | An unresolved entry contributes no A5 and no delete shield. |
| I5 | Normalisation uses `writeback._util.norm_title` and nothing else. |
| I6 | The map rebuilds on a plan-to-watch change or after one week. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Ambiguous title | Index build | Dropped (I1) — an unambiguous alias may still resolve it | Correct | ❌ **None** |
| No alias matches | — | No A5, no shield | Safe (D36) | ❌ **None** |
| Title outside the anime pool | Pool filter | Never compared | Correct | ❌ None |
| Library row added after the last build | TTL | Picked up within a week | Bounded lag | ❌ None |
| Plan-to-watch edited | Fingerprint | Immediate rebuild | Correct | ✅ |
| **`animeGenres` edited, movies unaffected** | **None** | Config change has no effect (§3.6) | 🟡 Surprising | ❌ **None** |
| Calendar's matcher diverges from the bridge's | **None** | Two answers to one question | 🟡 §3.3 | ❌ **None** |
| MAL API returns no ids | By design | The bridge exists for this | — | ✅ |

Every degradation is toward *no match*, which is the right direction given §1's
asymmetry. The gap is that **no row produces a signal** — an operator cannot tell
how many plan-to-watch entries resolved.

---

## 7. Configuration surface

| Key | Effect |
|---|---|
| `animeGenres` | Unioned into the movie pool — but Sonarr-shaped, so inert there (§3.6) |
| MAL auth / user | `README.md` |

Constants: `ID_MAP_TTL_S` 604,800 s · `ANIME_SERIES_TYPE` `"anime"` ·
`_ANIMATION_GENRES` `{animation, anime}`.
Cache key: `mal/{user}/id_map` (gzipped, fingerprinted).

---

## 8. Implemented capabilities

- ✅ Title bridge closing the one un-joinable forward-intent feed
- ✅ Anime-only candidate pools with measured selectivity
- ✅ Six title fields tried across both sides
- ✅ Ambiguity-drop with a documented real collision
- ✅ Shared normaliser — one definition of "same title"
- ✅ Zero network calls on the score/deletion path
- ✅ Fingerprint + TTL dual invalidation
- ✅ Gzipped, persisted id map
- ✅ **14.2 KB of tests against 13.8 KB of source** — ratio > 1

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-MAL-01` | 🟡 **Reconcile the two title matchers** — `calendar._library_ids_by_title` and `id_bridge`. They share `norm_title` but not the strategy; the bridge adds pool restriction and ambiguity-drop *(P-E)*. **Re-aims `GLD-CAL-03`** from "add aliases" to "should calendar use the bridge?" | §3.3 — one problem, two policies, and the weaker one is on the monitor-ahead path | S | `GLD-CAL-03` |
| `GLD-MAL-02` | 🎯 **Apply ambiguity-drop to `labels/labeling`** — the same no-id-available constraint with **no** guards today. Converts silent mis-attribution into silent omission — strictly better, and available before `GLD-LAB-02` lands | §3.7. This package is the model: it had no id and built the guards anyway | S | `GLD-LAB-02`, `GLD-LAB-04` |
| `GLD-MAL-03` | 🟡 **Audit `animeGenres` consumers** — it is Sonarr-shaped and contributes nothing to the movie pool; an operator editing it sees no effect | §3.6; the workaround is documented in one comment | S | `GLD-CFG-01` |
| `GLD-MAL-04` | **Report bridge resolution rate** — entries resolved / ambiguous-dropped / unmatched, per media type | §6: every failure mode is silent, and A5's anime coverage is unknowable | S | `GLD-NXW-04` |
| `GLD-MAL-05` | **Surface ambiguity drops specifically** — a title dropped for ambiguity is a *fixable* miss (add an alternate title in the *arr) | Distinguishes "can't match" from "won't guess" | S | `GLD-MAL-04` |
| `GLD-MAL-06` | **Reconsider the network refusal for a non-deletion path** — a Trakt lookup is unsafe in the score pass but fine in a one-shot enrichment tool | D5's reasoning is path-specific, not lookup-specific | M | `GLD-MAL-04` |
| `GLD-MAL-07` | **Document `api/` and `instances/`** — two subdirectories unread this pass | M | — |
| `GLD-MAL-08` | 🎯 **Cite §3.2 as the model for documenting a design decision** — a real collision, both candidate resolutions, and why the chosen one falls out with no heuristic | The clearest justification in the codebase; `DOCS_CONVENTIONS.md` has no exemplar | S | `GLD-DIS-04` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `calendar._library_ids_by_title` use aliases and ambiguity-drop, or only a primary-title map? | `GLD-MAL-01` |
| Q2 | What fraction of plan-to-watch entries actually resolve? | `GLD-MAL-04` |
| Q3 | Is `animeGenres` inert anywhere else it is read? | `GLD-MAL-03` |
| Q4 | Would ambiguity-drop alone materially improve `labels/`'s show join? | `GLD-MAL-02` |

**Q2 determines whether any of this matters.** The bridge exists to make
`mal_plantowatch` — ranked at **1.00**, the top intent tier — actually reach A5.
If the anime pool restriction plus exact matching resolves only a small fraction
of a household's plan-to-watch list, the feed is still mostly inert, just for a
subtler reason than before.

## 11. Related designs

- [`README.md`](./README.md) — the operational reference
- [`machine_learning/next_watch/DESIGN.md`](../../machine_learning/next_watch/DESIGN.md) — the consumer this bridge unblocked
- [`calendar/DESIGN.md`](../calendar/DESIGN.md) §3.3 — the precedent this cites, and the item §3.3 revises
- [`machine_learning/labels/DESIGN.md`](../../machine_learning/labels/DESIGN.md) §3.3 — the same constraint, unguarded
- [`writeback/DESIGN.md`](../writeback/DESIGN.md) — home of the shared `norm_title`
- [`machine_learning/discovery/DESIGN.md`](../../machine_learning/discovery/DESIGN.md) §3.3 — the fail-direction rule D5 follows
