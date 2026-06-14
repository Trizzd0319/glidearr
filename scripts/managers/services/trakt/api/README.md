# TraktAPIManager

- **File** — `scripts/managers/services/trakt/api/__init__.py`
- **One-liner** — The central authenticated HTTP layer for all Trakt TV API calls; it owns the OAuth bearer token, rate-limiting/retry logic, and constructs every Trakt sub-manager (history, ratings, recommendations, watchlist, lookup, analytics, universe, progress, lists, sync).

## What it does (for a senior Python engineer)

`TraktAPIManager(BaseManager, ComponentManagerMixin)` is the single point through which all Trakt traffic flows. Its responsibilities split into three areas: auth/session management, rate-limited HTTP, and sub-manager construction.

**Position in the manager tree.** `parent_name = "TraktManager"`, so it auto-links (via `BaseManager`) to `TraktManager` and inherits its logger/config/cache/validator. It is itself the parent of ten Trakt sub-managers. Note: although it mixes in `ComponentManagerMixin`, it does **not** call `load_components` — it instantiates sub-managers manually in a loop (see below).

**FETCH / CACHE / APPLY.** This manager itself performs only the transport half of FETCH: it issues raw HTTP requests and returns parsed JSON. It does no caching and writes no Parquet/`global_cache` keys of its own — caching and decision-application live in the sub-managers and the brain layer. Because `_make_request` supports `method="POST"`/`"DELETE"` etc., it is also the conduit through which sub-managers perform APPLY writes (e.g. sync add/remove), but the value judgement of *what* to write is not made here.

**Key public methods.**
- `get_username() -> str` — returns the configured Trakt username, defaulting to `"me"` (the OAuth-authenticated user) when unset.
- `_make_request(endpoint, method="GET", params=None, data=None, fallback=None, _retry=True)` — the workhorse. Technically prefixed `_` but it is the de-facto public surface sub-managers call. Builds `https://api.trakt.tv/<endpoint>`, throttles, sends, and returns `resp.json()` (or `fallback` on any non-2xx/empty body/exception). Args of note: `params` → query string, `data` → JSON body, `fallback` → what to return on failure (lets callers serve last-good cache), `_retry` → internal recursion guard so a retry only happens once.

**Auth helpers (private).**
- `_is_configured()` — true iff `client_id` is set.
- `_is_token_expiring()` — true when within `_TOKEN_BUFFER` (86 400 s = 1 day) of `token_expires_at`.
- `_sync_session_headers()` — sets `Content-Type`, `trakt-api-version: 2`, `trakt-api-key` (= client_id), and `Authorization: Bearer <token>` on the shared `requests.Session`.
- `_refresh_token()` — POSTs to `/oauth/token` with `grant_type=refresh_token`; on success updates the in-memory token fields, **persists** the new auth blob back via `self.config.set("trakt", ...)`, and re-syncs headers. Returns bool.
- `_throttle()` — sliding-window limiter (see Criteria).

**External API endpoints touched directly.** `POST https://api.trakt.tv/oauth/token` (refresh). All other endpoints are passed in by sub-managers via `_make_request`.

**Config keys read.** Under `trakt`: `client_id`, `client_secret`, `username`, and the `authorization` sub-object (`access_token`, `refresh_token`, `created_at`, `expires_in`). `token_expires_at` is derived as `created_at + expires_in`. On refresh it **writes back** the whole `trakt.authorization` object.

**`global_cache` / Parquet keys.** None read or written by this class directly.

**dry_run.** `self.dry_run` is captured from kwargs or the parent. This class does not itself branch on `dry_run` (it has no APPLY decisions of its own); the flag is threaded down to sub-managers via `init_kwargs` so each leaf manager honours it. Note: token refresh and config persistence happen regardless of dry_run, since auth is infrastructure, not a library mutation.

**Singleton / concurrency.** Via `BaseManager` it is a process-wide singleton keyed by `(class, singleton_key)`. A single `requests.Session` is shared by all sub-managers. `_throttle()` is guarded by `self._throttle_lock` (a `threading.Lock`); the rate-limit sleep is deliberately held *inside* the lock so concurrent threaded callers serialise rather than all bursting past the 5-minute window.

