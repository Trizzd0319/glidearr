# Auto-running `main.py` on viewing activity: Scheduler vs. Tautulli Watched Hook

A work-plan and **informed-consent** document for the Glidearr operator. The
question: should `scripts/main.py` re-run automatically when someone finishes
watching something, and if so — is it easier to **schedule it on a timer** (cron /
Windows Task Scheduler) or to **wire a Tautulli "Watched" notification agent** at
the glidearr launcher path?

> Status: design / decision doc. No Glidearr source changes are proposed by
> either path — both live entirely in OS-scheduler config or Tautulli config plus
> one small helper file.

---

## 1. TL;DR / Recommendation

- **Easier to set up:** the **scheduler** (one Task Scheduler entry / one crontab
  line; zero code, zero Tautulli changes).
- **Fresher:** the **Tautulli "Watched" hook** (fires within seconds of someone
  crossing the watched threshold) — *but only when Tautulli and Glidearr share
  a filesystem and a Python environment.*
- **Recommended default:** **start with a scheduled run** every 30–60 min. It is
  the simpler, more portable, more resilient baseline.
- **Best if you want freshness:** a **hybrid** — scheduler as the always-on safety
  net, **plus** the Tautulli hook **only if Tautulli is same-host** as
  Glidearr. The two coexist safely *via the `main_run.active` sentinel* — **but
  that sentinel only exists when the enrich daemon is enabled** (see §3.2 / §6). With
  the daemon off, the hybrid's overlap protection is not there.

One-line why: *the scheduler is the dependable floor; the event hook is a freshness
optimization that only pays off under the same-host + daemon-enabled conditions.*

**The real informed-consent question is not "timer vs. event" — it is "do I want
the capabilities I have already enabled (acquisition, write-back, and especially
space-pressure deletion) to fire unattended on every trigger?"** See §6.

---

## 2. Side-by-side comparison

