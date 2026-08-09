# services/trakt — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **trakt**

**Manager** — `TraktManager` · **Entry** — `run()` · **Subpackages** — 13 (+1 flat module)
**Status** — 🟡 Root read. `run()` reaches **5 of 13**; the rest are infrastructure, facades, or unreached.

> **Coverage:** `__init__.py` in full (95 lines). The 15 subpackages are unread except
> `history/` (head) and `movies/scorer.py` (a shim). This document maps the package;
> it does not yet describe it.

---

## 1. What `run()` actually does

Eight calls, and they are the whole of the Trakt sync:

```python
history.get_full_watch_history()            # episodes  ⚠️ see §3
history.get_full_movie_history_cached()     # movies
ratings.get_user_ratings()
recommendations.get_recommendations_shows()
recommendations.get_recommendations_movies()
watchlist.get_watchlist_shows()
watchlist.get_watchlist_movies()            # ⚠️ see §4
progress = progress.get_combined_progress_watched()
ratings.auto_rate_watched_shows(progress_map=progress)
```

### 1.1 Five of thirteen subpackages are reached

| Category | Members |
|---|---|
| **Reached by `run()`** | `history` · `ratings` · `recommendations` · `watchlist` · `progress` |
| **Infrastructure** | `api` · `instances` |
| **Named external caller** | `movies` → `main.py` → `RadarrOrchestrationManager.run_relational_pull`; `people_matrix.py` → `main.py` → `TraktPeopleMatrixManager.build()`; **`shows`** → `sonarr/cache/episode_files.py` → `TraktShowCacheManager` + `score_show` |
| **Constructed, invoked by nothing** | `lookup` — kept anyway (correct code; see §6) |
| **Deleted (session 94)** | ~~`sync`~~ · ~~`universe`~~ · ~~`analytics`~~ · ~~`lists`~~ (file kept, unconstructed) |

⚠️ `people_matrix.py` is a **40 KB flat module** at the package root while every
sibling is a directory. Not a defect, but it means a reader enumerating
subpackages misses it — which is how this document first counted 15.

🔴 **`shows` was listed here as "not instantiated anywhere" — that was wrong, and the
mistake is worth recording.** It is the TV twin of `movies/`: `score_show()` drives the
entire Sonarr watchability axis and `TraktShowCacheManager` reads the enrich daemon's
per-tvdbId show buckets, both imported by `sonarr/cache/episode_files.py`. The error
came from grepping for `TraktShowsManager` — a name that appears **only as a
`parent_name` STRING** in `cache.py`, never as a class. The real exports are
`TraktShowCacheManager` and a `score_show` function, so the pattern could not match.
`shows/__init__.py` states its contents in four lines; reading it first would have
settled it. Checklist Q10, applied to a package rather than a method.

**Checklist Q9 applies to the one remaining unreached module.** Of the five opened,
`sync` and `universe` and `analytics` were wrong as well as dead, `lists` was
partly-wrong but holds unique capability, and `lookup` is simply unused — so
"unreached" predicted a defect five times out of five, but said nothing about
whether the module was worth keeping.

### 1.2 The progress result is threaded, deliberately

```python
progress = self.trakt_api.progress.get_combined_progress_watched()
self.trakt_api.ratings.auto_rate_watched_shows(progress_map=progress)
```

*"Fetch progress once, then reuse it for auto-rating (avoids a second round of ~150
per-show API calls)"* — one of the few places a result is explicitly passed forward
rather than re-fetched. Worth citing: the same discipline applied to
`auto_rate_watched_movies` is why `run_movie_ratings` builds the completion map
itself instead of asking the ratings manager to.

---

## 2. 🔴 `TraktManager` aborts the whole run on validation failure

```python
if not self.instance_manager.register_and_validate():
    raise RuntimeError("[TraktManager] Instance validation failed — aborting setup.")
```

This **raises from `__init__`**, so a Trakt credential problem kills manager
construction. Compare the two neighbours:

| Service | On failure |
|---|---|
| **Trakt** | `raise RuntimeError` — construction aborts |
| **Plex** | *"Self-disables when unconfigured/unreachable/scope-fails"* |
| **MAL** | *"optional — self-disables if not authorized"* |

`main.py` constructs Trakt **before** Radarr and Sonarr, and `_validate_managers`
lists `trakt_initialized` as critical — so this is deliberate, not an oversight. But
it means an expired Trakt token stops the *arr work too, and the *arr passes read
nothing from Trakt that they cannot degrade without (`run_relational_pull` explicitly
falls back to *"studios-only … when no Trakt enrichment is available"*).

