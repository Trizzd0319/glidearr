# classification — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **classification**

**Package** — `scripts.managers.machine_learning.classification`
**Status** — ✅ Implemented · 🔴 Traces the `movieRootFolders` defect to its line · 🟡 Two small internal inconsistencies
**Related** — [README.md](./README.md) · [`services/radarr/DESIGN.md`](../../services/radarr/DESIGN.md) · [`space/DESIGN.md`](../space/DESIGN.md)

---

## 1. Problem statement

Two questions that look like one and are not:

- **What is this title?** Kids, anime, documentary, standard. A property of the
  title.
- **Where should it live?** A function of what it is *and* what the operator has
  turned on. A household with `kids_bucket_enabled` off still owns kids films —
  they just do not get their own folder.

Conflating them means the classifier has to know about preferences, and every
preference change becomes a reclassification.

A third, separate concern rides alongside: **protection**. Some titles must never
be deleted regardless of score — explicitly tagged keeps, and franchise anchors
where deleting the first film of a collection makes the rest incoherent. That is
not a scoring question; no watchability number should be able to override it.

This package keeps all three apart: classify, then route, then protect.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Classification is independent of preferences. |
| G2 | Add-time and re-organisation make **identical** decisions. |
| G3 | Protection is a hard override, never a score input. |
| G4 | Every collection has a protected anchor. |
| G5 | Preferences redirect only when a bucket is switched **off**. |
| G6 | Pure — no HTTP, no config object, no logging. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Cross-instance migration | *"a separate, deferred concern"* — [`GLD-RAD-01`](../../services/radarr/DESIGN.md) |
| N2 | Fetching tags or collections | Service FETCHes; this maps. |
| N3 | Applying moves | Emits `MovePlan`; the adapter executes. |
| N4 | Deciding *whether* to apply routing | *"The caller decides… e.g. only once `routing.configured` is stamped."* |

---

## 3. Architecture

### 3.1 Classify → route → target

```
item ──► classify(item)          → category   (kids | anime | documentary | standard)
              │                                 pure function of the title
              ▼
         route_category(category, is_show, routing)
              │                                 → eff_category
              │   redirects ONLY when a bucket is OFF (G5)
              ▼
         target_folder(eff_category, is_show, root_folders, movie_root_folders)
              │                                 → destination path
              ▼
         plan_moves → MovePlan when current_root ≠ target_root
```

`plan_moves` is shared by *"the add-time resolver and the in-run re-organizer, so
both make IDENTICAL decisions"* (G2). The `classify` callable is **injected**,
which is what keeps this module pure while still using the 42 KB classifier.

### 3.2 🔴 `target_folder` is where `movieRootFolders` dies

```python
def target_folder(eff_category, is_show, root_folders, movie_root_folders) -> str:
    if is_show:
        rf = root_folders or {}
        return rf.get(eff_category) or rf.get("series") or ""
    mrf = movie_root_folders or {}
    return mrf.get(eff_category) or mrf.get("standard") or ""
```

With `movieRootFolders = {}` — the documented live config state
([`config/DESIGN.md`](../../factories/config/DESIGN.md) §9, `GLD-CFG-03`) —
both lookups miss and the function returns **`""`**.

So the chain is intact right up to the last step: `classify` works,
`route_category` works, and then `target_folder` has **nothing to resolve
against**. Classification is computed and discarded exactly here.

Two consequences worth separating:

- The **classification itself** is fine. Nothing is miscategorised; there is
  simply no folder map.
- The return is `""`, not `None`. A caller testing `if target:` skips safely; a
  caller comparing paths sees `current_root != ""` and could emit a `MovePlan` to
  an empty destination. Which behaviour occurs depends on `plan_moves`' guards,
  which I have not read in full.

This gives `GLD-CFG-03` / `GLD-RAD-05` a precise line rather than a symptom.

### 3.3 ✅ A schema change that silently disabled protection

From [`franchise.py`](./franchise.py):

> Radarr v4/v5 payloads call the field `'title'`, v3 `'name'` — read both.
> **(v4+ has NO `'name'` key, which left `is_franchise_entry` all-False and
> category-1 franchise protection inert on modern instances.)**

