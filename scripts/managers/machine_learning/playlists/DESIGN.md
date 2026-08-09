# playlists — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **playlists**

**Package** — `scripts.managers.machine_learning.playlists`
**Status** — ✅ Implemented · 🔴 The spoiler invariant has no test file · 🟡 Two modules untested
**Related** — [README.md](./README.md) · [`services/plex/DESIGN_personal_playlists.md`](../../services/plex/DESIGN_personal_playlists.md)

---

## 1. Problem statement

A personalised playlist is the one output a household member *reads directly*.
Every other decision this system makes is invisible until something is missing;
a playlist is judged on sight.

Four constraints, and they conflict:

1. **Spoilers are unrecoverable.** Surface episode 7 above episode 4 once and the
   damage is done. Ordering by air date seems natural and is the trap — air dates
   are missing, wrong, or out of order often enough that the *"#1 spoiler trap"*
   is precisely that.

2. **Coherence beats ranking.** Strictly ranking by watchability would interleave
   three films of a trilogy with unrelated titles. A playlist that scatters a
   franchise is worse than one that ranks imperfectly.

3. **Size is unbounded and unpaginated.** *"Plex doesn't paginate it for the
   user"* — a whole-library playlist is unscannable, and expanding one 20-season
   show could swamp everything else.

4. **It must be explainable.** *"Why is this in my playlist?"* is the first
   question anyone asks of a recommendation.

The design answers all four with one ordering rule, a group-atomic cap, and a
checkable invariant.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | A later episode never precedes an earlier unwatched one. |
| G2 | Franchise groups stay contiguous. |
| G3 | Fully deterministic — no clock, no randomness, no input-order dependence. |
| G4 | No single group can starve the playlist. |
| G5 | Truncation is always observable. |
| G6 | Every item can state why it is there. |
| G7 | Pure — no HTTP, no Plex, no cache, no ratingKey resolution. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Resolving ratingKeys | Service does it before the brain sees an item. |
| N2 | Writing to Plex | Brain returns a `PlaylistPlan`. |
| N3 | Scoring watchability | Attached by the service from the scorer. |
| N4 | Fetching episode inventory | *"the service supplies the owned episode rows."* |

---

## 3. Architecture

### 3.1 The three-level ordering rule

```
GROUP     items sharing series / franchise / universe      → contiguous      (G2)
WITHIN    timeline index, else chronological
          series → (season, episode)  NOT air date                           (G1)
ACROSS    groups and standalone items → watchability
```

The middle line carries the weight. Ordering a series by `(season, episode)`
rather than air date makes G1 **structural**: the sort key itself cannot produce
an out-of-order result, regardless of how bad the air-date metadata is. Ordering
by air date and then checking for spoilers would be a guard on a fallible sort;
this is a sort that cannot fail that way.

### 3.2 Determinism, claimed absolutely

> no wall-clock, no randomness, **no input-order dependence** (every tie has an
> explicit deterministic breaker so a golden corpus can pin the result)

The third clause is the one most packages omit. Two items with equal watchability
must order identically whether the service happened to hand them over in one
sequence or another — otherwise a golden corpus pins nothing and a re-run
produces a different playlist from identical data.

Worth contrasting with [`scoring/DESIGN.md`](../scoring/DESIGN.md) §3.5, which
honestly admits F3 and G4 read the clock. This package claims none, and it is the
package where determinism is most visible to the household: a playlist that
reshuffles between runs for no reason reads as broken.

### 3.3 Group-atomic capping (G4, G5)

```python
for block in blocks:
    if len(kept) + len(block) <= max_items:
        kept.extend(block)      # fits → take the whole group
    # else: SKIP and keep filling from smaller lower-ranked groups
```

The skip-don't-stop choice is explicitly reasoned:

> a single oversized group — e.g. a 200-member mega-group — must not be able to
> starve the entire playlist down to the handful of items that happened to rank
> ahead of it