| Dimension | **Scheduler (cron / Task Scheduler, e.g. hourly)** | **Tautulli "Watched" hook** |
|---|---|---|
| **Setup effort** | Low. 5–15 min. No code, no Tautulli changes. | Same-host best case: ~1–3 h (write a ~40-line helper, drop it in Tautulli's Script Folder, add a Script agent). Separate-container case: materially higher and possibly infeasible on the stock Tautulli image (see portability). |
| **Freshness / latency** | Stale by up to the interval (watch at 1:59, hourly run at 3:00 ⇒ ~1 h stale). | Seconds after the watched-% threshold is crossed. |
| **Resource cost** | Runs every tick **even when nobody watched**. Only the ~39 s Radarr `GET /movie` is cache-gated (`radarr_movie_library_max_age_s`, default 900 s); the rest of the run — Sonarr, Tautulli, re-scoring, phase 3, daemon restart — executes on **every** tick. | Runs **only** on activity. A binge collapses to one run *via the sentinel* (daemon-enabled only). No idle runs. |
| **Portability — same host** | Works natively (Task Scheduler) or in-container cron. | Works: Script Folder + a reachable Python + readable `config.json`/secrets. |
| **Portability — separate hosts/containers** | Still works (the scheduler lives wherever Glidearr lives). | **Often breaks.** Tautulli only runs scripts in *its own* Script Folder, on *its own* filesystem, with *its own* interpreter. Separate host ⇒ unusable (Glidearr exposes no HTTP/webhook listener). Separate container ⇒ you must mount the repo + config + cache (+ the sentinel on a *shared* volume) and provide a Python-with-deps env inside Tautulli's image. |
| **Failure modes** | Silent: a disabled task / deleted crontab line stops runs with no alert. Concurrent runs can collide on multi-file Parquet writes. Keyring not readable by the scheduler's user. Missed runs while the machine sleeps. | Helper not in Script Folder; helper blocks past the timeout (Tautulli kills it at ~30 s, can leave a stale sentinel); no Python in Tautulli's env; repo/secrets unreachable; Windows job-object reaps the detached child; missing debounce ⇒ spawn storm. Crashes are invisible to Tautulli. |
| **Reversibility** | Trivial: disable/delete the task or crontab line. | Easy: delete the Script agent (or untick Watched) and remove the helper file. |
| **Prerequisites** | A scheduler (every OS has one) + secrets readable by its user. (No `PYTHONPATH` — main.py self-bootstraps.) | Same-filesystem Tautulli + a reachable Python interpreter + readable repo/config/secrets + the detached-spawn done correctly + (for debounce) the enrich daemon enabled. |

---

## 3. Recommended approach & why

**Default to the scheduled run; treat the event hook as an opt-in upgrade for
same-host setups.** Reasons tied to this codebase:

1. **Frequent runs are *relatively* cheap — but only with the enrich daemon on.**
   With `daemons.enrich.enabled`, main runs are **cache-only on Trakt** (no live,
   429-prone calls). The one expensive fetch — Radarr `GET /movie` (~39 s) — is
   gated by `radarr_movie_library_max_age_s` (default 900 s). **Caveat:** that gate
   covers *only* the Radarr movie fetch; Sonarr, Tautulli history, full re-scoring,
   and phase 3 still run on every tick. "Cheap" here means *cheaper than live
   Trakt*, not *free*.

2. **Overlap/debounce protection is free — but it is daemon-gated.** `main.py`
   writes `scripts/support/cache/trakt/main_run.active` (`{pid, ts}`) at startup
   and removes it in its `finally` block — **but both are behind `if
   _daemon_enabled:`** ([`scripts/main.py:651-652`](scripts/main.py) write,
   [`:683-684`](scripts/main.py) remove). The path + 30-min backstop
   (`MAIN_ACTIVE_MAX_AGE_S = 1800`, re-check cadence `MAIN_ACTIVE_POLL_S = 5`) live
   in [`daemon_paths.py:41-43`](scripts/managers/factories/daemons/daemon_paths.py).
   **With the daemon disabled, no sentinel is ever written** — so the helper's
   binge-debounce and the cron↔event cross-overlap guard simply do not exist, and
   you must rely on the Task Scheduler "do not start a new instance" setting (which
   only guards the scheduler against *itself*, not against an event-fired run).

   > **Therefore: enabling `daemons.enrich.enabled` is a prerequisite for the
   > hybrid and for any debounce.** It is also what makes runs cache-only/cheap.

3. **No code changes.** The scheduled path touches nothing in the repo. `main.py`
   is a one-shot batch. The Tautulli integration is **read-only** today
   (`TautulliAPI` issues GETs only); the event hook also needs **zero** Glidearr
   code — just Tautulli config + one helper file.

4. **Resilience vs. freshness.** A scheduler survives crashes and restarts on the
   next tick; its cost is bounded staleness. If you genuinely need second-level
   freshness *and* you are same-host *and* the daemon is on, layer the event hook on
   top of the scheduled safety net.

**Coupling you must see (one toggle, three effects).** `daemons.enrich.enabled`
simultaneously: (a) makes runs cache-only ⇒ cheap to run often [benefit], (b)
causes `main.py` to **restart the daemon on every launch** ⇒ restart churn [cost,
§6], and (c) creates the **sentinel** ⇒ overlap/debounce [benefit]. You cannot take
(a)/(c) without (b); and turning the daemon **off** to avoid (b) also removes (a)
*and* (c) — re-introducing live 429-prone Trakt calls and deleting your overlap
protection. Decide on the daemon first; everything else follows from it.

---

## 4. How to add it

### Recommended path — scheduled run

> **Launch prerequisite: none — `main.py` now self-bootstraps `sys.path`.** As of
> the import fix, [`main.py`](scripts/main.py) inserts the repo root onto
> `sys.path` at startup (mirroring `enrich_daemon.py` / `onboarding.py`) and all of
> its imports are `scripts.`-prefixed. So **`python <REPO>\scripts\main.py` works
> from any working directory with no `PYTHONPATH` set.** (Previously it required
> `PYTHONPATH=repo;repo/scripts` to satisfy one anomalous bare import — that line is
> gone.) The wrapper below now exists only for log redirection, not for env setup.

**Windows Task Scheduler (native; this machine).** You can point the task directly
at `python.exe` with argument `<REPO>\scripts\main.py` (Start in = anywhere). A tiny
wrapper is still convenient purely to capture a log:

`run_glidearr.bat` (place at the repo root):
```bat
@echo off
rem REPO resolves to wherever this .bat lives — place it at the repo root.
set "REPO=%~dp0"
"C:\Path\To\python.exe" "%REPO%scripts\main.py" >> "%REPO%scripts\support\logs\sched_main.log" 2>&1
```

1. Open Task Scheduler (`taskschd.msc`).
2. Task Scheduler Library → **Create Task** (not "Basic" — you want the run-as +
   "don't start new instance" controls). Name: `Glidearr Hourly`.
3. **General tab:** set **Run as** the *same Windows user who ran onboarding*
   (otherwise the keyring/Credential Manager secrets are not readable — see §6).
   For an always-on box prefer "Run only when user is logged on"; "whether logged
   on or not" runs in a non-interactive session where Credential Manager access can
   differ — **verify a manual run works in that mode before trusting it.**
4. **Triggers tab:** New → On a schedule → Daily → **Repeat task every 1 hour**,
   duration **Indefinitely** (adjust to 30 min if desired).
5. **Actions tab:** Start a program → Program/script: `run_glidearr.bat`.
6. **Settings tab:** select **"Do not start a new instance"** under *If the task is
   already running* (overlap guard against the scheduler racing itself). Leave
   *Stop the task if it runs longer than* unchecked/generous so a 2–5 min run is
   never cut off.
7. **Verify:** right-click → **Run**, then check `scripts\support\logs\` for a
   fresh run and Task Scheduler's *Last Run Result* = `0x0`.

**Linux / macOS host (alternative):**
```bash
crontab -e
# hourly — no PYTHONPATH needed (main.py self-bootstraps); note the scripts/support/ log path:
0 * * * * flock -n /tmp/glidearr.lock python /path/to/Glidearr/scripts/main.py \
  >> /path/to/Glidearr/scripts/support/logs/sched_main.log 2>&1
```
`flock -n` is the POSIX equivalent of the Windows "do not start a new instance"
guard.

**In-container cron (only if you containerize Glidearr — the repo ships no
Dockerfile today, so this is net-new):**
```
# crontab.txt — no PYTHONPATH; log path is scripts/support/logs, NOT a top-level support/
0 * * * * python /repo/scripts/main.py >> /repo/scripts/support/logs/sched_main.log 2>&1
```

> **Secrets note.** For any headless/container/different-user schedule, supply
> secrets via `RECOMMENDARR_*` **env vars** rather than the OS keyring — they take
> precedence in the secret store and sidestep the per-user keyring trap. Caveat:
> the override only applies to **non-empty** values for recognized secret keys; an
> empty-string env var falls through to the keyring/inline config.

---

### Alternative path — Tautulli "Watched" hook (same-host only)

**Tautulli UI** (mechanics confirmed against Tautulli docs/source):

1. Tautulli → **Settings** → **Notification Agents** → **Add a new notification
   agent** → **Script** (internal `agent_id = 15`).
2. **Configuration tab:** set **Script Folder** to a directory *on Tautulli's own
   filesystem*. The **Script File** dropdown then auto-populates from files in that
   folder with a supported extension (`.bat .cmd .php .pl .ps1 .py .pyw .rb .sh`).
   Select your helper. *This folder+file pair is the "glidearr path in the
   tautulli config."* On Windows, a `.py` entry can be interpreter-ambiguous — a
   `.bat`/`.cmd` wrapper that explicitly calls Python is effectively required, not
   optional (see wrapper below).
3. Set **Script Timeout** small (2–10 s). The default is **30 s** and Tautulli
   **kills** the script when exceeded. The helper only *detaches and returns*, so a
   small timeout is just a safety net — **do not** set it large hoping `main.py`
   finishes inline (it never will: 2–5 min ≫ 30 s).
4. **Triggers tab:** tick **only** "Watched". Save.
5. *(Optional)* **Conditions tab:** e.g. *Media Type is movie OR episode* to skip
   music, to reduce churn.
6. **Verify:** use the agent's **Test Notifications**; confirm Tautulli logs no
   timeout, a detached `main.py` appears (Task Manager / `ps`), and
   `scripts\support\logs\` shows a run starting.

> **Watched-trigger semantics.** "Watched" fires once a stream crosses the
> configured watched-% threshold (Tautulli's defaults for movie/TV/music are 85%).
> *Needs confirmation in your install:* the exact UI location of those % fields and
> whether `on_watched` fires exactly once per completed item per user — verify
> before relying on per-user dedupe.

**Helper** — mirror the proven primitives rather than reinventing them: the detach
flags from `EnrichDaemonSupervisor.spawn()`
([`supervisor.py:131-188`](scripts/managers/factories/daemons/supervisor.py)) and a
liveness check at least as strong as `_pid_alive` there (which also requires the
process to actually be `python`, to reject PID reuse).

```python
#!/usr/bin/env python
# glidearr_launch.py — place INSIDE Tautulli's Script Folder (same-host).
import json, os, sys, time, subprocess
from pathlib import Path

# This launcher lives in Tautulli's Script Folder (outside the repo), so point it
# at your checkout via the GLIDEARR_HOME env var (or edit the fallback path).
REPO_ROOT = Path(os.environ.get("GLIDEARR_HOME", r"C:\path\to\Glidearr"))
SCRIPTS   = REPO_ROOT / "scripts"
SENTINEL  = SCRIPTS / "support" / "cache" / "trakt" / "main_run.active"
MAIN      = SCRIPTS / "main.py"
LOGDIR    = SCRIPTS / "support" / "logs"
MAX_AGE_S = 1800   # must match daemon_paths.MAIN_ACTIVE_MAX_AGE_S

def _pid_is_python(pid: int) -> bool:
    # Mirror supervisor._pid_alive: alive AND actually a python process (reject PID reuse).
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True).stdout.lower()
            return str(pid) in out and "python" in out
        os.kill(pid, 0)            # POSIX liveness probe
        return True
    except PermissionError:
        return True                # exists but not ours → treat as alive
    except (OSError, Exception):
        return False

