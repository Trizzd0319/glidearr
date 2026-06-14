# SonarrSyncNamingManager

- **File** — `scripts/managers/services/sonarr/sync/naming.py`
- **One-liner** — Pushes a single Sonarr episode-naming configuration to every configured instance, lightly sanitizing the format-string fields first.

## What it does (for a senior Python engineer)

`SonarrSyncNamingManager(BaseManager, ComponentManagerMixin)` standardizes file/episode naming across instances. Given a naming config dict (typically read from a base/source instance), it trims the format-string fields and PUTs the result to each instance's `config/naming` endpoint.

Position in the manager tree:
- **Parent** — resolved from the class name: `parent_name` becomes `"SonarrSyncNaming"` (class name minus `"Manager"`; the literal `"SonarrStorage"` default is overwritten). Falls back to the parent's `sonarr_api` / `logger` / `manager` if not injected.
- **Submanagers** — none (leaf).

FETCH / CACHE / APPLY:
- APPLY — Sonarr `config/naming` (PUT, payload = sanitized naming config).
- FETCH / CACHE — none.

External API endpoints touched: Sonarr `config/naming` (PUT).

Config keys read: `self.config.get_sonarr_instances()` (the instance list).

global_cache / Parquet keys: none.

dry_run behavior: `self.dry_run` is captured in `__init__` (note: it is set twice — once via the `kwargs.get("dry_run", parent default)` pattern, then re-set to `kwargs.get("dry_run", False)` at the end of `__init__`, so the effective value is the passed `dry_run` or `False`). In `sync_naming_settings`, when `self.dry_run` is True, each instance logs `[DRY-RUN] Would apply naming config to <instance>.` and no PUT is sent.

Singleton / concurrency: BaseManager singleton. No threading.

Public methods:
- `sanitize_naming_format(fmt)` → str — returns `fmt.strip()` (or the falsy input unchanged). Deliberately does **no** regex manipulation so Sonarr naming tokens like `{Series Title}` are preserved.
- `sync_naming_settings(naming_config)` — copies the config, sanitizes the three format fields, then PUTs to every instance (or logs dry-run lines).

## How it functions

`__init__` is the standard leaf pattern (BaseManager wiring, `register()`, parent/dep resolution, logger guard), plus the duplicated `dry_run` assignment noted above.

`sync_naming_settings(naming_config)`:
1. Copies `naming_config` (shallow) so the caller's dict isn't mutated.
2. For each of `["standardEpisodeFormat", "dailyEpisodeFormat", "animeEpisodeFormat"]` present in the config, runs `sanitize_naming_format` and logs `🧽 Cleaned naming format field: <field>` only if trimming actually changed the value.
3. Gets instances via `config.get_sonarr_instances()`; if empty, logs a warning and returns.
4. For each instance: if `self.dry_run`, log the dry-run line; otherwise PUT `config/naming` with the sanitized payload, logging ✅ on success and ❌ with the exception on failure (errors are caught per-instance, not propagated).

Brain delegation: none.

## Criteria & examples

- **Only three fields are sanitized**, and only by whitespace trim. Example: `standardEpisodeFormat = "  {Series Title} - S{season:00}E{episode:00}  "` becomes `"{Series Title} - S{season:00}E{episode:00}"` and logs the "Cleaned" line. The token braces are untouched.
- **No-op trim is silent.** If a field is already trimmed, no "Cleaned" log is emitted.
- **Empty/falsy format** passes through unchanged (`sanitize_naming_format("")` returns `""`).
- **No instances** → logs `⚠️ No Sonarr instance configured for naming sync.` and does nothing.
- **Dry-run** → with two instances configured, two `[DRY-RUN] Would apply naming config to ...` lines and zero PUTs.

## In plain English

Every show file needs a tidy name like `The Office - S03E12.mkv` instead of a messy one. You decide the naming style once, and this manager copies that exact style onto every TV server you run, after first nipping off any stray spaces around the edges of the pattern (without touching the actual placeholders). In a "dry run" it just tells you "I would apply this naming style here" so you can preview before committing.

## Interactions

- **Parent** — `SonarrSyncManager` (registered as `SonarrSyncNaming`).
- **Sibling submanagers** — `SonarrSyncCustomFormatsManager`, `SonarrSyncFoldersManager`, `SonarrSyncMediaManager`, `SonarrSyncTagsManager`.
- **Services** — Sonarr API (`config/naming` PUT).
- **Brain modules** — none.
