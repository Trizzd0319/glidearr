# mdblist — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **mdblist**

**Package** — `scripts.managers.services.mdblist`
**Status** — ✅ Implemented · 🟢 Strong cache discipline · 🔴 A 429 loop with no failure budget · 🟡 Stale self-description
**Related** — [README.md](./README.md) · [`machine_learning/classification/DESIGN.md`](../../machine_learning/classification/DESIGN.md)

---

## 1. Problem statement

The kids/adult split is one of the few classifications with a *social* cost when
wrong — a title in the wrong bucket is visible to the household immediately, and
in one direction it is worse than visible.

Genre, certification and studio heuristics carry it today, but certification is
regional, inconsistent, and absent on a long tail. **Common Sense Media's
recommended age** is a better signal, and MDBList is the one API that exposes it
alongside six rating sources.

Three constraints shape the adapter:

1. **A hard daily budget.** 25,000 requests, shared with every other MDBList use.
   Re-querying titles already known is the fastest way to exhaust it.
2. **"No CSM rating" is a real answer.** Many titles genuinely have none. If that
   is not cached, every run re-asks the same question and gets the same nothing.
3. **tmdb ids are namespaced by media type.** Movie 550 and show 550 are
   unrelated. A single `{tmdbId: age}` dict silently merges them.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | A looked-up-and-absent answer is cached; a transient failure is not. |
| G2 | The cache is the resume state — no separate progress file. |
| G3 | Movie and show ids can never collide. |
| G4 | A partial write can never corrupt the cache. |
| G5 | Consumers fall back to heuristics without special-casing. |
| G6 | Absent config ⇒ byte-identical system. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Classifying | Supplies a signal; `classification/` decides. |
| N2 | Using the global cache | A standalone JSON the daemon and a CLI tool both populate. |
| N3 | Candidate gathering | Declared as later work — see §3.5. |

---

## 3. Architecture

### 3.1 ✅ Confirmed-absent vs transient — the fourth instance

> A value of `null` means "MDBList looked it up and Common Sense has no rating" —
> **cached so it's not re-queried**… **Transient failures are NOT cached, so they
> retry on the next pass.**

Two outcomes that both produce "no age" are stored differently (G1). This is now
the fourth independent instance of the same discipline:

| Module | Confirmed absent | Transient |
|---|---|---|
| [`plex/`](../plex/DESIGN.md) §3.3 | Discover miss memoised forever | Hop failure stays retryable |
| [`sizing/file_comparison`](../../machine_learning/sizing/DESIGN.md) §3.5 | `expected ≤ 0` ⇒ no opinion | — |
| [`quality_analytics/`](../../machine_learning/quality_analytics/DESIGN.md) §3.5 | `None` on no sample | `none_p` neutral prior |
| **`mdblist/age_cache`** | `null` cached | Skipped, not cached |

Four subsystems, one instinct, none referencing the others. Like **P-H**, this is
a design habit the codebase has rather than a defect — and it is the *inverse* of
§8 **P-C**, which is the same distinction being missed. Worth recording as the
positive form: `GLD-MDB-05`.

### 3.2 🎯 Two files because two id spaces

```python
# TV ages live in a SEPARATE file keyed by show-space tmdbId — movie and show
# tmdbIds share the same integer space, so they must never share a
# {tmdbId: age} dict.
```

A *namespace* collision rather than a string collision, and prevented
structurally — two files, not a prefixed key, so the two can never be mixed even
by a caller that forgets.

Fifth instance of the identity discipline the sweep keeps finding:

| Module | Collision guarded |
|---|---|
| `discovery/occupancy` | Title collisions across remakes |
| `people_matrix` | Name-vs-id, alias drift |
| `plex` | Sanitised display-name collision → uuid map |
| **`mdblist`** | **Movie/show tmdb id-space overlap** |
| `labels/labeling` | ❌ unguarded — no series id available |

### 3.3 🔴 The 429 path has no failure budget

