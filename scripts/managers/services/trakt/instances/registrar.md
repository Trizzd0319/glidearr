# TraktInstanceRegistrarManager

- **File** — `scripts/managers/services/trakt/instances/registrar.py`
- **One-liner** — A tiny config-presence checker that confirms the required Trakt credential keys exist before auth is attempted.

## What it does (for a senior Python engineer)

`TraktInstanceRegistrarManager(BaseManager, ComponentManagerMixin)` is a thin validation helper owned by `TraktInstanceManager`. Its sole public method:

- `check_config() -> bool` — reads `config.get("trakt")` and returns `True` only if the dict exists **and** contains all of `client_id`, `client_secret`, and `authorization`. On a missing top-level `trakt` block it logs `[TraktRegistrar] No Trakt configuration found...` and returns `False`; on partial config it logs the specific missing keys and returns `False`.

Position in the tree: `parent_name = "TraktManager"`; it is constructed directly (not via `load_components`) by `TraktInstanceManager.__init__` and called as the first gate inside `register_and_validate()`.

FETCH / CACHE / APPLY: none. No HTTP, no `global_cache`/Parquet, no config writes — it only reads config. Config keys read: `trakt` and the presence of `trakt.client_id`, `trakt.client_secret`, `trakt.authorization`. dry_run: irrelevant (no mutation). Singleton: standard `BaseManager` singleton behaviour.

## How it functions

`__init__` sets `parent_name`, calls `super().__init__(...)` to inherit the shared deps and auto-link to `TraktManager`, then `self.register()`. There is no `load_components` call and no submanagers. `check_config()` is a single membership check over a fixed `required_keys` list. No `machine_learning` delegation.

## Criteria & examples

- **All-present**: `trakt = {"client_id": "abc", "client_secret": "def", "authorization": {...}}` → all three required keys present → returns `True`.
- **Partial**: `trakt = {"client_id": "abc"}` → missing `client_secret`, `authorization` → logs `Missing config keys: client_secret, authorization` → returns `False`.
- **Absent**: no `trakt` block at all → logs `No Trakt configuration found in config.` → returns `False`.

Note: it checks key *presence*, not value validity — an empty-string `client_id` still "passes" the membership test (deeper credential validity is the job of the auth/validator path).

## In plain English

This is the bouncer's quick clipboard check at the door: before anything else happens, it just confirms you brought the three things you need — your app ID, your secret, and a login token. If any are missing it stops you right there with a clear "you forgot your secret" note, instead of letting you walk in and fail confusingly later.

## Interactions

- **Parent**: `TraktInstanceManager` (creates it and calls `check_config()` first thing in `register_and_validate()`).
- **Siblings**: `TraktInstanceSummaryFormatterManager` (same parent).
- **Brain modules / services**: none.
