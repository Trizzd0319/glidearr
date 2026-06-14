# DESIGN — Per-User Personalized Plex Playlists

The first-ever **WRITE** into Plex. Pure ordering/ranking lives in
`machine_learning/playlists/` (brain_purity-enforced); all HTTP/write lives in
`services/plex/playlists`. Opt-in (`plex.playlists.writeback.enabled`, default
false) **AND** dry_run-gated. Inert-by-default: stock config plans, previews, and
writes **nothing**.

> **STATUS (2026-06-12).** Produced by a 17-agent design+red-team workflow (recon →
> ideate → design → 5-lens red-team, 67 weaknesses found, 34 `must_fix`). The
> red-team proved the *grand* design rests on data that does not exist yet (see §4).
> So this is built as honest, gated increments — not the original blind 14-PR march.
>
> **DONE — PR-1: the pure ordering engine** (`machine_learning/playlists/`, 31 tests,
> brain_purity + secret_scan green). Every ordering `must_fix` is baked in.
> **NEXT** is data-foundation work (§4) + live-PMS decisions (§9), not more ordering.

---

## 1. Vision & taxonomy (Hard-req #2, #3)

Per-user playlists, one Plex playlist object per family per profile (Plex playlists
can't nest or hold a *show* object — a "show acquired" is its episodes, §3):

- **A · Up Next** (whole owned/acquired library): everything the user hasn't
  watched, cross-group-ranked by watchability. Covers the *entire* library, not just
  recent adds; a size cap + group-atomic rotation surfaces the whole catalog over
  time.
- **B · Fresh Arrivals** (recently acquired): items added within
  `acquired_window_days`, ranked by **recent** watch-history recency-decay.
- **C · Finish the Saga** (opt-in): franchises/universes/series the user partially
  watched, seeded at the next unwatched entry — the showcase for the timeline sort.

**Combine vs split:** A and B stay **separate** — different intent (browse catalog
vs what's new) and different sort key (watchability vs recency-decay); merging buries
new arrivals. They share 100% of the build/order/diff/write machinery; only the
candidate filter + cross-group sort key differ. C is a specialization of A.

---

## 2. THE ORDERING ALGORITHM (Hard-req #4) — **built** in `machine_learning/playlists/`

Pure, deterministic pipeline (`ordering.order_items`):

1. **Drop watched** (Hard-req #5 per-user filter).
2. **Group** by *connected components* over shared series/franchise/universe
   (`grouping.group_items`, union-find). Items sharing **any** affinity — directly or
   transitively — stay contiguous. *This replaces the naive "first universe label",
   which sorted alphabetically and scattered partial-overlap universes (red-team
   CRITICAL).*
3. **Order within a group** by spoiler-safe timeline (`timeline.order_within_group`):
   - A series is a **track** ordered by `(season, episode)` — **never** air date — so
     a NULL/out-of-order air date can't float a later episode ahead of an earlier one
     (the #1 spoiler trap). A series stays **atomic** (never split across a film).
   - Tracks (each series, each movie) interleave by lead chronological date
     (theatrical/air); an explicit `timeline_index` (future curated saga order) wins.
   - Specials (season 0) sink to the track tail; missing dates sink to the group tail.
4. **Rank groups** by watchability — a group is as compelling as its strongest still-
   watchable entry (max score), with optional **per-medium percentile normalization**
   so a movie score and a (different-scorer) show score are comparable across groups.
5. **Group-atomic size cap** (`caps.apply_size_cap`) — whole top-ranked groups until
   the cap; truncation is always counted, never silent.
6. Emit `PlaylistItemPlan`s with ordinal + rationale + a coverage stat.

**Spoiler invariant** (`spoiler.is_spoiler_safe`) is property-tested over input
permutations. Every tie ends in `(size, lead date, title, key)` → input-order
independent, golden-corpus-pinnable.

---

## 3. New features introduced (beyond the ask)

- **Spoiler-safe topological ordering** — the correctness backbone (built).
- **Episode-expansion guardrail** — show = episodes, always capped (`expansion.py`,
  built) so a 20-season library can't explode a playlist.
- **Per-medium score normalization** — fixes movie-vs-show comparability (built).
- **Per-user re-rank + cold-start ladder** — household score tilted by *that user's*
  affinity, default `personal_tilt_cap=0` (byte-identical until opted in). *(brain
  hook ready; needs the per-user watched/affinity feed — §4.)*
- **Per-profile kids-safe cert gating** — a positive allow-list; **NULL cert fails
  CLOSED** (excluded) so a missing rating never leaks adult content to a kid.
- **Dry-run preview grid** — per user: exact ordered contents + per-item rationale +
  a graceful-degradation coverage stat (what % grouped by real tag vs fallback).
- **Idempotent diff-based replace + find-or-create + snapshot/restore + audit +
  kill-switch / per-user opt-out / write-capability probe** — the safety spine.
- **Recency-decay re-rank** (Family B) and **group-atomic weekly tail rotation**
  (whole-library-over-time), both default-inert ramps.

---

## 4. DATA FOUNDATION REALITY — what must exist before TV / writes (red-team CRITICALs)

The ordering engine is done and correct, but the *inputs* it needs are partly
missing. **None of these are ordering bugs — they are upstream data gaps.**

| Gap | Reality (verified) | Impact |
|---|---|---|
| **Full episode inventory** | `episode_files.parquet` is **pruned** to pilots + watched + next-unwatched per series (`_remove_orphan_rows`). | "ALL owned unwatched episodes" (Family A, TV) has no source. **TV blocked.** |
| **Per-episode tvdb id** | Not persisted on episode rows (only `series_id`, season, episode). | Can't build the `{tvdb}:{s}:{e}` → Plex ratingKey join. **TV blocked.** |
| **Per-user watched set** | Only `tautulli/group/<group>/tmdb_completions` — per-**group**, movie-only. | Hard-req #5 "this user hasn't watched" is group-granular for movies, **absent for TV**. |
| **Episode ratingKey map** | No episode-level Plex scan; episode GUIDs often lack tvdb on list endpoints; section scan caps at 40k/section. | Weak/missing join; silent drops on large libraries. |
| **Franchise tag (TV)** | No `franchise` column / ingestion anywhere (Kometa `<<key>>` not ingested). | TV franchise grouping degrades to per-series (engine handles it; coverage stat surfaces it). |
| **Watchability per item** | Per-**series** broadcast (every episode shares one score), often NULL if `refresh_scores` didn't run. | Cross-media group ranking needs normalization (built); NULL handled. |

**Consequence:** the realistic first *shipping* surface is **movies-only** — `movie_files.parquet`
is a full owned inventory with universe tags, dates, and (household) watched data.
TV requires building the inventory + per-episode id + per-user watched prerequisites
first.

---

## 5. Architecture & file map

**Brain (built, pure, brain_purity-guarded):** `machine_learning/playlists/` —
`models.py` (PlaylistInput / PlaylistItemPlan / PlaylistPlan), `grouping.py`,
`timeline.py`, `spoiler.py`, `expansion.py`, `caps.py`, `ordering.py`.

**Service (TODO):** `services/plex/instances/api.py` += `get_machine_id` +
`create_playlist` / `add_items` / `remove_item` / `move_item` (note: items go in the
`uri` **query param**, *not* a JSON body — red-team CRITICAL); `services/plex/playlists/`
build→brain→diff→apply (dry_run-gated); `services/plex/libraries` += episode-level
`plex/ratingkey_map`; `main.py` Phase-3 writeback pass after acquisition.

**Boundary:** the service resolves external_id→ratingKey, attaches the opaque
`rating_key` + per-user `watched` + `score`, calls `order_items` (pure), diffs vs
live, logs the preview grid, and only then (enabled ∧ not dry_run) writes.

---

## 6. Weakness register — `must_fix` status (34 found)

**Fixed in PR-1 (ordering):** alphabetical multi-universe scatter → connected
components; missing `release_date` column → caller-supplied chrono date + year
fallback; NULL-date spoiler → `(season,episode)` authoritative; cross-media score
incomparability → per-medium normalization; episode explosion → expansion cap;
silent cap truncation → counted.

**Must fix in the SERVICE/data layer before any write (open):**
- **Gating defects (CRITICAL, pre-existing):** `main.py` reads `dry_run` defaulting
  to **False** when the key is absent; and `_cap_enabled` checks `plex.<cap>.enabled`
  **one** level deep, mismatching the `plex.playlists.writeback.enabled` two-level
  key. → The write path must read `dry_run` default **True**, use a dedicated
  fail-closed gate (`enabled ∧ not dry_run`), and not piggyback `_cap_enabled`.
- **PIN/token scrub gap (CRITICAL):** `register_secrets` drops values < 8 chars and
  `pin=` isn't in the logger scrub patterns → a 4-digit PIN can leak. (The onboarding
  `_redact` already scrubs `pin=`; the **runtime logger** must too.)
- **Plex write reality:** items via `uri` query param (not JSON body); managed/child
  profiles may not own playlists via a switched token (capability-probe + skip+count);
  no bulk-reorder verb (per-item `move` by playlistItemID); account-token write-scope
  on the local PMS unverified; single `machineIdentifier` assumption vs multi-PMS.
- **Safety:** find-or-create by non-unique title can adopt a hand-made playlist →
  require the cached managed anchor + a created-by-us marker; non-transactional
  partial-failure → snapshot before write, stop+report, restore.
- **Cross-user misattribution:** never fall back to the `owner` token for a user whose
  mint failed — skip+count instead.

---

## 7. Phased roadmap (corrected)

- **PR-1 ✅ DONE** — pure ordering engine + 31 tests + guards.
- **PR-2** — `PlexAPI` write verbs (uri query-param create/add/move, `get_machine_id`),
  mocked-session tests; unreferenced until wired.
- **PR-3** — movie `plex/ratingkey_map` (tmdb→rk); gated, unresolved counted.
- **PR-4** — **movies-only** candidate builder + `order_items` wiring + dry-run preview
  grid (no writes). The first end-to-end, fully observable, zero-write slice.
- **PR-5** — write path: find-or-create + diff + snapshot/restore + the fail-closed
  gate (fixes the two gating CRITICALs) + capability probe + audit. Movies only.
- **PR-6** — logger `pin=`/short-token scrub hardening (CRITICAL, can land anytime).
- **PR-7+** — TV data prerequisites (full episode inventory, per-episode tvdb,
  per-user watched set, episode ratingKey map), then TV candidates, then Families B/C,
  rotation, per-user re-rank ramp, kids cert gating.

---

## 8. Config surface (all default-inert)

`plex.playlists.writeback`: `enabled=false` (master), `families={up_next:true,
fresh:true, saga:false}`, `max_items=300`, `acquired_window_days=45`,
`acquired_half_life_days=0` (0⇒inf⇒byte-identical), `personal_tilt_cap=0`
(0⇒household-only⇒byte-identical), `include_specials=false`,
`episode_mode=next_unwatched_n`, `episode_cap=25`, `exclude_users=[]`,
`title_prefix="Glidearr"`, `cert_ceilings={kids:[g,pg,tv-g,tv-y,tv-y7]}` (NULL
cert ⇒ excluded).

---

## 9. Open questions (need the operator / a live PMS probe)

1. **Playlist ownership** — does `POST /playlists` with a managed user's switched
   token create a playlist that user actually sees? (Probe on the operator's PMS.)
2. **Reorder verb** — does the PMS support `PUT …/items/{id}/move?after=`, or fall
   back to remove+add?
3. **GUID coverage** — what fraction of the library exposes tmdb/tvdb on list
   endpoints? (Sets a threshold for enabling writeback; the coverage stat reports it.)
4. **Per-user watched source** — Tautulli per-user history (cheap, 24h stale) vs Plex
   `viewCount` via per-user token (authoritative, costly). MVP: Tautulli.
5. **Home-screen reality** — playlists are reliably visible in the user's *Playlists*
   hub; pinning to the literal top "home" row is not API-controllable per-user.

---

## 10. Testing

Brain (done): grouping precedence/contiguity/coverage; timeline NULL/out-of-order/
specials/mixed-media/explicit-index/determinism; spoiler property over permutations;
ordering ranking + group-atomic cap + per-medium normalization + input-order
independence; expansion cap. Service (TODO, mocked PlexAPI): uri format, machine_id,
find-or-create no-dupe, managed-only assertion, dry_run⇒zero writes (call-log),
snapshot→restore, capability-probe skip, token/PIN never in any cache/log.
Every PR: brain_purity + secret_scan + 3-lens review.