```python
for tmdb in tmdb_ids:
    if looked >= max_calls: break
    ...
    r = lookup(apikey, tmdb)
    if not r["ok"]:
        if r.get("status") == 429:
            time.sleep(30)
        continue          # ← looked is NOT incremented
    ...
    looked += 1
```

`max_calls` counts **successes only**. A failure does not increment `looked`, so
the `break` never fires on failures.

Under a sustained 429 the loop therefore walks **every id in `tmdb_ids`**,
sleeping **30 seconds each**. With a few thousand ids queued that is hours of
wall clock making zero progress — and the enrichment daemon would appear simply
hung.

The `stop` sentinel bounds it *if the caller passes one* (the daemon does). A
caller without `stop` — the one-shot `enrich_csm_ages` tool is the obvious
candidate — has no escape short of a kill.

The fix is small: count consecutive failures and break past a threshold, or count
attempts rather than successes against `max_calls`. `GLD-MDB-01`.

Note the loop is bounded by `len(tmdb_ids)` so this is a hang, not an infinite
loop — which is why it would present as "the daemon stopped doing anything"
rather than as a crash.

### 3.4 🟡 `budget()` fails toward action

```python
except Exception:
    return 0, 25000
```

On any failure the budget reads as **zero used, full limit available** — maximum
headroom. A caller checking `used < limit - BUDGET_FLOOR` proceeds at full rate
against an API it could not reach a moment ago.

That is the opposite of the [D36 rule](../../machine_learning/discovery/DESIGN.md)
— *fail toward the outcome that changes nothing*. The safe failure is
`(limit, limit)`: assume the budget is spent, do nothing this pass, retry next
run.

The blast radius is bounded — over-running produces 429s, which §3.3 handles
(badly, but it handles them) — so this is a rate-limit courtesy failure rather
than a data one. But the two findings **compound**: `budget()` says "go ahead",
the API says 429, and §3.3 then sleeps 30 s per id without counting failures.
`GLD-MDB-02`.

### 3.5 🟡 The package docstring describes a slice it has outgrown

> **First slice: AUTH + account-TIER validation only** (`client.validate_key`) —
> … Candidate-gathering + **rating-enrichment build on this foundation later.**

Rating enrichment is not later; [`age_cache.py`](./age_cache.py) is 4.8 KB of it,
with a live consumer in the classifier and two producers. Only candidate-gathering
remains unbuilt.

Third instance of §8 **P-G** inside the repo's own documentation, after
[`quality_analytics/__init__.py`](../../machine_learning/quality_analytics/DESIGN.md)
§3.2 (*"partly stubs, lowest priority"* on the most-tested package in the brain)
and `ARCHITECTURE.md`'s two wrong rows. All three are **`__init__` or map
docstrings written at inception and never revisited** — which is a useful,
narrow place to look. `GLD-MDB-03`.

### 3.6 A file cache outside the cache manager

```
support/cache/mdblist/age_ratings.json
```

Not a `global_cache` key — a standalone JSON on disk, written atomically
(temp + `os.replace`, *"so a hard kill never leaves a partial file"* — G4, the
same pattern as [`labels/first_run.py`](../../machine_learning/labels/DESIGN.md)'s
marker).

Reasonable, since two different processes populate it and the CLI tool must work
without the manager graph. Two consequences worth recording:

- It is **outside the TTL and invalidation machinery** every `global_cache` key
  gets. A stale entry lives forever, which is correct here (a CSM age does not
  change) but is a property nothing enforces.
- It is **outside the backup scope** — [D48](../backup/DESIGN.md) asks whether
  Glidearr's own caches belong in the rollback point, and this is a concrete
  instance: expensive to rebuild (thousands of budgeted API calls), trivially
  lost.

`_SCRIPTS = Path(__file__).resolve().parents[3]` is depth-dependent path
resolution — the same fragility as `labels/first_run.py`'s `parents[4]`. Moving
the file one directory breaks it silently.

