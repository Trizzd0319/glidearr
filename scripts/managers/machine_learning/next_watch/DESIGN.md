# next_watch — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **next_watch**

**Package** — `scripts.managers.machine_learning.next_watch`
**Status** — ✅ Implemented · 🎯 The best data-hygiene decision in the codebase · 🟡 One cross-boundary duplicate
**Related** — [README.md](./README.md) · [`scoring/SCORING_GROUPS.md`](../scoring/SCORING_GROUPS.md) · [`acquisition/DESIGN.md`](../acquisition/DESIGN.md)

---

## 1. Problem statement

Forward intent — *"someone said they want to watch this"* — is the strongest
signal the household produces, and it arrives fragmented across three services
that agree on nothing:

| | Plex watchlist | Trakt watchlist | MAL plan-to-watch |
|---|---|---|---|
| Per-item timestamp | **none** | `listed_at`, back to 2020 | `list_status.updated_at` |
| Joinable id | tmdb / tvdb | tmdb / tvdb | **none** — title match only |
| Multi-member | yes, `watchlisted_by[]` | one account | one account |

Four problems follow:

1. **The same human appears three times.** One person keeping a Plex watchlist, a
   Trakt watchlist and a MAL list is *one person asking*, not three.

2. **Two of three feeds are dated, one is not.** Decaying stale intent is
   correct — but only where a real timestamp exists.

3. **Solo intent dominates.** This household's watchlist is **258 of 259 solo**.
   A member-count signal that saturates at one member is a constant.

4. **The signal must not become a second opinion.** Acquisition already ranks
   feed strength. If keeping ranked them differently, a title would be graded one
   way to acquire and another to retain.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One human = one member, across all three services. |
| G2 | Decay only where a real timestamp exists. |
| G3 | The member ladder must grade the actual distribution, not saturate on it. |
| G4 | Feed strength has one definition, shared with acquisition. |
| G5 | Idempotent and order-independent folding. |
| G6 | Pure — `dict` in, `dict` out; brain-purity guarded. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Fetching any feed | *"The PlexWatchlistManager (a service) does the I/O."* |
| N2 | Bridging MAL ids | `services/mal/id_bridge` resolves the title match; this receives resolved rows. |
| N3 | Being the curation authority | *"The deterministic A–G scorecard stays the curation authority."* |
| N4 | Deciding retention | Emits the index; the shield decides. |

---

## 3. Architecture

### 3.1 One index, two consumers

```
plex_union · trakt_shows/movies · mal_shows/movies · member_anchors
        │
        ▼  build_intent_index
{"movies": {tmdb: entry}, "shows": {tvdb: entry}}
        │
        ├──► Group-A5   scoring/_shared.watchlist_intent_score
        └──► the watchlist delete shield  (via `anchor`)
```

The docstring is explicit that this dual use is what rescued the package:
*"`build_intent_index` is why this subpackage is no longer orphaned."* It was a
next-watch ranker nothing consumed; wiring it to A5 gave the same computation a
second, load-bearing reader.

That is the resolution of a §8 **P-A** instance — a signal computed and unread —
achieved not by deleting it but by finding its real consumer.

### 3.2 🎯 The refusal to fabricate a timestamp

This is the single best piece of data hygiene I have read in this codebase, and
it is worth quoting in full:

> **UNDATED**: the union has no per-item timestamp, and the rolling
> `plex/watchlist/snapshot/` files retain well under a day (**8 files spanning ~7
> hours on this install**), so a "first seen" derived from them would say every
> title was added yesterday — a **FABRICATED timestamp**, and a decay applied to
> it would be **noise dressed as evidence**. Plex intent is therefore scored at
> full strength with no decay until Plex exposes a real `addedAt`.

Every ingredient for a plausible-looking `first_seen` is present. The snapshots
exist, they are timestamped, a `min()` over them would produce a date, and the
decay machinery is already built for Trakt. It would have worked, produced
numbers, and been completely wrong — and *nothing downstream could have detected
it*, because a fabricated date is indistinguishable from a real one once it is in
the column.

