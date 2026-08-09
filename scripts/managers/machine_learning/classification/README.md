# classification

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **classification**

**Package** — `scripts.managers.machine_learning.classification`
**Run position** — Add-time resolution and in-run re-organisation; keep-policy and franchise maps built during the Radarr cache phase.
**One-liner** — What kind of thing is this, where does it belong, and is it protected — library routing, franchise anchors, keep-policy tags, and the whole-file delete guards.

---

## Purpose

From [`__init__.py`](./__init__.py):

> Library routing + franchise/pilot/keep-policy resolution + the whole-file delete
> guard predicates. These are **'protection' decisions, not service I/O**.

Two questions, answered separately and deliberately:

| Question | Owner |
|---|---|
| *What is this?* | [`library_classifier.py`](./library_classifier.py) — kids / anime / documentary / standard |
| *Where does it go, given preferences?* | [`library_router.py`](./library_router.py) — `route_category`, `target_folder` |
| *Is it protected from deletion?* | [`keep_policy.py`](./keep_policy.py), [`franchise.py`](./franchise.py), [`guards.py`](./guards.py) |

> *"The classifier decides **WHAT** a title is; this decides **WHERE** it goes given
> the prefs."*

---

## Script inventory

| Script | Size | Role | Status |
|---|---|---|---|
| [`library_classifier.py`](./library_classifier.py) | 42.4 KB | Category classification — the largest module in the brain | ✅ Implemented |
| [`guards.py`](./guards.py) | 10.7 KB | Whole-file delete guard predicates | ✅ Implemented |
| [`library_router.py`](./library_router.py) | 5.9 KB | `route_category`, `target_folder`, `plan_moves` | ✅ Implemented |
| [`keep_policy.py`](./keep_policy.py) | 5.8 KB | `build_keep_policy_map`, `resolve_keep_policy`, `series_keep_policy` | ✅ Implemented |
| [`franchise.py`](./franchise.py) | 4.0 KB | `resolve_franchise_entries`, `build_franchise_file_ids` | ✅ Implemented |

## Test coverage

[`test_library_classifier.py`](./test_library_classifier.py) (22.6 KB) ·
[`test_library_router.py`](./test_library_router.py) (7.3 KB) — the heaviest
classifier coverage in the repo.

---

## The keep-tag hierarchy

Verified against [`keep_policy.py`](./keep_policy.py). Priority, highest first:

| Tag label | Policy | Meaning |
|---|---|---|
| `keep` · `keep-forever` | `keep_forever` | Never touched |
| `keep-movie` | `keep_movie` | Never touched |
| `keep-universe` · `keep-universe-<name>` | `keep_universe` | **Never deleted — quality-change only** |
| bare `universe` | `universe` | **Deletable as an absolute last resort** |
| *(none)* | `None` | Normal lifecycle |

A non-`None` result is *"an explicit user override (never unmonitor/delete)."*

**Universe-name resolution**, in order: the suffix of `keep-universe-<name>` →
matching `FRANCHISE_HINTS` on the same movie → `"universe"`. Multiple hints
pipe-join (`"marvel|spiderman"`).

Series use a separate resolver: `keep_series` wins over `keep_season`.

---

## Routing preferences

`route_category` redirects **only when a bucket is turned off**:

| Media | Category | Redirects to | When |
|---|---|---|---|
| movie | kids | standard | `routing.movies.kids_bucket_enabled` off |
| movie | anime | standard | `routing.movies.anime_policy == "standard_only"` |
| show | anime | series | `routing.tv.anime_policy == "series_type"` |
| show | kids | series | `routing.tv.kids_bucket_enabled` off |

> seriesType is tracked separately, so a `series_type` anime **still parses as
> anime** — it just lands in the series folder rather than a dedicated one.

`plan_moves` emits a `MovePlan` when an item's current root differs from its
configured destination. **Same-instance moves only** — cross-instance migration
(anime / 4K instance) is *"a separate, deferred concern"*, which is
[`GLD-RAD-01`](../../services/radarr/DESIGN.md).

---

## Franchise protection

`resolve_franchise_entries` — the **earliest-year** movie in each collection.
Movies in no collection are never entries.

`build_franchise_file_ids` protects two categories:

1. **Real franchise entries** — `is_franchise_entry` and a non-null `movie_file_id`
2. **De-facto franchise** — for collections with no resolved entry, the
   earliest-year **watched** movie's file id

The second is a genuine safety net: every collection ends up with an anchor even
when entry resolution fails.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Consumers:** [`space/`](../space/README.md) delete guards · [`services/radarr/`](../../services/radarr/README.md) cache + resolver
- **Shim:** [`support/utilities/library_classifier.py`](../../../support/utilities/library_classifier.py) — re-export, Step 5a
