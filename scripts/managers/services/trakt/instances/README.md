# TraktInstanceManager

- **File** — `scripts/managers/services/trakt/instances/__init__.py`
- **One-liner** — Validates and obtains the Trakt OAuth token (refresh → device flow) and resolves the Trakt username so the parent `TraktManager.prepare()` stays a one-liner.

## What it does (for a senior Python engineer)

`TraktInstanceManager(BaseManager, ComponentManagerMixin)` is the instance/auth manager for the Trakt service. It deliberately inherits from `BaseManager` (the comment notes it is *not* `BaseInstanceManager`, which is arrapi-specific to Radarr/Sonarr), but normalises its summary-logging output to the same `BaseInstanceManager` format.

Position in the manager tree:
- `parent_name = "TraktManager"` — its parent is the top-level `TraktManager` service manager (`scripts/managers/services/trakt/__init__.py`), which constructs it via `self.instance_manager = TraktInstanceManager(**base_kwargs)` and immediately calls `register_and_validate()`.
- It does **not** use `load_components()`. Instead it directly instantiates two submanagers in `__init__` and attaches them as attributes:
  - `self.registrar = TraktInstanceRegistrarManager(...)` — config-key presence check.
  - `self.summary_formatter = TraktInstanceSummaryFormatterManager(...)` — builds a display dict (constructed but not invoked inside this class).

FETCH / CACHE / APPLY:
- FETCH — yes, indirectly. Username resolution issues a single `GET /users/me`; token refresh and device-flow auth POST to Trakt's OAuth endpoints. All HTTP is delegated to `scripts/managers/factories/onboarding/oauth.py` (`fetch_username`, `refresh_token`, `device_flow`); this class never calls `requests` directly.
- CACHE — no `global_cache` / Parquet writes. It persists results into **config** (`config.set("trakt", trakt_cfg)`), updating `trakt.authorization` and `trakt.username`.
- APPLY — not in the deletion/PUT sense; its only mutation is writing the refreshed token/username back to config.

Public methods:
- `register_and_validate() -> bool` — the entry point. Runs the registrar config check, then ensures a live token (refresh if missing/expired, else device flow), resolves the username, and emits one summary line. Returns `True` on success, `False` on any failure (also stamping `load_summary["trakt"]` with ✅/❌).
- `prepare() -> None` — no-op; validation already runs in `register_and_validate()`.

Notable internal helpers (private):
- `_is_token_expired(token)` — pure timestamp check: `(now - created_at) > expires_in`. Future `created_at` is treated as *not* expired (with a warning).
- `_refresh_token(trakt_cfg)` — refreshes via `oauth.refresh_token(...)` using `client_id` / `client_secret` / `authorization.refresh_token`; on success stores the new `authorization` dict and re-resolves the username.
- `_run_device_flow(trakt_cfg)` — runs `oauth.device_flow(...)`, surfacing the "visit URL / enter code" notice via a `print(f"\n📺 {m}")` callback; stores the token and re-resolves the username on success.
- `_ensure_username(trakt_cfg)` — returns an existing validated username, else fetches one and persists it.
- `_fetch_username(access_token, client_id)` — thin wrapper over `oauth.fetch_username`.
- `_finalize(ok)` — emits one `log_info` summary line in `BaseInstanceManager` format, e.g. `[TraktInstanceManager] ✅ 1/1: alice✅`.

Config keys read/written:
- Read: `trakt.client_id`, `trakt.client_secret`, `trakt.authorization` (and within it `access_token`, `refresh_token`, `created_at`, `expires_in`), `trakt.username`.
- Written: `trakt.authorization` (after refresh / device flow), `trakt.username`.

External API endpoints touched (via `oauth.py`): `GET https://api.trakt.tv/users/me`, `POST https://api.trakt.tv/oauth/token`, and the device-code endpoints used by `device_flow`.

Security note: the Trakt username is externally controlled and later flows into cache file paths (`trakt/<username>/...`). A strict allow-list `_USERNAME_RE = ^[A-Za-z0-9_-]+$` is enforced at the point of entry in `_ensure_username` so it can never introduce path separators or traversal segments; both stored and freshly fetched usernames are revalidated and discarded if they fail.