## How it functions

Lifecycle: `__init__` → `super().__init__` (injects shared deps, auto-links parent) → `self.register()` → read `trakt` config into auth fields → build the `requests.Session` and call `_sync_session_headers()` → instantiate sub-managers → log a debug line reporting `configured` and `token_ok`.

Sub-manager construction does **not** use `load_components`. Instead it builds a single `init_kwargs` dict (shared logger/config/cache/validator/registry, `manager=self`, `dry_run`, and crucially `trakt_api=self` so children can make HTTP calls), lazily imports each sub-manager class (lazy to break the circular import, since children import this module), then loops over a `sub_classes` map setting each as an attribute (`self.history`, `self.ratings`, …). Any sub-manager that raises during construction is logged as a warning and set to `None` rather than aborting the whole tree.

Request flow inside `_make_request`: bail early returning `fallback` if not configured → proactively refresh if the token is within a day of expiry → `_throttle()` → send. On `429`: read `Retry-After`; if it exceeds `_MAX_429_WAIT` (30 s) **or** this is already a retry, set `self.rate_limited = True`, log, and return `fallback` (so the caller serves cached data) — otherwise sleep and retry once. On `401` (first try only): refresh the token and retry once. On `404`: return `fallback`. Otherwise `raise_for_status()` and return JSON.

No decision is delegated to a `machine_learning` brain module here — this is pure transport.

## Criteria & examples

- **Rate limit:** `_RATE_LIMIT = 1000` requests per `_RATE_WINDOW = 300` s sliding window. `_throttle()` drops timestamps older than 300 s; if 1 000 remain, it sleeps `300 − (now − oldest) + 0.1` s. Example: if the oldest of 1 000 in-window requests was 250 s ago, the next call sleeps `300 − 250 + 0.1 = 50.1` s before proceeding.
- **Token refresh buffer:** `_TOKEN_BUFFER = 86 400` s. Example: a token with `token_expires_at` at 14:00 today triggers a proactive refresh on any request issued after 14:00 *yesterday* (`_is_token_expiring()` true).
- **429 cap:** `_MAX_429_WAIT = 30` s. Example: a `429` with `Retry-After: 45` exceeds the 30 s cap, so the fetch is skipped, `rate_limited` is set, and `fallback` (last-good cache) is returned — preventing a long rate-limit window from hanging the run. A `429` with `Retry-After: 8` instead sleeps 8 s and retries exactly once.
- **Retry depth:** retries are bounded to one attempt via `_retry=False` on the recursive call, for both the 401 and 429 paths.

## In plain English

Think of this class as the front desk for a members-only club (Trakt) where the app keeps notes on what your household has watched and rated. The front desk holds your membership card (the access token) and quietly renews it a day before it expires so you're never turned away mid-visit. It also politely limits how fast it knocks on the door — no more than 1 000 knocks every five minutes — so the club doesn't get annoyed and slam the door. If the club says "come back in 45 seconds," the desk decides that's too long to make everyone wait, so it just uses yesterday's notes (the cached data) instead and moves on. Every specialised clerk behind the desk — the one for your ratings, the one for your watchlist, the one tracking how far you got into *The Mandalorian* — uses this same front desk to talk to the club.

## Interactions

- **Parent manager:** `TraktManager`.
- **Sibling/child sub-managers it constructs and owns:** `TraktHistoryManager` (`history`), `TraktRatingsManager` (`ratings`), `TraktRecommendationsManager` (`recommendations`), `TraktWatchlistManager` (`watchlist`), `TraktLookupManager` (`lookup`), `TraktAnalyticsManager` (`analytics`), `TraktUniverseManager` (`universe`), `TraktProgressManager` (`progress`), `TraktListsManager` (`lists`), `TraktSyncManager` (`sync`). Each receives `trakt_api=self` and routes its HTTP through `_make_request`.
- **External services:** the Trakt TV REST API (`https://api.trakt.tv`).
- **Brain modules:** none directly — this layer is pure FETCH transport and delegates no value judgements to `machine_learning`.
