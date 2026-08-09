# features — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **features**

**Package** — `scripts.managers.machine_learning.features`
**Status** — 🟡 Half-migrated — 3 of 5 modules implemented · 🔴 Carries a fifth "watched" bar
**Related** — [README.md](./README.md) · [`contracts/DESIGN.md`](../contracts/DESIGN.md) · [`ARCHITECTURE.md`](../ARCHITECTURE.md)

---

## 1. Problem statement

The brain must score a Radarr movie and a Sonarr series with one model, and
`contracts/` defines the shapes that make that possible. But a frozen dataclass
does not populate itself. Something has to know that:

- `percent_complete` arrives as 0–100 and the scorer wants 0–1,
- `genres` is a JSON string in the Parquet, not a list,
- `user_rating` is not in `movie_files` at all — it lives in
  `trakt/{user}/ratings/movies`,
- and roughly forty other column names, types and null conventions.

If every scorer knew that, the vendor shape would leak past the boundary
`contracts/` exists to draw, and goal G1 (one model) would fragment by service.

`features/` is the one place that knowledge is allowed to live. Its own
docstring states the ambition plainly: *"Knows column names so nothing downstream
has to."*

The design problem it has not finished solving: **that is currently true for
movies and shows, and false for episodes.**

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Exactly one place knows a cache column name. |
| G2 | A partially-enriched row is always constructible. |
| G3 | Pure — the service does the I/O and passes results in. |
| G4 | Normalise units and encodings so scorers receive canonical values. |
| G5 | Reconstruct the scorer call exactly, so the adapter cannot change a score. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Fetching credits, related sets or affinity | G3 — those are I/O; they arrive as kwargs. |
| N2 | Deciding what a missing value means | `contracts/` §3.3 — the consumer decides. |
| N3 | Persisting anything | Service owns Parquet load/save. |

---

## 3. Architecture

### 3.1 The adapter

```
movie_files Parquet row
   +  credits            ← service reads Trakt people buckets
   +  related_tmdb_ids   ← service reads daemon C3 cache
   +  user_rating        ← service reads trakt/{user}/ratings/movies
        │
        ▼
build_movie_feature_row      ← the ONE place the cache schema is known
        │  _f / _s / _b / _i  coercion, pd.notna-guarded
        │  percent_complete → /100.0
        │  genres → json.loads → tuple
        ▼
   MovieFeatureRow
        │
        ▼
score_movie_features(fr, *, <library context>, return_breakdown=False)
        │  "reconstructs the exact score_movie call"
        ▼
   int | (int, breakdown)
```

The three kwargs are the interesting part. Each is documented as a kwarg
*because the value is not in the cache being adapted* — `user_rating` lives in
`trakt/{user}/ratings/movies`, and the show side passes it in from
`_build_user_show_rating_map` for the same reason. That keeps G1 honest: the
adapter knows `movie_files`' schema, and does not pretend to know Trakt's.

### 3.2 🟡 Half-migrated — the "single adapter" claim is partial

| Module | State |
|---|---|
| `movie_features.py` | ✅ 12.6 KB, implemented |
| `show_features.py` | ✅ 13.7 KB, implemented |
| `completion_stats.py` | ✅ 2.0 KB, relocated from Tautulli |
| **`episode_features.py`** | 🔵 **Stub** — docstring, declared API, `TODO(ml-migration)` |
| **`watched_set.py`** | 🔵 **Stub** — same |

Both stubs name their source precisely:

| Stub | Decision core still living in the service |
|---|---|
| `build_episode_feature_row` | `sonarr/cache/episode_files` — *"per-row field reads in `build_delete_candidates` etc."* |
| `build_watched_set` | `radarr/orchestration` — `watched_tmdb_ids` assembly (~line 350), `completion_map` assembly (~line 485) |

**The consequence for episodes is structural.**
[`contracts/feature_rows.py`](../contracts/README.md) defines
`EpisodeFeatureRow` — and **nothing in the brain builds one.** The type exists,
the adapter for it does not, so episode-row field reads remain inline in the
Sonarr cache. For episodes, "nothing downstream has to know column names" is
simply not yet true.

That matters more than the movie/show halves being done, because episodes are
the higher-volume entity — tens of thousands of rows against ~2,000 movies —
and `build_delete_candidates` is a **destructive** path.

### 3.3 🔴 A fifth "watched" threshold

[`completion_stats.py`](./completion_stats.py) counts completion for both
functions at:

```python
if entry.get("percent_complete", 0) >= 90:
```

Hardcoded, twice, with no import of `lifecycle.watched_definition`. Running the
tally:

