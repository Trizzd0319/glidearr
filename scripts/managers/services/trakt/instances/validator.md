# TraktInstanceValidatorManager

- **File** — `scripts/managers/services/trakt/instances/validator.py`
- **One-liner** — Validates Trakt credentials and token health, and can refresh an expired token directly against Trakt's OAuth endpoint.

## What it does (for a senior Python engineer)

`TraktInstanceValidatorManager(BaseManager, ComponentManagerMixin)` is a standalone credential/token-health validator for the Trakt service. Note: it is *not* instantiated by `TraktInstanceManager` (which only builds the registrar + summary formatter). It is a separate manager class in the same package — `parent_name = "TraktManager"` — used wherever a validate/refresh check is needed independently. Its module docstring advertises the trio `validate_keys()`, `is_token_expired()`, `validate_or_refresh()`.

Public methods:
- `validate_keys() -> bool` — confirms all of `client_id`, `client_secret`, `authorization.access_token`, `authorization.refresh_token` are present (logs the missing set otherwise), logs token age vs lifespan at debug, and returns `False` if `is_token_expired(token)` is true. A pure config + timestamp check — **no HTTP**.
- `is_token_expired(token: dict) -> bool` — pure timestamp check: `(now - created_at) > expires_in`, defaulting `expires_in` to `7_776_000` (90 days) when absent. A future `created_at` is treated as *not* expired (with a warning). Returns `True` on any parse error.
- `validate_or_refresh() -> bool` — returns `validate_keys()` OR, if that fails, attempts `_refresh_token()`.
- `validate() -> bool` — alias that simply calls `validate_or_refresh()`.

FETCH / CACHE / APPLY:
- FETCH — `_refresh_token()` issues `POST https://api.trakt.tv/oauth/token` directly via `requests` (grant_type `refresh_token`, `redirect_uri = urn:ietf:wg:oauth:2.0:oob`, `timeout=30`). This is the one class in the package that calls `requests` itself rather than delegating to `onboarding/oauth.py`.
- CACHE — none in the `global_cache`/Parquet sense.
- APPLY — on successful refresh it writes the new `authorization` dict back to config via `config.set("trakt", trakt_cfg)`.

Config keys read: `trakt.client_id`, `trakt.client_secret`, `trakt.authorization.{access_token, refresh_token, created_at, expires_in}`. Config keys written: `trakt.authorization` (after a successful refresh, preserving prior fields via `token_info.copy()` and stamping a fresh `created_at = int(time.time())` and `expires_in` from the response, default `7_776_000`).

dry_run: this class does not branch on `dry_run`; refresh is auth bootstrap and writes config unconditionally. Singleton/concurrency: standard `BaseManager` singleton; `__init__` logs whether an access token is currently set. No threading of its own.

## How it functions

`__init__` sets `parent_name`, calls `super().__init__(...)` (shared-dep injection + auto-link to `TraktManager`), `self.register()`, then debug-logs token presence. No `load_components`, no submanagers.

Control flow of `validate_or_refresh()`: try the cheap local checks (`validate_keys` → keys present + not expired); only if those fail does it spend a network call on `_refresh_token`. `_refresh_token` guards on `client_id`/`client_secret`/`refresh_token` all being present, POSTs to `/oauth/token`, `raise_for_status()`, verifies `access_token` is in the response, merges the new tokens onto a copy of the old auth dict, and persists. All exceptions are caught and logged with a traceback, returning `False`.

No `machine_learning` brain delegation — token validity is mechanical.

## Criteria & examples

- **Missing creds**: `authorization` has `access_token` but no `refresh_token` → `validate_keys` logs `Missing credentials: refresh_token` → returns `False` → `validate_or_refresh` then tries `_refresh_token`, which itself bails (no refresh token) → overall `False`.
- **Expired token**: `created_at = 1_700_000_000`, `expires_in = 7_776_000`, `now = created_at + 7_776_500` → `7_776_500 > 7_776_000` → `is_token_expired` True → `validate_keys` returns `False` → refresh attempted.
- **Default lifespan**: a token dict with no `expires_in` → `is_token_expired` assumes `7_776_000`s (90 days) before considering it stale.
- **Future clock**: `created_at = now + 100` → warning logged, treated as *not* expired (avoids a refresh loop on a skewed clock).
- **Successful refresh**: `/oauth/token` returns a JSON body containing `access_token` → merged onto the prior auth dict, `created_at` reset to current time, `expires_in` taken from the response (or 90-day default), written to config, returns `True`.

## In plain English

This is a second, more thorough doorman who can also renew your pass on the spot. First he checks the quick stuff for free — do you have all four parts of your credentials, and is your pass still in date? If yes, you're done. If your pass has lapsed, instead of turning you away he phones Trakt's office, swaps your old renewal slip for a brand-new pass, files it away, and lets you in — all without bothering you. Only if even that fails does he give up. It's the difference between "your gym card expired, come back later" and "your card expired, hang on, I'll print you a new one right now."

## Interactions

- **Parent**: `TraktManager` (the package's top-level service manager); auto-linked via `parent_name`.
- **Siblings**: `TraktInstanceManager`, `TraktInstanceRegistrarManager`, `TraktInstanceSummaryFormatterManager` (same package / parent).
- **External service**: Trakt API — `POST https://api.trakt.tv/oauth/token` (called directly, not via the shared `oauth` helper).
- **Brain modules**: none.