### 3.7 The read contract is deliberately lossy (G5)

`age_for` returns `None` for both *"looked up, no CSM rating"* and *"not looked up
yet"* — matching `router_movie._csm_age`'s `isinstance(v, int)` check so the
classifier falls back to heuristics either way.

Correct for the consumer, and worth noting as the counter-case to §3.1: the
*storage* preserves three states, the *read API* collapses two. That is fine
because the consumer genuinely does not care — but it means **coverage cannot be
measured through `age_for`**. Answering *"how many titles have we actually
resolved?"* requires the raw dict. `GLD-MDB-04`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Cache `null` for confirmed-absent | G1 — otherwise every run re-asks and re-pays | Cache hits only |
| D2 | Never cache transient failures | G1 — a network blip must not become a permanent "no rating" | Cache all outcomes |
| D3 | The cache is the resume state | G2 — no separate progress file to desynchronise | Separate cursor |
| D4 | Separate movie and TV files | G3 — the id spaces overlap; two files make mixing impossible | Prefixed keys |
| D5 | Atomic temp + `os.replace` | G4 — a kill mid-write must not corrupt thousands of budgeted lookups | Direct write |
| D6 | `age_for` collapses null and absent | G5 — the consumer's contract is `isinstance(v, int)` | Three-valued return |
| D7 | Standalone JSON, not `global_cache` | Two producers, one of them a CLI tool without the manager graph | A cache key |
| D8 | `BUDGET_FLOOR = 500` reserve | Leaves headroom for other MDBList uses | Spend to zero |
| D9 | 30 s sleep on 429 | Respects the rate limit | Immediate retry |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | A transient failure never becomes a cached `null`. |
| I2 | Movie and show ages never share a dict. |
| I3 | A partial write never replaces a good cache. |
| I4 | `age_for` returns an `int` only for a real CSM age. |
| I5 | An id already present is never re-queried. |
| I6 | Absent `mdblist.apikey` ⇒ nothing runs. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| No CSM rating exists | `age_rating is None` | Cached `null`; classifier falls back | Correct | ❌ None |
| Transient lookup failure | `r["ok"]` false | Skipped, retried next pass (I1) | Correct | ❌ None |
| Cache file corrupt | `except` in `load` | Returns `{}` — **all progress reads as unfetched** | 🟡 Re-queries everything | ❌ **None** |
| Kill mid-write | Atomic replace | Previous cache intact | Safe | ✅ By construction |
| **Sustained 429** | `status == 429` | **30 s sleep per id, no failure counter** — hours of no progress | 🔴 §3.3 | ❌ **Presents as a hang** |
| **`budget()` unreachable** | `except` | Reports `(0, 25000)` — **full headroom** | 🟡 §3.4, compounds with the above | ❌ **None** |
| Package moved | `parents[3]` | Wrong cache path, silently empty | 🟡 | ❌ **None** |
| Cache lost | — | Thousands of budgeted calls to rebuild | 🟡 Not backed up (D48) | ❌ None |

**Row 3 deserves a note**: a corrupt cache returns `{}`, which is
indistinguishable from a fresh install — so the recovery is to silently re-spend
the entire budget. Correct behaviour, expensive failure, no warning.

---

## 7. Configuration surface

| Key | Effect |
|---|---|
| `mdblist.apikey` | Absent ⇒ nothing runs (G6) |

Constants: `BUDGET_FLOOR` 500 · default limit 25,000 · `throttle` 0.08 s ·
429 sleep 30 s.
Paths: `support/cache/mdblist/age_ratings.json` · `…/age_ratings_tv.json`.

---

## 8. Implemented capabilities