| # | Consumer | Bar |
|---|---|---|
| 1 | Production producers | Tautulli `watched_status`, else `≥ 85` |
| 2 | `eval/replay`, `eval/forward` | `0.9` |
| 3 | `labels/labeling` — movies | `≥ 90`, or `≥ 50` if completed overall |
| 4 | `labels/labeling` — episodes | `≥ 50` |
| 5 | **`features/completion_stats` — series *and* episodes** | **`≥ 90`** |

**Two modules now count episode completion at different bars**: `labels` at 50
(justified as *"continued engagement, not per-episode completion"*) and this at
90. Both are counting episodes; only one can be measuring what the household
would call "watched."

This is the fifth instance of §8 **P-C**, and it strengthens `GLD-LAB-01` from
"unify eval and labels" to "audit every completion comparison in the brain."

### 3.4 🟡 Silent genre loss

```python
try:
    genres = json.loads(genres_raw) if genres_raw and pd.notna(genres_raw) else []
except Exception:
    genres = []
```

A malformed `genres` value yields `[]` with no signal. Downstream, an empty
genre tuple means genre affinity contributes **nothing** — so the title scores
lower for a parsing failure, indistinguishable from a title in genres the
household does not watch.

That is §8 **P-C** in miniature, and it lands on the same axis as the
enrichment-coverage problem (`GLD-ML-04`): absence reads as low preference.

### 3.5 Where partial-row safety is actually implemented

[`contracts/DESIGN.md`](../contracts/DESIGN.md) §3.3 documents that every
optional field defaults so a partial row never raises. The mechanism is here —
four coercers, each `pd.notna`-guarded, with `_i` additionally catching
`TypeError`/`ValueError` on the `int(float(v))` path.

Worth naming because it means the contract's safety property is enforced at
*construction*, not by the dataclass defaults alone. A future adapter that
skipped these helpers would satisfy the dataclass and still hand a scorer a
`NaN`.

### 3.6 ❓ Relationship to `_score_row` — unconfirmed

`movie_features.py` says it *"Mirrors the column reads previously inlined in
`space_pressure._score_row`."*

But [`thresholds/registry.py`](../thresholds/README.md) describes AXIS V2 — the
persisted `watchability_score` column — as *"Computed by `_build_score_map` →
`_score_row`, which passes the FULL input set."*

Either `_score_row` now delegates here (and "previously" means the reads moved),
or both paths exist. **I have not read `space_pressure.py`, so I am not asserting
either.** If both compute independently, that is a third watchability path
alongside AXIS V2 and AXIS LEGACY — which `thresholds/` already measures at 16.5 %
disagreement between the two it knows about. Worth an explicit check:
`GLD-FEA-04`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | One adapter owns cache schema knowledge | G1 — the boundary `contracts/` draws is only real if something enforces it | Inline reads per consumer |
| D2 | Cross-cache values arrive as kwargs | The adapter knows `movie_files`, not Trakt. Reading them here would be a second schema dependency | Read every source here |
| D3 | `pd.notna`-guarded coercers | G2 — Parquet nulls arrive as `NaN`, `None` and `pd.NA` depending on dtype | Direct casts |
| D4 | `percent_complete` normalised to 0–1 | G4 — one canonical unit; the scorer never divides | Pass 0–100 through |
| D5 | `score_movie_features` reconstructs the exact call | G5 — an adapter that reshaped arguments could change a score without touching the scorer | Let callers assemble |
| D6 | Stubs declare API, source and service remainder | Same convention as [`routing/`](../routing/DESIGN.md) — implementable without archaeology | Empty files |
| D7 | `completion_stats` relocated with its logic intact | The Tautulli managers keep the FETCH and the summary log; only the counting moved | Rewrite while moving |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | This package performs no I/O. |
| I2 | Cache column names appear here and nowhere downstream — **for movies and shows**. |
| I3 | A row with only defaults constructs without raising. |
| I4 | `percent_complete` on a feature row is a 0–1 fraction. |
| I5 | `score_movie_features` produces the same value as calling `score_movie` directly. |
| I6 | Values not in the adapted cache arrive as kwargs, never as column reads. |

**I2 carries an explicit exception today** — episodes. That is the honest form of
the `__init__.py` claim until `GLD-FEA-01` lands.

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| **Malformed `genres` JSON** | bare `except` | `[]` — genre affinity contributes nothing | 🟡 Title scores lower for a parse failure | ❌ **None** |
| Column absent from the Parquet | `pd.notna` guard | `None`/`False` default | Safe by contract | ❌ **None** |
| Column present with an unexpected dtype | `_i` catches; others coerce | `None`, or a surprising float | 🟡 | ❌ **None** |
| `credits` / `related_tmdb_ids` not passed | Default `{}` / `None` | Groups B and C3 contribute nothing | 🟡 Same shape as `GLD-ML-04` | ❌ **None** |
| Episode row needs a feature row | Stub | Service reads fields inline | 🟡 I2 exception | ✅ Documented |
| Watched-set needed | Stub | `radarr/orchestration` assembles it | 🟡 | ✅ Documented |
| Completion counted at 90 here, 50 in `labels` | **None** | Two answers to "how many episodes were watched" | 🔴 §3.3 | ❌ **None** |

