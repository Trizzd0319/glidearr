# SonarrRepairInstanceConfigManager

- **File** — `scripts/managers/services/sonarr/repair/instance/config.py`
- **One-liner** — Verifies each Sonarr instance has the required structural keys (`base_url`, `port`, `api`) and, in live mode, backfills any missing ones with obvious placeholder defaults.

## What it does (for a senior Python engineer)

Validates and repairs the *shape* of each Sonarr instance config. The required keys are declared as the class constant `REQUIRED_KEYS = ["base_url", "port", "api"]`.

**Construction & tree position.** Subclasses `BaseManager` and `ComponentManagerMixin`. `__init__` sets `self.parent_name = self.__class__.__name__`, stores `self.manager = manager`, and resolves `self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))` **before** `super().__init__` — the inline comment explains this guards against `BaseManager` reassigning `self.manager` to a registry-resolved parent, which could otherwise let a structural repair write to config mid dry-run. Loads no components. Runtime parent is `SonarrRepairInstanceManager`.

**Public methods.**
- `__init__(... manager=None, **kwargs)` — wiring + early `dry_run` capture; `self.register()`.
- `run()` — iterates `sonarr_instances`, skipping `default_instance` (a pointer) and non-dict entries. For each, computes `missing_keys = [k for k in REQUIRED_KEYS if k not in cfg]`. No missing keys → counted `skipped` (valid). Otherwise logs a warning; in dry-run logs `Would repair: <missing>` and continues. Live, it backfills each missing key with a placeholder, sets registry flag `sonarr.instance.<name>.config_repaired = True`, counts `repaired`; per-instance try/except records `errored`. Returns `{"repaired", "skipped", "errors", "success"}` with `success = len(errored) == 0`.

**Placeholder defaults (live repair).**
- `port` → `443`
- `base_url` → `"https://REPLACE_ME"`
- `api` → `"MISSING_API_KEY"`
- any other required key (none today) → `f"MISSING_{key.upper()}"`

**FETCH / CACHE / APPLY.** APPLY only — mutates the in-memory `cfg` dict. No HTTP, no caching. Persistence to disk is the config owner's job.

**Config keys.** Reads `sonarr_instances`; per-instance checks for `base_url`, `port`, `api`; writes those keys when missing (live).

**global_cache / Parquet.** None. Writes registry flag `sonarr.instance.<name>.config_repaired` per repaired instance.

**dry_run.** When true, nothing is written — it logs `[Dry Run] Would repair: <missing>` and moves on.

**Singleton / concurrency.** `BaseManager` singleton; single-threaded.

## How it functions

Lifecycle: construct → `run()`. One loop over `sonarr_instances`: skip pointer/invalid → list-comprehension diff against `REQUIRED_KEYS` → branch on dry-run → per-key placeholder fill in a try/except. Ends with a summary log (`N repaired, M valid, K failed`). No `machine_learning` delegation; the placeholders are intentionally non-functional sentinels meant to make a broken instance obvious to a human, not to silently "fix" it into a working state.

## Criteria & examples

- **Skip pointer:** `name == "default_instance"` is skipped (it points at another instance, not a config dict).
- **Complete:** `{"base_url": "...", "port": 8989, "api": "..."}` → no missing keys → `skipped`.
- **Repair, live:** `{"base_url": "https://x"}` (missing `port`, `api`) → in live mode becomes `{"base_url": "https://x", "port": 443, "api": "MISSING_API_KEY"}`, registry flag `sonarr.instance.<name>.config_repaired` set, counted `repaired`.
- **Repair, dry-run:** same input logs `Would repair: ['port', 'api']` and leaves the dict untouched.

Worked example (live): instances `main` (complete) and `new` (`{"base_url": "https://new"}`) → `{"repaired": ["new"], "skipped": ["main"], "errors": [], "success": True}`; `new` now has `port=443` and `api="MISSING_API_KEY"` as glaring placeholders.

## In plain English

This is the "is the address card filled out completely?" check. Every TV server needs three things written down: its web address, its port number, and its password. If any are blank, this step doesn't pretend to know the real value — it writes loud placeholders like `https://REPLACE_ME` and `MISSING_API_KEY` so you'll immediately spot what you forgot to enter. In dry-run it just tells you which fields it *would* stub out, without scribbling anything down.

## Interactions

- **Parent:** `SonarrRepairInstanceManager` (attribute `self.config_repair`).
- **Siblings:** flag, reachability, credentials submanagers (runs last in the pipeline). Overlaps with the credentials manager on the `api` key, but checks presence-of-key structurally rather than emptiness.
- **Brain modules:** none.
- **Other services:** none directly.