- ✅ Opt-in, byte-identical when unconfigured
- ✅ API key + account-tier validation
- ✅ Movie and show CSM age lookup
- ✅ Confirmed-absent caching with transient-failure retry
- ✅ Cache-as-resume-state batch fetch with a `stop` sentinel
- ✅ Separate id-space files preventing movie/show collision
- ✅ Atomic cache writes
- ✅ Daily-budget query with a reserve floor
- ✅ 9.2 KB of client tests

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-MDB-01` | 🔴 **Bound the 429 path** — `max_calls` counts **successes only**, so a sustained 429 walks every queued id at **30 s each** with no progress. Count attempts, or break after N consecutive failures | §3.3. Presents as the enrichment daemon hanging, not failing. A `stop`-less caller has no escape | S | — |
| `GLD-MDB-02` | 🟡 **Make `budget()` fail toward inaction** — it returns `(0, 25000)` on error, i.e. *full headroom*, then §3.3 absorbs the resulting 429s badly | §3.4; the D36 rule. Safe failure is `(limit, limit)` | S | `GLD-MDB-01`, D36 |
| `GLD-MDB-03` | 🟡 **Update the package docstring** — *"First slice: AUTH… only"* predates 4.8 KB of live rating enrichment with a consumer and two producers | §3.5 *(P-G)*. Third instance in the repo's own docs, all three in `__init__`/map docstrings written at inception | S | `GLD-QAN-03`, `GLD-ROU-06` |
| `GLD-MDB-04` | **Report cache coverage** — resolved / confirmed-absent / unfetched, per media type. `age_for` cannot answer it; the raw dict can | §3.7; the classifier's CSM coverage is currently unknowable | S | `GLD-CLS-07` |
| `GLD-MDB-05` | 🎯 **Record confirmed-absent-vs-transient as a positive pattern** — four independent instances (`plex`, `sizing`, `quality_analytics`, here), none referencing the others. The inverse of P-C | Like P-H, a design instinct worth stating so the fifth subsystem inherits it | S | `GLD-DIS-04` |
| `GLD-MDB-06` | **Warn on a corrupt cache** rather than returning `{}` | §6 row 3: recovery silently re-spends the whole budget | S | `GLD-MDB-04` |
| `GLD-MDB-07` | **Add `test_age_cache.py`** — the resume-state, id-space and atomic-write invariants are untested | I1–I3 are the load-bearing ones and none is pinned | S | — |
| `GLD-MDB-08` | **Include the age caches in the backup scope** — expensive to rebuild, trivially lost | Concrete instance of D48 | S | `GLD-BKP-08` |
| `GLD-MDB-09` | **Replace `parents[3]` with an anchored path** | §3.6; same fragility as `labels/first_run.py`'s `parents[4]` | S | — |
| `GLD-MDB-10` | **Document `client.py`** — 12.9 KB unread this pass | M | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `enrich_csm_ages` pass a `stop` sentinel? If not, §3.3 has no bound at all there. | `GLD-MDB-01` |
| Q2 | What CSM coverage does the library actually have? | `GLD-MDB-04` |
| Q3 | Should `budget()` fail closed? *(= D49)* | `GLD-MDB-02` |
| Q4 | Is candidate-gathering still planned, or has the package settled into an age-rating adapter? | `GLD-MDB-03` |

**Q1 determines how serious §3.3 is.** With a `stop` sentinel the 429 loop is
interruptible and the cost is a wasted daemon cycle. Without one — and the
one-shot tool is the likely case — a rate-limited run occupies a terminal for
hours with no output and no way to tell it apart from a hang.

## 11. Related designs

- [`machine_learning/classification/DESIGN.md`](../../machine_learning/classification/DESIGN.md) — the consumer of `age_for`
- [`plex/DESIGN.md`](../plex/DESIGN.md) §3.3 — the same confirmed-vs-transient discipline
- [`machine_learning/discovery/DESIGN.md`](../../machine_learning/discovery/DESIGN.md) §3.3 — the fail-direction rule §3.4 breaches
- [`backup/DESIGN.md`](../backup/DESIGN.md) — D48, of which §3.6 is a concrete case
- [`machine_learning/labels/DESIGN.md`](../../machine_learning/labels/DESIGN.md) §3.5 — the same atomic-marker and `parents[N]` patterns
