# SonarrValidatorAuthManager

**File** — `scripts/managers/services/sonarr/validator/auth.py`
**One-liner** — A placeholder auth/token-refresh manager for Sonarr; currently a stub because Sonarr uses static API keys (no OAuth / token expiry to refresh).

## What it does (for a senior Python engineer)

`SonarrValidatorAuthManager(BaseManager, ComponentManagerMixin)` is the `auth_handler` component of the validator subtree. It sets `self.parent_name = self.__class__.__name__`.

State at init:
- Dual cache: `self.sonarr_cache`, `self.global_cache`.
- `self.sonarr_api` — from the `sonarr_api` kwarg or the manager's `sonarr_api` attribute.
- `self.logger` — falls back to the manager's logger if not already set.
- `self.manager`, `self.dry_run` (from kwarg/manager, default `False`).
- Raises `ValueError` if no logger could be resolved.

Public/notable methods:
- `_refresh_token() -> bool` — Stub. Logs `🔁 Token refresh not implemented for Sonarr.` and returns `False`. It is a placeholder for any future Sonarr OAuth / token-expiry support and performs no network or state changes today.

FETCH / CACHE / APPLY: none — it is an inert placeholder. No endpoints, no config keys, no cache reads/writes. `dry_run` is stored but irrelevant (nothing mutates).

Wiring: loaded as the CRITICAL `auth_handler` component by `SonarrValidatorManager`. It is "critical" for subtree-load accounting (its construction must succeed for `sonarr.validator_manager_initialized` to be true), even though its only method is a no-op.

Singleton / concurrency: standard `BaseManager` caching; no threading.

## How it functions

Lifecycle: `__init__` (resolve dual cache, sonarr_api, logger fallback, manager, dry_run; register) → it exposes only `_refresh_token`, which always returns `False`. There is no `run()` and no real work; it exists so the validator subtree has a named auth slot and so future token-refresh logic has a home without changing the manager wiring.

No machine_learning delegation.

## Criteria & examples

- There is exactly one behavior: `_refresh_token()` → logs the "not implemented" line → returns `False`. Example: a caller that does `if not auth_handler._refresh_token(): proceed_with_static_api_key()` will always take the `proceed` branch, because Sonarr's API key does not expire and there is nothing to refresh.

## In plain English

Sonarr just uses a fixed password (the API key) that never expires, so there's nothing to renew. This component is a labeled-but-empty mail slot reserved for "renew the login token" — if Sonarr ever switches to the kind of login that expires (like signing in with Google, where you sometimes have to re-authenticate), the renewal code would go here. For now it just politely says "nothing to refresh" and does nothing, keeping the room reserved for a future feature.

## Interactions

- **Parent manager:** loaded as the critical `auth_handler` by `SonarrValidatorManager`.
- **Siblings:** `SonarrValidatorKeysManager` (which actually validates the static keys), `SonarrValidatorHealthManager`, `SonarrValidatorCacheManager`, `SonarrValidatorFactoryManager`.
- **External:** holds a reference to `sonarr_api` but does not use it yet.
- **Brain modules:** none.