Two properties fall out, both documented rather than left implicit:

- **`kept` is not a prefix.** *"The caller aligns metadata by item identity, not
  by slicing."* A caller that sliced parallel metadata arrays would silently
  mis-associate every item after the first skipped group.
- **The dropped count is returned** — *"so truncation is observable, never
  silent."*

That second point deserves emphasis given this sweep's running §8 **P-D** tally.
Nearly every silent-drop finding so far — partial history pulls, skipped
instances, unmatched affinity entries — could have been avoided by returning a
count. This module does it, in three lines.

The last-resort branch is also right: if even the smallest group exceeds the cap,
truncate within the top-ranked group, *"better than an empty playlist."*

### 3.4 🔴 The spoiler invariant has no test file

[`spoiler.py`](./spoiler.py) states its own purpose:

> this module asserts that property **so it can be pinned by property tests** and
> (cheaply) re-checked at runtime before a write

There is **no `test_spoiler.py`** in the package.

The test inventory is `test_cert_gate`, `test_expansion`, `test_grouping`,
`test_ordering`, `test_per_user`, `test_rationale`, `test_recency`,
`test_timeline`. No spoiler, no caps, no models, no engagement.

Two readings, and they matter differently:

1. **`test_ordering.py` (11.9 KB) uses `is_spoiler_safe` as an assertion helper.**
   Plausible, good practice, and would mean the invariant *is* exercised — just
   not through a file named for it.
2. **It genuinely has no coverage.** The one function whose stated reason for
   existing is to be property-tested, isn't.

I have not read `test_ordering.py`, so I am not asserting which. But the module
naming its own purpose and that purpose having no matching file is worth
resolving. `GLD-PLY-01`.

The second half of the sentence is a separate question: *"(cheaply) re-checked at
runtime **before a write**."* That is phrased as capability — *so it can be* —
not as confirmed wiring. Whether the service actually calls `is_spoiler_safe`
before the Plex write is the same shape as `GLD-QAN-01` and `GLD-SON-01`:
built, correct, wiring unverified. `GLD-PLY-02`.

**`caps.py` is also untested**, and its skip-don't-stop behaviour has exactly the
subtlety that regresses quietly — a refactor to `break` instead of skipping would
pass any test that only checks `len(kept) <= max_items`.

### 3.5 Expansion is bounded by construction

> This is the **single biggest blast radius** (a 20-season library could explode a
> playlist), so expansion is ALWAYS capped.

`expand_show` returns `eps[:cap]` unconditionally — the cap is not a policy the
caller may skip. Default 25, `next_unwatched_n` mode giving *"the natural
'continue the show' set."*

Specials are excluded unless asked, consistent with `spoiler.py` exempting them
from the ordering invariant. The two modules agree on what a special is
(`is_special or season == 0`), which matters — a disagreement would let a special
count for one rule and not the other.

### 3.6 Explainability, with a dormant half

`explain_reason` priority, strongest personal signal first:

```
active-watching (JIT) → user's top affinity genres → matching cast/crew
→ franchise/universe → "household pick"
```

The fallback is deliberate: *"a plain 'household pick' fallback for a profile with
no personal signal yet"* — a new viewer gets a reason rather than a blank.

Two details worth recording:

**Cast/crew is dormant, not missing.** *"cast/crew explanation lights up
automatically once the enrichment daemon populates `cast_names`/`director_names`
(sparse today); genres + JIT + franchise carry it now."* The feature degrades to
its other signals rather than erroring — and self-enables when the data arrives.
That is the **byte-identical opt-in** pattern again, now the sixth instance.

**Encoding safety.** *"ASCII + cp1252-safe only (`·` = 0xB7) so it never crashes
the Windows console log handler."* The same class of hardening as
[`hooks/`](../../../hooks/DESIGN.md)'s `reconfigure(errors="replace")` — a
non-obvious failure that has clearly bitten before.

