# TautulliTranscodeManager

- **File** — `scripts/managers/services/tautulli/transcode/__init__.py`
- **One-liner** — A thin Tautulli submanager that turns the already-fetched watch-history list into two transcode-related signals (a stream-codec-pair tally and a per-device play-vs-transcode codec matrix), delegating all computation to the machine-learning brain.

## What it does (for a senior Python engineer)

`TautulliTranscodeManager` is a leaf submanager under the Tautulli service manager. It subclasses `BaseManager` directly (not `ComponentManagerMixin`), so it loads **no** submanagers of its own. Its `__init__` does nothing but forward the standard injected dependencies (`logger`, `config`, `global_cache`, `validator`, `registry`, plus `**kwargs`) to `super().__init__(...)`; all the shared-dependency wiring, singleton caching, registry self-registration, and parent auto-linking come from `BaseManager`.

It is deliberately a **transform-only** adapter: it does no HTTP and no caching itself. In the FETCH / CACHE / APPLY model it is none of those at the manager level — the FETCH (the Tautulli history pull) and the CACHE (writing the resulting matrix to `global_cache`) both happen in the **parent** Tautulli manager's `run()`. This manager only converts an in-memory list into a dict and logs a one-line summary.

Public methods:

- `get_transcode_stats(history_entries: list) -> dict`
  Calls the brain function `transcode_stats(history_entries)` to build a tally of transcode stream-codec format combinations, logs `"[TautulliTranscode] N transcode format combinations found."`, and returns the resulting `format_map` dict.

- `get_device_codec_matrix(history_entries: list) -> dict`
  Calls the brain function `device_codec_matrix(history_entries)` to build a per-device codec play-vs-transcode matrix (described in-code as "the keystone signal for per-device profile selection"), logs `"[TautulliTranscode] device-codec matrix: N device(s)."`, and returns the `matrix` dict.

Both methods take the same input: `history_entries`, the list of raw Tautulli history records that the parent has already fetched and cached. Neither method reads `config`, calls any API endpoint, or writes any cache key by itself.

- **Parent manager:** the Tautulli service manager (`scripts/managers/services/tautulli/__init__.py`). The parent declares this class under key `"transcode"` in both `all_component_classes` and `critical_keys`, and instantiates it via its own `_load_component` / `_singleton` path (not via `load_components`); the parent passes the standard `init_args` bundle (`logger`, `config`, `global_cache`, `registry`, `validator`, `tautulli_api`, `parent_name`).
- **Submanagers:** none.
- **API endpoints touched:** none directly. The history that feeds both methods comes from the Tautulli API layer through the parent.
- **Config keys read:** none.
- **global_cache / Parquet keys:** none read or written *by this manager*. The parent's `run()` writes the matrix to the cache key `tautulli/device_codec_matrix` (`self.global_cache.set("tautulli/device_codec_matrix", device_codec_mtx)`). The transcode-stats tally returned by `get_transcode_stats` is currently computed and folded into the parent's run summary but is **not** persisted to its own cache key.
- **dry_run behavior:** none. There is no APPLY step (no PUT/DELETE/POST), so `dry_run` does not change anything here — these are pure read-side computations.
- **Singleton / concurrency / threading notes:** as a `BaseManager`, it is a process-wide singleton keyed by `(class, singleton_key)`; the parent obtains it through `_singleton`. No threading or locking of its own; it is a stateless transform.

## How it functions

Lifecycle is trivial:

1. **init** — `BaseManager.__init__` injects/auto-links the shared deps. No `load_components` call, no submanager tree.
2. **invocation** — There is no `run()` / `prepare()` entry method on this class. Instead, the parent Tautulli manager's `run()` performs the FETCH (pulling `all_entries`, the full cached history) and then, in its "Derived stats from the cached history — pure computation, no extra API calls" step, calls:
   - `self.transcode.get_transcode_stats(all_entries)`
   - `self.transcode.get_device_codec_matrix(all_entries)`
   The parent then persists the device-codec matrix to `tautulli/device_codec_matrix`.

Internal helpers: none — each public method is a single delegating call plus a log line.

Decisions delegated to the machine-learning brain: both methods delegate their entire computation to `scripts/managers/machine_learning/quality_analytics/transcode` — specifically the module-level functions `transcode_stats(...)` and `device_codec_matrix(...)`. Per scope rules, the brain itself is not documented here; this manager only does the FETCH-adjacent plumbing and the summary log, and notes that the value judgement (how codec pairs are tallied, how the matrix is shaped) lives in the brain.

## Criteria & examples

This manager applies **no** thresholds, guards, or selection rules of its own — all such logic is inside the brain functions. The only manager-level behavior is the count it reports:

- If `transcode_stats(history_entries)` returns a dict with 5 keys (5 distinct source-codec / stream-codec combinations seen across the history), the log reads `"[TautulliTranscode] 5 transcode format combinations found."` and that 5-key dict is returned verbatim.
- If `device_codec_matrix(history_entries)` returns a dict keyed by 3 device identifiers, the log reads `"[TautulliTranscode] device-codec matrix: 3 device(s)."` and that 3-key dict is returned. The parent then stores it under `tautulli/device_codec_matrix`.
- With an empty history list, both methods return an empty dict and report `0`.

## In plain English

Think of your TV, your phone, and your laptop all streaming from the same media server. Some of them can play a given video file straight off the disk; others force the server to re-encode ("transcode") it on the fly because they can't handle that codec — like how an older streaming stick might choke on a 4K HEVC copy of a Marvel film and make the server grind to convert it, while your new TV plays the same file instantly.

This manager doesn't decide anything; it just reads the play history and tallies two things: which video formats keep getting transcoded, and which of your devices play smoothly versus which keep triggering transcodes. That tidy summary is handed off to the "brain" so the app can later prefer downloading copies your devices play directly — fewer stutters, less server strain, and a smoother movie night for the person actually watching.

## Interactions

- **Parent manager:** Tautulli service manager (`scripts/managers/services/tautulli/__init__.py`) — constructs it as the `"transcode"` critical component, supplies the history entries, and owns the cache write to `tautulli/device_codec_matrix`.
- **Sibling submanagers (under the same parent):** `devices`, `episodes`, `instance`, `metadata`, `series`, `users`, `watch_history`, `validator_manager`. In the parent's run it sits alongside `devices.get_platform_usage`, `series.get_series_completion_stats`, and `episodes.get_episode_completion_stats` in the same derived-stats step.
- **Brain modules:** `machine_learning/quality_analytics/transcode` — functions `transcode_stats` and `device_codec_matrix` (computation only; not documented here).
- **Other services:** indirectly, the per-device codec matrix it produces is intended as a downstream signal for per-device download-profile selection (consumed via the `tautulli/device_codec_matrix` cache key the parent writes).
