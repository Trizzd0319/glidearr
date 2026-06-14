# TraktInstanceSummaryFormatterManager

- **File** — `scripts/managers/services/trakt/instances/summary.py`
- **One-liner** — A small read-only formatter that summarises the current Trakt auth state (username, client id, whether a token is set, and its computed expiry) as a plain dict.

## What it does (for a senior Python engineer)

`TraktInstanceSummaryFormatterManager(BaseManager, ComponentManagerMixin)` produces a human-readable snapshot of the Trakt configuration. Public method:

- `format_summary() -> dict` — reads `config.get("trakt")` and its `authorization` block and returns:
  - `username` — `trakt.username`, falling back to `authorization.username`, then `"unknown"`.
  - `client_id` — `trakt.client_id`, else `"not set"`.
  - `token_set` — `bool(authorization.access_token)`.
  - `expires_at` — ISO-8601 UTC string from `_format_expiry(created_at, expires_in)`, e.g. `2025-12-01T12:00:00Z`.

Position in the tree: `parent_name = "TraktManager"`; constructed directly by `TraktInstanceManager.__init__` (as `self.summary_formatter`), not via `load_components`. Note: in the current code the parent assigns it but does not invoke `format_summary()` itself, so this manager is wired in but its output is consumed elsewhere (or is available for diagnostics) rather than driven from `register_and_validate()`.

FETCH / CACHE / APPLY: none — no HTTP, no `global_cache`/Parquet, no config writes. It only reads config. dry_run: irrelevant (no mutation). Singleton: standard `BaseManager` behaviour.

Internal helper: `_format_expiry(created_at, expires_in)` computes `int(created_at) + int(expires_in)` as a Unix timestamp and renders `datetime.utcfromtimestamp(ts).isoformat() + "Z"`. Returns `"unknown"` if either input is falsy and `"invalid"` if parsing/conversion raises.

Config keys read: `trakt.username`, `trakt.client_id`, `trakt.authorization.{username, access_token, created_at, expires_in}`.

## How it functions

`__init__` sets `parent_name`, calls `super().__init__(...)` (shared-dep injection + auto-link to `TraktManager`), then `self.register()`. No `load_components`, no submanagers, no `machine_learning` delegation. `format_summary()` is a pure read-and-shape of config; `_format_expiry` is the only branching logic.

## Criteria & examples

- **Token present, valid timestamps**: `created_at = 1_700_000_000`, `expires_in = 7_776_000` → `ts = 1_707_776_000` → `expires_at` rendered via `utcfromtimestamp`, `token_set = True`.
- **Missing expiry inputs**: `created_at = None` (or `expires_in` missing) → `expires_at = "unknown"`.
- **Garbage inputs**: `created_at = "notanumber"` → `int(...)` raises → caught → `expires_at = "invalid"`.
- **No username anywhere**: neither `trakt.username` nor `authorization.username` set → `username = "unknown"`.

## In plain English

This is the little "account info" card you'd see in an app's settings screen: who you're signed in as, which app key you're using, whether you actually have a login token, and when that token runs out — written in a tidy, readable way. It doesn't change anything; it just reports the current state at a glance, like the membership summary on the back of a gym card.

## Interactions

- **Parent**: `TraktInstanceManager` (owns it as `self.summary_formatter`).
- **Siblings**: `TraktInstanceRegistrarManager` (same parent).
- **Brain modules / services**: none.