### 3.7 A stray character in the spec

The `__init__.py` docstring contains a mojibake in the crown-jewel rule:

```
a missing/￧out-of-order air date
```

`￧` (U+FFE7) is not intended. Cosmetic, but it sits in the sentence stating the
package's most important invariant — the one another engineer will read first.
`GLD-PLY-06`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Order series by `(season, episode)`, never air date | G1 structural — bad metadata cannot produce a spoiler | Air date + a guard |
| D2 | Group before ranking | G2 — a scattered trilogy is worse than an imperfect rank | Global watchability sort |
| D3 | Explicit tie-breakers everywhere | G3 — otherwise a golden corpus pins nothing | Stable sort only |
| D4 | Skip oversized groups, don't stop | G4 — one mega-group cannot starve the playlist | Stop at first overflow |
| D5 | Return the dropped count | G5 — truncation must be observable | Silent truncation |
| D6 | `kept` is not a prefix; align by identity | Direct consequence of D4, and documented so callers don't slice | Preserve prefix property |
| D7 | Truncate within the top group as last resort | *"better than an empty playlist"* | Return empty |
| D8 | Expansion cap is unconditional | The biggest blast radius in the package | Caller-optional cap |
| D9 | Specials exempt from the spoiler invariant | *"they legitimately sit at a track tail"* | Include them |
| D10 | Spoiler safety verified, not enforced by mutation | A checkable invariant beats a reordering pass that might itself be wrong | Sort-then-fix |
| D11 | `"household pick"` fallback | A new profile gets a reason, not a blank | No reason |
| D12 | cp1252-safe output only | The Windows console handler crashes otherwise | Unicode freely |
| D13 | Cast/crew self-enables on data arrival | Degrades to other signals; no flag to flip | Gate on a config flag |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | For every series, non-special episodes appear in non-decreasing `(season, episode)` order. |
| I2 | Items in one group are contiguous in the output. |
| I3 | Same input ⇒ same output, independent of input order. |
| I4 | `len(kept) <= max_items`, or the top group truncated. |
| I5 | The dropped count is always returned. |
| I6 | `expand_show` returns at most `cap` items. |
| I7 | Every item has a non-empty reason. |
| I8 | Output is cp1252-encodable. |
| I9 | This package performs no I/O and resolves no ratingKeys. |

**I1 is checkable at runtime** via `is_spoiler_safe` — the only invariant in the
brain with a dedicated verification function.

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Air dates missing or wrong | **N/A** | `(season, episode)` sort is unaffected (D1) | None | ✅ By construction |
| One group exceeds the whole cap | Guard | Truncate within the top group | Bounded | ✅ Dropped count |
| A 200-member group ranks first | Skip logic | Skipped; smaller groups fill | Bounded | ✅ Dropped count |
| Caller slices parallel metadata | **None** | Mis-associates every item after a skipped group | 🟡 Documented, not guarded | ❌ **None** |
| Cast/crew data absent | Falls through | Genres / JIT / franchise carry the reason | Graceful | ✅ Documented |
| No personal signal at all | Fallback | `"household pick"` | Graceful | ✅ |
| Show has 20 seasons | Unconditional cap | ≤25 episodes | Bounded | ❌ None |
| **Spoiler invariant regresses** | `is_spoiler_safe` exists | ❓ Unknown whether it runs before a write | 🔴 Unrecoverable if it fires | ❌ **Unconfirmed** |
| `caps.py` refactored to `break` | **No test** | Reverts to starve-on-first-overflow | 🟡 Silent regression | ❌ **None** |

**Row 8 is the one that matters.** A spoiler is the single unrecoverable failure
this package can produce, the invariant is written, and whether it is checked
before the write is unverified.

---

## 7. Configuration surface

Parameters, not config keys — the service supplies them:

