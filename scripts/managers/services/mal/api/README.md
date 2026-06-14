# MALAPIManager

- **File** — `scripts/managers/services/mal/api/__init__.py`
- **One-liner** — A thin authenticated HTTP client for the MyAnimeList (MAL) API v2 that attaches the Bearer token, throttles requests, transparently refreshes the token once on a 401, and exposes a handful of read/write helpers over a user's anime list.

## What it does (for a senior Python engineer)

`MALAPIManager` is the FETCH/APPLY transport layer for MyAnimeList. It mirrors the role of `TraktAPIManager`: there is a single private `_make_request` that all public methods funnel through, which attaches the `Authorization: Bearer <token>` header, throttles, parses JSON, and performs a one-shot token refresh on a 401.

Notable design points:

- **Not a `BaseManager` subclass.** Despite the name, this is a plain class (`class MALAPIManager:` with no base). It does *not* participate in the `BaseManager` singleton/registry tree, does not call `load_components`, and is not auto-linked to a parent. It is constructed manually by whatever MAL service manager owns it, receiving only `config` and `logger` via its constructor — `__init__(self, config=None, logger=None)`. Both are optional and the class degrades gracefully (returns fallbacks, skips logging) when either is `None`.
- **Reads the token live on every call.** `_token()` re-reads `config.get("mal", ...)["authorization"]["access_token"]` each request rather than caching it, so a token refresh performed elsewhere (e.g. by onboarding or another manager) is picked up automatically.

### Module-level constants

- `_BASE = "https://api.myanimelist.net/v2"` — API root; relative paths are joined to this.
- `_TIMEOUT = 15` — per-request timeout in seconds.
- `_MIN_INTERVAL = 0.2` — minimum seconds between calls (gentle client-side throttle).
- `MALAPIManager._LIST_FIELDS` — the default `fields` query string requested for anime objects: `list_status,num_episodes,genres,mean,media_type,start_season,alternative_titles`.

### Public methods

**Reads (FETCH):**

- `get_anime_list(status="", limit=1000) -> list` — Returns the authenticated user's anime list from `users/@me/animelist`. Requests `_LIST_FIELDS`, caps `limit` at 1000, sets `nsfw="true"`, and optionally filters by `status` (e.g. `watching`, `completed`) when provided. Paginated via `_paged`.
- `get_suggestions(limit=30) -> list` — Returns MAL's personalized suggestions from `anime/suggestions` (caps `limit` at 100, single page).
- `get_seasonal(year, season, limit=30) -> list` — Returns the seasonal anime chart from `anime/season/{year}/{season}` sorted by `anime_num_list_users` (caps `limit` at 100, single page).
- `get_anime(anime_id, fields=_LIST_FIELDS) -> dict` — Fetches a single anime by id from `anime/{anime_id}`; returns `{}` on failure.
- `search_anime(title, limit=5) -> list` — Searches `anime` by query string (`q`), truncating the title to 64 chars; single page.

**Write (APPLY):**

- `update_list_status(anime_id, *, status=None, score=None, num_watched_episodes=None) -> dict` — `PATCH anime/{anime_id}/my_list_status`. Builds a `data` body containing only the keyword args that were supplied (coercing `score` and `num_watched_episodes` to `int`). If no fields were passed, it short-circuits and returns `{}` without making a request.

### FETCH / CACHE / APPLY

- **FETCH** — yes; all five read methods are HTTP GETs against MAL v2.
- **CACHE** — **no.** This class does no caching itself. It returns raw deserialized JSON (lists/dicts) to its caller; persistence to `global_cache` / Parquet (if any) is the responsibility of the owning MAL service manager, not this transport layer.
- **APPLY** — yes; `update_list_status` performs the PATCH write-back.

### External API endpoints touched

- `GET users/@me/animelist`
- `GET anime/suggestions`
- `GET anime/season/{year}/{season}`
- `GET anime/{anime_id}`
- `GET anime` (search via `q`)
- `PATCH anime/{anime_id}/my_list_status`
- `POST` to the MAL OAuth token endpoint — performed indirectly via `oauth.mal_refresh_token` during a 401 refresh.

### Config keys read / written

Read from the `mal` config block:

- `mal.authorization.access_token` — Bearer token used on every request.
- `mal.authorization.refresh_token` — used by `_refresh`.
- `mal.client_id`, `mal.client_secret` — used by `_refresh`.

Written:

- On a successful refresh, `_refresh` replaces `mal["authorization"]` with the new token dict and persists it via `self.config.set("mal", mal)` (only when `self.config` is present).

### global_cache / Parquet keys

None read or written by this class.

### dry_run behavior

**None.** This class has no `dry_run` awareness. `update_list_status` issues a real PATCH whenever called. Any "would …" dry-run gating for MAL write-backs must be enforced by the calling service manager *before* it invokes `update_list_status`.

### Singleton / concurrency / threading notes

