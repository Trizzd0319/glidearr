# RadarrInstanceUpdaterManager

- **File** — `scripts/managers/services/radarr/instance/updater.py`
- **One-liner** — Persists per-instance validation outcomes back into the `radarr_instances` config: it sets the `failed` flag on confirmed failures and clears it on confirmed success.

## What it does (for a senior Python engineer)

`RadarrInstanceUpdaterManager(BaseManager, ComponentManagerMixin)` is a small write-back helper owned by `RadarrInstanceManager`. It does not perform HTTP itself and loads no submanagers of its own. Its sole job is to reconcile the in-memory/config `radarr_instances` map with the validation results that the parent's lifecycle helpers (`_process_instance`, `_confirm_and_clear_failed_flag`, `_handle_interactive_correction`) produce.

**Where it sits in the tree.** Parent is `RadarrInstanceManager` (`parent_name = "RadarrInstanceManager"`), which constructs it directly in `__init__` with `manager=self` and the captured `dry_run`. It is a `BaseManager` singleton with the usual injected deps.

**FETCH / CACHE / APPLY.** This is an **APPLY**-of-config helper, but for the local config store rather than the Radarr REST API. Its only mutation is `self.config.set("radarr_instances", radarr_instances)`; it issues no API calls and no `global_cache`/Parquet writes. No `machine_learning` brain delegation.

**Public methods.**
- `apply_corrections(validation_results: dict) -> dict` — given `{instance_name: result}` where `result ∈ {"fail", "success", "recovered", ...}`, update each matching instance config and return the (possibly updated) `radarr_instances` dict. Behavior:
  - Reads `config.get("radarr_instances")`; if it is not a dict, logs an error and returns `{}` (abort, no write).
  - For each result, looks up `radarr_instances[instance_name]`; skips with a warning if that entry is not a dict.
  - `result == "fail"`: if `failed` is already set, skip (debug log); otherwise set `failed = True` and mark the dict dirty.
  - `result in ("success", "recovered")`: if `failed` is currently set, pop it (clear the flag) and mark dirty; otherwise no-op.
  - Any other result string: debug log, no action.
  - If anything changed, persist once via `config.set("radarr_instances", ...)` and log an info line.

**Config keys read/written.** Reads and writes `radarr_instances` only.

**global_cache / Parquet keys.** None.

**dry_run behavior.** `self.dry_run` is captured (kwarg → parent → `False`) for inheritance correctness, but `apply_corrections` does not branch on it — failure-flag bookkeeping is config state, not a Radarr-side mutation, so it always persists regardless of dry_run. (Note: this means the `failed` flag is written to config even in a dry run; the dry_run capture exists primarily so this child does not silently lose the parent's setting.)

**Singleton / concurrency.** `BaseManager` singleton. No locks of its own; it relies on being driven serially from the parent's `__init__` validation loop.

## How it functions

`__init__` calls `super().__init__(...)`, registers, then resolves three references from kwargs (falling back to the parent `manager`): `radarr_api`, `instance_manager`, and `dry_run`. It logs a debug init line. There is no `load_components` call — it has no sub-components.

The control flow is entirely reactive: the parent calls `apply_corrections` at several points — once up front in `RadarrInstanceManager.__init__` to normalise all configured names (`{n: "success"}`), and again from the inherited `_process_instance` / `_confirm_and_clear_failed_flag` / `_handle_interactive_correction` helpers as each instance's true state is learned. The method is idempotent: a `"fail"` for an already-failed instance is a no-op, and a `"success"` for an instance with no `failed` flag is a no-op, so it only writes when something actually changed. No decision is delegated to a `machine_learning` module.

## Criteria & examples

- **Mark failed only on confirmed final failure.** `apply_corrections({"4k": "fail"})` where `radarr_instances["4k"]` has no `failed` key → sets `failed = True`, persists, logs a warning. If `"4k"` was already `failed`, nothing is written.
- **Clear only on confirmed success.** `apply_corrections({"4k": "recovered"})` where `radarr_instances["4k"]["failed"] is True` → pops `failed`, persists, logs "Clearing failed flag ... after confirmed success." If `"4k"` had no `failed` flag, nothing changes.
- **Corrupt config guard.** If `radarr_instances` is, say, a list or `None` instead of a dict, the method logs an error and returns `{}` without touching anything.
- **Bad per-instance entry.** `apply_corrections({"hd": "fail"})` where `radarr_instances["hd"]` is a string (not a dict) → warning "invalid config format", that entry is skipped, others still processed.
- **Unknown result string.** `apply_corrections({"hd": "pending"})` → debug "No action needed", no write.

## In plain English

This is the little clerk that keeps the contact list tidy. Every time the front desk (`RadarrInstanceManager`) finishes checking whether a Radarr server is reachable, it tells the clerk the result. If a server truly failed after all retries, the clerk writes "BROKEN" next to its name so the app stops wasting time calling it next run. If a previously-broken server is now answering, the clerk erases the "BROKEN" note. The clerk never picks up the phone or makes any decisions — it just keeps the notes accurate, and only rewrites the list when something actually changed.

## Interactions

- **Parent:** `RadarrInstanceManager` (`scripts/managers/services/radarr/instance/__init__.py`) — owns this object and drives every `apply_corrections` call.
- **Base helpers that call it:** `BaseInstanceManager._process_instance`, `_confirm_and_clear_failed_flag`, and `_handle_interactive_correction` (`factories/base_instance_manager.py`) — each calls `self.updater.apply_corrections(...)` to record `success` / `recovered` / `fail`.
- **Config store:** `ConfigManager` (`self.config`) — the only thing it reads from and writes to (`radarr_instances`).
- **Submanagers:** none.
- **Brain modules:** none.
