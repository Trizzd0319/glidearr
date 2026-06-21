# HybridUniverseAcquisitionManager — hybrid (film + TV) universe acquisition

- **File** — `scripts/managers/services/coordinator/universe_acquisition.py` *(planned — see Status)*
- **One-liner** — When the household watches *any* part of a shared universe (MCU, Star Trek, Star Wars, Arrowverse, One Chicago…), Glidearr acquires the **rest of that saga in in-universe timeline order** — films via Radarr, shows via Sonarr — filling from the **start** of the saga first, weaving movies and episodes onto one axis.
- **Status** — 🚧 **In progress.** The pure data + planning layers are landed and tested; the I/O coordinator + Radarr grab path + the TV per-group walk are pending. See [Status](#status).

> **Design north star (operator's words):** *"Prioritize accuracy of acquisition over efficiency of coding, but make sure there are no footgun obstacles in the way."* Every write goes through an existing, dry-run-safe, space-aware primitive. The walk never POSTs directly, never bypasses the free-space band, and never strands an add.

---

## What it does

Today acquisition is **per-service and per-series**: the Sonarr next-episode walk prefetches the next episodes of shows you're *already watching*; the Radarr/recommendation path adds Trakt/MAL/watchlist candidates. Neither knows that *Loki*, *Daredevil*, *WandaVision* and a dozen films are one saga.

This feature adds a **cross-service coordinator** that treats a universe as a single ordered timeline of films **and** shows and fills it from where the household is:

- **Watch Clone Wars (2008)?** → grab Episodes I–III (the saga *start*) **ahead of** more Clone Wars, plus continue Clone Wars itself.
- **Watch Birds of Prey S1E1?** → pull *Arrow* (earlier in the Arrowverse) up front, and keep grabbing Birds of Prey.
- **Watch one MCU film?** → backfill the unowned MCU films + Disney+ shows in MCU timeline order.

It is **extend-only** (it never cold-starts a saga nobody has touched), **default-off**, and **dry-run-safe**. *(The `cold_start` config knob is reserved for the Phase-7 coordinator and is currently **inert** — the TV walk always extends engaged sagas only.)*

### Universe vs franchise

| Kind | Examples | Order key | Acquire order |
|---|---|---|---|
| **Universe** (timeline) | MCU, Star Trek, Star Wars, Arrowverse | `timeline_index` (in-universe saga order, from the mdblist list) | saga order, films + shows interleaved |
| **Franchise** (curated) | One Chicago, Law & Order, Doctor Who | `timeline_index` (curated saga order) when present, else **air date** | saga order; TV-only |

The rule is uniform: **order by `timeline_index` whenever it is available, fall back to air date only when a group has none.** Curated TV franchises carry a saga order (`Fire=0, P.D.=1, …`), so they are `timeline`-kind too; air-date is purely the fallback for release-ordered groups.

---

## Locked operational decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| 1 | **Frontier audience** | **Household** | The library is shared. Movies via the tmdb-keyed `tautulli/group/<group>/tmdb_completions`; TV via the owned-episode `is_watched` signal — one consistent source across both media. Mixing per-user sources is incoherent (different keys + thresholds). |
| 2 | **When to grab** | **Extend engaged sagas, start-first** | A universe is *engaged* once the household has watched ≥1 member. Then its **unowned** members are acquired in ascending `timeline_index` (the **start** before the middle). No cold-start (the `cold_start` knob is honored by the Phase-7 coordinator; the TV walk is always extend-only). |
| 3 | **Continuation** | **Existing next-episode walk** | The actively-watched show's *next episodes* are already the per-series walk's job; the universe walk only adds the start-first **backfill** of the *other* members. |
| 4 | **Budget** | **Bypass `min_score` / `max_adds_per_run`; own cap** | A universe grab is explicit intent, not a scored recommendation — it must not be dropped below `min_score` or starved by Trakt recs. Capped by `acquisition.universe.max_per_run`. |
| 5 | **Safety** | **dry-run + free-space band + acquisition pause, always** | Never writes in dry-run, never grabs under disk pressure, never strands an add when free < reserve and deletions are off. |

---

## Architecture — five layers

```
                       cached mdblist universe lists  (plex/playlists/universe_source)
                                     │
              ┌──────────────────────┴───────────────────────┐
   LAYER 1    │ split_list_media: items[{media,id,rank}]      │  ← unified cross-media rank
   (data)     │ unified_universe_order(...,include_unowned)   │  → {key:[{media,id,rank,owned}]}
              └──────────────────────┬───────────────────────┘
                                     │  + household watched (tmdb_completions, is_watched)
   LAYER 2    ┌──────────────────────┴───────────────────────┐
   (plan)     │ universe_acquire_plan(unified, watched_*)     │  → engaged sagas' UNOWNED members,
              └──────────────────────┬───────────────────────┘     rank-ascending (start-first)
                                     │
   LAYER 5    ┌──────────────────────┴───────────────────────┐
 (coordinator)│ HybridUniverseAcquisitionManager (Phase 3)    │  reads source + watched, runs the plan,
              │  per item, route by media + own cap, dry-safe │  routes each grab:
              └───────────┬───────────────────┬───────────────┘
                          │ movie             │ show
              LAYER 3 ────▼────────┐   LAYER 4 ▼──────────────────┐
              │ Acquisition       │   │ Sonarr per-group walk     │
              │ .ensure_owned_    │   │ (_compute_next_episodes   │
              │  and_grab(tmdb)   │   │  group-aware) / add-by-tvdb│
              └───────────────────┘   └───────────────────────────┘
```

### Layer 1 — unified cross-media timeline data *(landed)*
`scripts/managers/services/plex/playlists/universe_order.py`
- **`split_list_media(items, timeline)`** now also returns `items: [{media, tmdb|tvdb, rank}]` — the cross-media order the source list carries, which the per-media `movies[]`/`shows[]` views discarded. Purely additive; existing consumers untouched.
- **`unified_universe_order(source, owned_movie_tmdbs, owned_tvdb_to_sid, *, include_unowned=False)`** → `{universe_key: [{media, id, rank, owned}…]}`. One saga axis per timeline universe; `id` is source-native (movie = tmdb, show = tvdb). `include_unowned=True` keeps the gaps (for acquisition); `False` returns owned-only (for the playlist). De-duped by `(media, id)`. Stale source lacking `items` → no order (caller falls back).

### Layer 2 — the acquire plan *(landed)*
`scripts/managers/services/plex/playlists/universe_order.py`
- **`universe_acquire_plan(unified_order, watched_movie_tmdbs, watched_show_tvdbs)`** → `{key: [{media, id, rank}…]}`: for each **engaged** universe (household watched ≥1 member), the **unowned** members, **rank-ascending** (start before middle). Pure.

### Layer 3 — Radarr grab-by-tmdb *(pending)*
`scripts/managers/services/acquisition/__init__.py`
- **`ensure_owned_and_grab(tmdb_id, *, instance=None, search=True)`** — the missing "make this specific film owned and grab it" entry point. Built on the existing, battle-tested primitives:
  1. **Dedup** — `ArrGateway.in_library` / `RadarrMoviesRetrievalManager.get_movie_by_tmdb`. Already owned + `hasFile` → `already-owned` (no-op).
  2. **Present-but-no-file** → monitor + `MoviesSearch` command (reuse `_trigger_search`).
  3. **Not present** → `Resolver.prepare({"type":"movie","ids":{"tmdb":id}})` (lookup fills title/profile/root, **exact tmdb match required — fail closed, never `matches[0]`**) → `Adder.add(enriched, search=…)`.
  - Honors `self.dry_run` (Adder/`_trigger_search` are gated) and the space-pressure deferral (`_acquisition_paused` / `new_deferred`). **Bypasses** `min_score` / `max_adds_per_run`.

### Layer 4 — Sonarr per-group walk *(pending — task #8)*
`scripts/managers/services/sonarr/cache/episode_files.py::_compute_next_episodes`
- Refactor the per-series loop into a **per-group** loop (`next_episode_planner.group_members`): members are visited in saga order (`timeline_index` for a `timeline` group, else series_id for an `airdate` group) and the walk is **member-SEQUENTIAL, frontier-first** — it walks each member's own episode sequence to budget exhaustion before the next member. It does **not** interleave episodes across shows by air date (that was an early idea, superseded by the timeline-index-first rule; the unified cross-media *ordering* still interleaves films+shows at the coordinator/playlist level via `unified_universe_order`). An **ungrouped series is its own singleton group**, so with the maps empty the walk reduces **byte-identically** to today. Universe **show** members enter here; the existing `_do_acquire_next_episodes` grab path is unchanged. *(Foundation — `group_key_for_series` / `group_members` / `order_groups_by_recency` / `tv_group_maps` — is landed and tested.)*

### Layer 5 — the coordinator *(pending — task #10)*
`scripts/managers/services/coordinator/universe_acquisition.py`
- A **Phase-3 capability manager** modeled exactly on `SpaceCoordinatorManager`: constructed in `main.py::_initialize_managers` with `sonarr=` + `radarr=` kwargs, reaches leaf managers via `self.registry.get("manager", …)`, gated by a single flag, dry-run resolved kwargs→parent→`Main`, no submanagers.
- **`run()`**: read `plex/playlists/universe_source` → `unified_universe_order(include_unowned=True)` → gather household watched (movies: `tmdb_completions`; TV: owned-episode `is_watched` → tvdbs) → `universe_acquire_plan(...)` → for each candidate up to `max_per_run`, route **movie → `ensure_owned_and_grab`**, **show → Sonarr** (add-by-tvdb / feed the group walk). Emits a dry-run preview grid.

---

## Data flow (end to end)

1. **Membership/order** comes from the **same** cached mdblist universe lists the playlist builder already fetches (`plex/playlists/universe_source`, weekly TTL, last-good on failure). No second fetch; gated by `plex.playlists.universe_timeline.enabled`.
2. **Owned resolution** — movie tmdb → owned via `plex/movies/owned_inventory`; show tvdb → owned via the `tvdb→series_id` map built from owned episodes. An item that resolves to nothing owned is an **acquire candidate**.
3. **Engagement** — household watched ≥1 member (movie: `tmdb_completions.pct ≥ threshold`; show: any episode `is_watched`).
4. **Plan** — `universe_acquire_plan` yields each engaged saga's unowned members, start-first.
5. **Grab** — the coordinator routes each, capped, through the dry-run-safe service paths.
6. **Playlist** *(downstream, optional later)* — the same unified rank can interleave a universe's films + episodes in *Up Next*. Out of scope for the acquisition build; ordering already correct per-media.

---

## Config keys

Under `acquisition.universe` (onboarding: `schema.py`; env: `env_map.py`; defaults: `config.json`):

| Key | Default (schema) | Testing (config.json) | Meaning |
|---|---|---|---|
| `enabled` | `false` | `true` | Master gate for hybrid universe acquisition. |
| `max_per_run` | `5` | `5` | Per-run cap on universe backfill grabs (bypasses `max_adds_per_run`/`min_score`). |
| `cold_start` | `false` | `false` | `false` = extend only sagas with ≥1 watched member; `true` = also start universes you own none of (aggressive). **⚠️ Coordinator-pending: inert until Phase 7 — the TV walk is always extend-only regardless of this flag.** |
| `movies` | `true` | `true` | Acquire unowned **film** members via Radarr. |
| `tv` | `true` | `true` | Acquire unowned **show** members via Sonarr. |

**Dependencies:** needs `plex.playlists.universe_timeline.enabled: true` (membership source) and the mdblist apikey (keychain at runtime). With `dry_run: true` the whole walk previews and writes nothing.

---

## Footguns & safety rails (explicit)

These are the obstacles the design removes by construction:

1. **dry-run leak** → route **every** write through `Adder.add` / `_trigger_search` / `_do_acquire_next_episodes` (all dry-run-gated). The coordinator **never** calls `ArrGateway.add`/`command` directly.
2. **Runaway adds** → `max_per_run` cap + extend-only. Universe grabs never join the scored `eligible[]` list, so they can't consume the recommendation budget either.
3. **Stranded adds under pressure** → reuse `AcquisitionManager._acquisition_paused` / `_space_band`; if free < reserve and deletions are off, defer (add monitored + search-off, queued) exactly like a normal add — never an orphaned monitor.
4. **Double-grab / churn** → always dedup first; for an already-present-no-file title, monitor + `MoviesSearch` instead of `POST movie`. The TV side reuses the next-episode flag idempotency (reset every sync, `episode_file_id`-null mask).
5. **Wrong film added** → require an **exact** tmdb match in the Radarr lookup; fail closed (skip) rather than adding `matches[0]`.
6. **Unreleased next-saga film** → an announced-but-unreleased film is added *monitored* with `minimumAvailability: released`, so Radarr holds it and grabs once it releases — it never grabs garbage. ⚠️ *Not yet pre-filtered:* it does occupy a monitored slot and (once the coordinator enforces `max_per_run`) a per-run slot until release. A precise pre-filter needs a digital-availability signal from the Radarr lookup (the lookup carries `year`, not release status) — a **coordinator refinement**, not a safety issue.
7. **Stale / empty universe source (no mdblist key)** → treat as "nothing to acquire this run" (no error); degrade like the playlist builder.
8. **Cross-media id collision** → identity is the `(media, id)` pair, never bare id (a movie tmdb and a show tvdb can collide).
9. **Phase ordering** → run in **Phase 3** (after Plex warms the universe cache) and drive grabs **directly** via the leaf managers the coordinator holds — don't depend on Sonarr's already-finished Phase-2 walk.
10. **Config fragmentation** → reuse `universe_timeline.enabled` for *membership*; the single new `acquisition.universe.enabled` gates *acquisition*. No third source of truth.
11. **Default-off regression** → the TV walk's per-group refactor must be **byte-identical** with empty maps (singleton groups); guarded by a flag-off regression test.

---

## Implementation plan & process (ordered, with tests)

Each step is independently shippable and tested; later steps are gated default-off until verified.

| Phase | Work | Files | Tests | Risk |
|---|---|---|---|---|
| **0 — foundation** ✅ | group resolution + canonical map merge (timeline-first) | `next_episode_planner.py`, `universe_order.py` | `test_next_episode_planner.py`, `test_universe_order.py` | low |
| **1 — unified data** ✅ | `split_list_media.items` + `unified_universe_order` | `universe_order.py` | `test_universe_order.py` (split + unified) | low (additive) |
| **2 — acquire plan** ✅ | `universe_acquire_plan` (engaged, start-first) | `universe_order.py` | `test_universe_order.py` (plan) | low (pure) |
| **3 — builder rewire** ✅ | `_tv_franchise_maps` → `tv_group_maps` | `builder.py` | full playlist suite | low (behavior-neutral) |
| **4 — Radarr grab** ✅ | `AcquisitionManager.ensure_owned_and_grab(tmdb)` | `acquisition/__init__.py` | `test_universe_grab.py` — dedup / present-no-file / not-present / dry-run / exact-match-fail-closed / pause / defer (9 tests) | medium |
| **5 — Sonarr accessor** ✅ | `universe_order.tv_group_maps_from_series` + `episode_files._universe_group_maps(instance)` (series cache + cached source, gated by `acquisition.universe.enabled`) | `test_universe_group_maps.py` + `test_universe_order.py` | low |
| **6 — TV per-group walk** ✅ | `_plan_group_walk` + group-boundary budget/cap in `_compute_next_episodes` | `episode_files.py` | `test_universe_walk.py` — byte-identical-off / shared-budget frontier-first / unstarted-member injection / off-doesn't-prefetch-unstarted (4 tests) | **high** |
| **7 — coordinator** | `HybridUniverseAcquisitionManager` + `main.py` wiring | `coordinator/universe_acquisition.py`, `main.py` | run() dry-run preview, household-watched gate, cap, space-pause, double-grab dedup | medium |
| **8 — playlist interleave** *(optional/later)* | unified rank into the combined Up Next block | `combined_builder.py` | spoiler-safety preserved | medium |

**Process discipline (the "accuracy over efficiency" mandate):**
- Pure brain functions first, fully unit-tested, before any I/O.
- The walk refactor (Phase 6) lands behind the flag with a **byte-identical-off** golden test as the gate — it does not change a single ungrouped series' output.
- The coordinator (Phase 7) is **no-op by default** and dry-run-previews its decisions (a "would acquire next MCU member" grid) before it ever writes.
- Validate each phase against the live config (`dry_run: true`) and the per-run `playlists.log` / acquisition preview before flipping any default.

---

## Status

**Landed + tested** (≈47 foundation + 24 universe-order assertions, all green):
- Layer 0 — `group_key_for_series` / `group_members` / `order_groups_by_recency`; `tv_group_maps` (timeline-first).
- Layer 1 — `split_list_media.items`; `unified_universe_order`.
- Layer 2 — `universe_acquire_plan`.
- Layer 3 — `AcquisitionManager.ensure_owned_and_grab(tmdb)` (Radarr grab; exact-tmdb guard, dedup+hasFile, dry-run-safe, space-pause/defer — 9 tests).
- Sonarr accessor — `tv_group_maps_from_series` + `episode_files._universe_group_maps(instance)` (gated by `acquisition.universe.enabled`; curated franchises group even without an mdblist source — tested).
- Builder rewired onto `tv_group_maps` (one source of truth).
- Config surface — `acquisition.universe.*` in onboarding schema + env_map + `config.json` (testing defaults on).

- Layer 6 — `_compute_next_episodes` per-group walk: `_plan_group_walk` (grouped, saga-ordered, unstarted-member injection for engaged sagas) + group-boundary budget/cap reset; cold-frontier footgun handled (group-lead detected after cold-skip); byte-identical-off test-guarded (4 tests).

**Pending:**
- Layer 5/7 — the `HybridUniverseAcquisitionManager` coordinator + `main.py` wiring.

---

## Scenario validation (Phases 0-6)

A 5-agent adversarial sweep verified **90 scenarios** against the real code: **64 work**, **5 by-design limits**, **9 coordinator-todos**, and **7 footguns** — all 7 either FIXED now or correctly owned by Phase 7. Tests: `test_universe_walk.py`, `test_universe_grab.py`, `test_universe_group_maps.py`, `test_universe_order.py`, `test_next_episode_planner.py`.

### TV per-group walk (`_compute_next_episodes`)
| # | Scenario | Expected | ✓ |
|---|---|---|---|
| 1 | Feature OFF, two started shows | each flagged independently (legacy) | ✅ byte-identical |
| 2 | OFF, unstarted series | never prefetched | ✅ |
| 3 | ON, ungrouped standalone | own singleton group = legacy | ✅ |
| 4 | ON, One Chicago both started | ONE shared budget, frontier (Fire) first; P.D. waits | ✅ |
| 5 | ON, Fire caught up + P.D. unstarted | budget flows → P.D. start prefetched ("finish Loki→Daredevil") | ✅ |
| 6 | ON, airdate group | member-sequential (NOT episode-interleave) | ✅ doc corrected |
| 7 | cold frontier member | skipped before group-lead → next member is frontier, not stranded | ✅ |
| 8 | all members cold | nothing flagged, no crash | ✅ |
| 12 | MCU TV mixed owned/unstarted | only in-Sonarr shows; unstarted-owned injected | ✅ |
| 13 | saga member not in Sonarr | invisible to walk (coordinator adds it) | ✅ by-design |
| 14 | movie saga member | invisible to TV walk | ✅ |
| 16 | frontier fully caught up | full budget flows to next member | ✅ |

### Radarr grab (`ensure_owned_and_grab`)
| # | Scenario | Expected | ✓ |
|---|---|---|---|
| 1/2 | not in Radarr (live / dry-run) | add+search / would-add | ✅ |
| 3 | in Radarr + file | already-owned no-op | ✅ |
| 4/5 | in Radarr no file (live / dry-run) | monitor+MoviesSearch / would-search | ✅ |
| 6 | lookup returns different tmdb | skipped (exact-match fail-closed) | ✅ |
| 8 | full + deletions OFF | paused (never strand) | ✅ |
| 9 | under pressure + deletions armed | add monitored, search OFF, queue defer | ✅ |
| 13/14 | anime-instance / other-instance film | found on any instance (dedup) | ✅ fixed (scans all instances) |
| owned-no-file under pressure | search must respect the band | ✅ fixed (pause/defer) |

### Data + plan (`universe_order` / planner)
All 32 verified ✅ — split items rank, unified owned-only/include-unowned, non-timeline skip, stale-source degrade, (media,id) collision distinct, engagement (movie tmdb / show tvdb), start-first ordering, timeline-first merge, raw-row split, group kinds, **mixed-group determinism** (✅ fixed), singleton byte-identical.

### Footgun ledger
| Footgun | Sev | Status |
|---|---|---|
| `group_members` mixed-group order-dependent kind/order | med | ✅ **fixed** — timeline if ANY member indexed (deterministic) + test |
| `ensure_owned_and_grab` owned-no-file search bypassed free-space band | med | ✅ **fixed** — pause/defer like a fresh add + tests |
| `_find_movie_record` scanned default+anime only (multi-instance dup-add) | med | ✅ **fixed** — scans all Radarr instances |
| `ensure_owned_and_grab` FRESH-ADD path trusted `prepare()`'s single-instance check → re-added a film owned on a **different mount** (4K/anime) onto default | med | ✅ **fixed** — all-instance scan hoisted to the front via `_grab_existing`; a film owned on ANY instance is searched in-place, never re-POSTed + 2 tests |
| `acquisition.universe.enabled` TV walk rode the always-on next-ep prefetch → ran even with `acquisition.enabled=false` | high | ✅ **fixed** — `_universe_group_maps` now gates on `acquisition.enabled` AND `acquisition.universe.enabled` (universe is a child of acquisition; master switch off → byte-identical legacy) + test |
| docstrings/doc claimed air-date *episode* interleave | low | ✅ **fixed** — corrected to member-sequential |
| doc footgun #6 claimed unreleased-skip done | low | ✅ **fixed** — corrected (harmless add; pre-filter = P7 refinement) |
| `cold_start` config key inert | low | ✅ **clarified** — coordinator-pending in schema/env_map/doc |
| crossover film double-listed across two universes | med | ⏳ **P7** — coordinator must dedup the plan by (media,id) before grabbing |

### Phase-7 coordinator contract (validated seams)
- **Route by media:** `movie → ensure_owned_and_grab(tmdb)` (ready); `show → a new add-by-tvdb path` (must build, mirrors the movie one) — and the **show-add path MUST replicate the movie free-space band** (`_acquisition_paused` / `_space_band` / `_persist_deferred`) on the **Sonarr** mount; the TV *walk* only prefetches already-owned series, so adding a NEW unowned series is the only place a TV add can pile onto a full TV disk.
- **Cross-mount prefix coherence:** films route to different Radarr instances (1080p/4K/anime = different mounts). Under pressure, one mount can pause while another grabs → the acquired saga can have **timeline holes** (rank 1-2 paused on a full mount, rank 3 grabbed on an open one) rather than a clean start-prefix. The coordinator should order/preview by global saga rank and surface the hole, not silently scatter.
- **Cross-universe ordering under `max_per_run`:** when several engaged universes compete for the same N slots, the inter-universe order is undefined — pick a deterministic policy (e.g. round-robin by frontier rank, or most-recently-engaged first) and `log()` what was dropped.
- **Two independent space disciplines are BY DESIGN:** the TV per-group 3h runtime budget (frontier-first) and the per-Radarr-instance free-disk band are separate — a saga can advance on TV while frozen on movies (or vice versa). This honours the "bypass min_score/max_adds, own cap" decision; it is not a bug, but the preview should make the split legible.
- **Household frontier:** movies via `tautulli/group/<g>/tmdb_completions` (**STRING tmdb keys**, `pct ≥ v['threshold']`); TV via the owned-episode Parquet `is_watched`+`series_id` → tvdb. **Do NOT use `WatchHistoryAggregator.get_all_watched_series()`** (it treats grandparent ratingKey as tvdb — wrong).
- **Dedup the plan** by `(media, id)` across universes (crossover films) before grabbing; **dedup shows** via `in_library` so the coordinator never re-adds/re-searches a series the next-episode walk already owns.
- **Caps:** `max_per_run` bounds the coordinator's NEW backfill grabs (orthogonal to the TV walk's per-episode budget).
- **Gates:** needs BOTH `acquisition.universe.enabled` (acquisition) AND `plex.playlists.universe_timeline.enabled` (membership source); **Phase 3** placement (after the universe cache + `tmdb_completions` + the Sonarr walk); model on `SpaceCoordinatorManager`; route every write through `ensure_owned_and_grab` / Adder / `_trigger_search` (never raw `ArrGateway`); dry-run preview grid.
- **cold_start** + **unreleased pre-filter** land here.

---

## See also
- `space_coordinator.md` — the cross-service Phase-4 capstone this coordinator is modeled on.
- `../acquisition/README.md` — the Radarr add → monitor → search primitives reused here.
- `../plex/playlists/` — `universe_order.py` (the data layer), `builder.py` (membership/source), `combined_builder.py` (the eventual playlist consumer).
