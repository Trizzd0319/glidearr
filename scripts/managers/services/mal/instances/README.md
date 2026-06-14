# MALInstanceManager

- **File** — `scripts/managers/services/mal/instances/__init__.py`
- **One-liner** — Runtime guard that confirms MyAnimeList (MAL) credentials are present, refreshes an expired OAuth token, and resolves the MAL username, so the parent `MALManager` knows whether MAL is usable this run.

## What it does (for a senior Python engineer)

`MALInstanceManager` is the MAL analogue of `TraktInstanceManager`: a lightweight credential/health-check manager that runs once when `MALManager` is constructed. It does **not** perform the initial OAuth authorization (the browser/paste PKCE flow) — that is owned by the onboarding step. Here it only validates, refreshes, and names.

Where it sits in the manager tree:
- `parent_name = "MALManager"` — it is a submanager of `MALManager`, which is itself a top-level service manager run by `Main`. `MALManager` instantiates it directly (`self.instance_manager = MALInstanceManager(**base)`) rather than via `load_components`, then immediately calls `register_and_validate()` and stores the boolean as `self.enabled`.
- It extends `BaseManager` (so it gets the shared singleton dep-injection: logger, config, global_cache, validator, registry, parent auto-link) and mixes in `ComponentManagerMixin`, but it loads **no** submanagers of its own — `prepare()` is a no-op and `load_components` is never called.

Key public methods:
- `__init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)` — sets `self.parent_name = "MALManager"`, calls `super().__init__(...)` to wire the shared deps and parent link, then `self.register()` to self-register with the registry.
- `register_and_validate(self) -> bool` — the entry method. Returns `True` if MAL is usable this run, `False` (never raises) so `MALManager` can skip MAL gracefully. Steps:
  1. Read the `mal` config block; if no `client_id`, log debug "MAL disabled" and return `False`.
  2. Read `mal.authorization`; if the `access_token` is missing or `_expired(...)`, attempt a token refresh.
  3. On a successful refresh, write the new authorization dict back into config (`self.config.set("mal", mal)`).
  4. Resolve/confirm the username via `_ensure_username`, log `[MALInstanceManager] OK: <username>`, return `True`.
  5. If refresh fails, log a warning telling the user to run onboarding, return `False`.
- `prepare(self) -> None` — no-op (satisfies the manager interface; there is nothing to prepare since it loads no components).

Internal helpers:
- `_expired(auth: dict) -> bool` (staticmethod) — treats a token as expired if `created_at` or `expires_in` is missing/non-numeric, or if `now - created_at > expires_in`.
- `_ensure_username(self, mal: dict) -> str` — returns the existing username if it already matches `_USERNAME_RE` (`^[A-Za-z0-9_-]+$`); otherwise fetches it from MAL, validates the format, persists it to config, and returns it (or `""` on failure).

FETCH / CACHE / APPLY classification:
- **FETCH** — yes, indirectly. Both network calls are delegated to the shared `scripts/managers/factories/onboarding/oauth.py` helpers:
  - `oauth.mal_refresh_token(client_id, client_secret, refresh_token)` → `POST https://myanimelist.net/v1/oauth2/token` (form-urlencoded, `grant_type=refresh_token`).
  - `oauth.mal_fetch_username(access_token)` → `GET https://api.myanimelist.net/v2/users/@me` with a bearer header, reading the `name` field.
- **CACHE** — none into `global_cache` / Parquet. It does **persist** the refreshed authorization dict and the resolved username back into the **config** (`config.set("mal", ...)`), but that is config state, not the cache layer.
- **APPLY** — none. It issues no PUT/DELETE/POST that changes the user's MAL library; the only POST is the OAuth token refresh.

Config keys read/written (all under the `mal` block):
- Read: `mal.client_id`, `mal.client_secret`, `mal.authorization` (with `access_token`, `refresh_token`, `created_at`, `expires_in`), `mal.username`.
- Written (via `config.set("mal", mal)`): `mal.authorization` (replaced with the refreshed dict) and `mal.username` (when newly resolved).

global_cache / Parquet keys: none read or written.

dry_run behavior: not consulted. There is no library-mutating APPLY step to suppress; a token refresh runs regardless of `dry_run` so MAL can be queried later. (No "would ..." logging here.)

