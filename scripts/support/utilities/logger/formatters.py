import logging
import json
import os
from datetime import datetime

#: Levels that carry the process id. INFO is the operator-facing narrative and
#: stays clean; DEBUG and the error levels are what someone reads when they are
#: trying to work out what happened, and that is exactly when "which process
#: wrote this?" matters.
#:
#: WHY THIS EXISTS (`GLD-MGR-13`): a run spawns subprocesses (the enrich daemon,
#: the pilot-search daemon) whose early bootstrap lines can land in the
#: orchestrator's `default.log` before their own sink is selected. With no pid on
#: the record, a block of lines in the middle of a run is indistinguishable
#: between "the main process re-initialised something" and "a subprocess started".
#: That single ambiguity cost SIX successive wrong attributions in one session,
#: every one of them plausible, none of them measurable from the log. One integer
#: settles it permanently.
_PID_LEVELS = frozenset({logging.DEBUG, logging.WARNING, logging.ERROR, logging.CRITICAL})


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        # Appended LAST so the field order every existing reader relies on is
        # unchanged, and an INFO record is byte-identical to what it was before.
        if record.levelno in _PID_LEVELS:
            log_record["pid"] = os.getpid()
        return json.dumps(log_record)
