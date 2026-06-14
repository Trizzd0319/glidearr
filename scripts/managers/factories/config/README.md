# ConfigManager

**File** — `scripts/managers/factories/config/__Init__.py`
**One-liner** — The process-wide configuration object: it loads `config.json` (overlaying secrets from the OS keyring / env vars), exposes typed getters/setters, and persists changes back to disk with secrets stripped out.

## What it does (for a senior Python engineer)

`ConfigManager` is the canonical handle every other manager receives as its `config` dependency. It is a plain class (NOT a `BaseManager` subclass) constructed once in `scripts/main.py` (`config_manager = config or ConfigManager(logger=logger)`) and then injected down the entire manager tree via `BaseManager.__init__`. So while it is not itself a node in the manager singleton tree, it is the shared config every node reads through.

Construction (`__init__(self, logger=None, config_path="support/config/config.json", **kwargs)`):
1. Resolves a logger (falls back to `LoggerManager()`).
2. Wraps the path in a `Path` and builds a `ConfigLoader(self.path, logger=...)`.
3. Calls `self.loader.load()` to get the in-memory `self.config` dict.
4. Best-effort runs `SecretBootstrap(self.loader, self.logger).ensure(self.config)` inside a `try/except` — this audits where each expected secret resolves from (env / keyring / inline / missing) and, on a first-run interactive TTY, launches a `getpass` wizard. It is wrapped so it never blocks a headless run and never raises fatally (`log_warning("[SecretBootstrap] skipped: ...")` on failure).
5. Builds a `ConfigSanitizer(self.logger)` (for redacted logging) and a `ConfigResolver(self.config, self.logger)` (for service-instance lookups).

Key PUBLIC methods:
- `get(key, default=None)` — `self.config.get(key, default)`. Top-level key lookup only (not a dotted-path resolver).
- `set(key, value)` — sets `self.config[key]` AND immediately calls `self.loader.save(self.config)` (writes through to disk).
- `set_bulk(new_config: dict)` — `self.config.update(new_config)` in memory only; does NOT persist. Caller must follow with `save()`.
- `save()` — `self.loader.save(self.config)`; atomic, owner-only (`0600`) write with secrets stripped to the SecretStore.
- `reload()` — re-runs `self.loader.load()`, replacing `self.config`.
- `get_sonarr_instances()` — delegates to `ConfigResolver.get_instances("sonarr")`, returning the `sonarr_instances` dict (`{}` if absent).
- `get_default_sonarr_instance()` — `ConfigResolver.get_default_instance("sonarr")`: reads the instance name out of `sonarr_instances["default_instance"]` (stored as a `{"name": <instance>}` dict; a bare string is also tolerated for legacy configs), then returns that instance's sub-dict. If the default is missing or points at an unknown name it falls back to the first real instance entry, returning `{}` only when none exist.
- `log_safe_config()` — `ConfigSanitizer.log_redacted(self.config)`: logs a grouped, secret-redacted summary.
- `raw_data` (property) — returns the live `self.config` dict.

Manager tree: not a `BaseManager`; it loads no submanagers via `load_components`. It instead composes three helper objects (`ConfigLoader`, `ConfigSanitizer`, `ConfigResolver`) and best-effort invokes `SecretBootstrap`. It is the `config` dependency that `BaseManager` shares down to every child manager.

FETCH / CACHE / APPLY: none of the three verbs apply — `ConfigManager` touches no HTTP API, no `global_cache`, and no Parquet. It is pure config I/O against the local filesystem (`config.json`) and the OS keyring (via `SecretStore`).

External API endpoints touched: none.

Config keys read: arbitrary top-level keys via `get` (callers pass keys like `dry_run`, `free_space_limit`, etc.); and the `sonarr_instances` map (including its `default_instance` pointer) via the resolver. Secret leaves anywhere in the tree are recognized by key name (`is_secret_key`) and overlaid from env (`RECOMMENDARR_*`) / keyring on load.

`global_cache` / Parquet keys: none read or written.

dry_run behavior: `ConfigManager` itself has no dry-run gate (it is read on its behalf — `main.py` does `config_manager.get("dry_run", False)`). However `set()` and `save()` always write to disk regardless of dry_run; nothing in this class is dry-run-suppressed.

Singleton / concurrency notes: not a `BaseManager` singleton, but effectively a singleton by construction — `main.py` builds one instance and threads it everywhere. There is no internal locking; `save()` is made crash-safe via `tempfile.mkstemp` + `os.replace` (atomic) inside `ConfigLoader`, and secret values are registered with `LoggerManager.register_secrets(...)` on load so they are scrubbed from all log output.