An upstream field rename turned franchise protection **completely off** on every
modern Radarr, with no error — `coll.get("name")` returned `None`, no collection
was registered, no movie was an entry, and the anchor of every collection became
deletable.

This is §8 **P-C** and **P-D** simultaneously: absent read as empty, and a
protection failure with no detector. It is now fixed and documented, and it
belongs beside `watched_definition` as a worked example.

### 3.4 🟡 The same shape, still unguarded

`build_franchise_file_ids` opens with:

```python
if "is_franchise_entry" not in df.columns or "movie_file_id" not in df.columns:
    return frozenset()
```

An empty frozenset means **nothing is franchise-protected**. A missing column —
a schema rename, a partial Parquet, a cache written by an older version — silently
disables the same protection §3.3 just finished restoring, by a different route.

The `is_watched` / `collection_name` guard has the same shape, returning whatever
category-1 protection it managed before giving up on category 2.

Absent → unprotected is the **dangerous** direction for a guard. Elsewhere in the
brain the equivalent choice goes the other way — `lifecycle/watched_definition`
fails **open** precisely *"because a guard must never shrink on missing data."*
`GLD-CLS-02`.

### 3.5 The de-facto franchise fallback

For collections with no resolved entry, the **earliest-year watched** movie's
file id is protected instead. That is a thoughtful backstop (G4): even when entry
resolution fails, a collection retains an anchor — and it chooses a *watched*
title, so the protected file is one the household has actually engaged with.

Note it would not have saved §3.3's bug: with no collection registered at all,
`real_fe_collections` is empty and `candidates` are grouped by `collection_name`
from the DataFrame — a different source than the API payload — so the fallback
may or may not have fired. Worth confirming; it determines whether the v4/v5 bug
left *some* protection standing or none.

### 3.6 🟡 Constants that claim to be shared and are not

[`keep_policy.py`](./keep_policy.py) defines:

```python
# Shared by build_keep_policy_map (list form) and resolve_keep_policy (single-movie form).
KEEP_FOREVER_LABELS = frozenset({"keep", "keep-forever"})
KEEP_MOVIE_LABELS = frozenset({"keep-movie"})
```

`resolve_keep_policy` uses them. `build_keep_policy_map` **redefines them
locally**:

```python
keep_forever_labels = {"keep", "keep-forever"}
keep_movie_labels   = {"keep-movie"}
```

The values agree today, so there is no live defect. But the comment asserts a
sharing that does not exist, and adding a label to the module constant would
reach only one of the two resolvers — silently, for the tag system that decides
what is *never deleted*. `GLD-CLS-03`.

The module docstring also names two further resolvers still in the services —
`radarr/repair/anomaly._resolve_keep_policy` and
`sonarr/cache/episode_files._resolve_keep_policy_map` — *"candidates for a later
micro-slice."* So keep-policy resolution exists in **four** places. Declared P-E.

### 3.7 🟡 `FRANCHISE_HINTS` still carries the token it deprecates

```python
# Use "startrek"/"starwars" rather than the ambiguous legacy "star".
FRANCHISE_HINTS = {
    "mcu", "xmen", "dc", "star", "startrek", "starwars", ...
}
```

`"star"` is still in the set. Two effects:

- A movie tagged `universe` + `star` groups under `"star"` — ambiguous between
  the two franchises the comment names.
- Because the resolver takes `sorted(labels & FRANCHISE_HINTS)` and pipe-joins,
  a movie tagged both `star` and `starwars` gets the compound universe name
  `"star|starwars"` — a third bucket distinct from either.

Pipe-joining is intentional for genuinely cross-franchise titles. The ambiguity
comes from leaving the deprecated token active. `GLD-CLS-04`.

### 3.8 Coverage note

