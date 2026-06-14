# Plex Service — Design

> Status: **DESIGN / NOT YET BUILT.** This document supersedes the planned-integration stub
> in `scripts/managers/services/plex/README.md`. It synthesizes the ground-architecture report,
> the ground-integration analysis, the 12 capability proposals, and four adversarial reviews
> (redundancy, api-stability, security-pii, efficiency, architecture-fit) into a single
> buildable spec. All paths are absolute. Every **unofficial Plex endpoint is marked UNSTABLE**;
> every **officially-stable local-PMS endpoint is marked STABLE**.

---

## 1. Executive summary

Glidearr's primary objective is being reframed to **next-watch propensity** — "what will the
household watch next" (`machine_learning/DESIGN_recommendation_enhancement.md` §0a). There is **no
Plex runtime manager today**: Plex is only a flat config block (`plex.{url, port, plex_token,
plex_media_path}`) captured in onboarding (`factories/onboarding/steps/media.py`, `PlexStep`),
validated server-side via `validators.plex_ping()` (`onboarding/validators.py:88`, `GET /identity`),
plus a server-side stress-test (`support/plex_stresstest.py`).

The Plex service exists to add the signals **only Plex has natively** and dedupe/reconcile them
against the services already wired (Tautulli = play history + completion% + per-user affinity;
Trakt = watched + watchlist + ratings + recommendations; MAL = anime). The flagship is the
**multi-user account WATCHLIST** — the strongest explicit-intent signal for the next-watch
objective and the one no other wired service can reproduce.

### The one-line role of Plex

> **Plex is the household's explicit forward-intent + identity layer:** its FETCHER caches the
> multi-user account **watchlist** (top-weighted next-watch signal + watchlisted-but-not-owned →
> acquisition candidates) and the **Plex-Home-user ↔ Tautulli-user ↔ rating_groups identity
> crosswalk** that lets every per-user Plex signal join the existing affinity/completion model —
> while the deterministic A–G scorecard remains the **curation** authority (delete/downgrade,
> pressure-gated on `free_space_limit` T/U). Plex **never** owns play history, the watched-set,
> affinity, or curation deletion — those stay with Tautulli/Trakt/Radarr.

Plex is **FETCH/CACHE-only in v1** (no APPLY, no write-backs). The `machine_learning/` brain only
THINKS via pure `plan()` functions reading already-cached Plex dicts; the brain-purity AST guard
(`scripts/hooks/brain_purity.py`) forbids `requests`/`services.*`/`*_api` imports inside brain
subpackages.

---

## 2. Recommended capability set — KEEP / DEFER / CUT

Twelve capabilities were proposed. The table reconciles all four adversarial reviews. Where the
reviews disagreed, the resolution column states the explicit decision and why.

