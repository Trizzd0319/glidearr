# tautulli — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **tautulli**

**Package** — `scripts.managers.services.tautulli`
**Status** — ✅ Implemented · 🟡 One signal built with no consumer
**Related** — [README.md](./README.md) · [`services/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Every value judgement Glidearr makes rests on one question: **what does this
household actually watch?** Not what is popular, not what is highly rated —
what gets played, by whom, on what device, and how far through.

Plex knows sessions but forgets them. Trakt knows what was scrobbled but not how
it played. Only Tautulli has the durable, per-user, per-device playback record.

That makes this manager the foundation of the run, and it creates four problems:

1. **It must run first.** Affinity and completion caches produced here are read
   by Radarr's ratings and space-pressure logic, by Trakt scoring, and by the
   watchability model. Nothing downstream is correct until they exist.

2. **Identity is fragmented.** Tautulli speaks `rating_key`. Radarr speaks
   `tmdb_id`. Sonarr speaks `tvdb_id`. Joining watch history to a library item
   requires a crosswalk that will sometimes fail, and failing silently would
   quietly under-report what the household has watched.

3. **"Watched" is not binary.** A movie stopped at 85% is watched. At 12% it is
   abandoned. Across a household, the *maximum* completion across members is the
   meaningful figure, not the mean.

4. **History is large and paginated.** A full pull is thousands of rows and must
   not be re-fetched wholesale every run.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Produce the affinity and completion caches every downstream scorer depends on. |
| G2 | Read-only. This manager never mutates Tautulli or the library. |
| G3 | Household aggregation is configurable, and works with zero configuration. |
| G4 | Unresolved identity mappings are visible, not silently dropped. |
| G5 | Per-device codec behaviour is captured for profile selection. |
| G6 | A partial failure degrades signals, not the run. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Writing to Tautulli | It is a record, not a control surface. |
| N2 | Being the watched-set authority | Trakt also has history; the brain reconciles them. |
| N3 | Real-time session monitoring | Batch. Sessions are read as history. |
| N4 | Scoring | Signals only. The brain scores. |

---

## 3. Architecture

### 3.1 Component map

```
TautulliManager(BaseManager, ComponentManagerMixin)     parent: Main
  │
  │  TautulliAPI  ← tautulli/api.py re-exports instances/api.py
  │
  │  split_components(...) → critical / non-critical
  │  lazy instantiation via BaseManager._singleton in _load_component
  │
  ├── CRITICAL (7 — eagerly loaded in prepare())
  │     watch_history   paginated history pull
  │     users           household membership
  │     metadata        rating_key → guid → tmdb/tvdb crosswalk
  │     episodes        episode-level playback
  │     series          series-level rollup
  │     transcode       stream decisions, fingerprints
  │     devices         per-device codec matrix
  │
  └── NON-CRITICAL (lazy on first access)
        instance        multi-instance resolution
        validator_manager   (stub)
```

Note the deviation from the usual pattern: this manager uses
[`split_components`](../../../support/utilities/managers/component_splitter.py)
rather than `ComponentManagerMixin.load_components`, partitioning declared
components into critical and non-critical, then instantiating lazily through
`BaseManager._singleton`. `prepare()` eagerly loads exactly the seven critical
components and emits one summary line.

### 3.2 Control flow

```
prepare()
    load 7 critical components → one summary line

run()
    _is_reachable()          → GET /api/v2?cmd=get_server_info
    users                    → household membership
    watch_history            → paginated pull
    metadata                 → rating_key → guid → tmdb_id / tvdb_id
    libraries                → library shape
        │
        └─► derive:
              device codec matrix       → tautulli/device_codec_matrix
              per-user affinity         → tautulli/users/<safe>/affinity
              household affinity        → tautulli/affinity
              group completions         → tautulli/group/<g>/tmdb_completions
              unresolved diagnostics    → tautulli/debug/group/<g>/unresolved_rating_keys
```

### 3.3 The identity crosswalk (problem 2)

```
rating_key ──► metadata ──► guid ──► tmdb_id | tvdb_id
                              │
                              └─ failure split into TWO diagnostic buckets:
                                   not_in_metadata   — the rating_key itself is unknown
                                   no_tmdb_guid      — known, but carries no tmdb guid
```

Splitting the failure into two named buckets (G4) is a deliberate diagnostic
choice: they have different causes and different fixes. `not_in_metadata`
suggests a stale metadata pull or a deleted item; `no_tmdb_guid` suggests a Plex
match problem on that specific item.

### 3.4 Data contracts

**Written to `global_cache`:**

| Key | Shape | Consumer |
|---|---|---|
| `tautulli/affinity` | Household genre / actor / director weights | Radarr, Trakt scoring |
| `tautulli/users/<safe_username>/affinity` | Per-user weights. Username sanitised: `[\/:*?"<>\|]` → `_` | Per-user playlists |
| `tautulli/group/<group>/tmdb_completions` | `{tmdb_id: max completion %}` | Radarr ratings, space pressure, owned-movie watched-set |
| `tautulli/device_codec_matrix` | Per-device play/transcode matrix | 🟡 **No consumer reads it today** |
| `tautulli/debug/group/<group>/unresolved_rating_keys` | `{not_in_metadata: [...], no_tmdb_guid: [...]}` | Diagnostics |

**Config read:**

| Key | Default | Effect |
|---|---|---|
| `tautulli` | — | Connection block. Accepts flat `{"url","api"}` **or** multi-instance `{"default": {...}}`; if values are not all strings it takes `"default"`, falling back to the first value |
| `rating_groups` | `{"household": {}}` | Household grouping. Defaulted so completions are always built (G3) |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Runs first in the pipeline | Everything downstream reads its caches (G1) | Run alongside Trakt |
| D2 | Strictly read-only | G2 — no `dry_run` gate needed because there is nothing to suppress | Bidirectional sync |
| D3 | `split_components` + lazy `_singleton` instead of `load_components` | Only 7 of 9 components are needed every run; `instance` and `validator_manager` are rarely touched | Load all eagerly |
| D4 | `rating_groups` defaults to `{"household": {}}` | G3 — an unconfigured install still gets completions, which are load-bearing for space pressure | Require configuration |
| D5 | **Maximum** completion across a group, not mean | If anyone in the household finished it, it is watched. A mean would mark a film watched by one person as 25% watched in a family of four | Mean / per-user only |
| D6 | Unresolved mappings split into two named buckets | G4 — different causes, different fixes | One "failed" list |
| D7 | Username sanitisation for cache paths | Usernames become filesystem paths; `/` or `:` would escape the cache root | Hash the username |
| D8 | Config block accepts flat *and* multi-instance shapes | Tolerates both legacy and current config without a migration | Force one shape |
| D9 | Reachability probed via `get_server_info` before work | Fails fast and cheaply rather than mid-pull | Let the first real call fail |
| D10 | Device codec matrix built despite having no consumer | It is the keystone signal for per-device profile selection, and building it now means the data exists when the consumer lands | Defer until needed |

**D5 is the one that matters most for correctness.** Household completion is a
max, not an average, and it is the input to both deletion protection and the
owned-movie watched-set.

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | This manager performs no APPLY. No POST, PUT or DELETE against Tautulli. |
| I2 | It runs before Trakt, Radarr and Sonarr. |
| I3 | `rating_groups` is never empty — it defaults. |
| I4 | Group completion is the **maximum** across members. |
| I5 | Usernames are sanitised before becoming cache path segments. |
| I6 | Unresolved rating_keys are recorded, never silently dropped. |
| I7 | The 7 critical components load in `prepare()`; the rest are lazy. |
| I8 | No scoring happens here — signals only. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Tautulli unreachable | `_is_reachable()` | Phase skipped; downstream reads last-good caches | Scores stale, not wrong |
| A critical component fails to load | `prepare()` summary | `❌` in the summary line; that signal absent | One signal |
| History pull times out mid-pagination | Per-page | Partial history; affinity computed from what arrived | 🟡 Under-weighted affinity, silently |
| `rating_key` not in metadata | Crosswalk | Recorded in `not_in_metadata` | That title excluded from completions |
| Metadata present, no tmdb guid | Crosswalk | Recorded in `no_tmdb_guid` | That title excluded |
| Username contains path separators | Sanitiser | Replaced with `_` | Collision possible if two users differ only by such chars |
| `rating_groups` misconfigured | None | Groups built as configured, possibly empty | Completions missing for that group |
| Device matrix written, nothing reads it | — | Wasted work, no harm | 🟡 Dead signal |

**Row 3 is the quiet one.** A partial history pull produces affinity weights that
look valid but under-represent recent viewing, and there is no marker
distinguishing "computed from complete history" from "computed from half of it."

---

## 7. Configuration surface

| Key | Type | Default | Effect |
|---|---|---|---|
| `tautulli` | dict | — | URL + API key; flat or multi-instance |
| `rating_groups` | dict | `{"household": {}}` | Household grouping for completion aggregation |

Identity: Tautulli group `household`.

---

## 8. Implemented capabilities

- ✅ Full read-only collection run: users, paginated history, metadata, libraries
- ✅ Household and per-user genre / actor / director affinity
- ✅ Per-group tmdb completion map keyed by `tmdb_id`, aggregated as a maximum
- ✅ `rating_key` → guid → `tmdb_id`/`tvdb_id` crosswalk
- ✅ Two-bucket unresolved-mapping diagnostics
- ✅ Per-device codec play/transcode matrix
- ✅ Transcode stream-decision capture and fingerprinting
- ✅ Episode- and series-level playback rollups
- ✅ Flat and multi-instance config shapes
- ✅ Cheap reachability probe before work
- ✅ Critical/non-critical component split with lazy loading

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **🟡 Consume `tautulli/device_codec_matrix`** in profile selection | The keystone per-device signal is built every run and read by nothing. Either wire it into [`quality_analytics/profile_selector.py`](../../machine_learning/quality_analytics/profile_selector.py) or stop building it | M | Per-device profile design |
| P2 | **Watched-definition contract** for the Trakt auto-prune loop — whose history counts, and what completion threshold marks a series watched | Directly blocks [`services/DESIGN.md`](../DESIGN.md) P1 | M | Q1, Q2 |
| P3 | **Completeness marker on affinity** — record whether history was fully pulled | Closes §6 row 3: consumers could then discount partial-run affinity | S | — |
| P4 | **Incremental history pull** via a persisted cursor | A full paginated pull every run is the dominant Tautulli cost | M | Cursor persistence (I10 pattern) |
| P5 | **Unresolved-mapping report surfaced in the run summary** | The diagnostic cache exists but nobody looks at it | S | — |
| P6 | **Per-user completion**, not just group max | Enables per-viewer next-watch and per-user playlists | M | [`playlists/per_user.py`](../../machine_learning/playlists/per_user.py) |
| P7 | **Device capability inference** — derive what each device *can* play, not only what it did | Stronger than observed behaviour for new content | M | P1 |
| P8 | **Session-level dwell signals** (pauses, rewinds, abandons) | Richer engagement signal than completion percentage | L | — |
| P9 | **Multi-instance Tautulli** | Currently effectively single-instance | M | `instances/` |
| P10 | **Affinity decay** — weight recent viewing above historical | A film watched five years ago currently counts equally | M | Brain-side |
| P11 | **Validator implementation** — `validator_manager` is a stub | No health signal for Tautulli beyond reachability | S | — |

**P1 is the clearest waste in this subsystem.** The device codec matrix is
described as the keystone signal for per-device profile selection, is computed on
every run, and has no reader.

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Whose watch history counts as "watched" — any household member, or a designated primary? | P2 |
| Q2 | What completion threshold marks a *series* watched — last aired episode, 90% of aired, or all? | P2 |
| Q3 | Should the device codec matrix be wired up (P1) or dropped? | P1 |
| Q4 | Should affinity decay with age, and on what half-life? | P10 |
| Q5 | Is Tautulli or Trakt the authority when their histories disagree? | P2 |

## 11. Related designs

- [`services/DESIGN.md`](../DESIGN.md) — the service contract and run order
- [`trakt/DESIGN.md`](../trakt/DESIGN.md) — the other watch-history source
- [`machine_learning/affinity/`](../../machine_learning/affinity/README.md) — consumes these caches
- [`machine_learning/lifecycle/household_watch.py`](../../machine_learning/lifecycle/household_watch.py)
- [`machine_learning/quality_analytics/`](../../machine_learning/quality_analytics/README.md) — the intended P1 consumer
- [`support/knowledge/reducing-plex-transcoding.md`](../../../support/knowledge/reducing-plex-transcoding.md)