## How it functions

Lifecycle: `__init__` → `ConfigLoader.load()` (read file, overlay secrets, register them for log-scrubbing) → `SecretBootstrap.ensure()` (audit + optional first-run wizard) → build `sanitizer` + `resolver`. After that the object is queried via `get` / the resolver helpers, mutated via `set` / `set_bulk`, and persisted via `save`.

Internal delegation (the heavy lifting lives in the sibling helper files, all out of scope for the "Manager" naming but summarized here because `ConfigManager` is a thin facade over them):
- `ConfigLoader.load()` walks the loaded dict and, for every secret-named leaf, overlays the resolved value from `SecretStore` (env var first, then OS keyring); legacy inline-plaintext secrets are warned about (suggesting `python scripts/support/setup/migrate_secrets.py`).
- `ConfigLoader.save()` deep-copies the config, persists each secret leaf to the `SecretStore`, blanks it on disk when it was safely persisted (kept inline only if nowhere safe to store it), and writes atomically with `0600` permissions.
- `ConfigResolver` resolves per-service instance maps and the default instance.
- `ConfigSanitizer` produces the redacted log summary, grouping by `ConfigGroups.SERVICE_KEYS` and masking values whose key is in `SensitiveKeys.DEFAULT`.

Brain delegation: none. `ConfigManager` makes no value-judgement and delegates nothing to any `machine_learning/` module — it is pure plumbing.

## Criteria & examples

The decisions in this class are about secret placement and instance resolution, not curation thresholds:

- Secret resolution precedence (in `ConfigLoader._overlay_secrets` / `SecretBootstrap.audit`): env wins, then keyring, then inline-plaintext, else missing. Example: a `trakt` `client_secret` present as `RECOMMENDARR_TRAKT_CLIENT_SECRET` is reported as `env` and used even if `config.json` is blank; if the env var is unset but the value lives in Windows Credential Manager, it resolves as `keyring`; if it is only sitting inline in `config.json`, it resolves as `inline` and triggers the migration warning.
- Save-time blanking guard: a secret is blanked on disk only if it was actually persisted to the keyring OR an env var for it is present (`stored or env_present`). Example: with no keyring backend and no env var, a freshly set `api_key` is left inline in `config.json` so it is never lost.
- `get_default_sonarr_instance` resolution: if `sonarr_instances = {"default_instance": {"name": "sonarr"}, "sonarr": {...}}`, the method pulls `"sonarr"` from the marker dict and returns the `"sonarr"` sub-dict; if `default_instance` points at a missing name it falls back to the first real instance entry, returning `{}` only when none exist. (Passing the `{"name": ...}` marker dict straight to `all_instances.get()` would raise `TypeError: unhashable type: 'dict'` — the name is extracted first.)
- `get` is a flat lookup: `get("dry_run", False)` returns the top-level `dry_run` value or `False`; it does NOT descend dotted paths.

## In plain English

Think of `ConfigManager` as the settings binder for the whole app — the single notebook that says which Sonarr/Radarr/Trakt accounts to use, whether to run in "pretend mode" (`dry_run`), and so on. Like the to-watch list pinned to your fridge, every part of the app reads from this one binder instead of keeping its own copy.

Crucially, it keeps your passwords out of that binder. When it reads the settings, it quietly fetches your real API keys from the computer's secure vault (the OS keyring) — the way a streaming app remembers your login without printing your password on the screen. When it saves, it puts the secrets back in the vault and leaves the visible settings file blank where the passwords were, so if you ever shared that file (say, to report a bug with your Princess Bride rewatch automation) you wouldn't accidentally hand over your keys. On a brand-new install it can even walk you through entering those secrets once, then remember that it already asked.

## Interactions

- Parent manager: none (it is a standalone object, not a `BaseManager` node). It is constructed by `Main` in `scripts/main.py` and then injected as the `config` dependency into `BaseManager.__init__`, so every manager in the tree shares this one instance.
- Submanagers: none via `load_components`. It composes `ConfigLoader`, `ConfigSanitizer`, and `ConfigResolver`, and best-effort calls `SecretBootstrap` (which in turn uses `SecretStore`).
- Services / external systems: the local filesystem (`config.json`) and the OS keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service) plus `RECOMMENDARR_*` environment variables, both reached through `SecretStore`. It registers resolved secrets with `LoggerManager` for log-scrubbing.
- Brain modules: none — `ConfigManager` delegates no decision into `machine_learning/`.
