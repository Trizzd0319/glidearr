"""
supervisor.py — spawn / safe-stop / restart the background daemons.
================================================================================
``main.py`` calls ``EnrichDaemonSupervisor(logger).restart()`` when the Trakt
enrichment daemon is enabled, and ``PilotSearchDaemonSupervisor(logger).ensure_running()``
when the pilot-search daemon is enabled. ``restart()`` gracefully stops any already-running
instance (via a stop sentinel the daemon polls, falling back to a hard kill after a grace
window) and then spawns a fresh DETACHED process that survives main.py exiting;
``ensure_running()`` spawns one ONLY if none is alive (so it never interrupts an in-flight
search batch — the pilot daemon must not be restarted out from under a spree).

Both daemons share the SAME detached-spawn / pid / stop-sentinel machinery, so it lives in
``_BaseDaemonSupervisor`` and each daemon is a thin subclass that supplies its own paths.

Windows notes:
  * DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP makes the child outlive the parent
    and not share its console.
  * CREATE_BREAKAWAY_FROM_JOB escapes an IDE's KILL_ON_JOB_CLOSE job object so the
    daemon truly outlives the run (we fall back without it if the job forbids breakaway).
  * stdin=DEVNULL + an explicit log-file handle + close_fds keep the child from
    holding the parent's stdio.
  * cwd=REPO_ROOT so the child's ``scripts.*`` imports resolve exactly like main.py.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from scripts.managers.factories.daemons.daemon_paths import (
    DAEMON_SCRIPT, GRACE_STOP_S, LOG_PATH, PID_PATH,
    PILOT_DAEMON_SCRIPT, PILOT_LOG_PATH, PILOT_PID_PATH, PILOT_POLL_INTERVAL_S,
    PILOT_STOP_SENTINEL, POLL_INTERVAL_S, REPO_ROOT, STOP_SENTINEL,
)
from scripts.support.utilities.logger.logger import LoggerManager


class _BaseDaemonSupervisor:
    """Shared spawn/stop/restart logic. Subclasses set the per-daemon paths + labels."""

    # ── Subclass-supplied configuration ────────────────────────────────────────
    _name: str          = "Daemon"          # log prefix, e.g. "EnrichDaemon"
    _spawned_desc: str  = "background daemon"
    _script             = None              # Path to the daemon entry script
    _pid_path           = None
    _stop_sentinel      = None
    _log_path           = None
    _grace_stop_s: float = GRACE_STOP_S
    _poll_interval_s: float = POLL_INTERVAL_S
    _cmd_match: str     = "python"          # substring proving a pid is our daemon (anti PID-reuse)

    def __init__(self, logger=None):
        self.logger = logger or LoggerManager()

    # ── PID helpers ────────────────────────────────────────────────────────────
    def _read_pid(self) -> int | None:
        try:
            return int(self._pid_path.read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _pid_alive(self, pid: int) -> bool:
        """Best-effort liveness probe that also rejects PID reuse by a non-python
        process (so we never signal an unrelated program)."""
        if os.name == "nt":
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=10,
                )
            except Exception:
                return False
            row = out.stdout.strip()
            return str(pid) in row and "python" in row.lower()
        # POSIX
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # Confirm it's our daemon, not a reused PID, when /proc is available.
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
            return self._cmd_match in cmd or "python" in cmd
        except OSError:
            return True

    def _hard_kill(self, pid: int) -> None:
        self.logger.log_warning(f"[{self._name}] pid {pid} did not stop in time — terminating.")
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=15)
            except Exception as e:
                self.logger.log_warning(f"[{self._name}] taskkill failed: {e}")
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            except OSError:
                pass
            time.sleep(0.5)
            if not self._pid_alive(pid):
                return

    def _cleanup(self) -> None:
        for p in (self._pid_path, self._stop_sentinel):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ── Public API ─────────────────────────────────────────────────────────────
    def is_running(self) -> bool:
        pid = self._read_pid()
        return bool(pid and self._pid_alive(pid))

    def stop(self, timeout: float | None = None) -> bool:
        """Gracefully stop a running daemon; hard-kill if it overruns the grace
        window. Always leaves no pid/sentinel behind. Returns True when stopped."""
        timeout = self._grace_stop_s if timeout is None else timeout
        pid = self._read_pid()
        if pid is None or not self._pid_alive(pid):
            self._cleanup()
            return True

        self.logger.log_info(f"[{self._name}] stopping existing daemon (pid {pid})...")
        self._stop_sentinel.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._stop_sentinel.write_text(str(time.time()))
        except OSError as e:
            self.logger.log_warning(f"[{self._name}] could not write stop sentinel: {e}")

        waited = 0.0
        while waited < timeout:
            if not self._pid_alive(pid):
                self.logger.log_info(f"[{self._name}] daemon (pid {pid}) stopped cleanly.")
                self._cleanup()
                return True
            time.sleep(self._poll_interval_s)
            waited += self._poll_interval_s

        self._hard_kill(pid)
        self._cleanup()
        return True

    def spawn(self) -> int:
        """Launch a fresh detached daemon and record its pid. Returns the pid."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._pid_path.parent.mkdir(parents=True, exist_ok=True)

        base_flags = 0
        breakaway  = 0
        extra: dict = {}
        if os.name == "nt":
            base_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            # Break out of the launcher's Job Object. IDEs (PyCharm) run the script
            # inside a job with KILL_ON_JOB_CLOSE; DETACHED_PROCESS does NOT remove a
            # child from that job, so the "detached" daemon is terminated when the
            # IDE tears the run down (the daemon dies seconds after the run ends).
            # CREATE_BREAKAWAY_FROM_JOB escapes the job so the daemon truly outlives
            # the run. We fall back below if the job forbids breakaway.
            breakaway = subprocess.CREATE_BREAKAWAY_FROM_JOB
        else:
            extra["start_new_session"] = True

        # Leak the log handle to the child intentionally (it owns its stdout/stderr).
        logf = open(self._log_path, "a", encoding="utf-8")

        # Mark the child as a daemon so any 'default' LoggerManager it incidentally builds
        # (e.g. via ConfigLoader) is redirected to a daemon-owned sink and never rotates/clobbers
        # the orchestrator's run log (LoggerManager.DAEMON_ENV / _effective_log_name).
        child_env = {**os.environ, "GLIDEARR_DAEMON": "1"}

        def _popen(flags: int):
            return subprocess.Popen(
                [sys.executable, str(self._script)],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
                cwd=str(REPO_ROOT),
                env=child_env,
                **extra,
            )

        try:
            proc = _popen(base_flags | breakaway)
        except OSError:
            # The launcher's job object disallows breakaway — spawn without it. The
            # daemon then survives a terminal / Task Scheduler launch but may still
            # be reaped when an IDE run window closes.
            if breakaway:
                self.logger.log_warning(
                    f"[{self._name}] CREATE_BREAKAWAY_FROM_JOB rejected by the launcher's "
                    "job object — the daemon may not outlive an IDE run. Launch main.py from "
                    "a terminal or Task Scheduler for a persistent background daemon."
                )
            proc = _popen(base_flags)

        try:
            self._pid_path.write_text(str(proc.pid))
        except OSError as e:
            self.logger.log_warning(f"[{self._name}] could not write pid file: {e}")
        self.logger.log_info(
            f"[{self._name}] spawned {self._spawned_desc} (pid {proc.pid}); "
            f"logging to {self._log_path}"
        )
        return proc.pid

    def restart(self) -> int:
        """Stop any running instance, then spawn a fresh one. Returns the new pid."""
        self.stop()
        return self.spawn()

    def ensure_running(self) -> int | None:
        """Spawn a fresh daemon ONLY if none is alive; return the running pid. Unlike
        ``restart`` this never interrupts a daemon that's mid-batch — used by the pilot
        daemon so a queued search spree is picked up without killing one already underway."""
        pid = self._read_pid()
        if pid and self._pid_alive(pid):
            self.logger.log_debug(f"[{self._name}] already running (pid {pid}); leaving it alone.")
            return pid
        return self.spawn()


class EnrichDaemonSupervisor(_BaseDaemonSupervisor):
    _name         = "EnrichDaemon"
    _spawned_desc = "background enrichment daemon"
    _script       = DAEMON_SCRIPT
    _pid_path     = PID_PATH
    _stop_sentinel = STOP_SENTINEL
    _log_path     = LOG_PATH
    _grace_stop_s = GRACE_STOP_S
    _poll_interval_s = POLL_INTERVAL_S
    _cmd_match    = "enrich_daemon"


class PilotSearchDaemonSupervisor(_BaseDaemonSupervisor):
    _name         = "PilotSearch daemon"
    _spawned_desc = "background pilot-search daemon"
    _script       = PILOT_DAEMON_SCRIPT
    _pid_path     = PILOT_PID_PATH
    _stop_sentinel = PILOT_STOP_SENTINEL
    _log_path     = PILOT_LOG_PATH
    _grace_stop_s = GRACE_STOP_S
    _poll_interval_s = PILOT_POLL_INTERVAL_S
    _cmd_match    = "pilot_search_daemon"
