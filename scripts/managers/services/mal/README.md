# MALManager

- **File** — `scripts/managers/services/mal/__init__.py`
- **One-liner** — Optional MyAnimeList service manager that ingests the user's anime list / suggestions / current season, exposes plan-to-watch + suggestions as Sonarr acquisition candidates, and provides the API handle other managers use to write list updates.

## What it does (for a senior Python engineer)

`MALManager(BaseManager, ComponentManagerMixin)` is a top-level Phase-2 service manager (it runs alongside Trakt). MAL is **optional**: if it is not configured/authorized the manager flips `self.enabled = False` in `__init__` and every public method degrades to a no-op or empty result.

Position in the manager tree:
- **Parent**: constructed by `Main` (`scripts/main.py`) as one of the sequential service managers; `parent_name = "MALManager"` (it is its own auto-link root for its children).
- **Children it constructs directly** (not via `load_components`; they are built by hand in `__init__`):
  - `MALInstanceManager` (`mal/instances/`) — validates/refreshes the OAuth token and resolves the username; its `register_and_validate()` return value becomes `self.enabled`.
  - `MALAPIManager` (`mal/api/`) — the authenticated HTTP layer, stored as `self.mal_api`. NOTE: `MALAPIManager` is a plain class, **not** a `BaseManager`; it is instantiated with only `config` and `logger`.

Per the FETCH / CACHE / APPLY model this manager is essentially **FETCH + CACHE only**. `run()` performs HTTP GETs (via `mal_api`) and persists the raw responses to `global_cache`. It performs no writes itself; the only APPLY-capable surface is `self.mal_api.update_list_status(...)` (a PATCH), which is invoked by `WritebackManager`, not by this manager.

### Key public methods
- `run() -> None` — the Phase-2 entry point. If `enabled`, fetches the full anime list, derives the `plan_to_watch` subset, fetches suggestions, and fetches the current season; then caches all four. Decorated with `@timeit("run")` and `@LoggerManager().log_function_entry`.
- `prepare() -> None` — no-op (present to satisfy the manager interface).
- `acquisition_candidates() -> list` — returns plan-to-watch + suggestions normalized into the shared acquisition-candidate dict shape, tagged `source="mal_plantowatch"` / `"mal_suggestions"`. Cache-first: reads `mal/{user}/plan_to_watch` and `mal/{user}/suggestions` from `global_cache`, falling back to live API calls if the cache is empty. Filters out any candidate lacking a `title`. Consumed by `AcquisitionManager`.
- `enrich(title: str) -> dict` — best-effort metadata lookup: `search_anime(title, limit=1)` and return the first match's `node` dict (or `{}`). Returns `{}` when disabled or `title` is falsy.
- `mal_api` (attribute) — the `MALAPIManager` handle; the public surface for `WritebackManager` list updates.
- `_user` (property) — `config["mal"]["username"]`, defaulting to `"default"`.

### External API endpoints touched (via `mal_api`)
All under `https://api.myanimelist.net/v2`:
- `GET users/@me/animelist` (with `status=plan_to_watch` on the fallback path)
- `GET anime/suggestions`
- `GET anime/season/{year}/{season}`
- `GET anime` (search, `q=`) — used by `enrich`

### Config keys read
- `mal.username` (via `_user`)
- (indirectly, through its children) `mal.client_id`, `mal.client_secret`, `mal.authorization.{access_token,refresh_token,created_at,expires_in}`
- `dry_run` is taken from the `dry_run` kwarg or the parent manager's `dry_run`.

### global_cache keys written (by `run`)
- `mal/{user}/animelist` — full list
- `mal/{user}/plan_to_watch` — filtered subset
- `mal/{user}/suggestions`
- `mal/seasonal/{year}/{season}` — note this key is **not** user-scoped

### global_cache keys read (by `acquisition_candidates`)
- `mal/{user}/plan_to_watch`
- `mal/{user}/suggestions`

### dry_run behavior
`MALManager` itself only FETCHes and CACHEs, so `dry_run` does not change its own behavior. `self.dry_run` is captured and forwarded to the instance manager; the actual "would …" guarding for the only write path (`update_list_status`) lives wherever `WritebackManager` calls it.