- Not a `BaseManager` singleton; an ordinary object instantiated by its owner.
- Holds a single mutable instance field `self._last_call` (a `time.monotonic()` timestamp) used by `_throttle`. There is no lock around it, so the throttle is only correct under single-threaded use; concurrent calls on one instance could race on the timestamp. The 0.2 s sleep is a courtesy throttle, not a hard rate limiter.

## How it functions

Lifecycle is simple — there is no `load_components` and no run loop:

1. **Init** — `__init__` stores `config` and `logger` and zeroes `self._last_call`. No network or registry activity.
2. **Per call** — every public method delegates to `_make_request` (directly, or via `_paged` for list endpoints).

Internal control flow of `_make_request(path, method="GET", params, data, fallback, _retry=True)`:

1. Read the live token via `_token()`. If empty, return `fallback` immediately (no request).
2. `_throttle()` — sleep so at least `_MIN_INTERVAL` (0.2 s) has elapsed since the previous call, then stamp `_last_call`.
3. Build the URL — use `path` verbatim if it already starts with `http` (so the `paging.next` absolute URLs from MAL work), else join to `_BASE`.
4. Issue `requests.request(...)` with the Bearer header and a 15 s timeout.
5. **401 handling** — if the response is 401 and `_retry` is still True, call `_refresh()`; on success, retry exactly once with `_retry=False` (preventing infinite refresh loops).
6. On 2xx, return parsed JSON (`{}` if the body is empty); on any other status, log a warning and return `fallback`; on exception, log a warning and return `fallback`.

`_paged(path, params, max_pages=10)` walks MAL's cursor pagination: it accumulates each response's `data` list, follows `paging.next` (an absolute URL) up to `max_pages` (default 10; the read helpers that wrap it pass `max_pages=1` for single-page endpoints), and passes `params` only on the first page since the `next` URL already carries the query string.

`_refresh()` reuses the shared MAL OAuth helper `oauth.mal_refresh_token(client_id, client_secret, refresh_token, logger=...)` (from `scripts/managers/factories/onboarding/oauth.py`). On a non-falsy result it stores the new authorization dict back into config and returns `True`; otherwise `False`.

No decision is delegated to any `machine_learning` brain module — this is pure transport. Value judgements (which anime to update, what score to write) are made upstream by the owning MAL service manager.

## Criteria & examples

- **Empty-token guard** — `_make_request` returns the caller-supplied `fallback` (e.g. `{}` or `[]`) without touching the network when `access_token` is `""`. Example: if MAL has never been authorized, `get_anime_list()` returns `[]` silently instead of erroring.
- **Single refresh retry** — a 401 triggers at most one `_refresh()` + retry. Example: the access token expired but the refresh token is valid → first request gets HTTP 401 → `_refresh()` swaps in a fresh token and persists it to config → the request is replayed once and succeeds. If the second attempt also 401s, it falls through to the warning + `fallback` path rather than looping.
- **Limit clamping** — `get_anime_list(limit=5000)` sends `limit=1000` (the `min(limit, 1000)` cap); `get_suggestions(limit=500)` sends `limit=100`. These mirror MAL's per-endpoint maximums.
- **Pagination cap** — `get_anime_list` uses the default `max_pages=10`, so a list of 1000-per-page entries is followed for up to 10 pages before stopping, even if MAL keeps offering a `paging.next`.
- **No-op write guard** — `update_list_status(12345)` with no `status`/`score`/`num_watched_episodes` builds an empty `data` and returns `{}` *without* issuing a PATCH. By contrast `update_list_status(12345, score=8)` sends `{"score": 8}`.
- **Title truncation** — `search_anime("a very long title …", limit=5)` truncates the query to its first 64 characters before sending.

## In plain English

Think of this as the polite courier between Glidearr and your MyAnimeList account. When the app wants to know which anime you're watching, or wants to update "Attack on Titan" to *completed* with a score of 9, this courier carries the message. It always shows your membership card (the Bearer token) at the door, waits a fraction of a second between trips so it never bangs on MAL's door too fast, and if the card has expired it quietly gets a new one and walks right back in — but it will only try that once, so it never gets stuck in a loop. If you've never linked your MAL account, the courier simply shrugs and comes back empty-handed rather than causing a fuss. It does not decide *what* to update or *how* you should rate a show — it only delivers what it's told to.

## Interactions

- **Owner / parent** — constructed and owned by the MAL service manager (one directory up, `scripts/managers/services/mal/`), which supplies the `config` and `logger`. It is *not* part of the `BaseManager` registry tree itself.
- **Submanagers** — none; it loads no components.
- **Shared helper** — `scripts/managers/factories/onboarding/oauth.py` `mal_refresh_token(...)` for OAuth token refresh (the same helper used during onboarding).
- **External service** — the MyAnimeList API v2 (`https://api.myanimelist.net/v2`) plus its OAuth token endpoint via the oauth helper.
- **Brain modules** — none. This class delegates no decisions to `machine_learning/`.
