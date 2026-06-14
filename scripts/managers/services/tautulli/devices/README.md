# TautulliDevicesManager

- **File** — `scripts/managers/services/tautulli/devices/__init__.py`
- **One-liner** — A thin Tautulli submanager that turns a raw watch-history list into a per-platform play-count tally by delegating the actual counting to a machine-learning brain helper.

## What it does (for a senior Python engineer)

`TautulliDevicesManager(BaseManager)` is one of the nine submanagers loaded by the parent `TautulliManager` (`scripts/managers/services/tautulli/__init__.py`). It is intentionally tiny: its only job is to expose one summary method over already-fetched history.

**Constructor** — `__init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs)` simply forwards everything to `BaseManager.__init__`. It does no extra wiring: no `self.api` assignment, no `load_components` call, no submanagers of its own. Per the shared `BaseManager` contract it is a process-wide singleton (cached in `_instances`), self-registers under the registry `"manager"` category, and auto-links to its parent so it inherits the shared logger/config/global_cache/validator. The Tautulli `tautulli_api` instance is passed in via the parent's `init_args`, but this class never references it.

**Public methods**
- `get_platform_usage(self, history_entries: list) -> dict` — calls the brain function `platform_usage(history_entries)` to produce a platform-keyed tally, logs one summary line `[TautulliDevices] {N} platforms found.` where `N` is `len(result)`, and returns the dict unchanged. No filtering, mutation, or persistence happens in the manager itself.

**Where it sits in the tree** — Parent: `TautulliManager`. It is registered in the parent under the component key `"devices"` (`all_component_classes["devices"] = TautulliDevicesManager`) and is one of the parent's `critical_keys`, so the parent's `prepare()` will instantiate it during load. It loads **no** submanagers of its own (its entry in `component_dependencies` is `[]`).

**FETCH / CACHE / APPLY** — None of the three, strictly. It performs no HTTP itself (the raw history FETCH and caching is done upstream by `TautulliWatchHistoryManager`), it writes nothing to `global_cache`/Parquet, and it issues no PUT/DELETE/POST. It is a pure pass-through summarizer over data the parent already has in memory.

**External API endpoints touched** — None directly.

**Config keys read** — None directly.

**global_cache / Parquet keys** — Reads none, writes none. Note: the parent's `run` flow assigns the return value to a local `platform_stats = self.devices.get_platform_usage(all_entries)` (parent `__init__.py` line 188) but **never persists it** — `platform_stats` is computed and then discarded (no `global_cache.set(...)` for it). So today this manager's output is effectively inert beyond its log line.

**dry_run behavior** — Not applicable; there is no APPLY step to suppress. Read-only computation runs identically regardless of `dry_run`.

**Singleton / concurrency / threading** — Standard `BaseManager` singleton semantics; no manager-specific locking or background threads.

## How it functions

Lifecycle is trivial: the parent `TautulliManager` constructs it (via `_load_component` → `_singleton`) as part of its critical-component set, injecting shared deps. There is no `load_components` of its own. The only "control flow" is the single method: receive the in-memory `all_entries` history list, hand it to the brain helper `affinity.platform_usage.platform_usage`, log the platform count, and return.

The decision/computation is delegated to the machine-learning module `scripts/managers/machine_learning/affinity/platform_usage.py` (function `platform_usage`). Per scope rules that brain module is **not** documented here — the manager only owns the FETCH-side summary log around it.

## Criteria & examples

There are no thresholds, guards, or selection rules in this manager — it counts what it is given. The only branch-free "rule" is the log line, which reports `len(result)`.

Worked example: suppose `all_entries` contains 50 history rows spanning the platforms `"Roku"`, `"iOS"`, `"Chrome"`, and `"Android TV"`. `platform_usage` returns a dict with those four keys (mapped to their play tallies), so `len(result) == 4` and the manager logs `[TautulliDevices] 4 platforms found.` and returns that 4-key dict. If `all_entries` is empty, `platform_usage` returns an empty dict, `len(result) == 0`, and it logs `[TautulliDevices] 0 platforms found.`

## In plain English

Think of your household's streaming history as a stack of paper ticket stubs, one per thing watched, each stamped with the device it played on — the Roku in the living room, an iPhone, a laptop browser. This manager doesn't decide anything; it just sorts the stubs into piles by device and tells you "you watched on 4 different gadgets." If half your *Bluey* episodes played on the living-room Roku and the rest on a tablet, this is the step that simply counts those piles. The smart part — figuring out what those counts *mean* for picking video quality or recommendations — happens elsewhere; this is the humble clerk doing the tally.

## Interactions

- **Parent manager** — `TautulliManager` (constructs it, calls `get_platform_usage` during its run, currently discards the result).
- **Sibling submanagers** — `TautulliWatchHistoryManager` (produces the `all_entries` history this consumes), plus `transcode`, `series`, `episodes`, `users`, `metadata`, `instance`, `validator_manager`. The closely related per-device codec signal is produced by the sibling `TautulliTranscodeManager.get_device_codec_matrix` (whose output *is* cached at `tautulli/device_codec_matrix`).
- **Brain modules** — delegates to `machine_learning/affinity/platform_usage.py::platform_usage` (computation only; not documented here).
- **Other services** — none directly.