The decision is to leave the feature off for that source and say why.

Contrast this with the running §8 **P-C** tally, where the recurring failure is
absent data being silently treated as present-and-zero. Here the same temptation
appears in its most seductive form — the data is *derivable*, just not *real* —
and is refused explicitly. `GLD-NXW-05` proposes recording it as the reference
example.

### 3.3 One human, three lists (G1)

`trakt_member` maps the Trakt account to a household member; MAL rides the same
member. The reason is stated as a measured consequence:

> counting them separately would walk a solo title up the member ladder
> (**0.60 → 0.84** of the cap) on nothing but **account sprawl**

Without this, the household's most engaged member — the one most likely to keep
lists on all three services — would systematically inflate every title they
touched. The signal meant to detect *"several people want this"* would instead
detect *"one person uses several apps."*

### 3.4 The ladder is calibrated to the real distribution (G3)

```
member_fraction:  1 → 0.60   2 → 0.72   3 → 0.84   4 → 0.96   5+ → 1.00
```

> A solo watchlister is deliberately worth 0.60 of the cap rather than the whole
> thing — otherwise the member term is invisible (every title already at cap) on
> the **258-of-259 solo distribution this household has today**

Starting a solo title at the cap would make the multi-member case unrepresentable
— there is nowhere above the ceiling for the one title two people both asked for
to go. Starting at 0.60 leaves headroom for exactly the case the signal exists to
detect.

Same instinct as [`likelihood/DESIGN.md`](../likelihood/DESIGN.md) §3.5's
rejection of a gain change: *"titles pinned at the cap are indistinguishable — a
constant."*

### 3.5 🟡 A duplicate that brain purity makes unavoidable

```python
# THE SAME RANKING as services/acquisition/scorer._SOURCE_SCORE, divided by 100
INTENT_SOURCE_STRENGTH = { "plex_watchlist": 1.00, ... }
```

The intent (G4) is right and clearly stated. The implementation is a **hardcoded
copy**, and nothing keeps the two in step — no import, no equivalence test. Change
`_SOURCE_SCORE` and this silently disagrees.

**The interesting part is why it cannot simply delegate.** The
[`ARCHITECTURE.md`](../ARCHITECTURE.md) import rule is one-way: `services → ml`,
never the reverse, and `brain_purity` enforces it. So the brain **cannot import**
`services/acquisition/scorer._SOURCE_SCORE`. Copying is the only option available
under the current layering.

This is §8 **P-E** produced *by a correct architectural rule* rather than by
carelessness — and it is milder than
[`foundation/downgrade_credit`](../foundation/DESIGN.md) §3.5 only in that the
values are simpler, not in that it is better guarded: `downgrade_credit` at least
has an equivalence test.

The clean fix is available and small: move the source-strength table **into the
brain** — `contracts/` or a shared constants module — and have
`services/acquisition/scorer` import it. That inverts the copy in the direction
the layering already permits. `GLD-NXW-01`.

### 3.6 Folding is idempotent and order-independent (G5)

`_add` uses sets for `sources`, `members` and `anchors`, so *"the same title seen
twice from the same member/source produces the same entry."* `_freeze` converts
to sorted tuples — *"JSON-safe and memo-stable."*

Two per-field rules worth noting:

**Newest listing wins per source.** *"re-adding a title to your Trakt watchlist is
fresh intent, and the oldest copy must not be allowed to speak for it."*

**Anchor is the max across members.** *"intent lives while the member who
expressed it is still active."* So a title watchlisted by a lapsed member and an
active one anchors on the active one — the shield holds while anyone who asked is
still watching anything.

### 3.7 Stale intent floors rather than vanishing

> a plan-to-watch entry last touched in 2023 is **honest evidence of stale
> intent, not of none**, so it floors at `INTENT_STALE_FLOOR` rather than
> vanishing