| Capability | Verdict | Value / Effort | One-line rationale + disagreement resolution |
|---|---|---|---|
| **guid-metadata** (GUID→tmdb/tvdb/imdb resolver) | **KEEP — P0, build FIRST** | high / M | The join key without which every other signal fails `_dedup` (`candidates.py:91` keys on `tmdb‖tvdb‖imdb‖title`). All four lenses unanimous KEEP. REUSE Tautulli's `_extract_tmdb_id` + imdb→tmdb bridge; only the bare `plex://` Discover second-hop is new. |
| **home-users-identity** (Home enum + per-user tokens + crosswalk) | **KEEP — P0, build SECOND** | high / L | The mandatory infra every per-user Plex signal depends on; only place multi-Plex-user identity resolves. Unanimous KEEP. Per-user tokens **in-memory only** (security lens, non-negotiable). |
| **watchlist** (multi-user account watchlist) | **KEEP — P1, flagship** | high / L | The named blocker for the next-watch objective + top-tier acquisition feed; the one genuinely-additive top signal. Unanimous KEEP, conditional on the scope-probe + snapshot + resolve-before-dedup harness. |
| **on-deck** (continue-watching) | **DEFER — P2, A/B-gated** | medium / M | DESIGN §0a treats it interchangeable with Tautulli `percent_complete`; TV resume already computed by `next_episode_planner.last_watched_per_series` with zero Plex calls. **All four lenses say DEFER**, not cut: ship as enrichment emitting its own `plex/on_deck/*` key for forward A/B; do not weight in production until the eval harness exists. |
| **per-user-ratings** (per-member `userRating`) | **DEFER — P2, fold into per-user pass** | medium / S | Genuinely per-user (Trakt is single-account, Tautulli has none), zero marginal request cost riding the same scan. Reviews agree: not standalone; owner-dedupe vs Trakt mandatory or one verdict hits A4 twice. |
| **libraries-sections reconcile** (orphan/missing + section→root) | **KEEP-the-reconciler, CUT-the-inventory — P3** | medium / M | Reviews **split the capability**: the orphan/missing reconciler is a true gap nothing fills (verified no Plex↔*arr reconcile exists) and is pure zero-API set-diff → KEEP (deferred, cache-only, late). The bare section inventory is near-redundant (Tautulli's `get_library_index()` already fetches `{section_id:{name,type,count}}` and discards it) → CUT; the cheap win is having Tautulli cache what it already fetches. |
| **collections** (manual + smart) | **DEFER — P4, default-off** | medium / M | C1 completeness math already exists; only operator-curated non-TMDB collections are additive, often null. Stable local-PMS endpoints (low API risk). Default-off in scoring, deduped vs `collection_members`, forward-validate first. |
| **discover-hubs** (Plex Discover recs) | **CUT — revisit LAST, default-off** | low / M | Most redundant: Plex personalized hubs are global CF heavily overlapping already-wired Trakt `/recommendations` (`_SOURCE_SCORE:65`) + MAL. DESIGN §1/§2 explicitly: do NOT stack a third opaque global recommender. Unstable surface for marginal recall. **All four lenses CUT.** |
| **playlists** | **CUT → DEFER default-off — P4** | medium / M | Subordinate to + noisier than watchlist (queues/mixtapes ≠ intent); smart-rule grammar re-derives the A–G scorer. Build strictly after watchlist forward-validates; default-off. |
| **play-history** (viewCount watched-set) | **CUT** | low / M | ~90% redundant with Tautulli (the watched-set source of record). Only a Tautulli-ABSENT fallback is additive. Unanimous CUT. **Note (redundancy lens gap):** the existing watched-set folds Tautulli *movies* by lowercased TITLE (`orchestration:362`), so Plex's tmdb-precise GUID resolution would be a *precision upgrade* to the exclusion set — but that is folded behind a `tautulli/history/all` stale-guard, not a new fetcher. A **dead shim** `machine_learning/watchhistoryaggregator.py::_get_plex_watched()` already exists (Step-9 cleanup target) — reconcile/delete it before any Plex watched work; do not build a third path. |
| **sessions-activity** (now-playing) | **CUT → 1-call diagnostic stub** | low / S | No real-time consumer in a batch process; transcode/codec/bandwidth owned better historically by `tautulli/device_codec_matrix`. Keep at most one cached `plex/sessions` snapshot for run-summary color; build nothing on it. |
| **webhooks-realtime** | **CUT** | low / L | Decisive architecture mismatch: `main.py::run()` is one-shot batch + exit; **no HTTP server exists anywhere in `scripts/`**; a receiver is a brand-new always-on inbound listener for data Tautulli already aggregates for ALL users. Plex-Pass-per-user gating defeats multi-user. The one additive use (forward watchlist validation) needs a snapshot, not a listener. Unanimous hard CUT. |

**Irreducible v1 (P0+P1):** `guid-metadata` + `home-users-identity` + `watchlist`. This is the only
set where architecture-fit is clean AND value is high/load-bearing. Everything else layers on top.

---

## 3. Service architecture

### 3.1 Manager tree

`PlexManager` is a `BaseManager + ComponentManagerMixin` singleton constructed by Main, exactly
mirroring `TautulliManager` (`scripts/managers/services/tautulli/__init__.py`). Each submanager is
a plain `BaseManager` subclass with `parent_name = "PlexManager"` (required or `split_components`
silently drops it).

```
PlexManager (BaseManager + ComponentManagerMixin, parent_name="PlexManager")
├── plex_api : PlexAPI                 # the *_api HTTP handle (NEVER generic 'api')
├── validator_manager : PlexValidatorManager   # stub validate()->True
│
├── PlexUsersManager        (users/)        P0  Home enum + per-user token mint + identity crosswalk
├── PlexMetadataManager     (metadata/)     P0  GUID→{tmdb,tvdb,imdb} resolver + plex/guid_map
├── PlexWatchlistManager    (watchlist/)    P1  per-user + union account watchlist (flagship)
├── PlexOnDeckManager       (on_deck/)      P2  continue-watching (deferred, A/B-gated)
├── PlexRatingsManager      (ratings/)      P2  per-member userRating (folds into users pass)
├── PlexLibrarySectionsManager (libraries/) P3  section→root inventory + orphan/missing reconcile
└── PlexCollectionsManager  (collections/)  P4  manual+smart collections (deferred, default-off)
```

> **Singleton footgun (architecture lens, verified `base_manager.py:19-31,122-130`):** instances
> key on `(cls, singleton_key)` / `(parent_class_name, component_name)`. Do **NOT** instantiate one
> submanager *per user* — it would collapse to a single shared singleton. Per-user state lives in
> the **DATA** (cache keys / dicts keyed by `safe_user`), never in per-user manager instances.
> Follow the Tautulli model exactly: one `users` submanager, per-user data in cache.

### 3.2 The `plex_api` handle + the unbuilt HTTP client (a real gap)

Service-specific API naming convention: the handle is **`plex_api`**, never generic `api` (matches
`sonarr_api`/`radarr_api`/`tautulli_api`/`trakt_api`). A **dormant seam already exists**:
`TraktManager.__init__` already does `self.plex_api = kwargs.get("plex_api")`
(`services/trakt/__init__.py:27`) — Main just never passes it.

**Gap flagged by all reviews:** there is **no `PlexAPI` client class** today. `plex_stresstest.py`
has ad-hoc request code only. This must be built as foundational P0 work, analogous to
`TautulliAPI` (`tautulli/instances/api.py`), but with **Plex-specific hardening** because
plex.tv/Discover are external + rate-limited (not LAN like the local PMS):

| Requirement | Why | Pattern to mirror |
|---|---|---|
| Shared keep-alive `requests.Session` | one connection across all calls | `TautulliAPI` (`instances/api.py:36`) |
| **HTTP 429 + Retry-After backoff**, capped (~30s) | plex.tv is WAN/throttled; Tautulli client does NOT handle 429 | Trakt's 429 discipline (`trakt/api/__init__.py:171-220`, `_MAX_429_WAIT`) |
| Sliding-window `_throttle` lock | avoid hammering plex.tv | `trakt/api/__init__.py:64` |
| **Stable `X-Plex-Client-Identifier`** (persisted once) | v2 endpoints silently 401 without it; a per-run `uuid4` (as in `plex_stresstest.py`) spawns device churn / 2FA challenges | new — see open questions |
| `X-Plex-Token`, `X-Plex-Product/Version`, `Accept: application/json` headers | required contract | — |
| Enforce TLS verification (no `ssl_verify=False`) | these calls bear the highest-privilege token | audit ssl_verify discipline |
| Strip query strings from any logged `response.url` | `X-Plex-Token` is a URL param on Discover endpoints | the audit's history.py fix |

Package shim files (mirror Tautulli): `plex/api.py` re-exports `from .instances.api import PlexAPI`;
the canonical client lives at `plex/instances/api.py`. `plex/validator.py` = stub
`PlexValidatorManager(BaseManager)` with `validate()->True`.

### 3.3 Config shape (FLAT — differs from Tautulli)

Plex is a **flat** block `{url, port, plex_token, plex_media_path}` (`PlexStep`,
`media.py:62-95`), NOT the nested `{"default": {...}}` Tautulli collapses. So `PlexManager` does
**not** need the `.default` collapse. Token key is `plex_token`.

```python
plex_cfg = self.config.get("plex", {}) if self.config else {}
self.plex_api = PlexAPI(logger=self.logger, instance_config=plex_cfg)
```

### 3.4 `dry_run` (the propagation footgun)

Verified: `BaseManager.__init__` does **NOT** capture `dry_run`. Every manager re-derives it
(`tautulli/__init__.py:47`, `acquisition/__init__.py:49`). **Mandatory in `PlexManager.__init__`
and in every submanager `__init__`:**

```python
self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False))
```

Put `"dry_run": self.dry_run` into `init_args`. Plex is FETCH/CACHE-only in v1 so `dry_run` gates
nothing yet, but the wiring must exist so any future write (collection write-back, ratings sync) is
gated from day one.

### 3.5 Cache-key scheme

Slash-delimited, service-prefixed `plex/...`, mirroring `tautulli/...`. Usernames in path segments
**must** be sanitized with the existing rule and the raw username kept as a dict field:

```python
_safe = re.sub(r'[\\/:*?"<>|]', '_', username).strip()   # exactly tautulli/__init__.py:242
```

> Route sanitization through `cache/key_builder._sanitize_part` (which already rejects `''`/`.`/`..`
> and neutralizes traversal) — a Plex display name is externally-controlled (security lens).

| Cache key | Contents | Notes |
|---|---|---|
| `plex/users` | Home roster: `{uuid, title, is_admin, is_managed, protected(PIN), token_scope_ok}` | **NO email, NO token on disk** (security) |
| `plex/identity_map` | `{plex_uuid: {tautulli_username, tautulli_user_id, rating_group(s), safe_key}}` | the join table; email used in-memory only |
| `plex/guid_map` | `{raw_plex_guid: {tmdb, tvdb, imdb, resolved_via, ts}}` | persistent, long TTL, `regenerate_on_expiry=True` |
| `plex/users/<safe_user>/watchlist` | per-user GUID-resolved watchlist w/ attribution | |
| `plex/watchlist/union` | household union retaining per-user attribution | **top next-watch signal** |
| `plex/watchlist/snapshot/<ISO-ts>` | timestamped snapshot | **load-bearing** for forward validation (§5.4); retention-bounded |
| `plex/users/<safe_user>/ratings` | per-user `{id: userRating}` (P2) | id-keyed, no PII value |
| `plex/users/<safe_user>/on_deck` | per-user `{id, view_offset_ms, duration_ms, resume_fraction, hub_rank}` (P2) | |
| `plex/on_deck/union` | household on-deck union (P2) | A/B vs Tautulli resume |
| `plex/sections` | `{section_key: {title, type, locations[], item_count}}` (P3) | |
| `plex/reconcile/orphans` / `plex/reconcile/missing` | Plex∖*arr / *arr∖Plex (P3) | diagnostic only, never auto-delete |
| `plex/collections/index` / `.../completeness` / `.../membership_by_tmdb` (P4) | resolved member sets + completeness | |
| `plex/run_stats` | `{watchlist_items, users_tracked, users_pin_skipped, scope_ok, guid_network_hops, calls_made, ...}` | Main reads this |
| `plex/debug/unresolved_guids` | unresolved-GUID buckets | mirror `tautulli/debug/...` |
| **`plex/users/<u>/token`** | **FORBIDDEN — never create this key** | tokens in-memory only (security) |

### 3.6 Placement in `main.py` run order

`PlexManager` constructed in `_initialize_managers` (`main.py:79`). Two firm constraints from the
reviews resolve the brainstorm's internal contradiction:

1. **Construct AFTER Tautulli** (needs `tautulli/users` + `rating_groups` for the crosswalk),
   before acquisition. Identical kwargs Tautulli gets; set `self.registry.set_flag("plex_initialized")`.
2. **Plex is NON-critical** — leave it OUT of `_validate_managers` (`main.py:216`). Like MAL, it
   self-disables when unconfigured/unreachable/scope-fails; a Plex-less or scope-failed install must
   still run.

**Run-order split (resolves the reconcile tension):** the brainstorm wanted Plex "early like
Tautulli" AND wanted libraries reconcile to see warm Radarr/Sonarr caches — but Sonarr/Radarr run
in Phase 2 *after* Tautulli/Trakt/MAL. Resolution:

| Pass | When | What | API cost |
|---|---|---|---|
| **Plex inventory/identity/watchlist** | Phase 2, **before `trakt.run()`** | users → metadata(guid) → watchlist/on_deck/ratings; writes `plex/watchlist/union` so acquisition (last phase) reads it warm | network |
| **Plex reconcile (P3)** | Phase 2, **after Radarr+Sonarr** populate `RADARR_LIBRARY`/`SONARR_LIBRARY` | pure set-diff over cached *arr id sets | **zero API** |

Do **not** couple both into one monolithic early `run()`. Wrap each in the universal
`try/except → summary.add_error(...)`. Add a `plex` branch to Main's run-summary block
(`main.py:~426-443`) reading `plex/run_stats` — Main has no Plex summary branch today (gap).

Run-order summary: **Tautulli → Plex(inventory) → Trakt → MAL → Sonarr → Radarr → Plex(reconcile) →
space-coordinator → acquisition**.

### 3.7 Brain-purity split (FETCH vs THINK)

| Layer | Does | May import |
|---|---|---|
| **Plex SERVICE** (`services/plex/…`) | ALL I/O + side effects: `requests` to local PMS + plex.tv/Discover, Home enum, per-user token switch, GUID resolution, every `global_cache.set(...)`, logging | `requests`, `plex_api`, `global_cache` — **not** under the guard |
| **Brain** (`machine_learning/…`) | only THINKS: pure `plan(features, ctx, config) -> Plan` over already-fetched dicts | **no** `requests`/`plex_api`/`global_cache`/logging |

The mirror already in production: `TautulliUsersManager.compute_genre_affinity` FETCHes+caches but
delegates the math to `affinity.genre_affinity.aggregate_affinity` (pure). The future next-watch
ranker reads cached `plex/watchlist/union` as a pure dict arg.

> **Guard gap (architecture lens, verified):** `_GUARDED_SUBPACKAGES` (`brain_purity.py:37-41`)
> does **NOT** include `next_watch`. **When `machine_learning/next_watch/` is created, it MUST be
> added to `_GUARDED_SUBPACKAGES` in the same PR**, or the purity invariant is unenforced for
> exactly the new code. The acquisition handoff stays service-to-service (CandidateGatherer is a
> service holding `trakt`/`mal` handles) — no brain involvement there.

---

## 4. Identity model

Three identity spaces must join; **a Plex username need not equal the Tautulli `user` field** every
per-user signal is keyed on (DESIGN §0a explicit warning).

| Space | Source | Key |
|---|---|---|
| Plex Home users | `plex.tv/api/v2/home/users` **(UNSTABLE)** | `uuid` + `title`/`username` (+ `email`) |
| Tautulli users | `TautulliUsersManager.get_all_users()` | `username` / `user_id` (Tautulli also stores the Plex `user_id`/uuid) |
| `rating_groups.<group>.members` (config) | consumed at `space_pressure.py:402`, `group_completion.py:44` | lists of usernames |

### 4.1 The crosswalk (`PlexUsersManager.reconcile`)

Produces `plex/identity_map`. Matching cascade (fail-CLOSED on ambiguity — never write user A's
data under user B's `safe_key`):

1. **Plex uuid** ↔ the Plex `user_id`/uuid Tautulli records (most reliable).
2. **email** (in-memory only) ↔ Tautulli email.
3. **title/username** ↔ Tautulli username (last resort).
4. **Unmatched** (e.g. a Home user who never streamed → absent from Tautulli) → attributed to the
   **household wildcard**, not dropped.

Honor the memberless-group convention: empty `rating_groups` defaults to `{"household": {}}` and a
memberless group is a household-wide wildcard counting every user
(`group_completion.py:41`, verified `tautulli/__init__.py:254`). The Plex union must behave the same
— a household-wide union when no per-user grouping is configured.

### 4.2 Per-user tokens (the auth primitive)

`PlexUsersManager` mints a per-user token **once per run** via the switch flow and shares it via an
**in-memory table** in `init_args` (NOT a cache key). All per-user fetchers (watchlist/on_deck/
ratings) reuse it. Endpoints involved (**all UNSTABLE / community-documented**):

| Endpoint | Purpose | Stability |
|---|---|---|
| `GET https://plex.tv/api/v2/user` | **token-scope probe** — gate the whole per-user surface | UNSTABLE (v2 account API, stable-ish) |
| `GET https://plex.tv/api/v2/home/users` | enumerate Home/managed users | **UNSTABLE** |
| `POST https://plex.tv/api/v2/home/users/{uuid}/switch` (`?pin=NNNN`) | mint per-user authToken | **UNSTABLE** |

**Scope gate (the #1 stability risk):** the captured `plex_token` has only ever been used
server-side (`plex_stresstest.py`), so its account-owner scope is **unverified**. At
`PlexManager.run()` top, probe `GET /api/v2/user` once; set `self.account_scope_ok` /
`self.enabled` (MAL `self.enabled` pattern, `mal/__init__.py:47`). On 401: warn once, write empty
`plex/users` + a `run_stats` flag, degrade per-user fetchers to **owner-only / household-union** —
**never fall through to broader-scope attempts and never abort the run**.

### 4.3 PIN handling

PIN-protected profiles need an operator-supplied PIN to `/switch`. PINs are **credentials**:

- Stored in **gitignored config** via `SecretStore`/keyring (`is_secret_key` already matches `pin`);
  never inline plaintext, never in any `plex/...` cache.
- **Registered with the logger scrubber** and a `pin=` redaction pattern added to
  `_SECRET_SCRUB_PATTERNS` (today there is NO pin pattern — security gap).
- PIN-less-unavailable users are **skipped and COUNTED** in `plex/run_stats.users_pin_skipped` —
  the union must shrink *visibly*, never silently.

---

## 5. Integration into the recommendation phases

### 5.1 Next-watch (the primary objective — §0a feature blend)

There is **no next-watch ranker yet** (only the Phase-0 eval baseline). So the integration is a
**cache the future ranker reads**, not a call:

| §0a feature | Plex contribution | Weight |
|---|---|---|
| #1 watchlist union (Plex ∪ Trakt ∪ MAL) | `plex/watchlist/union` — the **top-weighted** signal | strongest |
| #2 on-deck / continue-watching | `plex/on_deck/union` (P2, A/B vs Tautulli `percent_complete`) | high, A/B-gated |
| per-user A4 rating | per-member `userRating` → `scoring/_shared.user_rating_score` | confidence-gated |
| exclusion (a next-watch is unwatched) | watched-set already assembled at `radarr/orchestration/__init__.py:350` (folds Tautulli completions + Trakt history); Plex does NOT build it | — |

When `machine_learning/next_watch/` is built (beside `likelihood/`), it reads
`plex/watchlist/union` as a pure top-weighted feature arg; brain-purity holds. **Sequencing risk:**
every KEEP capability produces a cache key with **no reader yet**. The thin next-watch consumer
must be sequenced alongside (P1), or the flagship signal sits inert and unvalidatable.

### 5.2 Acquisition (`candidates.py`)

> **The brainstorm's wiring claim was WRONG and is corrected here.** `AcquisitionManager` does take
> `trakt=`/`mal=` kwargs (verified `__init__:51-52`), **but `CandidateGatherer.__init__(self, trakt,
> mal, logger, sources_cfg, limit)` has NO `plex` param** and is constructed **positionally** at
> `acquisition/__init__.py:181`: `CandidateGatherer(self.trakt, self.mal, self.logger, ...)`.

Adding a Plex source requires **three edits**, not one main.py line:

1. **`acquisition/candidates.py`** — extend `CandidateGatherer.__init__` to accept `plex`; add a
   `_plex()` branch in `gather()` gated by `self.sources.get("plex_watchlist", True)` reading
   `plex/watchlist/union`, emitting the exact `_norm` shape with **resolved ids
   `{tmdb,tvdb,imdb}`** (resolution done in the fetcher, not here — `_dedup` at `:91` keys on
   `tmdb‖tvdb‖imdb‖title`, so a `plex://`-only item double-adds against Trakt/MAL).
2. **`acquisition/__init__.py:181`** — thread `self.plex` into the `CandidateGatherer(...)`
   construction; `AcquisitionManager` already pulls handles from kwargs — add `self.plex =
   kwargs.get("plex")` (~`:50-53`).
3. **`acquisition/scorer.py:29`** — add `"plex_watchlist": 100` (top explicit-intent tier alongside
   `trakt_watchlist`/`mal_plantowatch`). Lower tiers for deferred feeds: `plex_playlist ≈ 55-65`,
   `plex_hubs ≈ 55-60` (suggestion tier, below `trakt_recommendations:65`) — **default-off**.

Owned-vs-not split for acquisition is **already handled downstream**: `gateway.in_library()`
(`acquisition/gateway.py:83`) filters titles already in Sonarr/Radarr, so the fetcher only needs
correct ids. No separate owned-check there.

### 5.3 Curation

Plex feeds curation only weakly and only via deferred capabilities (collections completeness as a
C1 tiebreaker, protect-set membership for actively-watched manual collections — both P4,
default-off, deduped vs the existing TMDB `collection_members`). The orphan/missing reconciler (P3)
is **diagnostic only** — orphans (in Plex, not *arr-managed) must **NEVER auto-feed deletion**;
deletion stays pressure-gated on `free_space_limit` over *arr-owned files. The next-watch feed does
NOT drive delete/downgrade — the A–G scorecard remains the curation authority.

### 5.4 Forward watchlist-validation path (the eval blind spot)

DESIGN §8: retrospective eval **cannot** score watchlist/on-deck intent (current-watchlist ∩
past-held-out ≈ 0). Therefore:

- Every forward-looking feed (watchlist, on-deck, playlists) writes a **timestamped snapshot** key
  (`plex/watchlist/snapshot/<ISO-ts>`, on-deck equivalent) at fetch time.
- A later batch run measures overlap of a past snapshot against the existing watched-set
  (`radarr/orchestration:350`) → "did they watch what was listed?".
- This is the **only validation path** for these signals; the snapshot is **load-bearing, not
  optional**, and gates whether an unstable-endpoint capability's blend weight is justified.
- **Retention bound (security):** snapshots are per-member intent PII that accumulates unbounded —
  prune by age/count (rolling window), documented policy, not bolted-on.

---

## 6. Efficiency + Security standards (agreed rules)

### 6.1 Efficiency standards

| Rule | Detail |
|---|---|
| **Poll-once-per-run, never per-fetch** | Enumerate Home users + mint per-user tokens once at `PlexManager.run()` top; all fetchers reuse the in-memory token table. Per-run ceiling ≈ `1 scope-probe + 1 home/users + N switch + (~2 paged scans/user per kept per-user cap) + GUID residue`. |
| **No inbound listeners** | Stay within fetch→cache→exit. Webhooks/SSE/WebSocket banned (no consumer in batch). Forward-validation = one snapshot `cache.set`/run, not a listener. |
| **Two-tier GUID resolution** | FREE/regex for `tmdb://`/`imdb://`/`tvdb://`/legacy `com.plexapp.agents.*` (zero network); PAID network (`metadata.provider.plex.tv` **UNSTABLE**) only for bare `plex://`. Dedupe the unseen-GUID request set first; memoize `ratingKey→ids` within the run; persist `plex/guid_map` long-TTL so each `plex://` resolves **at most once ever**. |
| **Pre-build the bridge dicts** | The imdb→tmdb bridge does a LINEAR `next(...)` scan over `radarr.movies.standard.full` **per item** (`tautulli/__init__.py:314-318`) — NOT free in CPU. Build an `imdbId→tmdbId` (and `tvdbId→tmdbId` via Sonarr full) **dict once per run** and reuse everywhere, or it becomes the hottest loop. |
| **Share one `plex/guid_map` service-wide** | watchlist/on_deck/collections/reconcile must never re-resolve the same `ratingKey` independently or per-user-per-run. |
| **`regenerate_on_expiry=True` explicitly** | `get_or_generate_cache` defaults to serve-stale-FOREVER (`cache/__init__.py:124-129`) — the exact bug already fixed once for the watched-set. Any key that must refresh (watchlist union, on_deck, guid_map) MUST pass it. |
| **Generous TTLs where static** | roster + guid_map change rarely → generous TTL (most runs skip `/switch`). on-deck TTL aligned to **actual run cadence** — do not pay for sub-run freshness with no consumer. |
| **Page-cap + early-stop** | cap `X-Plex-Container-Size` (≤100-500), stop on `totalSize` (the stress-test already does `start >= totalSize`). |
| **Bounded concurrency on cold-cache** | first run (empty guid_map + full multi-user watchlist of `plex://` GUIDs) is the worst burst; cap parallel plex.tv hops; log that first run is expensive, steady-state near-cache-only. |
| **Request observability** | emit `calls_made / cache_hits / cache_misses / users_switched / guid_network_hops` to `plex/run_stats` so cost regressions are visible. |
| **Tautulli-present short-circuit** | provide one reusable `tautulli_history_fresh()` predicate; play-history/sessions fallbacks issue ZERO Plex calls when Tautulli is healthy. |

### 6.2 Security standards (post-incident posture — non-negotiable)

| Rule | Detail |
|---|---|
| **Token-scope probe is a HARD gate** | `GET /api/v2/user` before any Home/switch; non-200 → self-disable per-user path, record in `run_stats`. Never fall through to broader scope. |
| **Per-user minted tokens: IN-MEMORY ONLY** | hold in an in-process dict for the run, discard. **Never** create `plex/users/<u>/token`. The pre-commit `secret_scan` only sees staged git diffs and the cache is gitignored → it will **NOT** catch a token written to cache; in-memory-only is the *only* defense. State this in the `PlexUsersManager` docstring so no future contributor "helpfully" adds a token cache. (Daemon reuse, if ever needed → OS keyring via `SecretStore` behind explicit config, never the JSON cache.) |
| **Register every minted token + PIN with the logger scrubber** | call `LoggerManager.register_secrets([token])` the instant each `/switch` returns; a token logged BARE matches no existing `_SECRET_SCRUB_PATTERN` (the bare-token blind spot). |
| **Add `pin=` redaction** | extend `_SECRET_SCRUB_PATTERNS` with `(?i)\bpin=\d+ → pin=<redacted>`; never build a log/exception string containing a raw PIN or token. |
| **PII-minimize every per-user cache** | mirror the Tautulli writer: key on `safe_user` + opaque uuid; **DROP raw email** from any persisted dict; keep human-readable name in the in-memory roster only. Document dropped fields in a header comment (as `tautulli/watch_history` does). |
| **Sanitize via `_sanitize_part`** | route every `<safe_user>` path segment through `cache/key_builder._sanitize_part` (rejects traversal); a Plex display name is externally-controlled. |
| **Fail-CLOSED attribution** | on parse ambiguity / missing uuid / schema mismatch from UNSTABLE endpoints → SKIP that user + bucket to `plex/debug`; never risk writing user A's data under user B's key. Preserve the prior good snapshot (don't zero the union on a transient failure). |
| **Snapshot retention bound** | documented age/count cap on `plex/.../snapshot/<ISO>` so forward-validation PII doesn't accumulate forever. |
| **Rate-limit / lockout discipline** | mint each token once/run; cache the (non-secret) roster; back off on 429; treat a 2FA/challenge response as "self-disable for this run", not retry-storm. |
| **0600 perms + delete-by-member** | apply the config-writer's 0600 perms to per-user PII cache files; provide a delete-by-member helper (enumerate `plex/users/<safe>/*` for a removed Home user) + a per-user exclude filter honored across ALL per-user signals (consent/opt-out for non-owner members incl. kids/managed profiles). |
| **TLS verification + URL scrubbing** | enforce TLS on all plex.tv/Discover calls (these bear the highest-privilege token); strip query strings from any logged `response.url` (`X-Plex-Token` is a URL param). |
| **READ-ONLY / dry_run posture holds** | no write-backs in v1 (no "Recommended Next" smart collection/playlist); any future write is `dry_run`-gated and out of v1 scope. |

### 6.3 API-fragility fallbacks (two-tier classification is load-bearing)

| Tier | Endpoints | Failure handling |
|---|---|---|
| **STABLE** (local PMS, `X-Plex-Token`) | `/library/sections`, `/sections/{key}/all`, `/library/metadata/{rk}`, `/library/collections`, `/playlists`, `/status/sessions`, `/library/onDeck` | normal error handling |
| **UNSTABLE** (community-documented) | `plex.tv/api/v2/{user,home/users,home/users/{uuid}/switch}`; `metadata.provider.plex.tv/{library/sections/watchlist/all, library/metadata/{rk}, hubs, related}` | defensive `try/except`; schema-tolerant `.get(...)` parsing (never index); empty/parse-failure → **soft-empty** (`[]` + bucket to `plex/debug`), **never raise**; serve last-good cache |

Additional fragility controls (api-stability gaps): a **schema-drift sentinel** (hash/required-key
check on the first UNSTABLE response per run; if required keys vanish → distinct `PLEX SCHEMA DRIFT`
warning + self-disable that capability for the run, distinguishing "Plex changed the contract" from
"empty watchlist"); capture + log **PMS version** once (from `/identity`) so version-dependent parse
fallbacks (legacy GUIDs, `/hubs` shape variance) are deliberate; a defensive `/home/users` parser
tolerant of missing `admin/guest/restricted/protected` flags with the documented match fallback
chain (§4.1).

---

## 7. Phased build roadmap

Each phase is independently shippable, opt-in, and dry_run-safe. Plex is non-critical throughout.

### P0 — Foundation (prerequisites, no user-visible signal yet)

| Item | Deliverable | Depends on |
|---|---|---|
| **PlexAPI client** | `plex/instances/api.py` + `plex/api.py` shim: keep-alive Session, 429/Retry-After backoff, stable `X-Plex-Client-Identifier`, TLS-verify, URL scrub | — |
| **PlexManager shell** | top-level `__init__` (flat config, `plex_api`, `init_args`, `component_dependencies`/`all_component_classes`/`critical_keys`, `split_components`, `_is_reachable`/scope-probe gate, `prepare()`), `validator.py` stub | PlexAPI |
| **guid-metadata** | `PlexMetadataManager` (`metadata/`): two-tier resolver reusing `tautulli/metadata._extract_tmdb_id` (lift to shared helper, extend for `tvdb://` + legacy + bare `plex://` Discover hop) + pre-built bridge dicts; persistent `plex/guid_map` | PlexManager |
| **home-users-identity** | `PlexUsersManager` (`users/`): scope-probe, Home enum, per-user token mint (in-memory), PIN handling, `reconcile()` → `plex/identity_map`/`plex/users` | PlexManager, Tautulli users + `rating_groups` |
| **Onboarding hardening** | `PlexStep`: add `GET /api/v2/user` account-scope probe (today only `/identity` ping — misleadingly green); optional per-profile PIN capture (secret); persist stable client-identifier. Extend `auth_validator.validate_all` with a Plex scope check (today covers only radarr/sonarr/trakt) | — |
| **main.py wiring** | construct `PlexManager` after Tautulli; `set_flag("plex_initialized")`; add `plex/run_stats` summary branch; keep OUT of `_validate_managers` | — |

### P1 — Watchlist (flagship) + thin next-watch consumer

| Item | Deliverable | Depends on |
|---|---|---|
| **watchlist** | `PlexWatchlistManager` (`watchlist/`): per-user watchlist (Discover `watchlist/all` **UNSTABLE**), GUID-resolve via P0, household union w/ attribution, timestamped snapshot, `regenerate_on_expiry` | P0 (guid + identity) |
| **acquisition seam** | 3 edits §5.2: `CandidateGatherer` `_plex()` branch + signature, `acquisition/__init__.py:181` construction, `scorer.py:29` `plex_watchlist:100` | watchlist cache |
| **next-watch consumer (thin)** | `machine_learning/next_watch/` (add to `_GUARDED_SUBPACKAGES`!) reading `plex/watchlist/union` as a pure top-weighted feature; sequence with the Phase-0 eval harness so the signal is not inert/unvalidatable | watchlist cache, eval harness |

### P2 — On-deck + per-user ratings (A/B-gated enrichment)

| Item | Deliverable | Gate |
|---|---|---|
| **on-deck** | `PlexOnDeckManager`: `GET /library/onDeck` per user (STABLE) + shared guid_map; `plex/on_deck/union`; **emit own key for A/B vs Tautulli `percent_complete`**; decide reconciliation policy (prefer-max vs prefer-server) before coding | weight only after forward-eval A/B shows lift |
| **per-user ratings** | `PlexRatingsManager`: `userRating!=0` filter on the SAME per-user scan (zero marginal cost); feed `scoring/_shared.user_rating_score` via `sonarr/cache/episode_files._build_user_show_rating_map`; **owner-dedupe vs Trakt**, normalize 0-10 half-steps to integer | owner-dedupe mandatory |

### P3 — Libraries reconcile (diagnostic, cache-only late pass)

| Item | Deliverable | Notes |
|---|---|---|
| **section→root inventory** | `plex/sections` (STABLE local endpoints); have Tautulli cache its already-fetched `library_index` rather than re-walking | CUT full per-item re-enumeration |
| **orphan/missing reconcile** | `plex/reconcile/{orphans,missing}` — pure set-diff, **UNION all Radarr/Sonarr instance caches** (pending tier-routing task #7) or it over-reports missing; **diagnostic only, never auto-delete**; unresolved GUIDs excluded (else false-positive orphans) | runs after *arr in Phase 2 |

### P4 — Collections / playlists (default-off, forward-validated)

| Item | Deliverable | Gate |
|---|---|---|
| **collections** | `PlexCollectionsManager` (STABLE local endpoints): `plex/collections/{index,completeness,membership_by_tmdb}`; additive-but-deduped vs TMDB `collection_members`, **byte-identical to golden scorer when input is None** | default-off in scoring until measured non-TMDB collections exist + forward-validated |
| **playlists** | `PlexPlaylistsManager`: video-only, deduped vs watchlist union; smart-rule grammar captured as opaque metadata (never executed) | default-off; `plex_playlist` source-score below watchlist |

### CUT (do not build): webhooks-realtime, play-history (primary), discover-hubs, sessions-activity (beyond 1-call stub). Reconcile/delete the dead `watchhistoryaggregator._get_plex_watched()` shim during Step-9 cleanup.

---

## 8. Prioritized enhancements + open questions

### Prioritized enhancements (post-v1)

1. **Forward-eval online loop** (highest leverage) — the only way to justify on-deck/playlist blend
   weights; snapshot keys are written from P1 but the measurement loop (`machine_learning/eval/`)
   is the unblocker for trusting any forward-looking Plex signal.
2. **tmdb-precise watched-set correction** — fold Plex GUID-resolved watched ids behind the
   `tautulli/history/all` stale-guard to upgrade the lossy lowercased-title Tautulli-movie fold
   (`orchestration:362`) — a precision gain to the next-watch *exclusion* set, even though
   play-history is otherwise CUT.
3. **Per-member A4 ratings rollout** — once P2 ratings prove out, replace the single broadcast
   household rating in `_build_user_show_rating_map` with the per-member blend.
4. **Multi-instance-aware reconcile** — write P3 reconcile instance-union-aware from day one
   (interacts with pending tier-routing task #7) rather than retrofitting.

### Open questions

| # | Question | Owner / resolution path |
|---|---|---|
| Q1 | **Is the captured `plex_token` account-owner-scoped?** Only ever used server-side today. If server/managed-scope, every per-user capability degrades to owner-only. | Resolve at build via the `/api/v2/user` probe; surface in onboarding + `auth_validator` so failure is visible at config time, not silently at run. |
| Q2 | **Where does the stable `X-Plex-Client-Identifier` come from?** v2 endpoints 401 silently without it; a per-run uuid4 spawns device churn / 2FA. | Generate once at onboarding, persist in config (or a stable cache key), reuse across all calls and runs. |
| Q3 | **on-deck `viewOffset` vs Tautulli `percent_complete` reconciliation** — prefer-max or prefer-server? | Decide BEFORE P2 coding or the two signals contradict in the blend. |
| Q4 | **PIN config schema** — per-profile PINs need a secret schema, redaction guarantee, and `run_stats` surfacing of skipped users. | Define in P0 onboarding; PINs via `SecretStore`. |
| Q5 | **PII threat-model / encryption-at-rest** for per-member watchlist/ratings caches — gitignored ≠ encrypted; anyone with host/volume access reads cleartext intent history. | At minimum 0600 perms + delete-by-member helper + consent note for non-owner members; consider encryption if scope grows. |
| Q6 | **Next-watch ranker home** — `machine_learning/next_watch/` doesn't exist; no reader for `plex/watchlist/union` yet. | Sequence the thin consumer in P1 alongside the eval harness; add `next_watch` to `_GUARDED_SUBPACKAGES` at creation. |
| Q7 | **Token-leak revocation story** — per-user/account token leak impact + remediation. | Document "sign out all devices" as the rotation lever (the incident memory's account-token fix); keep minted-token lifetime to a single run. |

---

### Key files referenced (repo-relative)

- Template to clone: `scripts\managers\services\tautulli\__init__.py`
- HTTP-wrapper template: `scripts\managers\services\tautulli\instances\api.py`
- Submanager template: `scripts\managers\services\tautulli\users\__init__.py`
- GUID resolution to REUSE: `...\services\tautulli\metadata\__init__.py:110` + `...\services\tautulli\__init__.py:304`
- Trakt 429/Session discipline to mirror: `...\services\trakt\api\__init__.py:64,171-220`
- Acquisition seam (3 edits): `...\services\acquisition\candidates.py:12,86,91`, `...\acquisition\__init__.py:181`, `...\acquisition\scorer.py:29`
- Dormant `plex_api` seam: `...\services\trakt\__init__.py:27`
- Identity/sanitizer: `...\services\tautulli\__init__.py:242,254`; `...\machine_learning\affinity\group_completion.py:41`
- Watched-set / exclusion: `...\services\radarr\orchestration\__init__.py:350,362`
- Brain-purity guard: `scripts\hooks\brain_purity.py:37` (`_GUARDED_SUBPACKAGES` — add `next_watch`)
- Cache: `...\factories\cache\__init__.py:76-163` (`get_or_generate_cache`, `regenerate_on_expiry`) + `...\cache\key_builder.py` (`_sanitize_part`)
- `split_components`: `...\support\utilities\managers\component_splitter.py`
- main.py: `...\scripts\main.py` (`_initialize_managers:79`, `_validate_managers:216`, run-summary `:426-443`)
- Onboarding flat config: `...\factories\onboarding\steps\media.py:62`; auth: `...\factories\onboarding\validators.py:88`
- Dead shim to reconcile: `...\machine_learning\watchhistoryaggregator.py` (`_get_plex_watched`, Step-9 cleanup)
- Objective spec: `...\machine_learning\DESIGN_recommendation_enhancement.md` §0a, §1, §2, §8
- Existing stub: `...\services\plex\README.md`
