# sonarr/cache/episode_files.py — verification notes

> Records what the module docstring and import block established, session 43.
> **Scope: the first ~120 lines only.** The body is unread.

---

## 1. The single largest brain consumer in the codebase

~60 symbols across **17 modules**, of which 13 are `machine_learning/`:

| Brain package | Symbols |
|---|---|
| `acquisition.next_episode_planner` | 12 |
| `acquisition.pilot_stepping` | 11 |
| `lifecycle.viewer_retention` | 7 |
| `space.jit_planner` | 7 |
| `lifecycle.restore_policy` | 6 |
| `sizing.size_model` | 4 |
| `lifecycle.grace_policy` | 3 |
| `likelihood.watch_likelihood` · `lifecycle.household_watch` · `lifecycle.stale_prune_policy` · `space.downgrade_planner` · `thresholds.registry` | 1 each |

This is the service/brain split working exactly as
[`ARCHITECTURE.md`](../../../machine_learning/ARCHITECTURE.md) intends: the module
does the I/O and Parquet work, and **delegates every decision**. Worth recording
as the reference example — most service modules import two or three brain
symbols; this one imports sixty and computes nothing itself.

---

## 2. 🔴 One file, two import paths for the same concept

```python
from scripts.managers.machine_learning.likelihood.watch_likelihood import series_universe_credits
...
from scripts.support.utilities.watch_likelihood import (
    resolution_cap_for_likelihood, watch_likelihood,
)
```

**The same module, reached two ways in the same file** — once directly, once
through the `support/utilities` re-export shim.

And [`series/quality.py`](../series/VERIFICATION_quality.md) imports those *same
two functions* (`resolution_cap_for_likelihood`, `watch_likelihood`) **directly**
from the brain. So the codebase currently imports one pair of functions by two
different paths, in two files, in the same service.

This is the sharpest evidence yet for `GLD-ML-15`/`GLD-COORD-02`: the shims are
not a tidy compatibility layer awaiting one deletion pass — they are **actively
mixed with direct imports inside individual files**. `GLD-CACHE-S01`.

Shim callers found so far, all by accident in unrelated reads:

| File | Shim |
|---|---|
| `coordinator/space_coordinator.py` | `space_targets` |
| `sonarr/orchestration/series.py` | `space_targets` |
| `sonarr/series/quality.py` | `space_targets` |
| `sonarr/series/space_pressure.py` | `space_targets` |
| **`sonarr/cache/episode_files.py`** | **`space_targets` + `watch_likelihood`** |

Five files, two shims. MIGRATION Step 10 needs an import search before anything
can be deleted.

---

## 3. 🟡 `services → services` is a pattern, not a one-off

```python
from scripts.managers.services.plex.playlists.universe_order import tv_group_maps_from_series
```

**Sonarr importing from Plex**, after
[`series/space_pressure.py`](./VERIFICATION_space_pressure.md) §5 found Sonarr
importing a private method from Radarr.

Two cross-service imports, two different targets. `ARCHITECTURE.md` rule 3
declares `services → contracts` and `services → ml.*` and is silent on
`services → services`; `brain_purity` guards the brain, not the services; and
`GLD-MGR-01`'s proposed service-purity hook does not cover import direction.

So this is now an **undeclared architectural pattern** rather than a lapse.
`GLD-SP-03` should be re-scoped from *"move one picker"* to *"decide whether
`services → services` is permitted, and state it."* Both instances share a shape:
genuinely shared logic that has no home in the brain **yet**, so it is reached
sideways.

---

## 4. ✅ Partial answer to `GLD-BKP-07`

```python
from scripts.support.utilities.backup_gate import effective_dry_run
```

`GLD-BKP-07` asks whether **all** destructive primitives read the backup gate —
the guarantee *"nothing is ever deleted without a validated rollback point"* is
only as broad as its readers, and nothing enumerates them.

The episode-files cache — which owns `build_delete_candidates` and the episode-file
delete path — **does** read it. That is the highest-volume destructive path in
Sonarr, so the most important reader is confirmed.

Still unenumerated, but the answer is now "at least the big one."

---

## 5. ✅ The canonical watched predicate is used here

```python
from scripts.managers.machine_learning.lifecycle.viewer_retention import (
    ..., watched_by_tautulli,
)
```

[`lifecycle/DESIGN.md`](../../../machine_learning/lifecycle/DESIGN.md) §1 records
that `is_watched` was once `watch_count > 0` in **both producers**, counting
30-second samples as watches (9.8 % of episode plays, 36.2 % of movie plays).

