# mdblist

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **mdblist**

**Package** — `scripts.managers.services.mdblist`
**Run position** — Populated by the enrichment daemon and the one-shot `enrich_csm_ages` tool; read by the movie classifier and `router_movie`.
**One-liner** — FETCH/CACHE adapter for MDBList, currently supplying Common Sense Media age ratings to the kids/adult classification path.

---

## Purpose

From [`__init__.py`](./__init__.py):

> MDBList (mdblist.com) aggregates ratings (IMDb / TMDb / Trakt / Letterboxd / RT
> / Metacritic / MAL) and hosts curated/dynamic lists in ONE API. This package is
> the FETCH/CACHE adapter for it.

**Opt-in**: *"with no `mdblist.apikey` configured nothing runs and the rest of the
system is byte-identical."*

⚠️ That docstring also says *"First slice: AUTH + account-TIER validation only…
rating-enrichment build[s] on this foundation later."* — which
[`age_cache.py`](./age_cache.py) has since overtaken. See
[`DESIGN.md`](./DESIGN.md) §3.5.

---

## Script inventory

| Script | Size | Role | Tests |
|---|---|---|---|
| [`client.py`](./client.py) | 12.9 KB | API client — `validate_key`, `movie_ratings`, `show_ratings` | ✅ 9.2 KB |
| [`age_cache.py`](./age_cache.py) | 4.8 KB | CSM age cache — read, write, batch-fetch, budget | ❌ **none** |
| [`__init__.py`](./__init__.py) | 0.5 KB | Package docstring | — |

---

## The age cache

```
support/cache/mdblist/age_ratings.json     { "<tmdbId>": <age int> | null }
support/cache/mdblist/age_ratings_tv.json  same, show-space tmdbIds
```

**Two files, deliberately:**

> movie and show tmdbIds share the same integer space, so they **must never share
> a `{tmdbId: age}` dict**.

**Three states, two of which read alike:**

| Stored | Meaning | `age_for` returns |
|---|---|---|
| `int` | Real CSM age | the int |
| `null` | Looked up — Common Sense has no rating | `None` |
| key absent | Not looked up yet | `None` |

> A value of `null` means "MDBList looked it up and Common Sense has no rating" —
> cached so it's not re-queried; **the cache itself is therefore the resume
> state**. **Transient failures are NOT cached, so they retry on the next pass.**

The collapse of `null` and *absent* in `age_for` is deliberate — it matches
`router_movie._csm_age`'s `isinstance(v, int)` contract, *"so a null/missing entry
lets the classifier fall back to its genre/cert/studio heuristics."*

---

## Budget handling

| Constant | Value | Meaning |
|---|---|---|
| `BUDGET_FLOOR` | 500 | *"leave this many MDBList daily requests in reserve"* |
| `budget()` | `(used, limit)` | `(0, 25000)` on failure |

`fetch_into(apikey, tmdb_ids, cache, *, max_calls, throttle=0.08, stop=None, lookup=movie_ratings)`
walks uncached ids, skipping transient failures and sleeping 30 s on a 429.

---

## Navigation

- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Readers:** [`machine_learning/classification/`](../../machine_learning/classification/README.md) · `support/tools/router_movie.py`
- **Writers:** the enrichment daemon · `support/tools/enrich_csm_ages.py`
