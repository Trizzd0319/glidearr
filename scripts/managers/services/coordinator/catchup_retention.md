# Catch-up (trailing-viewer) retention — engagement-derived, per-saga, multi-tenant

**Goal.** When one household viewer is **behind** on a saga another has finished, never delete the content the trailing viewer still needs to reach. At the critical free-space floor, **downgrade** (shrink) the held portions instead of deleting them. The set of viewers who can block a saga's deletion is **derived from data every run — never hardcoded** — so it self-configures for any **Household** (the shipped product, every deployment).

Status: **Phases 0-2 built + tested** (config knobs + the pure `saga_retention.py` brain + the `SagaRetentionProducerManager` that writes `lifecycle/saga_gates` — read-only + a cache write, no deletes yet, default-OFF). Deletion is destructive + currently ARMED (`deletions_consent=true`, `free_space_limit=2050`); only `dry_run` guards it. Build + verify the remaining (delete-guard) phases entirely under dry-run.

---

## The model
For each saga **S**, the retention **gating set**

> **G(S) = { viewer U : U WATCHED any member of S (any point, any completion) OR U WATCHLISTED any member of S }**

A title **T** in S is **HELD** from deletion until **every still-climbing member of G(S)** has watched T. "Still-climbing" = engaged, hasn't passed T, and not dormant. At the floor (`free < free_space_limit`) held titles are excluded from the **delete** pass but kept in the **downgrade** pass → they shrink, never vanish. A crossover title (in two sagas) is held by the **union** of G(S) across its sagas. **No usernames in config** — G(S) is recomputed each run from observed watch + watchlist data; config holds only tuning knobs.

This is the retention twin of acquisition's frontier: acquisition fills *forward* from who's ahead; retention protects *backward* for who's behind.

---

## Feasibility — verified against code (zero new external fetches)
| Signal | Source | Reliability |
|---|---|---|
| **Per-user WATCH** (movies + TV) | raw `tautulli/history/all` ([watch_history/__init__.py:124](../../machine_learning/../services/tautulli/watch_history/__init__.py)) retains `user_id`+`grandparent_title`+`(season,ep)`+`percent_complete` per row — the **only** per-user-attributable watch source (every aggregate collapses it to a union) | solid; bucket by `user_id`, resolve via the **stable title join** (`genre_affinity.build_library_index`), **never** `rk→tmdb` (dies on Plex re-scan) |
| **Per-user WATCHLIST** | `plex/users/{safe_user}/watchlist` ([watchlist/__init__.py:65](../../services/plex/watchlist/__init__.py)) — already id-resolved to `{tmdb,tvdb,imdb}` | solid; join `safe_user → tautulli_user_id` via `plex/identity_map`. **Do NOT** use `plex/watchlist/union.watchlisted_by` (keyed on display title — drifts/collides) |
| **Saga membership** | `plex/playlists/universe_source` → `saga_member_sets()` (full, ownership-independent) + `build_universe_maps()` (owned reverse lookup) | 16 `UNIVERSE_LISTS` enumerable; curated TV franchises (One Chicago, L&O…) are title-match-only, **owned-only gating** (unowned blind spot — documented) |
| **Per-user MOVIE watched-set** | ❗ **does not exist today** — `_fetch_watch_map` ([movie_files.py:681](../../services/radarr/cache/movie_files.py)) drops the user field | the **one new build** — derive inline in the producer from the raw history cache filtered to `media_type=='movie'` |
| **Trakt watchlist** | account-level single OAuth token | **cannot** be split per-user — exclude from per-user gating, or map the whole account to one configured member |

---