The Sonarr producer now imports the shared predicate. The fix is confirmed in
place on this side.

---

## 6. ⚠️ D31 — my session-43 hypothesis was half right, and the wrong half

Last session I proposed that `features/episode_features.py` might be a
**deliberate** non-migration because the Parquet already emits *"flat, ML-ready
columns … without further unpacking."*

Comparing the two guarantees directly:

| | `SCHEMA_COLUMNS` (Parquet) | `feature_rows.py` (contract) |
|---|---|---|
| Stated guarantee | *"missing API fields become **NaN** rather than causing KeyErrors, and the DataFrame schema is stable across partial runs"* | *"Optional fields default to **None/0** so a partially-enriched row is safe"* |
| Column names known | in the cache module | *"the **ONLY** place a column name / API JSON shape is known"* |
| Absent boolean | `NaN` | `is_watched: bool = False` |
| Absent count | `NaN` | `watch_count: int = 0` |
| Absent fraction | `NaN` | `percent_complete: float = 0.0` |

**`NaN` is not `False`, `0`, or `None`.** A `NaN` in a boolean column is truthy in
some contexts; `NaN != NaN`, so equality checks fail silently; and arithmetic
propagates it rather than raising.

So the Parquet's flatness removes the **unpacking** reason for an adapter and
leaves the **typed-null** reason entirely intact — which is precisely what
[`features/`](../../../machine_learning/features/DESIGN.md) §3.5 exists for:

> a future adapter that skipped these helpers would satisfy the dataclass and
> **still hand a scorer a NaN**

**This strengthens `GLD-FEA-01` rather than dissolving it.** Episode consumers
reading the Parquet directly get `NaN` exactly where a feature row would give a
typed default — and they are the ones on `build_delete_candidates`.

D31's answer is therefore *"flat, yes; safe, no."* My hypothesis was right about
what the Parquet provides and wrong about whether that is sufficient.

**Still outstanding:** I read `MovieFeatureRow` and `ShowFeatureRow` but not
`EpisodeFeatureRow` itself — **resolved in §6.0 below.**

### 6.0 ✅ The field-by-field comparison, session 45

`EpisodeFeatureRow` has **19 fields**, and every one appears in `SCHEMA_COLUMNS`.
It is a **strict subset** of the Parquet schema:

| Contract field | In `SCHEMA_COLUMNS`? |
|---|---|
| `series_id` · `episode_file_id` · `season_number` · `episode_number` · `series_title` | ✅ |
| `is_pilot` · `is_watched` · `watch_count` · `percent_complete` · `last_watched_at` | ✅ |
| `all_household_watched` · `air_date_utc` · `size_bytes` · `runtime_seconds` · `resolution` | ✅ |
| `keep_policy` · `marked_for_deletion` · `watchability_score` · `watchability_percentile` | ✅ |

The Parquet carries **far more**: `next_episode`, `household_last_watched_at`,
`available_until`, `date_added`, `relative_path`, `path`, `quality_name`,
`quality_source`, the `retention_hold`/`retention_hold_by` pair, and the whole
video block (`video_codec`, `video_bitrate`, `video_fps`, `video_bit_depth`,
`width`, `height`, `scan_type`, `hdr`, …).

### 6.2 🎯 The real finding: `EpisodeFeatureRow` cannot feed Group D

Compare the three rows' transcode-risk inputs:

| Group D v2 input | `MovieFeatureRow` | `ShowFeatureRow` | `EpisodeFeatureRow` |
|---|---|---|---|
| `video_codec` | ✅ | ✅ modal | ❌ |
| `video_bitrate` | ✅ | ✅ median | ❌ |
| `audio_codec` / `audio_channels` / `audio_languages` | ✅ | ✅ | ❌ |
| `subtitles` | ✅ | ✅ | ❌ |
| `relative_path` / `container` | ✅ | ✅ | ❌ |
| `resolution` · `size_bytes` · `runtime_seconds` | ✅ | ✅ | ✅ |

**The episode row carries none of them** — while the Parquet demonstrably has
every one.

That is not an omission. Its docstring states the purpose:

> One Sonarr episode file row (**carries the broadcast series score**).

Episodes are **not scored**; series are. `ShowFeatureRow` aggregates the episode
files' playback facts (*"modal codec across the series' files"*, *"MEDIAN for
bitrate"*) and Group D is computed once per series. The episode row exists to
carry **identity + lifecycle + the broadcast score down to the file**, so that
delete/keep decisions can be made per-file against a series-level score.

