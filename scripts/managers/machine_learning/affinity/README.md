# affinity

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **affinity**

**Package** — `scripts.managers.machine_learning.affinity`
**Run position** — Called during the Tautulli phase; results cached and read by every scorer thereafter.
**One-liner** — Taste from behaviour: ranked genre/cast/crew/studio maps, household and per-user, plus group movie-completion and device usage — all pure derivations over pre-fetched history.

---

## Purpose

From [`__init__.py`](./__init__.py):

> Pure derivations (no Tautulli HTTP): genre/cast/crew/studio affinity, per-user +
> household, group movie-completion, platform & transcode usage.

This is where *"what does this household like?"* is computed. Scoring Group A
(household intent) and Group B (people affinity) both read its output, and
Group D (device fit) reads its platform tallies.

The Tautulli managers keep the FETCH and the cache-write of the same keys
(`tautulli/affinity`, `tautulli/users/{user}/affinity`); the computation moved
here at ML Step 3a/3b.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`genre_affinity.py`](./genre_affinity.py) | `aggregate_affinity`, `per_user_affinity`, `build_library_index`, `merge_library_first` — **incl. optional temporal decay** | ✅ Implemented |
| [`group_completion.py`](./group_completion.py) | `group_movie_completions` — per-group max completion with grace members | ✅ Implemented |
| [`platform_usage.py`](./platform_usage.py) | `platform_usage`, `per_user_platform_usage` | ✅ Implemented |

## Test coverage

[`test_genre_affinity.py`](./test_genre_affinity.py) ·
[`test_aggregate_person_affinity.py`](./test_aggregate_person_affinity.py) ·
[`test_platform_usage.py`](./test_platform_usage.py)

---

## Temporal decay — built, and off by default

```python
weight = exp(-age_days / half_life_days)
```

> DEFAULT (`half_life_days` None/0) — each watch counts **1 (int)**, so the maps
> are **byte-identical** to the legacy raw counts.

A film watched five years ago currently counts exactly as much as one watched
last week. The mechanism to change that is present, tested, and awaiting a
half-life value. See [`DESIGN.md`](./DESIGN.md) §3.2.

A missing or unparseable date returns `1.0` — *"neutral, no decay rather than
dropping the watch."*

---

## What `aggregate_affinity` returns

```
{genres, actors, directors, composers, producers, studios, format_metrics}
```

each a `{name: weight}` map sorted descending. `per_user_affinity` returns
`{username: aggregate_affinity(...)}`, **omitting users with zero matching
entries**.

---

## The library-genre backstop

`build_library_index` resolves genres from the **owned library** (Radarr movies /
Sonarr series) via a stable title join, rather than the churn-prone Tautulli
`rating_key`:

> This fills the coverage holes the per-key Tautulli metadata fetch leaves: a
> low-volume, movie-only profile whose handful of rating_keys never made the
> sampled index would otherwise score **affinity = 0 and collapse to the flat
> household ranking**.

`merge_library_first` then gives library genres precedence; Tautulli supplies
people/studios and the not-owned fallback.

Title normalisation keeps only `[a-z0-9]`, with a trailing-year fallback — so
`Bluey (2018)` matches a `Bluey` entry and vice versa.

---

## Group completion, with grace

`group_movie_completions` returns, per rating group, the **max** completion any
member reached per `rating_key`:

```
{group: {rating_key: {"pct": 0.0–1.0, "threshold": float}}}
```

| Concept | Default | Meaning |
|---|---|---|
| `completion_threshold` | `0.9` | The bar for regular members |
| `grace_threshold` | `0.7` | A gentler bar for `grace_members` |
| memberless group | — | **Household-wide wildcard** — every user counts toward it |

Tie-break: same `pct`, **more lenient threshold wins**.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Consumers:** [`scoring/`](../scoring/README.md) Groups A/B/D · [`features/`](../features/README.md)
- **Sibling:** transcode usage lives in [`quality_analytics/transcode.py`](../quality_analytics/) — *"transcode is a quality/playback concern"*
- **Source:** [`services/tautulli/`](../../services/tautulli/README.md) keeps FETCH + cache-write