def _run_active() -> bool:
    # NOTE: the sentinel only exists when daemons.enrich.enabled is true.
    # With the daemon off this always returns False → NO debounce.
    try:
        d = json.loads(SENTINEL.read_text())
        pid, ts = int(d["pid"]), float(d["ts"])
    except Exception:
        return False
    if time.time() - ts > MAX_AGE_S:
        return False
    return _pid_is_python(pid)

def main():
    if _run_active():
        print("glidearr run already active — skip"); sys.exit(0)
    LOGDIR.mkdir(parents=True, exist_ok=True)
    log = open(LOGDIR / "launch.log", "a")
    # No PYTHONPATH needed — main.py self-bootstraps the repo root onto sys.path.
    kwargs = dict(cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
                  stdout=log, stderr=log, close_fds=True)
    if os.name == "nt":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen([sys.executable, str(MAIN)],
                             creationflags=base | 0x01000000, **kwargs)  # +BREAKAWAY_FROM_JOB
        except OSError:
            # Breakaway rejected by the job object — fall back WITHOUT it. Residual
            # risk: a child spawned without breakaway can still be reaped when
            # Tautulli's job object closes (see §6).
            subprocess.Popen([sys.executable, str(MAIN)], creationflags=base, **kwargs)
    else:
        subprocess.Popen([sys.executable, str(MAIN)], start_new_session=True, **kwargs)
    sys.exit(0)   # return immediately — NEVER wait on the child