[`guards.py`](./guards.py) (10.7 KB) and
[`library_classifier.py`](./library_classifier.py) (42.4 KB) were **not read** for
this pass. The classifier is the largest module in the brain and the guards hold
the whole-file delete predicates — both warrant their own read. Nothing in this
document asserts their behaviour.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Classify and route separately | G1 — a preference change must not reclassify the library | One combined step |
| D2 | `classify` injected into `plan_moves` | Keeps the router pure while using the 42 KB classifier | Import it |
| D3 | One `plan_moves` for add-time and re-org | G2 — two implementations would drift on exactly the decisions that move files | Separate paths |
| D4 | Redirect only when a bucket is off | G5 — an enabled bucket always wins | Preference-driven reclassification |
| D5 | seriesType tracked separately from folder | An anime routed to `series` still parses as anime | Folder implies type |
| D6 | Keep-tags are a hard override | G3 — *"an explicit user override (never unmonitor/delete)"* | Score-weighted protection |
| D7 | `keep-universe` vs bare `universe` split | Two genuinely different intents: never-delete vs last-resort | One universe tag |
| D8 | Franchise entry = earliest year | The anchor of a collection is its first film | Highest-scored |
| D9 | De-facto fallback on the earliest **watched** | G4 — a collection keeps an anchor even when entry resolution fails | No fallback |
| D10 | Read both `collection.title` and `.name` | Radarr v3 and v4/v5 disagree (§3.3) | Single field |
| D11 | Same-instance moves only | N1 — cross-instance is a distinct problem with its own migration semantics | Handle both here |
| D12 | Franchise hints, not per-franchise tags | *"the LESS AWKWARD alternative to a per-franchise `keep-universe-<name>` tag"* | Explicit tag per franchise |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | `keep_universe` is never deleted — quality change only. |
| I2 | Bare `universe` is deletable only as an absolute last resort. |
| I3 | A non-`None` keep policy overrides every score-based decision. |
| I4 | Classification does not depend on routing preferences. |
| I5 | Add-time and re-organisation produce identical routing decisions. |
| I6 | Every collection with a watched member has a protected anchor. |
| I7 | Redirects occur only when the target bucket is disabled. |
| I8 | This package performs no I/O. |

**I1 and I2 are the project-wide invariants I have been citing since session 1**
— now verified against the code that implements them.

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| **`movieRootFolders` empty** | **None** | `target_folder` → `""`; classification discarded | 🔴 All movies to one bucket | ❌ **None** |
| **Required column missing from the DataFrame** | Early return | `frozenset()` — **nothing franchise-protected** | 🔴 Protection silently off | ❌ **None** |
| Radarr changes the collection field again | **None** | Same shape as §3.3 — entries all-False | 🔴 | ❌ **None** |
| Collection has no year on any member | Fallback to unfiltered `min` | Arbitrary member becomes the entry | 🟡 Wrong anchor | ❌ **None** |
| Movie tagged `universe` + `star` | — | Ambiguous universe bucket | 🟡 §3.7 | ❌ **None** |
| Label added to `KEEP_*_LABELS` only | **None** | Reaches `resolve_keep_policy`, not `build_keep_policy_map` | 🟡 §3.6 | ❌ **None** |
| Four keep-policy resolvers diverge | **None** | Different answers per call site | 🟡 Declared P-E | ❌ **None** |
| Routing applied before `routing.configured` | Caller's guard | Caller decides | Handled | ✅ By contract |

**Rows 1–3 share one shape and it is the worst in this package**: a protection or
placement decision that fails to *absent*, silently. §3.3 proves the shape has
already caused a real outage of franchise protection; §3.4 shows the same door is
still open on a different hinge.

---

## 7. Configuration surface

| Key | Effect |
|---|---|
| `movieRootFolders` | Movie category → folder map. 🔴 **Empty today** (§3.2) |
| `rootFolders` | Show category → folder map |
| `routing.movies.kids_bucket_enabled` | Default `True` |
| `routing.movies.anime_policy` | `"standard_only"` redirects anime → standard |
| `routing.tv.kids_bucket_enabled` | Default `True` |
| `routing.tv.anime_policy` | `"series_type"` redirects anime → series |
| `routing.configured` | Caller's gate on applying preferences |
| Tag labels | `keep`, `keep-forever`, `keep-movie`, `keep-universe[-<name>]`, `universe`, `keep_series`, `keep_season` |

`FRANCHISE_HINTS` is a code constant, not config — ~40 tokens.

---

## 8. Implemented capabilities