dry_run: this class does not branch on `dry_run`. Token refresh / device-flow are auth bootstrap (run before service managers) and write to config regardless. Note the design intent doc treats auth checks as out-of-band; `auth_validator.py` independently calls `_check_trakt()` before any manager is constructed, so this class keeps per-step noise at `log_debug` and emits only the one summary line at INFO.

Singleton / concurrency: as a `BaseManager`, it is a process-wide singleton keyed by `(class, singleton_key)` and auto-links to its `TraktManager` parent, inheriting the shared logger/config/cache/validator/registry. No explicit threading here; the parallel auth check lives in `main.py` before construction.

## How it functions

Lifecycle:
1. `__init__` — sets `parent_name`, calls `super().__init__(...)` (injecting shared deps + auto-linking to `TraktManager`), `self.register()`, initialises `self.load_summary = {}`, and constructs the `registrar` + `summary_formatter` submanagers with the inherited logger/config.
2. `register_and_validate()` control flow:
   - `registrar.check_config()` — if it fails, stamp ❌, finalize, return `False`.
   - Load `trakt_cfg` and its `authorization` dict.
   - If `authorization` is missing or `_is_token_expired(...)` → try `_refresh_token(...)`; if that fails → try `_run_device_flow(...)`; if both fail, stamp ❌, finalize, return `False`. Otherwise log "token valid".
   - `_ensure_username(trakt_cfg)`, stamp ✅, `_finalize(ok=True)`, return `True`.

All HTTP auth/identity decisions are delegated to the `oauth` onboarding module (FETCH); this class only orchestrates and persists. No `machine_learning` brain module is consulted — auth is mechanical, not a value judgement.

## Criteria & examples

- **Token-expiry rule** (`_is_token_expired`): expired when `(now - created_at) > expires_in`. Example: a token with `created_at = 1_700_000_000`, `expires_in = 7_776_000` (90 days). If `now = 1_700_000_000 + 7_776_001`, then `7_776_001 > 7_776_000` → **expired** → triggers refresh. If `now` is only `+7_775_000` in → still valid → skips refresh.
- **Future-clock guard**: if `created_at > now` (e.g. a clock-skewed `created_at` 100s in the future), it logs a warning and treats the token as **not expired** rather than looping into a refresh.
- **Username allow-list**: a stored username `"alice_99"` matches `^[A-Za-z0-9_-]+$` → kept. A value like `"../etc"` or `"a/b"` fails the regex → ignored/discarded with a warning, returning `""`, so it can never become a `trakt/<username>/...` path-traversal segment.
- **Refresh prerequisites**: `_refresh_token` aborts (returns `False`, warning "Cannot refresh — missing credentials") unless all of `client_id`, `client_secret`, and `authorization.refresh_token` are present — only then does it fall back to the interactive device flow.

## In plain English

Think of this as the doorman who checks your Trakt "season pass" before you walk in. When the app starts, the doorman looks at your pass: if it's still valid he waves you through; if it's expired he quietly renews it using your saved renewal slip; and if there's no slip at all he puts a notice on the screen — "go to this web page and type in this code" — so you can sign in fresh (like activating a Netflix login on a new TV). Once you're in, he also writes down your username so the app knows whose watch-history to look at — and he refuses any username with weird characters in it, the same way a coat-check refuses a ticket someone scribbled on, so nobody can trick the system into rummaging through the wrong folder.

## Interactions

- **Parent**: `TraktManager` (`scripts/managers/services/trakt/__init__.py`), which builds it and calls `register_and_validate()` during its own setup.
- **Sibling/owned submanagers**: `TraktInstanceRegistrarManager` (config-key check) and `TraktInstanceSummaryFormatterManager` (display dict), both instantiated directly in `__init__`.
- **Onboarding helper**: `scripts/managers/factories/onboarding/oauth.py` — `fetch_username`, `refresh_token`, `device_flow` (all the actual Trakt HTTP).
- **External service**: Trakt API (`https://api.trakt.tv`) — `/users/me` and `/oauth/token`.
- **Brain modules**: none — no `machine_learning` delegation.