### 6.3 Which sharpens `GLD-FEA-01` rather than shrinking it

The adapter's job is **narrow** — 19 typed fields, not a per-episode analogue of
the movie row. But every one of those 19 feeds a **lifecycle decision**:
`is_watched`, `watch_count`, `percent_complete`, `keep_policy`,
`all_household_watched`, `marked_for_deletion`.

So the typed-null mismatch in §6 lands **exactly where it matters most**. A `NaN`
in `is_watched` on the `build_delete_candidates` path is the failure mode
[`lifecycle/DESIGN.md`](../../../machine_learning/lifecycle/DESIGN.md) §3.3
describes protecting five real files from.

### 6.4 A deliberate tri-state in the contract

```python
is_watched: bool = False                    # binary
all_household_watched: bool | None = None   # TRI-STATE
```

`is_watched` defaults `False`; `all_household_watched` defaults `None`. The
asymmetry is right: *"nobody watched it"* is a real answer, but *"has every
household member watched it"* is **unanswerable** when no household is configured
— and `False` would wrongly assert "not everyone has."

§8 **P-C** discipline expressed in a type signature. Worth noting because the
Parquet column carries no such distinction: it would be `NaN` for both.

### 6.5 A fourth independent confirmation of the stub analysis

`ShowFeatureRow`'s Group D block:

> Modal for the categoricals (codec/audio/container), **MEDIAN** for bitrate (one
> oversized special must not speak for a whole series), **MAX** for resolution.
> **All None on a pilot STUB, which is why a stub scores exactly 0.0 on Group D.**

[`thresholds/DESIGN.md`](../../../machine_learning/thresholds/DESIGN.md) §3.5
derived the stub population's −2 shift; `series/quality.py` and
`series/space_pressure.py` each restate the re-anchoring rationale. This is the
fourth statement, and the only one that gives the **mechanism** — every Group D
input is `None` on a stub, so the group contributes exactly zero.

---

## 7. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-CACHE-S01` | 🔴 **One file imports the same concept two ways** — `watch_likelihood` via the shim, `series_universe_credits` direct, in `episode_files.py`; and `series/quality.py` imports the same two functions **directly**. The shims are mixed with direct imports *inside* files, not layered above them | S | `GLD-ML-15`, `GLD-COORD-02` |
| `GLD-CACHE-S02` | ✅ **CLOSED session 45.** `EpisodeFeatureRow`'s 19 fields are a **strict subset** of `SCHEMA_COLUMNS`. But the row carries **none** of the Group D transcode-risk inputs the movie/show rows do — by design: *"carries the broadcast series score"*. Episodes are not scored; series are. The adapter's job is narrow (identity + lifecycle + broadcast score) and **every one of its 19 fields feeds a delete/keep decision**, which is exactly where the `NaN`-vs-typed-default gap bites | S | ✅ Closed |
| `GLD-CACHE-S03` | 🎯 **Cite this module as the service/brain split reference** — ~60 brain symbols from 13 packages, and it computes nothing itself | S | `GLD-MGR-01` |
| `GLD-CACHE-S04` | **Read the body** — `build_delete_candidates`, the JIT path, grace marking, restore ledger. ~10 destructive/stateful flows in one module | L | — |

## 8. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is `services → services` permitted? Two instances now, both for genuinely shared logic with no brain home | `GLD-SP-03` |
| Q2 | Does `SCHEMA_COLUMNS` cover `EpisodeFeatureRow`'s fields? | `GLD-CACHE-S02` |
| Q3 | Which other files mix shim and direct imports? | `GLD-CACHE-S01` |

**Q1 is worth settling before it grows.** Both instances are defensible —
`_pick_stepdown_release` and `tv_group_maps_from_series` are real shared logic, and
copying them would be a P-E instance. The alternative to a sideways import is a
brain home, which is where `ARCHITECTURE.md` would put them. Leaving it undeclared
means the next such case is decided by whoever writes it.

## 9. Related

- [`VERIFICATION_space_pressure.md`](../series/VERIFICATION_space_pressure.md) §5 — the first cross-service import
- [`machine_learning/features/DESIGN.md`](../../../machine_learning/features/DESIGN.md) §3.2 — the episode stub §6 reframes
- [`backup/DESIGN.md`](../../backup/DESIGN.md) §9 — `GLD-BKP-07`, partially answered in §4
- [`machine_learning/lifecycle/DESIGN.md`](../../../machine_learning/lifecycle/DESIGN.md) §1 — the watched-predicate fix confirmed in §5