The right distinction. Someone who added a title in 2023 and never removed it
still, weakly, wants it — decaying to exactly zero would treat that as equivalent
to never having asked.

`INTENT_STALE_FLOOR` is referenced here but defined elsewhere (presumably
`scoring/_shared`), so the decay curve's floor is not visible from the package
that documents it. `GLD-NXW-06`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Do not fabricate a Plex timestamp | §3.2 — *"noise dressed as evidence"*; undetectable once written | Derive from snapshots |
| D2 | Decay only `DATED_SOURCES` | G2 — a decay needs a real date | Decay everything |
| D3 | Trakt + MAL attributed to one member | G1 — one human, three lists | Count accounts |
| D4 | Solo = 0.60 of cap | G3 — measured against 258/259 solo | Solo at cap |
| D5 | Mirror acquisition's source ranking | G4 — one grading for acquire and keep | Independent tiers |
| D6 | Unknown feed ⇒ 0.0 | *"rather than a guessed tier"* — fail toward inaction | Default mid-tier |
| D7 | Newest `listed_at` per source | Re-adding is fresh intent | First-seen |
| D8 | Anchor = most recent member activity | Intent lives while the asker is active | Oldest, or per-member |
| D9 | Stale floors, never vanishes | Stale intent ≠ no intent | Decay to 0 |
| D10 | Sets → sorted tuples on freeze | G5 — JSON-safe and memo-stable | Keep sets |
| D11 | Entire package in `__init__.py` | Small enough that submodules would be ceremony | Split it |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Folding is idempotent and order-independent. |
| I2 | One human contributes one member regardless of how many lists they keep. |
| I3 | Only `DATED_SOURCES` entries carry a `listed_at`. |
| I4 | An unknown feed contributes 0.0. |
| I5 | `member_fraction ∈ [0.60, 1.00]`. |
| I6 | `anchor` is the maximum activity timestamp among contributing members. |
| I7 | The newest listing per source wins. |
| I8 | This package performs no I/O. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Plex exposes no `addedAt` | Documented | Full strength, no decay (D1) | Correct | ✅ In-source |
| A new feed is added upstream | Missing key | Contributes 0.0 | 🟡 Silent omission | ❌ **None** |
| `trakt_member` unresolved | `None` | Trakt intent has no member and no anchor | 🟡 Ladder under-counts; shield loses its anchor | ❌ **None** |
| MAL title bridge misses | Service-side | Row never reaches this function | 🟡 Silent | ❌ **None** |
| **`_SOURCE_SCORE` changes in the service** | **None** | Acquire and keep grade a feed differently | 🟡 §3.5 | ❌ **None** |
| Member anchor missing | `anchors.get` → None | Entry has no anchor; shield cannot expire it | 🟡 Shield may hold indefinitely | ❌ **None** |
| Item has neither tmdb nor tvdb | `_int_or_none` → None | `_add` returns early — item dropped | 🟡 Silent | ❌ **None** |

**Rows 2 and 5 are the same risk from two directions**: the table is a snapshot of
another module's constant, and both a new feed and a changed weight desynchronise
it silently. Row 3 is worth a signal too — an unresolved `trakt_member` costs both
the ladder position *and* the shield anchor, which is a lot to lose quietly.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `scoring.watchlist_intent.cap` | — | Group-A5's scale for the member ladder |
| `trakt.username` | — | Resolved to a household member for D3 |

Constants: `INTENT_SOURCE_STRENGTH` (9 feeds) · `DATED_SOURCES` (2) ·
`MEMBER_BASE_FRACTION` 0.60 · `MEMBER_STEP_FRACTION` 0.12 · `watchlist_intent`
weights base 60 / +12 / cap 100.

---

## 8. Implemented capabilities