if __name__ == "__main__":
    main()
```

Windows `.bat` wrapper (point Tautulli's Script File here to avoid `.py`
interpreter ambiguity):
```bat
@echo off
rem glidearr_launch.py sits next to this .bat in Tautulli's Script Folder.
start "" /B pythonw "%~dp0glidearr_launch.py"
exit /b 0
```

---

## 5. Benefits (the sell)

- **Decisions stay current automatically.** Acquisition, space decisions, and
  recommendation ranking reflect what was actually watched — no remembering to run
  `main.py` by hand.
- **Cheaper to run often *with the daemon on*.** Cache-only Trakt means a 30–60 min
  schedule (or an event fire) costs little beyond the cache-gated Radarr fetch.
  (See the coupling note in §3 — this benefit is tied to the daemon toggle.)
- **Event hook = zero idle work + instant freshness** *when same-host and
  daemon-enabled*: nothing fires when nobody's watching; runs fire within seconds
  when they do; a binge collapses to one run via the sentinel.
- **Reuses proven primitives.** Detached spawning and the single-flight sentinel
  already ship for the enrich daemon. (The helper itself is **new, untested-in-this-
  context** code that *imitates* those primitives — see §6; reusing a pattern is not
  the same as reusing tested code.)
- **Fully reversible, near-zero footprint.** Scheduler = one task. Event hook = one
  Tautulli agent + one file. Neither changes Glidearr's source.

---

## 6. Risks & informed consent

Read these before committing — no sugar-coating.

- **⚠ Automation auto-arms whatever capabilities you have already enabled —
  including deletion.** Running `main.py` on a timer/event does **not** by itself
  enable anything, but if you have already turned on the gated write capabilities
  (phase-3 acquisition / write-back with `dry_run` off, and **space-pressure
  deletion**, which is hard-disabled unless `free_space_limit` is set), then
  automation makes those **fire unattended on every trigger** — including
  acquisitions and deletions you are no longer in the loop to veto. Before
  automating, confirm your intended posture: leave `dry_run` on (plan-only, safe),
  or accept unattended writes/deletes. *This is the single most important
  informed-consent point.*

- **Sentinel/debounce is daemon-gated.** The overlap and binge-collapse protection
  exists **only when `daemons.enrich.enabled` is true** ([`main.py:651-652`,
  `:683-684`](scripts/main.py)). With the daemon off there is no sentinel; the
  helper has no debounce and the scheduler↔event cross-overlap is unguarded.

- **The daemon toggle is a three-way trade.** Enabling it gives cheap cache-only
  runs *and* the sentinel, but also makes `main.py` **restart the daemon on every
  launch** (graceful: 1.5 s poll granularity, 10 s grace window, hard-kill
  fallback — so *up to a couple seconds* of restart work per launch, not a fixed
  tiny number). Frequent triggers restart it often; repeated short-interval
  restarts can also leave the Trakt cache perpetually half-warmed, eroding the very
  "cache-only = cheap" premise. Low severity, but real.

- **Keyring vs. the run-as user.** Secrets resolve env var (`RECOMMENDARR_*`) →
  OS keyring → inline config. If the scheduler/Tautulli runs as a *different* OS
  user than the one who onboarded, the OS keyring (Windows Credential Manager) is
  **not shared** and auth fails. *Mitigation:* same user, or `RECOMMENDARR_*` env
  vars (non-empty values only).

- **Double-run / Parquet collision.** If a run outlasts the interval, two instances
  can write the same Parquet files; single-file writes are atomic but multi-file
  updates are **not** serialized. *Mitigation:* "do not start a new instance" /
  `flock -n`, or interval ≫ run time. The "1 h vs 2–5 min" margin is only
  comfortable when every external dependency is healthy — a cold cache + a slow
  upstream API can push a run well past 5 min.

- **Same-host constraint breaks the event hook (the big one for path B).** Tautulli
  runs scripts only on its own filesystem with its own interpreter. Separate host ⇒
  the hook **cannot** work (no remote execution; Glidearr has no HTTP listener).
  Separate container ⇒ you must mount the repo + `config.json` + cache (sentinel on
  a **shared** volume) and provide Python-with-deps inside Tautulli's image — the
  stock Tautulli image likely lacks this; confirm for your tag.

- **Timeout footgun (event hook).** If the helper ever waits on the child or runs
  `main.py` inline, Tautulli kills it at ~30 s mid-run, leaving a half-finished run
  and (if daemon-enabled) a stale sentinel until the 1800 s backstop clears it.

- **Windows job-object reaping (event hook).** A detached child can still be killed
  if Tautulli's parent lives in a job with kill-on-close. `CREATE_BREAKAWAY_FROM_JOB`
  guards this — but the OSError **fallback path** (recommended above) drops the
  breakaway flag and therefore *re-exposes* the reaping risk. Prefer launching from
  a context that allows breakaway.

- **Silent failures + no built-in heartbeat.** A disabled task / deleted crontab
  line stops runs with no alert; a detached event-fired child that crashes is
  invisible to Tautulli (the helper already returned 0). You only learn via
  Glidearr's own logs / Discord notifications. A "last run was N hours ago"
  heartbeat would be **net-new work** — it does not exist today.

- **Log growth.** `launch.log` + `sched_main.log` are append-only with no rotation;
  an event hook firing per-watch plus an hourly schedule grows logs unbounded.
  Add rotation if this runs on a busy server.

- **Machine sleep / DST (Windows).** "Repeat every 1 hour / Indefinitely" misses
  runs while the box is asleep; decide whether you want "Run task as soon as
  possible after a missed start" and how DST shifts affect the cadence — relevant
  since the target is a personal Windows 11 machine, not a server.

- **Security surface (event hook).** Tautulli Script agents are remote-code-
  execution by design. Anyone who can reach/compromise the Tautulli UI can trigger
  arbitrary `main.py` runs. Keep Tautulli access controlled.

- **First WRITE to a read-only integration (future only).** Auto-registering the
  Script agent via Tautulli's API (§8) would be the first write from Glidearr to
  Tautulli (today strictly read-only). Out of scope here — the manual UI setup above
  writes nothing from Glidearr.

---

## 7. Rollback / reversibility

- **Windows Task Scheduler:** right-click `Glidearr Hourly` → **Disable**
  (reversible) or **Delete**. Remove `run_glidearr.bat` if desired.
- **Linux/macOS cron:** `crontab -e` and remove the line (or `crontab -r`). Delete
  `/tmp/glidearr.lock` if present.
- **In-container cron:** remove the lines / rebuild, or stop the cron sidecar.
- **Tautulli event hook:** Settings → Notification Agents → untick **Watched**
  (pause) or **Delete** the Script agent; delete the helper from the Script Folder.
  A leftover `main_run.active` self-clears after 1800 s — delete it to clear now.

No Glidearr source changes are made by either path, so there is nothing to
`git revert`.

---

## 8. Optional future: auto-register via the Tautulli API (later phase — NOT part of this plan)

The Script notifier *could* be created programmatically instead of via the UI.
This would be the **first write** from Glidearr to Tautulli, so it is
deliberately deferred. Verified command set (params from Tautulli docs; `agent_id =
15` for Script from source):

- **`add_notifier_config`** — `agent_id=15` to create the Script agent.
- **`set_notifier_config`** — `notifier_id`, `agent_id=15`, plus agent-prefixed
  config (`scripts_script_folder`, `scripts_script`, `scripts_timeout`); enable the
  trigger with the action key `on_watched=1` (+ optional `on_watched_subject` /
  `on_watched_body`).
- **`get_notifiers`** — discover/find the created notifier; returns
  `{id, agent_id, agent_name, friendly_name, active, ...}`.
- **`delete_notifier`** — `notifier_id`, for teardown / re-register (idempotency).

**Caveats (confirm before automating):** the exact per-action script-selection key
is only **medium-confidence** from source, not fully documented — manually
configure one Script notifier, dump it via `get_notifiers`/the config endpoint, and
capture the literal keys first. Network/mounted Script Folders are blocked unless
`allow_mounted_folders = 1` in Tautulli's `config.ini`. This path still inherits the
same-host constraint and the security surface of §6.

---

*Verified against the codebase 2026-06-13: sentinel path + daemon-gating
([`main.py:651-652,683-684`](scripts/main.py)), path layout + constants
([`daemon_paths.py:24-43`](scripts/managers/factories/daemons/daemon_paths.py)),
launch via self-bootstrap — `main.py` inserts the repo root onto `sys.path` at
startup (mirroring `enrich_daemon.py` / `onboarding.py`) and is 100%
`scripts.`-prefixed, so no `PYTHONPATH` is required (import fix applied 2026-06-13),
detached-spawn pattern
([`supervisor.py:131-188`](scripts/managers/factories/daemons/supervisor.py)),
read-only Tautulli integration. Tautulli-side facts (Script `agent_id=15`, 30 s
default kill-timeout, 85% watched default, script-folder constraint, API command
set) verified against Tautulli docs/source.*
