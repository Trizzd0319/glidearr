# SonarrRepairInstanceManager

- **File** — `scripts/managers/services/sonarr/repair/instance/__init__.py`
- **One-liner** — Top-level orchestrator that sequentially runs the four instance-level Sonarr config repairs (flag cleanup, reachability, credentials, structural config).

## What it does (for a senior Python engineer)

Master repair manager for instance-level Sonarr configuration issues. It owns four submanagers and drives them in a fixed order from a single `run()` entry point.

**Construction & tree position.** Subclasses `BaseManager` and `ComponentManagerMixin`. In `__init__` it sets `self.parent_name = self.__class__.__name__` (i.e. `"SonarrRepairInstanceManager"`), reads `dry_run` from kwargs (default `False`), calls `super().__init__(...)`, then `self.register()`. Its parent in the manager tree is whatever constructs it (the Sonarr repair layer one directory up). It does **not** use `load_components` despite inheriting `ComponentManagerMixin`; instead it directly instantiates the four submanagers and attaches them as attributes, threading the shared deps (`logger`, `config`, `global_cache`, `validator`, `registry`, `manager=self`) plus `dry_run=self.dry_run` into each:
  - `self.flag` → `SonarrRepairInstanceFlagManager`
  - `self.reach` → `SonarrRepairInstanceReachabilityManager`
  - `self.cred` → `SonarrRepairInstanceCredentialsManager`
  - `self.config_repair` → `SonarrRepairInstanceConfigManager`

**Public methods.**
- `__init__(logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)` — wires up the four submanagers; logs `initialized with dry_run=<bool>`.
- `run(skip_flags=False, skip_reach=False, skip_cred=False, skip_config=False)` — entry point. Runs, in order: `self.flag.run()`, `self.reach.run()`, `self.cred.run()`, `self.config_repair.run()`. Each stage can be skipped via its boolean flag for CLI/partial-debug use. Logs a banner before each stage and `Instance repair complete.` at the end. Returns `None` (it does not aggregate the submanagers' return dicts).

**FETCH / CACHE / APPLY.** Itself it performs none of these — it is pure orchestration. Its submanagers do the work: reachability FETCHes (HTTP GET), and flag/credentials/config APPLY in-memory config mutations.

**Config keys.** Reads nothing directly; submanagers read `sonarr_instances`.

**global_cache / Parquet.** None read or written here; submanagers write registry flags only (not global_cache).

**dry_run.** Captured at construction and forwarded explicitly to every submanager so the whole repair tree shares one value. (This is deliberate — see the per-submanager docs: `BaseManager` reassigns `self.manager` to a registry-resolved parent, so each submanager also independently re-resolves `dry_run` at construction rather than reading it from `self.manager` later.)

**Singleton / concurrency.** As a `BaseManager`, it is a process-wide singleton keyed by `(class, singleton_key)`. Single-threaded sequential execution; no threading of its own.

## How it functions

Lifecycle: construct (`__init__` builds and attaches the four submanagers) → call `run()`. `run()` is a straight-line pipeline with four guarded stages; there is no branching beyond the `skip_*` flags. No decision is delegated to a `machine_learning` brain module — this is mechanical config hygiene, not value judgement.

## Criteria & examples

The only selection logic here is the four `skip_*` flags. Example: calling `mgr.run(skip_reach=True, skip_cred=True)` runs only the flag-cleanup and structural-config stages — useful when the network is known-down and you only want to fix the local config file structure. With all defaults (`run()`), all four stages execute in order: flags → reachability → credentials → config structure.

## In plain English

Think of this as the "service check-up coordinator" for each of your TV-download servers (Sonarr instances). When something looks wrong with how a server is configured, this manager walks a fixed checklist with four specialists: one wipes stale "this one's broken" sticky notes, one pings the server to see if it answers, one confirms it has its password (API key), and one makes sure the address card has all its fields filled in. It does not make any judgement calls about your shows — it just makes sure the plumbing is sound before the rest of Glidearr tries to use the server.

## Interactions

- **Parent:** the Sonarr repair manager one directory up (whatever instantiates this class with `manager=`).
- **Sibling submanagers it owns:** `SonarrRepairInstanceFlagManager`, `SonarrRepairInstanceReachabilityManager`, `SonarrRepairInstanceCredentialsManager`, `SonarrRepairInstanceConfigManager`.
- **Brain modules:** none. No `machine_learning` delegation.
- **Other services:** indirectly Sonarr instances over HTTP (via the reachability submanager).