| Parameter | Default | Effect |
|---|---|---|
| `max_items` | — | Playlist size cap; falsy or ≤0 disables |
| `cap` (expansion) | `25` | Max episodes per expanded show |
| `mode` | `next_unwatched_n` | vs `full_series` |
| `include_specials` | `False` | Expansion only |
| `max_genres` | `2` | Genres named in a reason |

---

## 8. Implemented capabilities

- ✅ Three-level ordering: group → timeline → watchability
- ✅ `(season, episode)` sort making spoiler safety structural
- ✅ Explicit deterministic tie-breakers throughout
- ✅ Group-atomic size cap with skip-don't-stop and an observable dropped count
- ✅ Non-empty last-resort truncation
- ✅ Unconditionally capped show expansion in two modes
- ✅ Runtime-checkable spoiler invariant with specials exemption
- ✅ Certification gating with tier levels
- ✅ Per-user tilt scoring
- ✅ Prioritised rationale with a household fallback
- ✅ Self-enabling cast/crew explanation
- ✅ cp1252-safe output
- ✅ Group coverage statistics
- ✅ Per-group member limit (`caps.limit_per_group`) for one-offs families
- ✅ Per-user weekday habit model (`habits.py`) with jitter disabled
- ✅ Multi-level grouping ladder: series → franchise → genre → medium
- ✅ Candidate resolution per group level with cross-level de-duplication
- ✅ Learned per-weekday session-length cap, shrunk from a conventional prior
- ✅ Runtime sourced from the Radarr/Sonarr parquets, zero Plex calls
- ✅ Outcome ledger (`outcomes.py`) + row-level provenance (`provenance.py`)
- ✅ Walk-forward backtest (`backtest.py`) for threshold selection

## 8a. The Tonight subsystem

Added across one session; the numbers below are measured on this household, not
assumed. Recorded because several were surprising and all of them are load-bearing.

### 8a.1 What Tonight is

A **per-profile** list for **one upcoming day**: 5–10 different groups, exactly
one item each. Built for *tomorrow* so it is waiting when somebody sits down.

It is a per-user PLAYLIST, not an account-wide shelf, because a weekday habit is
a property of one person. That choice costs the Home row — Plex will not promote
a playlist — and the trade was made deliberately.

It is **not** runtime-filtered at selection time and never was successfully: an
earlier version filtered on `owned_inventory[*].duration_ms`, a field that was
always `None` because Plex returns `duration` on `/library/metadata/{rk}` but
**not** on the `/library/sections/{key}/all` listing the scan pages through. The
shelf selected nothing on every run. Runtime now enters only as a per-day cap
(§8a.4), sourced from parquet.

### 8a.2 The grouping ladder

`habits.derive_key` resolves each play to ONE namespaced group, most specific
first: `series:` → `franchise:` → `genre:` → `medium:`.

A TV show repeats, so its weekday shape is solid. **A movie is watched once** —
nobody watches Alien every Saturday — so movies group one level up. Detectability
falls off down the ladder, which makes it a confidence ordering as well as a
fallback: a series accumulates a dozen plays a season; a franchise might get
three Saturday plays in a quarter, barely over `min_plays` and easily noise.
`medium:movie` ("Saturday is movie night") is the most reliable movie signal,
because every movie play feeds it.

Namespacing is not cosmetic — an unprefixed franchise called "3" would collide
with a series whose ratingKey is "3" and silently fuse into one bogus habit.

### 8a.3 Jitter is OFF, and that is one decision with the scoring

`habits.DEFAULT_JITTER_SIGMA_DAYS = 0.0`. A play counts only toward the day it
happened on. The circular-Gaussian kernel is still implemented and correct; it is
simply not used.

Measured before switching it off: the Tuesday list was **identical** with the
kernel on or off, and the shares got sharper (a clean Tuesday show 0.499 →
1.000). What it removed was BLEED — with the kernel on, a Tuesday show also
appeared on Wednesday's list, where under strict same-day scoring it could only
ever record a miss. The model was manufacturing its own failures.

