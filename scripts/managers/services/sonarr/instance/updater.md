# SonarrInstanceUpdaterManager

- **File** — `scripts/managers/services/sonarr/instance/updater.py`
- **One-liner** — A small config-write helper that flips the `failed` flag on Sonarr instance entries in `config.json` based on validation outcomes, and persists the change.

## What it does (for a senior Python engineer)

`SonarrInstanceUpdaterManager` subclasses `BaseManager` + `ComponentManagerMixin`. It is the APPLY-to-config arm of `SonarrInstanceManager`: it does not touch the Sonarr HTTP API at all — it only reads and rewrites the `sonarr_instances` section of `config.json`.

**Where it sits in the manager tree.** Built eagerly inside `SonarrInstanceManager.__init__` as `self.updater`. Its declared `parent_name` is `"SonarrManager"`, and in `__init__` it looks that parent up in the registry to inherit `sonarr_api`, `logger`, and `manager`. It loads no submanagers of its own (no `load_components` call despite mixing in `ComponentManagerMixin`).

**FETCH / CACHE / APPLY.** APPLY only — and specifically to the local config file, not to Sonarr. No HTTP, no Parquet, no `global_cache` writes.

**External API endpoints touched.** None.

**Config keys read / written.** Reads and writes `sonarr_instances` via `self.config.get(...)` / `self.config.set(...)`. Within each instance's sub-dict it sets or pops the `failed` key.

**global_cache / Parquet keys.** None. It stores references to `sonarr_cache` and `global_cache` (resolved from kwargs or the parent) but does not read/write them in this file.

**dry_run behavior.** It captures `self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))`. NOTE: in the code as written, `apply_corrections` calls `self.config.set(...)` unconditionally — it does not branch on `self.dry_run`. So the `failed`-flag bookkeeping is treated as operational state that advances even in a dry run, consistent with "the clock/state still advances where noted." If a future change must suppress the config write under dry_run, this is where the guard would go.

**Concurrency / singleton.** Standard `BaseManager` singleton semantics. No threading or locking of its own; config writes are not guarded here beyond whatever `ConfigManager.set` provides.

### Public methods

- `apply_corrections(validation_results)` — the single public entry point. `validation_results` is a dict mapping instance name → outcome string (`"fail"`, `"success"`, or anything else). Returns the (possibly mutated) `sonarr_instances` dict; returns `{}` if the config section is missing/corrupt.

## How it functions

1. **Init.** Sets `parent_name = "SonarrManager"`, calls `super().__init__`, resolves dual caches (`sonarr_cache`, `global_cache`) from kwargs or the parent manager, calls `self.register()`, then pulls `sonarr_api` / `logger` / `manager` / `dry_run` off the registry-resolved parent. It raises `ValueError` if no logger could be obtained (a hard fail-fast), then logs an init debug line.

2. **apply_corrections control flow:**
   - Reads `sonarr_instances`; if it is not a `dict`, logs an error and returns `{}` (refuses to operate on corrupt config).
   - Iterates each `instance_name → result`:
     - Skips entries whose own config is not a dict (warns).
     - `result == "fail"`: if `failed` is already set, skip (idempotent); otherwise set `failed = True` and mark `updated`.
     - `result == "success"`: if `failed` is currently set, pop it (recovery) and mark `updated`; otherwise no-op.
     - any other result (e.g. `"recovered"`, intermediate states): logs a debug "no action needed" line and leaves the entry untouched.
   - If anything changed, `self.config.set("sonarr_instances", ...)` persists it and logs a save line; otherwise logs "no corrections needed."
   - Returns the `sonarr_instances` dict.

**Brain delegation.** None. No `machine_learning/` module is consulted; this is mechanical config bookkeeping.

## Criteria & examples

- **Flag a failure (transition only):** `apply_corrections({"anime": "fail"})` where `sonarr_instances["anime"]` has no `failed` key → sets `anime.failed = True`, `updated = True`, writes config.
- **Idempotent failure:** the same call when `anime.failed` is already `True` → logs "already marked as failed; skipping," `updated` stays `False`, no write.
- **Clear on recovery:** `apply_corrections({"anime": "success"})` when `anime.failed == True` → pops `failed`, writes config. If `anime` had no `failed` flag, it is a silent no-op (nothing to clear).
- **Unknown / intermediate result:** `apply_corrections({"anime": "recovered"})` → falls into the `else` branch, logs debug, makes NO change. (Note: in `BaseInstanceManager._handle_interactive_correction` the value passed for a successful interactive retry is literally `"recovered"`, which this method intentionally treats as "no flag change needed" — the recovery is confirmed separately via `_confirm_and_clear_failed_flag`, which sends `"success"`.)
- **Corrupt config guard:** if `sonarr_instances` is, say, a list or `None`, the method aborts early returning `{}` rather than risk clobbering bad data.

## In plain English

This is the little notepad next to the Sonarr front desk. When the front desk discovers a TV server is genuinely down for good, it writes "BROKEN — don't bother" next to that server's name so the app stops wasting time on it; when a server that was marked broken comes back to life, it erases that note. It is careful: it won't scribble the same "broken" note twice, and if the notepad itself looks garbled it refuses to write on it at all so it can't make things worse.

## Interactions

- **Parent manager:** `SonarrInstanceManager` (which owns it as `self.updater`); its declared `parent_name` is `"SonarrManager"`, looked up via the registry to inherit shared deps.
- **Callers:** `SonarrInstanceManager.run()` seeds it once per instance, and the inherited `BaseInstanceManager` methods `_process_instance`, `_confirm_and_clear_failed_flag`, and `_handle_interactive_correction` call `self.updater.apply_corrections(...)` with `"success"` / `"fail"` outcomes.
- **Writes to:** `config.json` `sonarr_instances` section via `ConfigManager`.
- **Brain modules / external services:** none.
