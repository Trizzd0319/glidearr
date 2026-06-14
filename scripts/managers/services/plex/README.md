# PlexManager

- **File** — `scripts/managers/services/plex/__init__.py`
- **One-liner** — Optional, NON-critical top-level service manager that adds the signals only Plex has natively — the **multi-user account watchlist** (the strongest explicit forward-intent / next-watch signal) and the **Plex-Home-user ↔ Tautulli-user ↔ rating_groups identity crosswalk** — caching them for the next-watch ranker and the acquisition pipeline, while owning none of the curation authority. **FETCH/CACHE-only in v1** (no write-backs). Full design spec: [`DESIGN_plex_service.md`](DESIGN_plex_service.md).

## What it does (for a senior Python engineer)

`PlexManager(BaseManager, ComponentManagerMixin)` is a top-level service manager constructed by `Main` in `scripts/main.py` **immediately after Tautulli and before Trakt** (so its identity crosswalk can read the warm Tautulli user list / `rating_groups`, and its watchlist union lands before the last-phase acquisition reads it). It is a process-wide `BaseManager` singleton and structurally mirrors `TautulliManager` (uses `split_components(...)`, not `load_components`).

Plex is **OPTIONAL + NON-critical**, exactly like MAL: when it is unconfigured (`self.configured = False` when there is no `plex.plex_token`), unreachable, or its account token fails the scope probe, it self-disables / degrades and the run continues. It is deliberately left OUT of `Main._validate_managers` — a Plex-less or scope-failed install must still complete.

