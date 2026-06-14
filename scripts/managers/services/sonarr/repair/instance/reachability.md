# SonarrRepairInstanceReachabilityManager

- **File** — `scripts/managers/services/sonarr/repair/instance/reachability.py`
- **One-liner** — HTTP-pings each configured Sonarr instance's `base_url` and records a registry "reachable" flag for the ones that answer.

## What it does (for a senior Python engineer)

Performs a lightweight liveness probe against every Sonarr instance and logs the outcome, setting a registry flag only when an instance is reachable.

**Construction & tree position.** Subclasses `BaseManager` and `ComponentManagerMixin`. `__init__` hard-codes `self.parent_name = "SonarrRepair"`, stores `self.manager = manager`, and resolves `self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))` **before** `super().__init__`. The inline comment is explicit: `BaseManager` reassigns `self.manager` to a registry-resolved parent, so reading `self.manager.dry_run` later is unreliable — that mismatch previously caused this manager to log `dry_run=False` mid dry-run. Loads no components. Runtime parent is `SonarrRepairInstanceManager`.

**Public methods.**
- `__init__(... manager=None, **kwargs)` — dependency wiring + early `dry_run` capture; `self.register()`.
- `run()` — iterates `sonarr_instances` with a fixed `timeout_seconds = 5`. Skips `default_instance` and non-dict entries. Reads `base_url` (and `port`, only for logging). Missing `base_url` → warning + `failure`. In dry-run it logs `Dry run enabled — skipping actual request.` and continues (no HTTP). Live, it does `requests.get(base_url, timeout=5, verify=ssl_verify)` where `ssl_verify = cfg.get("ssl_verify", True)`: on `response.ok` it logs success, sets registry flag `sonarr.instance.<name>.reachable = True`, increments `success`; on a non-OK status it logs the status/reason and increments `failure`; on `requests.exceptions.RequestException` it logs the error and increments `failure`. Logs a final tally and returns `None` (no dict).

**FETCH / CACHE / APPLY.** FETCH only — an HTTP GET against the raw `base_url`. It does not hit a specific Sonarr API endpoint (e.g. `/api/v3/...`); it just checks that the base URL responds. No caching; no config mutation.

**External API endpoints.** `GET <base_url>` (the instance root URL as configured), with `verify=ssl_verify`.

**Config keys.** `sonarr_instances`; per-instance `base_url`, `port` (log only), `ssl_verify` (default `True`).

**global_cache / Parquet.** None. Writes registry flag `sonarr.instance.<name>.reachable` only on success.

**dry_run.** When true, no network request is made; it logs intent and continues.

**Singleton / concurrency.** `BaseManager` singleton; requests are issued sequentially, one instance at a time, each with a 5s timeout. No threading.

## How it functions

Lifecycle: construct → `run()`. Single loop over instances, guarded by skip rules, with a per-instance try/except around the HTTP call so a connection error on one instance does not stop the rest. The success/failure counters are local and surfaced only via the summary log line. No `machine_learning` delegation — pure connectivity check.

## Criteria & examples

- **Skip:** `name == "default_instance"` or `cfg` not a dict → skipped (not counted as success or failure).
- **Missing URL:** `cfg` with no `base_url` → warning, counts as `failure`, no request.
- **Reachable:** `base_url = "https://sonarr.local:8989"`, server returns HTTP 200 → `response.ok` true → success logged, `sonarr.instance.main.reachable` flag set.
- **Bad status:** server returns 401 → not `ok` → logged as `responded with status 401 (Unauthorized)`, counts as `failure`, no reachable flag.
- **SSL:** an instance with `ssl_verify: False` skips certificate validation on its probe (self-signed cert scenario).

Worked example: instance `main` (`https://sonarr.local`, returns 200) and instance `legacy` (no `base_url`) → `main` gets the `reachable` flag, `legacy` counts as a failure; final log: `1 succeeded, 1 failed`.

## In plain English

This is the "knock on the door and see who answers" step. For each TV server you've configured, it sends a quick hello over the internet (giving up after 5 seconds). If the server says "hi" back, it pins a green "this one's alive" note on it. If the door's the wrong address or nobody answers, it just notes the miss and moves on. In dry-run it doesn't even knock — it just says which doors it *would* have tried.

## Interactions

- **Parent:** `SonarrRepairInstanceManager` (attribute `self.reach`).
- **Siblings:** flag, credentials, config submanagers (runs second in the pipeline, after flag cleanup).
- **Brain modules:** none.
- **Other services:** the Sonarr instances themselves, over HTTP via the `requests` library.
