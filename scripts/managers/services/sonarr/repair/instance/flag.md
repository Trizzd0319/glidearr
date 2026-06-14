# SonarrRepairInstanceFlagManager

- **File** — `scripts/managers/services/sonarr/repair/instance/flag.py`
- **One-liner** — Clears stale `failed` flags from each Sonarr instance's config so a previously-quarantined instance can be retried.

## What it does (for a senior Python engineer)

Repairs instance-level `failed` flags in the Sonarr config. The premise: a prior run may have marked an instance `failed: True` (e.g. the credentials submanager does exactly that when an API key is missing). This manager removes that marker so the instance is eligible again.

**Construction & tree position.** Subclasses `BaseManager` and `ComponentManagerMixin`. `__init__` hard-codes `self.parent_name = "SonarrRepair"` (a label, not the runtime parent), stores `self.manager = manager`, and resolves `self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))` **before** `super().__init__`. It loads no components. Its runtime parent is `SonarrRepairInstanceManager`.

**Public methods.**
- `__init__(... manager=None, **kwargs)` — dependency wiring + early `dry_run` capture; calls `self.register()`.
- `run()` — scans `sonarr_instances`. For each entry: if the value is not a `dict`, logs a warning and counts it as `failed`; if it has no truthy `failed` key, it is `skipped`; otherwise it attempts to clear the flag. In dry-run it logs `would have cleared 'failed' flag` and moves on. Live, it does `cfg.pop("failed", None)`, sets registry flag `sonarr.instance.<name>.flag_repaired = True`, and records the instance as `repaired`. Returns `{"repaired": [...], "skipped": [...], "failed": [...], "success": len(failed) == 0}`.

**FETCH / CACHE / APPLY.** APPLY only — it mutates the in-memory config dict (`cfg.pop`). No HTTP, no caching. Note it mutates `config["sonarr_instances"][name]` in place; persistence to disk is the responsibility of whatever owns/saves the config, not this manager.

**Config keys.** Reads `sonarr_instances`; per-instance reads/removes the `failed` key.

**global_cache / Parquet.** None. Writes registry flags `sonarr.instance.<name>.flag_repaired`.

**dry_run.** When true, no flag is cleared; it logs the intended action only.

**Singleton / concurrency.** `BaseManager` singleton; single-threaded.

## How it functions

Lifecycle: construct → `run()`. Control flow is a single loop over `sonarr_instances` with three outcomes per instance (invalid/non-dict → failed, no flag → skipped, flag present → cleared or dry-run-logged), wrapped in try/except so one bad instance does not abort the others. No `machine_learning` delegation.

## Criteria & examples

- **Non-dict guard:** if `sonarr_instances["hd1"]` is a string (corrupt config), it is logged and counted under `failed`.
- **No-op skip:** an instance `{"base_url": "...", "failed": False}` (or with no `failed` key) is `skipped`.
- **Clear:** an instance `{"base_url": "...", "api": "...", "failed": True}` in live mode has `failed` popped, registry flag `sonarr.instance.<name>.flag_repaired` set, and is reported under `repaired`.

Worked example: two instances, `hd` with `failed: True` and `kids` with no flag, in live mode → result `{"repaired": ["hd"], "skipped": ["kids"], "failed": [], "success": True}`.

## In plain English

Imagine each TV server had a red "OUT OF ORDER" sticker slapped on it during an earlier check. This manager walks down the row and peels off those stickers from servers that should get another chance — so Glidearr stops avoiding them. In dry-run it just says "I would peel this one off" without actually touching anything.

## Interactions

- **Parent:** `SonarrRepairInstanceManager` (attribute `self.flag`).
- **Siblings:** reachability, credentials, config submanagers (it runs first in the pipeline; the credentials submanager is the one that *sets* `failed`, so this can clear what a later credentials pass re-applies).
- **Brain modules:** none.
- **Other services:** none directly.