Singleton / concurrency / threading notes: as a `BaseManager` subclass it participates in the process-wide singleton cache keyed by `(class, singleton_key)`. The auth check that `Main` runs in parallel before any manager is built covers Radarr/Sonarr/Trakt — MAL's validation happens later, synchronously, inside `MALManager`'s construction. No threading of its own.

## How it functions

Lifecycle: `MALManager.__init__` → `MALInstanceManager(**base)` (shared deps injected, registry registration) → `register_and_validate()` returns a boolean stored as `MALManager.enabled`. There is no `load_components` step and `prepare()` does nothing.

Control flow inside `register_and_validate`:
1. Gate on `client_id` (absent ⇒ disabled, debug-logged).
2. Token check: `not access_token or _expired(auth)` ⇒ call `oauth.mal_refresh_token`. Falsy result ⇒ warn + `False`. Truthy result ⇒ replace `mal["authorization"]`, persist config.
3. `_ensure_username(mal)`: reuse a valid existing username, else `oauth.mal_fetch_username` → validate format → persist.
4. Log OK and return `True`.

Brain delegation: none. This manager makes no value judgements and delegates no decision to any `machine_learning/` module — it is purely credential plumbing.

## Criteria & examples

- **No `client_id`** — MAL is disabled for the run. Example: a fresh install with an empty `mal.client_id` returns `False`, and `MALManager` skips all MAL work without error.
- **Expired-token rule** (`_expired`): a token is expired when `now - created_at > expires_in`. Example: with `created_at = 1_700_000_000` and `expires_in = 2_678_400` (MAL's 31-day default), the token is valid through `1_702_678_400`; a `register_and_validate()` at `1_702_700_000` (≈ 6 hours past expiry) sees `now - created = 2,700,000 > 2,678,400`, so it refreshes. A run at `1_702_000_000` (still inside the window) skips the refresh and reuses the cached token.
- **Malformed/missing timing fields**: if `created_at` or `expires_in` is `0`, missing, or non-numeric, `_expired` returns `True` (fail-safe → refresh). Example: an `authorization` dict with `created_at = "abc"` triggers a refresh rather than trusting a bad timestamp.
- **Username validation** (`_USERNAME_RE = ^[A-Za-z0-9_-]+$`): only alphanumerics, underscore, hyphen are accepted. Example: a stored `username = "anime_fan-99"` is reused as-is; a `username = "bad name!"` fails the regex, so it is re-fetched from `/users/@me`. If the fetched name is also empty or malformed, `_ensure_username` returns `""` and the OK line logs `MAL: ?`.
- **Refresh failure**: `mal_refresh_token` returns `None` if any of `client_id` / `client_secret` / `refresh_token` is missing, or on an HTTP error. Example: a config with a `client_id` but a blank `client_secret` ⇒ refresh returns `None` ⇒ warning "run onboarding to authorize" ⇒ `False`.

## In plain English

Think of this as the doorman for your MyAnimeList account. Before the app tries to do anything with MAL — like checking which anime you've watched — the doorman quickly checks your "ticket" (your login token). If you never bought a ticket (no client ID set up), he politely waves the app past MAL entirely: no MAL today, no drama. If your ticket is stamped but expired (MAL tickets last about 31 days), he quietly swaps it for a fresh one using your stored renewal pass, so you don't have to log in again. He also double-checks your name on the ticket is spelled with normal characters. If he genuinely can't get you a valid ticket, he doesn't crash the party — he just notes "you'll need to log in via setup" and lets the rest of the app carry on. The point: MAL features either work seamlessly or are skipped cleanly, and a stale login never breaks the whole run.

## Interactions

- **Parent manager**: `MALManager` (`scripts/managers/services/mal/__init__.py`) — constructs it and stores `register_and_validate()` as `self.enabled`.
- **Sibling submanagers**: none loaded by this manager.
- **Shared helpers / services it talks to**: `scripts/managers/factories/onboarding/oauth.py` (`mal_refresh_token`, `mal_fetch_username`), which perform the actual HTTP to `myanimelist.net/v1/oauth2/token` and `api.myanimelist.net/v2/users/@me`. It reads/writes the `mal` block via the shared `ConfigManager`, logs via the shared logger, and registers with the shared `RegistryManager`.
- **Brain modules (`machine_learning/`)**: none — no decision is delegated.