### Singleton / concurrency notes
As a `BaseManager`, `MALManager` is a process-wide singleton keyed by class + singleton key. `MALAPIManager` applies a gentle `0.2s` minimum interval between calls (`_throttle`), so there is no concurrency built in here.

## How it functions

Lifecycle:
1. `__init__` runs `BaseManager.__init__` (injecting shared deps + auto-linking), calls `self.register()`, resolves `dry_run`, then builds the two children by hand. `self.enabled = self.instance_manager.register_and_validate()` — so MAL silently disables itself when there is no `client_id` or no refreshable token.
2. `run()` is the per-cycle entry. It guards on `enabled`, then issues the three GET families and writes four cache keys, ending with one summary `log_info` line:
   `[MALManager] ingest: N list · M plan-to-watch · K suggestions · S seasonal.`
3. `acquisition_candidates()` is called later (by `AcquisitionManager`) and prefers cached data, falling back to live calls.

Internal helpers:
- `_norm(item, source)` (staticmethod) — flattens a MAL list/suggestion item into the acquisition shape: pulls `node`, `start_season.year`, genre names (defaulting to `["anime"]`), `mean` → `rating`, and sets `type="show"`, `is_anime=True`, `ids.mal=node.id` with all other id slots `None`.
- `_SEASONS` (module dict) — maps a calendar month (1–12) to a MAL season string (`winter`/`spring`/`summer`/`fall`).

Brain delegation: **none.** This manager makes no value judgements and delegates no decision to `machine_learning/`. It is a pure ingest + candidate-source adapter; the ranking/selection of these candidates happens downstream.

## Criteria & examples

- **Enablement guard**: requires `mal.client_id` to be set AND a valid (or refreshable) access token; otherwise `enabled=False` and every method returns empty. Example: a user who never ran MAL onboarding has no `client_id`, so `run()` logs `[MAL] disabled — skipping ingest.` and exits.
- **Plan-to-watch filter** (in `run`): an item is kept only when `item["list_status"]["status"] == "plan_to_watch"`. Example: *Frieren: Beyond Journey's End* sitting in your "Plan to Watch" list is included; a title you already marked "completed" is not.
- **Season selection**: `_SEASONS[now.month]`. Example: on 2026-06-10 (`now.month == 6`) the manager fetches `anime/season/2026/spring`, because months 4–6 map to `spring`.
- **Candidate title guard**: `acquisition_candidates()` drops any normalized candidate where `title` is falsy — a malformed suggestion node with no `title` is silently excluded.
- **Genre fallback**: in `_norm`, if a node has no usable genres the candidate's `genres` becomes `["anime"]` rather than an empty list.

## In plain English

Think of `MALManager` as the part of the app that quietly reads your MyAnimeList account. Once a cycle it grabs three things: your full anime list, the shows you've flagged "I plan to watch this," MAL's personalized suggestions, and what's airing this season. It writes those down (caches them) so the rest of the app can use them without re-asking MAL every time.

Its main payoff: the shows on your "Plan to Watch" pile — say you bookmarked *Attack on Titan* there months ago — get handed to the part of the app that actually adds shows to Sonarr, so they can be downloaded for you automatically. If you never connected a MyAnimeList account, this whole feature just turns itself off and stays out of the way.

## Interactions

- **Parent**: `Main` (`scripts/main.py`) constructs and runs it in Phase 2.
- **Children it builds**: `MALInstanceManager` (token validation/refresh + username → drives `enabled`) and `MALAPIManager` (`self.mal_api`, the HTTP layer). Both are separate work items in their own subdirectories.
- **Downstream consumers**:
  - `AcquisitionManager` — calls `acquisition_candidates()` to feed anime into the Sonarr acquisition pipeline.
  - `WritebackManager` — uses `self.mal_api.update_list_status(...)` to push list/score/episode updates back to MAL (the only APPLY path that touches MAL).
- **Brain modules**: none — this manager delegates no decisions to `machine_learning/`.