- ✅ Category classification (largest module in the brain, 22.6 KB of tests)
- ✅ Preference-aware routing that redirects only on disabled buckets
- ✅ Shared `plan_moves` for add-time and re-organisation
- ✅ seriesType correction independent of folder placement
- ✅ Four-level keep-policy hierarchy with explicit override semantics
- ✅ Universe naming from tag suffix, franchise hints, or default
- ✅ Compound universe names for cross-franchise titles
- ✅ Series keep-policy with `keep_series` > `keep_season`
- ✅ Franchise entry resolution across Radarr v3 and v4/v5 payload shapes
- ✅ De-facto franchise anchor for collections without a resolved entry
- ✅ Whole-file delete guard predicates

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-CLS-01` | 🔴 **Warn when `target_folder` returns `""`** — the precise point where classification is discarded (§3.2) | Turns `GLD-CFG-03`'s silent symptom into a signal at source | S | `GLD-CFG-03`, `GLD-RAD-05` |
| `GLD-CLS-02` | 🔴 **Fail loudly on missing columns** in `build_franchise_file_ids` rather than returning an empty frozenset | §3.4 — absent currently means *unprotected*, the opposite of `watched_definition`'s fail-open discipline | S | — |
| `GLD-CLS-03` | 🟡 **Make `build_keep_policy_map` use the module constants** it is documented as sharing | §3.6 — the tag system deciding what is never deleted has two label sets | S | — |
| `GLD-CLS-04` | 🟡 **Remove or alias the deprecated `"star"` hint** | §3.7 — the comment deprecates it; the token is still live and produces ambiguous and compound buckets | S | — |
| `GLD-CLS-05` | **Consolidate the four keep-policy resolvers** — this module's two plus `radarr/repair/anomaly` and `sonarr/cache/episode_files` *(declared P-E)* | Four answers to "is this protected?" | M | `GLD-ML-15` |
| `GLD-CLS-06` | **Assert the collection-field read** against a live Radarr payload each run | §3.3 recurs the moment the API changes again; nothing detects it | S | `GLD-CLS-02` |
| `GLD-CLS-07` | **Report franchise-protection coverage** — collections with a real entry, with a de-facto anchor, and with none | Would have made §3.3 visible immediately | S | `GLD-SPA-05` |
| `GLD-CLS-08` | **Read and document `guards.py` and `library_classifier.py`** | §3.8 — 53 KB of delete predicates and classification logic undocumented here | M | — |
| `GLD-CLS-09` | **Confirm whether the de-facto fallback fired during the v4/v5 outage** | §3.5 — determines whether that bug left partial protection or none | S | `GLD-CLS-06` |
| `GLD-CLS-10` | **Surface the resolved universe name** per title in a report | Compound names like `"star|starwars"` are invisible until something behaves oddly | S | `GLD-WEB-04` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `plan_moves` guard against a `""` target, or would it emit a move to an empty destination? | `GLD-CLS-01` |
| Q2 | Should franchise protection fail **open** (protect everything) when its inputs are missing, matching `watched_definition`? | `GLD-CLS-02` |
| Q3 | Is the de-facto anchor the right fallback, or should an unresolvable collection protect *all* its members? | `GLD-CLS-09` |
| Q4 | Should `FRANCHISE_HINTS` be config rather than a code constant, given it is household-specific? | `GLD-CLS-04` |

**Q2 is the substantive one.** This package and `lifecycle/` make opposite
choices on identical inputs: `watched_definition` fails **open** because *"a
guard must never shrink on missing data"*; `build_franchise_file_ids` fails
**closed**, removing all protection. One of those is wrong, and §3.3 shows which
direction actually caused an outage.

## 11. Related designs

- [`services/radarr/DESIGN.md`](../../services/radarr/DESIGN.md) §3.3 — cross-instance routing, the deferred half
- [`factories/config/DESIGN.md`](../../factories/config/DESIGN.md) §9 — `GLD-CFG-03`, whose mechanism §3.2 locates
- [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md) §3.3 — the opposite fail-direction choice
- [`space/DESIGN.md`](../space/DESIGN.md) — consumer of the keep and franchise guards
- [`support/utilities/library_classifier.py`](../../../support/utilities/library_classifier.py) — the Step 5a shim and its dual-import constraint