This pairs with `outcomes.SCHEDULED_FAMILIES`. **Raise the kernel only alongside
re-introducing grace**, or the model is penalised for doing what it was told.

### 8a.4 The session-length cap is LEARNED

"Cap movies on weeknights, allow them at the weekend" is a claim about somebody
else's week. Measured against this household's own history it is wrong for two
profiles in three:

| profile | busiest days | conventional? |
|---|---|---|
| 8592385 | Sat 138 | yes |
| 569473003 | Wed 37, Mon 33, Fri 33 · Sat only 22 | **no** — midweek |
| 795458226 | Mon 16, Sun 15 · Fri 2 | **no** — Monday |

`habits.session_profile` computes, per weekday, the decayed share of viewing that
ran long (≥ `DEFAULT_LONG_MINUTES = 75`), shrunk toward a conventional prior by
`n / (n + 8)`. So the answer IS the convention on day one and becomes the
profile's own week as evidence accumulates. Observed on synthetic data for a
Mon/Tue-off household — note Saturday CLOSING as its 0.60 prior loses to evidence:

```
                Mon Tue Wed Thu Fri Sat Sun
day one         60  60  60  60  60  --  --
after 2 weeks   --  --  60  60  60  --  --
after 6 weeks   --  --  60  60  60  60  60
after 14 weeks  --  --  60  60  60  60  60
```

75 rather than 60 for "long" so a 65-minute drama episode does not read as a
film-length commitment. `runtime_cap_for` returns `None` for an open day rather
than a large number: "no cap" and "a very high cap" read the same in output but
differ in intent.

### 8a.5 Runtime comes from the parquets — no Plex calls

Measured coverage:

| source | column | coverage |
|---|---|---|
| `radarr/*/movie_files.parquet` | `runtime_minutes` | **938/938** (100%), median 105 min |
| `sonarr/*/episode_files.parquet` | `runtime_seconds` | **3,939/3,949** (99.7%) of OWNED episodes |

The episode figure looks like 52% against the raw 16,033-row file because that
parquet also holds episodes we do not own. Joined to `owned_episodes` on
`(series_id, season_number, episode_number)` it is all but complete. Median
episode 23 min; **99% of measured episodes are already under 60 minutes**, so the
cap acts almost entirely on films.

This replaced a design that would have spent ~292 batched `/library/metadata`
calls per scan to fill a field the download clients had already recorded.

### 8a.6 The cap rejects DURING selection

`candidates.pick_one_per_group(..., accept=...)` filters as it walks, so a
rejected candidate lets its group offer the NEXT one. Applied afterwards, a
Tuesday whose top pick is a two-hour film would lose that group entirely instead
of getting the show's shorter episode.

**Unknown runtime is ADMITTED here** — the opposite of the habit model's rule,
deliberately. In `session_profile` an unknown is evidence being counted and must
be excluded; in `accept` it is a candidate being judged, and refusing everything
unmeasured would empty the list over a metadata gap rather than over anything the
household did.

### 8a.7 Attribution: Tonight vs Up Next

```
played on the local calendar day Tonight was built for  -> TONIGHT
played on any other day                                 -> UP NEXT
played another day, and Up Next never had it            -> nobody
```

No grace, no partial credit, **no knob** — `grace_days` was removed rather than
defaulted to 0, because a knob that exists gets turned. Tonight's claim is not
"you will watch this" but "you will watch this ON TUESDAY"; a Wednesday play means
the show was right and the DAY was wrong, which is exactly the error the weekday
model must see.

Precedence: a SCHEDULED family that hit its day outranks every continuous family
regardless of surfacing order. Ranking on earliest-surfaced alone inverted the
rule in the common case — Up Next is a standing list, so an item usually sits in
it for days before Tonight picks it up, and Up Next took the credit on Tonight's
own target day.