Every row degrades toward a **lower** score. That is the same directional bias
`GLD-ML-04` names, arriving through a different door: missing input contributes
zero rather than being renormalised out.

---

## 7. Configuration surface

None. Every input is passed by the caller; no config key is read.

---

## 8. Implemented capabilities

- ✅ `MovieFeatureRow` construction from a `movie_files` Parquet row
- ✅ `ShowFeatureRow` construction with series-level aggregation
- ✅ Exact scorer-call reconstruction, breakdown-optional
- ✅ Four null-safe coercers implementing the contract's partial-row guarantee
- ✅ Unit and encoding normalisation (`percent_complete`, `genres`)
- ✅ Cross-cache values as documented kwargs
- ✅ Per-show and household episode completion tallies
- ✅ Group-D input handling and C4 person-matrix threading (tested)

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-FEA-01` | 🔴 **Implement `build_episode_feature_row`** — `EpisodeFeatureRow` is defined in `contracts/` and built by nothing in the brain; episode field reads stay inline in `sonarr/cache/episode_files`, including on the destructive `build_delete_candidates` path | Makes I2 unconditional for the highest-volume entity | M | `contracts/` |
| `GLD-FEA-02` | **Implement `build_watched_set`** — assembly still in `radarr/orchestration` at two named line ranges | Completes the migration; the watched-set feeds Group A | M | `GLD-LAB-01` |
| `GLD-FEA-03` | 🔴 **Route `completion_stats`' `>= 90` through `lifecycle.watched_definition`** — or document why series/episode completion tallies use a different bar from `labels`' 50 | §3.3: the fifth watched bar, and the second one counting *episodes* | S | `GLD-LAB-01`, D28 |
| `GLD-FEA-04` | ❓ **Confirm whether `space_pressure._score_row` delegates here** or computes independently | §3.6 — if both exist, that is a third watchability path alongside the two `thresholds/` already measures at 16.5 % disagreement | S | `GLD-THR-01` |
| `GLD-FEA-05` | **Warn on malformed `genres`** instead of silently yielding `[]` | §3.4 — a parse failure currently reads as "no genres the household likes" | S | — |
| `GLD-FEA-06` | **Coverage flags on the built row** — record which kwargs were supplied | The natural implementation point for `GLD-CON-03`; the adapter is the only place that knows what arrived | M | `GLD-CON-03`, `GLD-ML-16` |
| `GLD-FEA-07` | **Assert the Parquet schema** the adapter expects, and fail loudly on a rename | I2 makes this the single point where a schema change is detectable | S | `GLD-LAB-10` |
| `GLD-FEA-08` | **Property-test the coercers** against real null shapes (`NaN`, `None`, `pd.NA`, empty string) | §3.5 — they carry the contract's safety guarantee and are covered only incidentally | S | `GLD-CON-04` |
| `GLD-FEA-09` | **Document the show-side aggregation** in `show_features.py` alongside the statistic table in `contracts/DESIGN.md` §3.4 | Modal/median/max choices are stated in `contracts`; the code implementing them is here | S | `GLD-CON-05` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should episode feature rows be built here, or is the inline path in `sonarr/cache/episode_files` deliberate given the row volume? | `GLD-FEA-01` |
| Q2 | Is `completion_stats`' 90 measuring something different from `labels`' 50, or is one of them drift? *(feeds D28)* | `GLD-FEA-03` |
| Q3 | Does `_score_row` still compute independently? | `GLD-FEA-04` |
| Q4 | Should the adapter carry coverage flags, or should `contracts/` carry a sentinel type? *(= D21)* | `GLD-FEA-06`, `GLD-CON-03` |

**Q1 deserves a real answer rather than an assumption.** Constructing a frozen
dataclass per episode across tens of thousands of rows has a cost the movie path
never faces at ~2,000. The stub may be unmigrated because it is *hard*, not
because it is unfinished — and `GLD-CON-06` (`slots=True`) is the mitigation if
so.

## 11. Related designs

- [`contracts/DESIGN.md`](../contracts/DESIGN.md) §3.3 partial-row safety, §3.4 show aggregation
- [`scoring/DESIGN.md`](../scoring/DESIGN.md) — the consumer of `score_movie_features`
- [`thresholds/DESIGN.md`](../thresholds/DESIGN.md) §3.4 — the two watchability axes §3.6 might extend
- [`labels/DESIGN.md`](../labels/DESIGN.md) §3.4 — the other episode completion bar
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — *"the single adapter between service caches and scorers"*
