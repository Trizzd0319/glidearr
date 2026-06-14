# SonarrRepairStorageManager

**File** — `scripts/managers/services/sonarr/repair/storage.py`
**One-liner** — Audits whether each Sonarr series sits under a valid root folder, and detects/removes broken symlinks inside Sonarr's storage paths.

## What it does (for a senior Python engineer)

`SonarrRepairStorageManager(BaseManager, ComponentManagerMixin)` is a leaf repair sub-manager under `SonarrRepairManager`. It mixes Sonarr API reads with **filesystem** inspection. Today it performs **FETCH** + read-only auditing for path mappings, and **APPLY** (unlink) for broken symlinks; it explicitly does **not** remap mismatched series paths (no safe remap logic implemented yet).

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` and listed in its `critical_keys`.
- **Deps:** `sonarr_api` resolved from the `sonarr_api` kwarg or the `manager` kwarg (raises `ValueError` if unresolved); `dry_run` from kwarg or manager.
- **Loads submanagers:** none.

Public methods:

- **`repair_storage_paths()`** — for each instance in `get_all_sonarr_apis()`, builds a map of resolved root-folder paths from `api.root_folder.all()`, then for each series in `api.series.all()` checks whether `Path(series.path).resolve()` is relative to any valid root (`is_relative_to`). Series without a matching root are logged (warning under dry-run, error otherwise) and counted as `skipped`. Matched series are also counted as `skipped`. **`repaired` stays 0** — there is currently no remap action, only auditing. Returns `None`.
- **`repair_symlinks()`** — for each instance and each root folder that exists, walks `path.rglob("*")` and collects entries that are symlinks whose `resolve(strict=False)` target does not exist (broken symlinks). If none, logs success. Otherwise, for each broken symlink it either logs `🧪 Would remove:` (dry-run) or calls `symlink.unlink()` to remove it. Returns `None`.

- API endpoints touched (via the client): `root_folder.all`, `series.all`.
- Config keys read: none. global_cache keys: none.
- FETCH / CACHE / APPLY: **FETCH** + filesystem read; **APPLY** for symlink removal only (path remap is intentionally a no-op).
- dry_run: gates symlink removal; for path audit it only changes the log level (warning vs error).
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__`, `self.register()`, resolves the API and `dry_run`, raises if no API, logs an init line. `repair_storage_paths` is purely diagnostic (it deliberately leaves remapping unimplemented and notes this in the error message). `repair_symlinks` is the only method that mutates the filesystem, and only outside dry-run. No `machine_learning` brain module is involved.

## Criteria & examples

- **Unmapped series:** `Path(series.path).resolve()` is not relative to any root. Example: series "Lost" at `/mnt/old/Lost` while roots are `{/data/tv}` → logs `⚠️ Series Lost path '/mnt/old/Lost' is not mapped to a valid root folder` (with `[dry-run]` suffix if dry-run) — and does **not** attempt to move it.
- **Broken symlink:** `sub.is_symlink()` and its target `resolve(strict=False)` does not exist. Example: `/data/tv/Show/ep01.mkv -> /broken/target.mkv` (target gone) → removed via `unlink()` (or `🧪 Would remove:` under dry-run).

## In plain English

This is the storeroom's structural inspector. First it checks that every show is filed under one of the official shelving zones; if a show is somehow sitting in a non-official spot, it flags it but won't move it (there's no safe relocation procedure yet, so it just warns you). Second, it hunts for "dead shortcuts" — those little pointer files that claim "the real video is over there" but point at nothing. Those it can safely tear up (unless you're in practice mode, where it just lists them).

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists.
- **Services:** the Sonarr per-instance API clients (`sonarr_api`) and the local filesystem (`pathlib`).
- **Brain modules:** none.
