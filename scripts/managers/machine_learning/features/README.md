# features

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **features**

**Package** — `scripts.managers.machine_learning.features`
**Run position** — Called by Radarr/Sonarr scoring paths during phase 2.
**One-liner** — The adapter between service Parquet caches and the scorers: the one place a cache column name is known — for movies and shows. Episodes and the watched-set are still declared stubs.

---

## Purpose

From [`__init__.py`](./__init__.py):

> The **single adapter** between service Parquet caches and the scorers/planners.
> **Knows column names so nothing downstream has to.**

That is the load-bearing claim of the whole brain layer. `contracts/` defines the
shapes; this package is where a `movie_files` Parquet row *becomes* one. After
it, no scorer knows that Radarr nests codec at `movieFile.mediaInfo.videoCodec` or
that `percent_complete` arrives as 0–100 rather than 0–1.

**The claim currently holds for three of five modules.** See
[`DESIGN.md`](./DESIGN.md) §3.2.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`movie_features.py`](./movie_features.py) | `build_movie_feature_row` + `score_movie_features` — *"the ONE place the cache schema is known"* | ✅ Implemented |
| [`show_features.py`](./show_features.py) | The series twin | ✅ Implemented |
| [`completion_stats.py`](./completion_stats.py) | `series_completion_stats`, `episode_completion_stats` — relocated from the Tautulli series/episodes managers | ✅ Implemented |
| [`episode_features.py`](./episode_features.py) | `build_episode_feature_row` — **declared, not implemented** | 🔵 Planned |
| [`watched_set.py`](./watched_set.py) | `build_watched_set` — **declared, not implemented** | 🔵 Planned |

## Test coverage

| Test | Covers |
|---|---|
| [`test_movie_features.py`](./test_movie_features.py) | Row marshalling |
| [`test_device_fit.py`](./test_device_fit.py) | Group D inputs |
| [`test_c4_threading.py`](./test_c4_threading.py) | C4 person-matrix threading |

---

## What the movie adapter does

```python
build_movie_feature_row(row, *, credits=None, related_tmdb_ids=None,
                        user_rating=None) -> MovieFeatureRow
score_movie_features(fr, *, <library context>, return_breakdown=False) -> int | (int, dict)
```

Three inputs arrive as **kwargs rather than column reads**, because they do not
live in `movie_files`:

| Kwarg | Actually lives in |
|---|---|
| `credits` | Trakt people buckets |
| `related_tmdb_ids` | Daemon-cached C3 neighbours |
| `user_rating` | `trakt/{user}/ratings/movies` |

The `user_rating` docstring states the reason explicitly — *"the same reason the
show side passes it in from `_build_user_show_rating_map` instead of reading the
parquet."*

Pure throughout: *"the service does the cache reads (credits, related set,
affinity maps) and passes them in."*

---

## Where partial rows are made safe

[`contracts/DESIGN.md`](../contracts/DESIGN.md) §3.3 says every optional field
defaults so a partially-enriched row never raises. **This is where that is
implemented** — four `pd.notna`-guarded coercers:

| Helper | Absent ⇒ |
|---|---|
| `_f` | `None` |
| `_s` | `None` |
| `_b` | `False` |
| `_i` | `None` (also on `TypeError`/`ValueError`) |

Plus two normalisations the scorers then rely on:

- `percent_complete` → **0–1 fraction** (`/100.0`)
- `genres` → JSON-parsed to a tuple

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Produces:** [`contracts/`](../contracts/README.md) feature rows
- **Feeds:** [`scoring/`](../scoring/README.md) · [`likelihood/`](../likelihood/README.md) · [`space/`](../space/README.md)
- **Contract:** [`ARCHITECTURE.md`](../ARCHITECTURE.md)
