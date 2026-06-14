# RadarrValidatorAuthManager

- **File** — `scripts/managers/services/radarr/validator/auth.py`
- **One-liner** — Verifies that every configured Radarr instance has a non-empty API key in config (and holds a stub for future token refresh).

## What it does (for a senior Python engineer)

`RadarrValidatorAuthManager(BaseManager, ComponentManagerMixin)` is a thin validation leaf under `RadarrValidatorManager`. It performs a config-only authentication sanity check: it does not actually call the Radarr API to test credentials (that is `RadarrValidatorKeysManager.check_instance_reachability`'s job). It simply confirms a key string is present and non-blank.

Position in the tree: `parent_name = "RadarrValidatorManager"`, so it auto-links to the `RadarrValidatorManager` instance. It loads no submanagers of its own. As a `BaseManager` it is a singleton with injected logger/config/global_cache/validator/registry; in `__init__` it additionally resolves `self.radarr_api`, `self.instance_manager`, and `self.dry_run` from kwargs or the parent.

- **FETCH / CACHE / APPLY**: none of the three against the API. `validate_api_keys` only reads config in memory.
- **Public methods**:
  - `validate_api_keys() -> dict` — iterates `config["radarr_instances"]`, reads each instance's `cfg.get("api") or cfg.get("api_key") or ""`, and maps `{instance_name: bool}` where the bool is `True` iff the key is non-empty after `.strip()`. Logs a warning per instance with no key.
  - `_refresh_token() -> bool` — a stub; logs "Token refresh not implemented for Radarr." and returns `False`. (Underscore-prefixed; effectively a placeholder for future OAuth support.)
- **External API endpoints**: none.
- **Config keys read**: `radarr_instances` (and per-instance `api` / `api_key`).
- **global_cache / Parquet keys**: none.
- **dry_run**: captured but unused — this manager is read-only and never mutates state regardless.
- **Singleton / concurrency**: `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` calls `super().__init__`, `self.register()`, resolves deps, and logs an init-debug line. There is no `load_components` call (no children). The only meaningful runtime path is `validate_api_keys`, which a caller (or the parent's orchestration) invokes to get a per-instance pass/fail map. No `machine_learning` brain module is involved — this is a pure presence check.

## Criteria & examples

- A key passes only if `bool(key.strip())` is truthy.
- Example: `radarr_instances = {"1080": {"api": "abc123"}, "4k": {"api_key": "   "}, "720": {}}` →
  - `"1080"` → `True` (non-empty `api`).
  - `"4k"` → `False` (`api_key` is whitespace, `.strip()` → `""`), and a warning "No API key configured for Radarr instance '4k'" is logged.
  - `"720"` → `False` (neither `api` nor `api_key` present → `""`).
  - Returns `{"1080": True, "4k": False, "720": False}`.

## In plain English

Before the system tries to use your Radarr servers, this checks that you actually wrote down a password for each one. It's like a bouncer glancing at the guest list to confirm each name has a ticket number next to it — it doesn't yet try the ticket at the door (another inspector does that), it just makes sure the box isn't blank. If you forgot to fill in the key for your "4K" library, it raises a hand and says "this one has no ticket."

## Interactions

- **Parent manager**: `RadarrValidatorManager`.
- **Sibling submanagers**: `RadarrValidatorCacheManager`, `RadarrValidatorHealthManager`, `RadarrValidatorKeysManager` (the keys sibling is the one that actually does live reachability checks).
- **Other services**: reads `ConfigManager` (`self.config`) only.
- **Brain modules**: none.