## Membership pipeline (pure)
1. **Saga member sets** — `saga_member_sets(universe_source) → {key: {movies:set(tmdb), shows:set(tvdb)}}` (FULL set, not owned-only — engagement off a since-deleted member still counts).
2. **Owned membership** — `build_universe_maps(...) → {tmdb:set(keys)}, {series_id:key}` (reverse lookup for a delete candidate; keeps **all** keys for a crossover).
3. **Per-user watched sets** — bucket raw history by `user_id` → `{user_id: {movie_tmdbs, show_tvdbs, episode_keys, last_watch_iso_per_member}}`.
4. **Per-user watchlisted sets** — per `identity_map` entry read `plex/users/{safe}/watchlist` → `{tmdb, tvdb}`, key by `tautulli_user_id`.
5. **G(S)** = users who watched OR watchlisted any member of S (parameterizes `universe_acquire_plan`'s engagement from household-union to per-user + per-title).
6. **Per-title hold** — T held until every still-climbing non-excluded gate user has watched T; multi-saga → union.

---

## Per-run data flow
1. **Prereqs warm:** Tautulli history, `plex/identity_map` + per-user tokens, `plex/users/*/watchlist`, `universe_source`.
2. **One producer step** (coordinator pre-pass, after those prereqs, before the space coordinator + before `episode_files` sync stamps the hold): gather inputs once, call `compute_saga_gates(...)`.
3. **Write one de-identified cache key** `lifecycle/saga_gates = {movies:{tmdb:[keys]}, series:{series_id:[keys]}, gate_user_count:{key:int}, computed_at}` — **ids + saga keys + opaque counts only, never usernames** (PII discipline from `watch_history` + `per_user_affinity`).
4. **Consumers read that one key:** Radarr sync stamps `is_saga_held`; Sonarr sync ORs `saga_held` into `all_household_watched`; the coordinator shield reads it directly.
5. **At the floor:** held titles excluded from delete pool, left in the downgrade pass (Stage-1 downgrades already run before deletion, `space_coordinator.py:179-188`) → **downgrade-instead-of-delete is automatic**.

**Locked posture — FAIL-OPEN to no-hold** on any input uncertainty (missing cache / unresolved id / empty source / identity-empty). Deletions are armed; a bug that holds everything strands deletes and fills disk. Never hold *more* than today on uncertainty.

---

## Phases (each independently valuable; all dry-run-verified)
| # | Phase | Seams | Effort |
|---|---|---|---|
| 0 | ✅ **Config knobs** (`saga_retention` block, default OFF → byte-identical) | `onboarding/schema.py` + `env_map.py` + `config.json` | small |
| 1 | ✅ **Pure brain** `saga_retention.py` — `compute_saga_gates(...)` + `saga_member_sets()` beside `unified_universe_order` (19 tests) | `machine_learning/lifecycle/saga_retention.py` + test; `plex/playlists/universe_order.py` | large |
| 2 | ✅ **Per-user producer + the new movie watched-set** — `SagaRetentionProducerManager` buckets raw `tautulli/history/all` by `user_id` (watched≥thr vs started-in-grace, title→id via owned inventory), joins per-user watchlist via `tracked_users`/`identity_map`, calls the brain, writes `lifecycle/saga_gates`; runs after `plex.run_reconcile()`, before the space coordinator; fail-open; 9 tests | `coordinator/saga_retention_producer.py` + `main.py` | large |
| 3 | **Movie hold (Radarr)** — `is_saga_held` column + `continue` guard in `build_movie_delete_candidates` AND coordinator `build_delete_candidates`; leave downgrade untouched | `radarr/cache/movie_files.py`, `space/delete_planner.py:88`, `radarr/quality/space_pressure.py` | medium |
| 4 | **Coordinator shield** (2nd line) — extend `_shield_protected_picks` to drop saga-held ids | `coordinator/space_coordinator.py:355-393` | small |
| 5 | **Episode hold (Sonarr)** — OR `saga_held` into `all_household_watched` (move all 3 readers together: grace :2370, delete :3045, guards :88); keep memberless household hold as coexisting fallback | `sonarr/cache/episode_files.py`, `classification/guards.py:83`, `lifecycle/grace_policy.py:85` | medium |
| 6 | **Velocity persistence** (optional precision) — persist `household/saga_position/<user_id>/<saga_key> → {furthest_rank, last_watch_iso}` (by stable id, rank fresh) → "advancing" = recent watch AND rank increased | NEW writer in the Phase-2 producer | medium |
| 7 | **Movie-grace symmetry + tests + dry-run verify** on the concrete scenario | `grace_policy.py:67`; new test modules | medium |
| 8a | **"Use it or lose it" boost** — gate emits per-viewer expiring-soon ids; new boost tier in `order_items` lifts them to the top of that viewer's playlists in the final `expiry_boost_days` | `machine_learning/playlists/ordering.py:125` (new `expiry_boost`/tier, like `resume_boost`); producer emits `saga_gates.expiring_by_user` | medium |
| 8b | **"Leaving Soon" surfacing (BOTH)** — (i) shared Movies+TV collections of the household-union expiring set, additive, promoted FIRST on Home; (ii) per-user "Leaving Soon" playlist (each viewer's own set) pinned first via per-server token; create-if-non-empty, tear down when empty | `plex/collections` (new writer), `plex/instances/api.py` (`create_collection`/`add_collection_items`/`promote_collection_home` exist; ADD `move_managed_hub`), `plex/playlists/writeback.py` (per-user "Leaving Soon" playlist) | large |
| 9 | **Multi-user Trakt (optional)** — onboarding asks "set up Trakt for multiple household accounts?"; if yes collect a token per profile (cache is already per-user `trakt/{user}/watchlist/*`) and gate per-user; if no, map the single account to owner | `trakt/instances` + `trakt/api` (multi-token), `trakt/watchlist/__init__.py:23` (already per-user keyed), onboarding | large |

---

## Edge cases (do-not-miss)
- **Identity misjoin (highest correctness risk):** watch keys on Tautulli `user_id`/friendly-name; watchlist on Plex display title. Route **both** through `identity_map` or one human becomes two gate members and the gate **never releases** → disk-fill. Reuse `genre_affinity`'s user_id-first join.
- **Not-owned / since-deleted member:** match watched/watchlisted ids against the **full** `saga_member_sets`, not owned-only, or engagement silently drops.
- **rating_key churn:** stable title / `grandparent_title+(season,ep)` join only; never `rk→tmdb`.
- **Dormant ghost viewer:** someone who watched Avengers years ago must be **dropped via `dormancy_window`** or MCU is held forever. Finite default mandatory.
- **Watchlist-only member (intent, watched nothing):** in G(S) but has passed no title → would hold the **entire** saga indefinitely. Needs `watchlist_hold_policy` (windowed) and/or "hold only the prefix up to the watchlisted title."
- **Watchlist removed:** snapshot-diff (`plex/watchlist/snapshot/{ts}`), but only a successful non-empty fetch that drops a title counts as removal (not a transient preserve-prior). Errs toward hold.
- **Crossover title:** held by the union of G(S) across its sagas.
- **ANY-play vs threshold:** a 2% accidental play marks "engaged" under ANY-play. `tmdb` keys are **strings** in `tmdb_completions` but **ints** in member sets — normalize before intersecting.
- **Unowned saga show / curated TV franchise:** invisible to the Sonarr watch signal (no `is_watched` row); curated franchises are title-match-only → blind to unowned members. Under-retention there is acceptable per fail-open; document.
- **Empty/stale `universe_source`:** degrade to **no hold** (byte-identical), never error, never hold-everything.
- **PIN-skipped / scope-failed user:** "no watchlist signal" = "did not watchlist" (never "watchlisted everything"); on full scope-fail fall back to the watch-only half.
- **`all_household_watched` shared by 3 readers** (grace/delete/guards): move all together or a stale reader re-exposes a held file.
- **Downgrade reachability:** a held high-watchability title is protected from downgrade (`WATCHABILITY_PROTECT_THRESHOLD`) AND delete → immovable. Ensure `space_pressure_downgrade_before_delete` widens the band so held titles are downgrade-reachable; verify floor math when many exist.
- **Episode downgrade = unmonitor + re-grab** (not in-place transcode): confirm the step-down pass handles episode files before promising the TV downgrade valve (movie downgrade is proven).
- **Stale-score ordering** (known latent bug): saga holds ride the same pass; validate the held set is computed from fresh inputs.

---

## Config knobs (all tuning — membership is data-derived)
- `saga_retention.enabled` (default **false** → byte-identical)
- `saga_retention.dormancy_window_days` (default **90** — drop a member from G(S) after no saga activity for N days; the primary disk-safety knob; never infinite)
- `saga_retention.completion_threshold` (default **0.8** — a ≥80% play is a "meaningful watch" = engaged)
- `saga_retention.engagement_grace_days` (default **7** — a STARTED but sub-threshold play still counts as engaged for N days, so a kid/work/life interruption mid-watch doesn't drop the viewer; expires if never completed, so a 2-minute accidental play doesn't pin forever)
- `saga_retention.watchlist_hold_policy` (default **windowed** — a watchlist-only member who never starts is expired after the dormancy window)
- `saga_retention.expiry_boost_days` (default **30** — in the final N days before a held title is released from a viewer's gate, lift it to the TOP of that viewer's playlists: "use it or lose it". A month gives a relaxed runway rather than a rushed final week; with a 90-day dormancy this covers the last third of the hold)
- `saga_retention.downgrade_at_floor` (default **true**)
- `saga_retention.exclude_users` (subtraction only — e.g. a kiosk profile; not a membership source)
- `saga_retention.quorum {enabled:false, fraction:1.0}` (optional escape valve; active-member gating supersedes it for saga paths)
- `trakt.multi_user` (onboarding-set: when **true**, collect a Trakt OAuth token per household profile → per-user Trakt watchlist gating, mapped to the profile that owns it; when **false**, the single account maps to the **owner**)

### Engagement bar (locked)
A viewer is **engaged** with saga S when, for any member of S, EITHER: a play reached `completion_threshold` (meaningful watch), OR a play **started** within the last `engagement_grace_days` (in-progress; gives a real interruption a week to finish). A watchlist entry also engages (windowed). Provisional (in-progress) engagement that never completes expires after the grace window — so accidental brief plays don't arm a multi-run hold.

### "Use it or lose it" — boost + a "Leaving Soon" collection (locked)
The gate emits, per viewer, the held titles whose release is within `expiry_boost_days` (dormancy about to expire, or a windowed watchlist intent about to lapse) → `saga_gates.expiring_by_user`. Surfaced two ways:

**(a) Per-user playlist boost** — the playlist ordering lifts those to the **top** of that viewer's playlists via a new boost tier (above `resume_boost`), the true per-viewer sightline.

**(b) "Leaving Soon" collection** (`saga_retention.leaving_soon_collection`, default on when the feature is enabled) — a dedicated Plex collection of the expiring titles, surfaced **first** on the Glidearr/Plex Home:
- **Title** configurable (`leaving_soon.title`, default "⏳ Leaving Soon"). **Only created if non-empty**; torn down / unpromoted when it empties (idempotent each run).
- **Additive** — members are also kept in their pre-existing saga/franchise collections (`add_collection_items` never removes from others; we never `remove_collection_item` from the saga collections).
- **Mixed media** → one collection in the **Movies** section (`item_type=1`) + one in the **TV** section (`item_type=2`); both promoted.
- **First in line** → `promote_collection_home(..., home=true, shared=true)` + a new `move_managed_hub(section_key, hub, position=0)` API (small addition — Plex has no reorder method today) to pin it to the top; falls back to best-effort promotion order if the move endpoint is unavailable.
- **Surface = BOTH (locked):** a **shared** household collection (union of all viewers' expiring titles) promoted first on Home, AND a **per-user** "Leaving Soon" playlist (each viewer's own expiring set) pinned first, written with each user's per-server access token via the existing writeback path. The shared collection is the at-a-glance household view; the per-user playlist + the 8a boost are the precise per-viewer sightline.
- Dry-run writes nothing; logs the would-create/promote/write.

---

## Decisions (locked 2026-06-20) + risks
- **Engagement bar:** meaningful watch `≥0.8` OR started within `engagement_grace_days` (7); watchlist engages too.
- **Watchlist-only:** windowed — expires if the viewer never starts within the dormancy window.
- **Dormancy:** `90` days, with a final-**month** (`expiry_boost_days=30`) **"use it or lose it"** playlist boost as it approaches (a week felt rushed).
- **Trakt:** per-user via onboarding (multi-account → map each watchlist to its profile); else map the single account to **owner**.
- **Still-open (can default):** curated TV franchises gate owned-only (documented blind spot); velocity starts as a flat recency window (Phase 6 rank-aware is the later precision upgrade).

**Top risks:** (1) disk-fill regression from too-long dormancy / indefinite watchlist holds / ghost viewers — mitigate with finite defaults + windowed watchlist + default-OFF + dry-run; (2) identity misjoin → gate never releases; (3) movie hold is the first ever to fire — validate the floor/downgrade interaction under dry-run; (4) fail-CLOSED footgun strands deletes — fail-OPEN everywhere; (5) PII — ids + counts only in the cache.

---

## See also
- `space_coordinator.md` — the deletion-owning capstone + the floor/U band + Stage-1 downgrades.
- `universe_acquisition.md` — the acquisition twin (frontier = who's ahead; this = protecting who's behind).
- memory: `trailing-viewer-retention-state`, `size-anomaly-and-backup`, `radarr-stale-score-ordering`.