This is an **inference, not a measurement**: Tautulli records no referrer.
`provenance.exclusivity` reports how much of the data is unambiguous so a hit rate
drawn from the ambiguous half can be discounted honestly.

### 8a.8 Thresholds are set by backtest, not by taste

`backtest.py` replays history walk-forward — for target day D the profile is
built only from plays strictly before D's local midnight. A day nobody watched is
DORMANT and excluded rather than counted as a miss.

First run on 717 real plays / 130 scored days:

```
 thresh  hit   prec  silent
  0.16   0.32  0.12  0.09
  0.28   0.07  0.05  0.39   <- the guessed default
  0.50   0.00  0.00  0.68
```

`DEFAULT_DAY_THRESHOLD` was moved 0.28 → **0.16** on this evidence: a 4.5×
improvement in hit rate and silence on 9% of days instead of 39%. `best_cell`
enforces `min_coverage=0.5` to reject the degenerate optimum — a high threshold
speaks rarely and only when confident, scoring well while being absent six days a
week.

### 8a.9 Poster copy must not outlive its rule

Tonight's poster read `{{COUNT}} under an hour` after the runtime filter was
removed, and shipped live onto a playlist holding 8h12m. Copy asserting a property
the selection no longer enforces is worse than vaguer copy, because it is
checkable and wrong. Now `{{COUNT}} for tonight`.


## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-PLY-01` | 🔴 **Add `test_spoiler.py`** — or confirm `test_ordering.py` exercises `is_spoiler_safe`. The module's stated purpose is *"so it can be pinned by property tests"* and no such file exists | The one unrecoverable failure this package can cause | S | — |
| `GLD-PLY-02` | ❓ **Confirm `is_spoiler_safe` runs before the Plex write** — the docstring says *"can be… re-checked at runtime"*, which is capability, not wiring | §6 row 8; same shape as `GLD-QAN-01` | S | `GLD-QAN-01` |
| `GLD-PLY-03` | 🟡 **Add `test_caps.py`** — skip-don't-stop would regress silently under a `break` refactor | §6 row 9; a test asserting `len(kept) <= max` would not catch it | S | — |
| `GLD-PLY-04` | **Report truncation in the run summary** — the count is returned and may not be surfaced | The mechanism for observability exists; the observation may not | S | `GLD-DIS-01` |
| `GLD-PLY-05` | **Assert `kept`-is-not-a-prefix in a test** — the metadata-alignment hazard is documented, not guarded | §6 row 4 mis-associates silently | S | `GLD-PLY-03` |
| `GLD-PLY-06` | **Fix the `￧` mojibake** in the `__init__.py` crown-jewel rule | It sits in the sentence stating the package's most important invariant | S | — |
| `GLD-PLY-07` | **Add `test_engagement.py`** — 8.8 KB untested | Second-largest untested module here | S | — |
| `GLD-PLY-08` | **Resolve `test_recency.py`'s missing source module** — no `recency.py` exists | Either rename the test or document where recency lives | S | — |
| `GLD-PLY-09` | **Surface the `why` strings in a preview** before publishing | Third package now computing an explanation and discarding it | M | `GLD-WEB-15` |
| `GLD-PLY-10` | **Verify the determinism claim** with a shuffle-input property test | G3 asserts input-order independence; nothing pins it | S | `GLD-ML-05` |
| `GLD-PLY-25` | 🔴 **Delete `provenance.py` and repoint `discovery.py` at `labels/recommendations.py`** | **P-E, and the redundant half is the NEW one.** `labels/recommendations.py` is parquet-backed, month-partitioned, deduped on `(recommended_date, surface, profile, entity_id)`, surface-parameterised and carrying live production rows; `provenance.py` is an in-memory dict with no dedup that nothing has ever written to. The first instinct this session was to fold the ESTABLISHED ledger into the new one — exactly backwards, caught only by reading both. `provenance`'s causality guard already exists in `gems.classify_outcome` ("a play strictly before `recommended_ts` never counts") | S | `GLD-PLY-24` |
| `GLD-PLY-26` | 🔴 **Wire `append_events` into Anniversary and Tonight** | The ledger only grows for Hidden Gems. `GLD-PLY-24` made the identity survive to the point of recording; nothing yet records it, so `discovery.completions` has an empty ledger to join against and the Iron Giant case stays unmeasurable | S | `GLD-PLY-24` |
| `GLD-PLY-27` | 🟡 **Test files for `habits`, `candidates`, `outcomes`, `provenance`, `caps.limit_per_group`** | Five modules shipped untested in one session. `pick_one_per_group` first — it carries cross-level de-duplication, the runtime gate AND the spoiler-order guarantee in one function, so a regression there is silent and unrecoverable | M | — |
| `GLD-PLY-28` | 🟡 **Measure whether a discovery play predicts better than an ordinary one** | `discovery.DEFAULT_DISCOVERY_WEIGHT` is 1.0 (no boost) on purpose. The argument for >1.0 — you did not seek it out, so finishing it reveals LATENT taste — is plausible and untested, and the last confidently-reasoned constant here (`DEFAULT_DAY_THRESHOLD = 0.28`) was wrong by 4.5× when finally measured | M | `GLD-PLY-26` |
| `GLD-PLY-29` | 🟡 **Delete `habits.one_per_show`** and point `build_day_list` at `candidates.pick_one_per_group` | Two functions doing one job, and the older one has no cross-level de-duplication — fine while groups were series-only (naturally disjoint), wrong the moment movies arrived and one film could sit in `franchise:`, `genre:` AND `medium:` at once | S | `GLD-PLY-27` |
| `GLD-PLY-30` | 🟡 **Build `because_you_watched`** | The poster family EXISTS — accent, glider formation, copy `"because you watch {{GENRE}}"` — and there is no builder. It is the visible payoff of the discovery ledger: the follow-up list for the thing somebody actually discovered, which today is surfaced once and then forgotten | M | `GLD-PLY-26` |