**Manager-tree position.** Parent in the runtime tree is `Main`. Like the other top-level service managers (`TautulliManager`, `MALManager`), its inferred `parent_name` resolves to `"Services"` (the `plex` folder is not in `base_manager._infer_parent_from_path`'s recognized-service set) — this is identical to MAL and is cosmetic only: every submanager registers under its own class name and siblings are resolved by class name (see below), so functionality does not depend on it.

**Submanagers it loads** (each in a sibling subdirectory, its own work item):

| Attribute (`self.…`) | Class | Phase | Critical? |
|---|---|---|---|
| `plex_api` | `PlexAPI` | P0 | — (HTTP handle, not a submanager) |
| `users` | `PlexUsersManager` | P0 | yes |
| `metadata` | `PlexMetadataManager` | P0 | yes |
| `watchlist` | `PlexWatchlistManager` | P1 | yes |
| `on_deck` | `PlexOnDeckManager` | P2 | no (default-off, A/B-gated) |
| `ratings` | `PlexRatingsManager` | P2 | no (default-off) |
| `libraries` | `PlexLibrarySectionsManager` | P3 | no (reconcile, default-off id-scan) |
| `collections` | `PlexCollectionsManager` | P4 | no (default-off) |
| `playlists` | `PlexPlaylistsManager` | P4 | no (default-off) |
| `validator_manager` | `PlexValidatorManager` | — | no (stub `validate()->True`) |

`critical_keys = {users, metadata, watchlist}` — the irreducible v1 (P0+P1). `prepare()` eagerly loads every declared component and logs one summary line; everything beyond the irreducible set is enrichment that layers on top and is default-off in scoring.

**Sibling resolution convention.** A submanager that needs a sibling (e.g. the watchlist fetcher needs the GUID resolver and the tracked-user roster) resolves it with `self.registry.get("manager", "PlexMetadataManager")` / `"PlexUsersManager"` — the **same registry-by-class-name pattern** Tautulli/Radarr/`base_manager.get_tag_monitor` use. Plex never relies on `self.manager.<attr>` for siblings.

**API client.** The HTTP handle is **`plex_api`** (never a generic `api`), matching `sonarr_api`/`radarr_api`/`tautulli_api`/`trakt_api`. `PlexAPI` is re-exported through `plex/api.py` from the canonical `plex/instances/api.py`. It has Plex-specific hardening because plex.tv/Discover are external + rate-limited (unlike the LAN PMS): a shared keep-alive `requests.Session`; transient-timeout retry with a fresh session (mirrors `TautulliAPI`); **HTTP-429 + Retry-After backoff capped at 30 s** and a sliding-window throttle for external calls (mirrors Trakt); a **stable `X-Plex-Client-Identifier`** (v2 endpoints 401 silently without it); TLS verification always on; and **query-string scrubbing of every logged URL** (`X-Plex-Token` is a URL param on Discover).

**FETCH / CACHE / APPLY.** FETCH + CACHE only — no APPLY in v1. It issues HTTP GET/POST against the local PMS, plex.tv account v2, and the Discover provider, and writes derived dicts to the global cache. `dry_run` is captured and threaded through `init_args` so any *future* write (a "Recommended Next" collection/playlist write-back) is gated from day one, but today it gates nothing.

**Endpoints touched** (UNSTABLE = community-documented; STABLE = local PMS):
- **STABLE (local PMS, `X-Plex-Token`)** — `/identity` (reachability + PMS version), `/library/sections`, `/library/sections/{key}/all` (**`includeGuids=1`** so modern `plex://` items carry external ids), `/library/metadata/{rk}`, `/library/onDeck` (`includeGuids=1`), `/library/collections` + `/children`, `/playlists` + `/items`, `/status/sessions`.
- **UNSTABLE (plex.tv account v2)** — `GET /api/v2/user` (token-scope probe, the HARD gate), `GET /api/v2/home/users` (Home enum), `POST /api/v2/home/users/{uuid}/switch` (per-user token mint).
- **UNSTABLE (Discover)** — `GET discover.provider.plex.tv/library/sections/watchlist/all` (**`includeExternalMedia=1`**; the old `metadata.provider.plex.tv` path 404s), `GET discover.provider.plex.tv/library/metadata/{rk}` (bare-`plex://` resolution hop).

**Config keys read.**
- `plex` — flat block `{url, port, plex_token, plex_media_path, client_identifier, pins, <cap>.enabled, watchlist.snapshot_retention}` (NOT the nested `{"default": {...}}` shape Tautulli uses). `client_identifier` is generated + persisted on first configured run; `pins` is the nested `{title: {"pin": …}}` secret map for PIN-protected profiles. Per-capability opt-in flags (`on_deck`/`ratings`/`reconcile`/`collections`/`playlists`/`sessions`) default OFF; only `users`/`metadata`/`watchlist` run by default.
- `rating_groups` — household grouping for the identity crosswalk; a memberless group is a household-wide wildcard (defaults to `{"household": {}}`).
- `radarr_instances` / `sonarr_instances` — to build the imdb→tmdb / tvdb→tmdb resolver bridges and to UNION the *arr id-sets for reconcile.

**global_cache keys written.**
- `plex/run_stats` — `{configured, enabled, scope_ok, pms_version, users_tracked, users_pin_skipped, watchlist_items, watchlist_users, guid_network_hops, calls_made}` (Main's run-summary reads this).
- `plex/users` — PII-minimized Home roster (`{uuid, title, is_admin, is_managed, protected, token_scope_ok}` — **NO email, NO token**).
- `plex/identity_map` — `{plex_uuid: {tautulli_username, tautulli_user_id, rating_groups, matched_via, safe_key}}` (the join table; email is used in-memory only).
- `plex/guid_map` — append-only `{raw_guid: {tmdb, tvdb, imdb, resolved_via, ts}}` (id mappings are immutable, so a resolved guid — or a *confirmed* Discover miss — is cached forever and never re-hopped).
- `plex/users/<safe>/watchlist`, `plex/watchlist/union` (the flagship next-watch signal + acquisition feed, retaining per-user `watchlisted_by` attribution), `plex/watchlist/snapshot/<ISO-ts>` + `plex/watchlist/snapshots_index` (retention-bounded, for forward validation).
- `plex/users/<safe>/on_deck` + `plex/on_deck/union` (P2); `plex/users/<safe>/ratings` (P2); `plex/sections` + `plex/library_ids` + `plex/reconcile/{orphans,missing}` (P3); `plex/collections/{index,membership_by_tmdb,completeness}` + `plex/playlists/index` (P4); `plex/sessions` (1-call diagnostic); `plex/debug/unresolved_guids`.
- **`plex/users/<u>/token` — FORBIDDEN.** Minted tokens live only in an in-memory dict; this key is never created.

**global_cache keys read.** `radarr.movies.standard.full` (+ per-instance variants) for the imdb→tmdb bridge; the Sonarr series cache (via the registered `SonarrCacheSeriesManager`) for the tvdb→tmdb bridge; the *arr libraries (via `ArrGateway`) for the zero-API reconcile set-diff; `plex/watchlist/union` (playlists dedup).

**Security posture (non-negotiable, post-incident).** Per-user minted tokens are **IN-MEMORY ONLY** and each is registered with the logger scrubber the instant it is minted; a `pin=` redaction pattern is in the logger; per-user caches drop email + token; usernames flow through `cache/key_builder._sanitize_part` (traversal-safe) and a **collision-safe per-uuid map** guarantees two display names that sanitize identically never share a token/cache key (fail-CLOSED attribution); snapshots are retention-bounded; TLS is never disabled; logged URLs are query-scrubbed.

**Singleton / concurrency notes.** Standard `BaseManager` singleton. `PlexAPI` uses one shared session and a lock-guarded sliding window for external calls. The per-user token table and the in-memory tracked roster are shared by reference (not per-user manager instances — per the singleton footgun, per-user state lives in the DATA, never in per-user instances).

## How it functions

**Lifecycle.**
1. `__init__` — `BaseManager.__init__` (inject deps, self-register), capture `dry_run`, read the flat `plex` block, set `self.configured`, ensure + persist a stable `client_identifier` (only when configured, so a Plex-less `config.json` is untouched), build `PlexAPI`, allocate the shared in-memory `user_tokens` dict, assemble `init_args`, declare `component_dependencies`/`all_component_classes`/`critical_keys`, run `split_components(...)`.
2. `prepare()` — eagerly load every submanager via `_load_component`, then a one-line summary.
3. `run()` — **PASS 1** (inventory/identity/watchlist), called in Phase 2 before `trakt.run()`.
4. `run_reconcile()` — **PASS 2** (zero-API set-diff), called in Phase 2 after Radarr + Sonarr populate their libraries.

**`run()` (PASS 1) control flow** — every sub-pass wrapped in `try/except` so one failure never aborts the run; capabilities beyond watchlist are gated:
1. **Reachability** — `_is_reachable()` (local-PMS `/identity`), capturing the PMS version. Non-fatal: an unreachable PMS just skips the local-PMS passes.
2. **`metadata.prime()`** — load the persistent `guid_map` + build the imdb→tmdb / tvdb→tmdb bridge dicts once (cache-only, zero-API).
3. **`users.run()`** — the scope probe (`/api/v2/user`, the HARD gate) → Home enum → per-user token mint (in-memory; PIN-protected profiles need a configured PIN or are skipped+counted) → identity crosswalk → PII-minimized persist. Sets `self.account_scope_ok`.
4. **`watchlist.run()`** (if `account_scope_ok`) — per-user Discover watchlist (paged, GUID-resolved) → household union with attribution → timestamped snapshot. Preserves the prior good union on a transient all-fail (fail-closed).
5. **on_deck / ratings / collections / playlists / sessions** — each behind its `plex.<cap>.enabled` flag (and `reachable` for the local-PMS ones), default-off.
6. **`metadata.flush()`** — persist the merged `guid_map` exactly once; write `plex/run_stats`.

**`run_reconcile()` (PASS 2)** — `libraries.run_reconcile()` diffs the resolved Plex id-set (built during PASS 1 when `plex.reconcile.enabled`) against the UNION of all Radarr/Sonarr instance id-sets → `plex/reconcile/{orphans,missing}`. **Diagnostic only** — orphans never auto-feed deletion.

**Two-tier GUID resolution** (`PlexMetadataManager.resolve`) — persistent memo → FREE parse of the external `Guid[]` array / the raw guid (incl. legacy `com.plexapp.agents.*`) → bridge (imdb/tvdb → tmdb) → PAID Discover hop only for a bare `plex://`. The paid hop fires at most once per run per `rating_key` (killing the per-household-user multiplier) and a *confirmed* miss is memoized so it never re-hops on a later run; a *transient* hop failure stays retryable.

**Delegation to a brain.** `PlexManager` makes no value judgement — it FETCHes and CACHEs. The next-watch ranker (`machine_learning/next_watch/`, in the brain-purity guard's `_GUARDED_SUBPACKAGES`) reads `plex/watchlist/union` as a pure top-weighted feature; the deterministic A–G scorecard remains the curation authority. Acquisition reads the union via `candidates._plex()` at source-score `plex_watchlist = 100`.

## Criteria & examples

- **Token-scope gate.** `GET /api/v2/user` returns non-200 → `account_scope_ok = False`; the per-user surface self-disables (owner-only roster recorded, watchlist pass skipped), `run_stats.scope_ok = False`, and the run continues. Never falls through to a broader-scope attempt.
- **Self-disable.** No `plex.plex_token` → `enabled = False`; `run()` writes a disabled `run_stats` and returns. No `config.json` pollution, no crash.
- **PIN handling.** A PIN-protected profile with no configured PIN is skipped and counted in `run_stats.users_pin_skipped` — the union shrinks *visibly*, never silently.
- **Collision-safe attribution.** Home profiles `"Rob/Kids"` and `"Rob:Kids"` both sanitize to `Rob_Kids`; the per-uuid map disambiguates the second to a uuid-suffixed key, so each member keeps a distinct token slot and `plex/users/<safe>/…` namespace (user A is never served user B's watchlist).
- **Watchlist host/params.** The watchlist hits `discover.provider.plex.tv/library/sections/watchlist/all?includeExternalMedia=1` — the deprecated `metadata.provider.plex.tv` watchlist path 404s, and `includeExternalMedia=1` is what makes each item carry the external `Guid[]` so its tmdb/tvdb/imdb resolve and de-dup cleanly against Trakt/MAL.
- **Paging.** The early-stop uses the grand `totalSize` only (never the per-page `size`); when `totalSize` is absent it falls through to the empty-page terminator, so a >100-item watchlist is never silently truncated to one page.
- **imdb-bridge example.** A watchlist movie arriving with only `imdb://tt0111161`: resolved to `tmdb 278` via the `radarr.movies.*.full` imdb→tmdb bridge; `resolved_via = "bridge_imdb"`.

## In plain English

Think of Plex as the household's "want-to-watch" corkboard plus the family name-tags. `PlexManager` is the assistant who, once per run, reads everyone's corkboard — *each* family member's, not just the account owner's — and writes a tidy combined list of "things this household actually said it wants to watch," noting who pinned each one. It also matches up each Plex profile with the matching Tautulli viewer and household group, so every per-person Plex signal can join the rest of the app's picture. It reads only — it never adds, deletes, or rates anything. Those lists are handed to the part of the app that decides what to download next and what to recommend. The assistant is careful with keys to the house: each person's access pass is held only in memory for the run and shredded after, never written down, and two people with confusingly-similar names never get handed each other's corkboard. If the house key doesn't open the family door (the account token isn't owner-scoped), or Plex is unplugged, the assistant quietly does the owner-only bit it can and lets the rest of the run carry on rather than stopping everything.

## Interactions

- **Parent manager:** `Main` (`scripts/main.py`) — constructs it after Tautulli / before Trakt, calls `prepare()`, then `run()` (Phase-2 inventory) and later `run_reconcile()` (Phase-2 post-*arr); reads `plex/run_stats` into the Discord run summary.
- **Sibling submanagers (loaded by this class, each its own work item):** `PlexUsersManager`, `PlexMetadataManager`, `PlexWatchlistManager`, `PlexOnDeckManager`, `PlexRatingsManager`, `PlexLibrarySectionsManager`, `PlexCollectionsManager`, `PlexPlaylistsManager`, plus the `PlexValidatorManager` stub.
- **API client:** `PlexAPI` (via `plex/api.py` → `plex/instances/api.py`).
- **Other services:** reads `radarr.movies.*.full` and the Sonarr series cache (via the registry) for GUID bridges; reads `tautulli/…` users (via the registered `TautulliManager`) for the crosswalk; UNIONs the *arr libraries (via `ArrGateway`) for reconcile; feeds `services/acquisition` (`candidates._plex()` + `scorer._SOURCE_SCORE["plex_watchlist"] = 100`).
- **Brain modules:** `machine_learning/next_watch/` consumes `plex/watchlist/union` as a pure feature; no brain decision is invoked from this manager.
