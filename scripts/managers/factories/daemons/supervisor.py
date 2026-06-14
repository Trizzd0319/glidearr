"""
supervisor.py — spawn / safe-stop / restart the enrichment daemon.
================================================================================
``main.py`` calls ``EnrichDaemonSupervisor(logger).restart()`` when the daemon is
enabled. ``restart()`` gracefully stops any already-running instance (via a stop
sentinel the daemon polls, falling back to a hard kill after a grace window) and
then spawns a fresh DETACHED process that survives main.py exiting.

Windows notes:
  * DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP makes the child outlive the parent
    and not share its console.
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
    DAEMON_SCRIPT, GRACE_STOP_S, LOG_PATH, PID_PATH, POLL_INTERVAL_S,
    REPO_ROOT, STOP_SENTINEL,
)
from scripts.support.utilities.logger.logger import LoggerManager


class EnrichDaemonSupervisor:
    def __init__(self, logger=None):
        self.logger = logger or LoggerManager()

    # ── PID helpers ────────────────────────────────────────────────────────────
    def _read_pid(self) -> int | None:
        try:
            return int(PID_PATH.read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
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
            return "enrich_daemon" in cmd or "python" in cmd
        except OSError:
            return True

    def _hard_kill(self, pid: int) -> None:
        self.logger.log_warning(f"[EnrichDaemon] pid {pid} did not stop in time — terminating.")
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=15)
            except Exception as e:
                self.logger.log_warning(f"[EnrichDaemon] taskkill failed: {e}")
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
        for p in (PID_PATH, STOP_SENTINEL):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ── Public API ─────────────────────────────────────────────────────────────
    def is_running(self) -> bool:
        pid = self._read_pid()
        return bool(pid and self._pid_alive(pid))

    def stop(self, timeout: float = GRACE_STOP_S) -> bool:
        """Gracefully stop a running daemon; hard-kill if it overruns the grace
        window. Always leaves no pid/sentinel behind. Returns True when stopped."""
        pid = self._read_pid()
        if pid is None or not self._pid_alive(pid):
            self._cleanup()
            return True

        self.logger.log_info(f"[EnrichDaemon] stopping existing daemon (pid {pid})...")
        STOP_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        try:
            STOP_SENTINEL.write_text(str(time.time()))
        except OSError as e:
            self.logger.log_warning(f"[EnrichDaemon] could not write stop sentinel: {e}")

        waited = 0.0
        while waited < timeout:
            if not self._pid_alive(pid):
                self.logger.log_info(f"[EnrichDaemon] daemon (pid {pid}) stopped cleanly.")
                self._cleanup()
                return True
            time.sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S

        self._hard_kill(pid)
        self._cleanup()
        return True

    def spawn(self) -> int:
        """Launch a fresh detached daemon and record its pid. Returns the pid."""
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)

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
        logf = open(LOG_PATH, "a", encoding="utf-8")

        def _popen(flags: int):
            return subprocess.Popen(
                [sys.executable, str(DAEMON_SCRIPT)],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
                cwd=str(REPO_ROOT),
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
                    "[EnrichDaemon] CREATE_BREAKAWAY_FROM_JOB rejected by the launcher's "
                    "job object — the daemon may not outlive an IDE run. Launch main.py from "
                    "a terminal or Task Scheduler for a persistent background daemon."
                )
            proc = _popen(base_flags)

        try:
            PID_PATH.write_text(str(proc.pid))
        except OSError as e:
            self.logger.log_warning(f"[EnrichDaemon] could not write pid file: {e}")
        self.logger.log_info(
            f"[EnrichDaemon] spawned background enrichment daemon (pid {proc.pid}); "
            f"logging to {LOG_PATH}"
        )
        return proc.pid

    def restart(self) -> int:
        """Stop any running instance, then spawn a fresh one. Returns the new pid."""
        self.stop()
        return self.spawn()