### 9.0 Recycle-bin awareness — STAGED for the Unraid container

Filed from a live near-miss on 2026-08-08. At 20:28 the pressure pipeline read
**1546.6 GB free** against a 3500 GB floor, declared CRITICAL, and planned an
exhaustive step-down admitting **523 titles**. Overnight the operator dropped the
*arr recycle-bin retention from 7 days to 1; the bin cleaned out and free space
went to **8.25 TB**. The deficit was never a shortage of disk — it was a week of
already-deleted files waiting to age out of a holding area, and the bin was
holding **287% of the entire shortfall**.

Nothing was lost: `deletions_consented` was unset, so the 247 marked rows stayed
marked and `audit.log` shows zero deletions. The consent gate is what saved it.

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SPA-00` | 🔴 **Log `bin_total_gb` beside every pressure decision** | The number that would actually have prevented the 523-title plan. A 24h forecast horizon does NOT prevent it (effective 2347 GB, still under floor) — but "the bin holds 5600 GB" makes "holding area, not full disk" visible AT the decision instead of reconstructable from cache mtimes a day later. Cheapest item here and the highest value | S | — |
| `GLD-SPA-01` | 🟡 **Direct bin scan when running in-container** | Turns every estimate in `bin_forecast` into a measurement. Deliberately a SOURCE swap: `pending_entries` returns `[{ts, expires_at, bytes}]` and a directory walk produces that shape from mtime + st_size, so `reclaim_within` / `bin_total` / `effective_free_gb` are untouched. **Run both in parallel first** — the delta between reconstruction and scan is the only measurement of how wrong the estimator is, and its SIGN says whether the error was dangerous (optimistic → deferred reclaims it should not have) or harmless | M | container |
| `GLD-SPA-02` | 🔴 **The VERIFIER — did the replacement actually land?** | `bin_forecast` currently counts every deleted file as reclaimable. True for disk, NOT necessarily true for safety: if an upgrade failed, the bin copy is the ONLY copy. Classify each binned entry redundant-vs-last-copy from `has_file` + a changed `movie_file_id` + the `downloadFolderImported` ↔ `movieFileDeleted` pairing. **Buildable today, needs no container, and nothing else in this section is safe without it** | M | — |
| `GLD-SPA-03` | 🟡 **Per-item bin purge** | No *arr API exists — the bin is a directory the *arr does not index, so this is an unlink and only reachable in-container. Must be gated behind BOTH the verifier and `deletions_consented` | S | `GLD-SPA-01`, `GLD-SPA-02` |
| `GLD-SPA-04` | ⚪ **Early reclaim via `CleanUpRecycleBin`** | Least valuable, most likely to be regretted. The task ENFORCES retention rather than bypassing it, so triggering it early on a 7-day bin holding 2-day-old files frees nothing. Pulling space forward means lowering `recycleBinCleanupDays` → run → restore, which drops the safety net for EVERY binned file to reclaim one | S | `GLD-SPA-02` |

**Naming.** These modules are `bin_forecast` / `bin_verify`, never "recycle" —
`_recycle_to_fund_acquisition` already owns that word for deleting OWNED episodes
to fund grabs, which is the opposite direction and unrelated. `diag_recycle.py`
at the repo root belongs to THAT feature, not this one.

**The asymmetry is normative and survives all of the above.** Pending reclaim may
only make the system LESS aggressive — it may defer a deletion or a step-down; it
must NEVER authorise an acquisition or raise a quality target. The failure modes
are not symmetric: being wrong while deferring costs one run and the deletion
happens next pass, while being wrong while acquiring fills a disk that had no
room, which is the exact condition the floor exists to prevent. Even with a
perfect directory listing, reclaimable space is space that is not free YET.

### 9.1 Operator actions carried out of this session

Not code, and they will not resolve themselves:

| Action | Why it cannot self-heal |
|---|---|
| 🔴 **Rotate the Plex token and the Radarr API key** | Both were pasted into a transcript. `X-Plex-Token` grants full server access |
| 🔴 **Delete the seven orphaned old-name shelves** by hand (`Anniversary Picks — Movies`, `Fresh Arrivals — TV Shows-Series`, …) | `ensure_smart_playlist` only knows CURRENT names, so nothing re-creates or cleans them. They keep their own `!!1`/`!!2` sort keys and compete with the renamed set for the top of the listing — which is what "the sort looks alphabetical again" actually was |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `test_ordering.py` use `is_spoiler_safe`, or is the invariant untested? | `GLD-PLY-01` |
| Q2 | Is the invariant re-checked before the Plex write, or only available to be? | `GLD-PLY-02` |
| Q3 | Is expansion `cap=25` right? It bounds how much of a show a viewer sees queued at once. | — |
| Q4 | Where does recency logic live, given `test_recency.py` has no source twin? | `GLD-PLY-08` |

**Q1 and Q2 are the same worry at two levels.** The package has done the hard part
— it identified the unrecoverable failure, made the sort structurally immune, and
wrote a verification function. What is unconfirmed is whether either the test
suite or the write path actually exercises it. Both are small reads.

## 11. Related designs

- [`services/plex/DESIGN_personal_playlists.md`](../../services/plex/DESIGN_personal_playlists.md) — the service half
- [`support/knowledge/personalized-playlists.md`](../../../support/knowledge/personalized-playlists.md)
- [`scoring/DESIGN.md`](../scoring/DESIGN.md) §3.5 — the determinism contrast
- [`discovery/DESIGN.md`](../discovery/DESIGN.md) §3.2 — the other package returning an observable dropped count
- [`affinity/DESIGN.md`](../affinity/DESIGN.md) §3.2 — the byte-identical opt-in pattern §3.6 extends