- ✅ Unified intent index across Plex, Trakt and MAL
- ✅ Nine-tier source-strength ladder mirroring acquisition's ranking
- ✅ Dated/undated split with an explicit refusal to fabricate timestamps
- ✅ Cross-service member deduplication against account sprawl
- ✅ Member ladder calibrated to the measured 258/259 solo distribution
- ✅ Idempotent, order-independent, JSON-safe folding
- ✅ Newest-listing-per-source resolution
- ✅ Activity anchor for the delete shield
- ✅ Stale-intent floor rather than decay-to-zero
- ✅ Dual consumption — next-watch ranking **and** Group-A5

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-NXW-01` | 🟡 **Move the source-strength table into the brain** and have `services/acquisition/scorer` import it — inverting the copy in the direction the layering permits | §3.5: brain purity makes the current duplicate unavoidable *and* unguarded. Fixes the cause, not the symptom *(P-E)* | S | `GLD-CON-01` |
| `GLD-NXW-02` | **Warn on an unknown feed** reaching `INTENT_SOURCE_STRENGTH` | §6 row 2: a new upstream feed contributes 0.0 silently | S | `GLD-NXW-01` |
| `GLD-NXW-03` | **Warn when `trakt_member` is unresolved** — it costs both the ladder position and the shield anchor | §6 row 3 | S | — |
| `GLD-NXW-04` | **Report intent-index coverage** — titles per source, members resolved, entries dated vs undated | The index feeds A5 and the delete shield with no visibility | S | `GLD-LAB-03` |
| `GLD-NXW-05` | 🎯 **Record §3.2 in `DOCS_CONVENTIONS.md`** as the reference example of *refusing to derive a plausible value* | The strongest counter-example to the P-C pattern in the repo, currently visible only in one docstring | S | `GLD-DIS-04` |
| `GLD-NXW-06` | **Surface `INTENT_STALE_FLOOR`** in this package's docs — the decay floor is referenced here and defined elsewhere | The curve is documented where its floor is invisible | S | — |
| `GLD-NXW-07` | **Re-measure the solo distribution** — 258/259 justified the 0.60 base and is a moving number | D4's calibration has an expiry date nobody is watching | S | `GLD-FND-04` |
| `GLD-NXW-08` | **Revisit decay once Plex exposes `addedAt`** — the code is ready, the data is not | D1 is explicitly provisional | S | upstream |
| `GLD-NXW-09` | **Warn on items with neither tmdb nor tvdb** rather than dropping silently | §6 row 7 | S | — |
| `GLD-NXW-10` | **Validate `people_cooccurrence` at 0.60** — it is the only algorithmic feed sourced from this repo rather than a third party | Self-generated intent graded like a third-party recommendation | M | `GLD-PPL-01` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should the source-strength table live in the brain, with the service importing it? *(= D40)* | `GLD-NXW-01` |
| Q2 | Is 0.60 still right for solo, or has the distribution moved? | `GLD-NXW-07` |
| Q3 | Should an entry with no anchor be shielded indefinitely, or not shielded at all? | `GLD-NXW-03` |
| Q4 | Is `people_cooccurrence` correctly graded alongside third-party recommendation feeds? | `GLD-NXW-10` |

**Q3 has a real edge.** The anchor is what lets the shield expire. An entry
without one cannot expire, so a watchlisted title from a member with no recorded
activity is protected forever — which under the [D36
rule](../discovery/DESIGN.md) is arguably correct (fail toward not deleting), but
is worth being a deliberate choice rather than a consequence of a missing lookup.

## 11. Related designs

- [`scoring/SCORING_GROUPS.md`](../scoring/SCORING_GROUPS.md) — Group A5, the consumer that de-orphaned this package
- [`acquisition/DESIGN.md`](../acquisition/DESIGN.md) — the `_SOURCE_SCORE` this mirrors
- [`likelihood/DESIGN.md`](../likelihood/DESIGN.md) §3.5 — the same anti-saturation argument
- [`discovery/DESIGN.md`](../discovery/DESIGN.md) §3.3 — the fail-direction rule D6 follows
- [`people_matrix/README.md`](../people_matrix/README.md) — the `people_cooccurrence` feed
- [`services/mal/README.md`](../../services/mal/README.md) — the title-match id bridge