Worth deciding whether Trakt is genuinely critical or should join Plex/MAL as
self-disabling. `GLD-TRK-10`.

---

## 3. 🔴 The episode history is fetched and thrown away

```python
self.trakt_api.history.get_full_watch_history()          # return value DISCARDED
self.trakt_api.history.get_full_movie_history_cached()   # note the suffix
```

The movie call is named `_cached`; the episode call is not — and
`get_full_watch_history` (read in §GLD-THY-01) **returns a list and writes nothing to
the cache**. Its return value is discarded here.

So this line paginates the entire episode history — 100 rows per call, against a
rate-limited API — and drops the result on the floor, every run.

Unless `get_history` caches internally (unverified), this is pure cost. The naming
asymmetry between the two adjacent calls is the tell, and it is **P-J again**: two
history fetchers, one cached, one not. `GLD-TRK-11`.

---

## 4. ✅ A shipped bug recorded at its fix site — and a P-I instance

```python
# The MOVIE watchlist was never fetched here even though the manager has always
# exposed it, so ``trakt/{user}/watchlist/movies`` had never been written — the
# acquisition path calls it lazily, and acquisition is off by default. Group-A5
# reads BOTH halves (Trakt is the only feed carrying a real ``listed_at``, so it is
# the only place the staleness term has evidence to work with), and a TV-only
# watchlist would have meant movie intent silently had no dated source at all.
```

Built, exposed, never called — until someone noticed the scorer's A5 term had no
dated movie source. The note explains **what broke downstream**, not just what was
missing, which is what makes it worth citing.

Ninth instance of a shipped bug documented where it was fixed, and a textbook P-I:
the capability existed and only the call was absent.

---

## 5. ✅ Fixed on this read

**A ninth `dry_run` clobber site** — and the highest-leverage one yet:

```python
self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
```

after `super().__init__()`, at the **Trakt service root**. `self.dry_run` is passed
straight into `base_kwargs` and therefore into every Trakt sub-manager — including
`ratings`, which POSTs to a third-party account with no undo. Removed; `BaseManager`
resolves it.

---

## 6. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-TRK-10` | 🔴 **Decide whether Trakt is critical.** `register_and_validate()` failure **raises from `__init__`**, aborting construction — while Plex and MAL self-disable. An expired token therefore stops the *arr work, which needs nothing from Trakt it cannot degrade without (`run_relational_pull` falls back to studios-only). Either justify the asymmetry or make it self-disable | S | — |
| `GLD-TRK-11` | 🔴 **`get_full_watch_history()`'s result is discarded** — the call paginates the whole episode history against a rate-limited API and drops it. Its neighbour is `get_full_movie_history_cached()`; the missing `_cached` suffix looks like the tell. Confirm whether `get_history` caches internally; if not this is pure cost every run | S | `GLD-THY-01` |
| `GLD-TRK-12` | **Q9 the seven unreached subpackages** — `analytics`, `lists`, `lookup`, `movies`, `shows`, `sync`, `universe`. `movies` has a named external caller (`main.py` → `run_relational_pull`); the other six are unverified. **P-I says defects accumulate in exactly these** — `GLD-SQ-01` and `GLD-REP-11` were both found this way | M | — |
| `GLD-TRK-13` | 🎯 **Cite the progress-threading as the reference** — `get_combined_progress_watched()` is fetched once and passed into `auto_rate_watched_shows(progress_map=…)`, *"avoids a second round of ~150 per-show API calls"*. Rare explicit result-forwarding; most passes re-fetch | S | — |

## 7. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `get_history` cache internally, or is the discarded episode-history fetch pure waste? | `GLD-TRK-11` |
| Q2 | Which of the six unverified subpackages have callers, and which are dead? | `GLD-TRK-12` |
| Q3 | Is Trakt's raise-on-failure deliberate, given Plex and MAL self-disable? | `GLD-TRK-10` |

## 8. Related designs

- [`machine_learning/scoring/movie_scorer.py`](../../machine_learning/scoring/DESIGN.md) — Group A5 reads both watchlist halves; §4 is why the movie half exists
- [`radarr/orchestration`](../radarr/orchestration/DESIGN.md) — `run_relational_pull`, the named caller for `movies/`
