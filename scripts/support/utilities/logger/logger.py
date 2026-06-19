import functools
import glob
import gzip
import inspect
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from scripts.support.utilities.logger.formatters import JsonFormatter

# ──────────────── Log Paths ──────────────── #
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
# os.chdir(ROOT_DIR)

# parents[3] = scripts/ (this file: scripts/support/utilities/logger/logger.py).
# The old parent.parent.parent landed on scripts/support and then appended
# support/logs — silently creating and logging into a bogus nested
# scripts/support/support/logs tree alongside the real scripts/support/logs.
LOG_DIR = Path(__file__).resolve().parents[3] / "support" / "logs"
LOG_FILES = {
    "recommender": LOG_DIR / "recommender.log.jsonl",
    "api_failures": LOG_DIR / "api_failures.log.jsonl",
    "function_tracking": LOG_DIR / "function_tracking.log.jsonl",
    "tvdb_trace": LOG_DIR / "TVDB_enrichment_trace.log",
}

# ── Per-run log rotation (Kometa-style) ───────────────────────────────────────
# At the start of each run the previous run's file is rolled aside
# (default.log -> default-1.log, -1 -> -2, …). The current run PLUS this many
# previous runs are kept; anything older is trashed. Bump to retain more history.
RUN_LOG_BACKUPS = 5                           # current + 5 previous = 6 logs retained


def _rotate_run_logs(log_path: Path, backups: int = RUN_LOG_BACKUPS) -> None:
    """Roll ``<name>.log`` -> ``<name>-1.log`` -> ``<name>-2.log`` … keeping ``backups``
    previous runs (so ``backups`` + the fresh current = ``backups`` + 1 files), and delete
    any older numbered backups. Best-effort — a missing/locked file never raises. MUST run
    before the file handler opens the log (an open file can't be renamed on Windows)."""
    try:
        parent, stem, suffix = log_path.parent, log_path.stem, log_path.suffix
        # Trash anything at/beyond the retention window: the oldest kept slot is overwritten
        # by the shift below, and leftovers from a previously-larger setting are removed here.
        for p in parent.glob(f"{stem}-*{suffix}"):
            m = re.fullmatch(rf"{re.escape(stem)}-(\d+){re.escape(suffix)}", p.name)
            if m and int(m.group(1)) >= backups:
                p.unlink(missing_ok=True)
        # Shift survivors up one slot: (backups-1) -> backups, … , 1 -> 2.
        for n in range(backups - 1, 0, -1):
            src = parent / f"{stem}-{n}{suffix}"
            if src.exists():
                src.replace(parent / f"{stem}-{n + 1}{suffix}")
        # The just-finished run's file becomes -1; the new run opens a fresh <name>.log.
        if log_path.exists():
            log_path.replace(parent / f"{stem}-1{suffix}")
    except OSError:
        pass                                  # never let log rotation break the run

# ──────────────── Secret scrubbing ──────────────── #
# Defense-in-depth: every log line passes through LoggerManager._scrub() so a
# credential that slips into a message, exception string, or request URL never
# reaches the log file or console. Tight, credential-context-only patterns keep
# false positives low; exact known secret values (registered at config load)
# catch anything that appears outside a recognised pattern.
_SECRET_SCRUB_PATTERNS = [
    # Credential query-string params: ?apikey=… &token=… X-Plex-Token=…
    (re.compile(
        r'(?i)\b((?:api_?key|app_?key|access_token|refresh_token|client_secret|'
        r'x[-_]?plex[-_]?token|auth_?token|token)=)[^&\s"\'#}]+'),
     r'\1<redacted>'),
    # Plex profile PIN: pin=NNNN (the /home/users/{uuid}/switch credential — never
    # build a log/exception string containing a raw PIN, but redact defensively).
    # Mirror the api_key shape so a NON-numeric PIN and an ampersand-terminated PIN
    # (pin=12ab&foo) are caught too, not just the bare-numeric pin=1234 case.
    (re.compile(r'(?i)\b(pin=)[^&\s"\'#}]+'), r'\1<redacted>'),
    # Authorization / api-key / plex headers: "Bearer xxx", "X-Api-Key: xxx"
    (re.compile(
        r'(?i)((?:bearer|x[-_]?api[-_]?key|x[-_]?plex[-_]?token|authorization)\s*[:=]\s*)'
        r'[^\s,;"\'}]+'),
     r'\1<redacted>'),
    # JWTs (e.g. the TVDB v4 token)
    (re.compile(r'eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}'),
     '<redacted-jwt>'),
]

# ──────────────── Console colour + decoration stripping ──────────────── #
# Status is conveyed by COLOUR, not emoji: red = stopped/error, yellow = caution,
# green = good (neutral for plain info). Decorative emoji / symbols / box-drawing
# are stripped from every log line; letters, digits, CJK and accented characters
# (show titles, usernames, …) are preserved.
SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")

_ANSI = {"reset": "\033[0m", "red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m"}


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        try:  # enable ANSI/VT processing on Windows consoles
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass
    return True


# Decorative codepoints to drop (emoji, pictographs, dingbats, symbols, box
# drawing, geometric shapes, arrows, regional indicators, variation selectors).
_DECOR_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji & pictographs & supplemental symbols
    "\U00002600-\U000026FF"   # miscellaneous symbols (warning, gear, …)
    "\U00002700-\U000027BF"   # dingbats (check / cross marks)
    "\U00002B00-\U00002BFF"   # misc symbols & arrows
    "\U00002500-\U000025FF"   # box drawing, blocks, geometric shapes
    "\U00002190-\U000021FF"   # arrows
    "\U00002300-\U000023FF"   # misc technical (clock, stopwatch, hourglass, watch)
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "️‍⃣"      # variation selector, ZWJ, combining enclosing keycap
    "]"
)
# Decorative punctuation swapped for ASCII (applied BEFORE the range removal so
# the common arrow/bullet glyphs stay readable rather than vanishing).
_DECOR_REPL = {
    "→": "->", "←": "<-", "•": "-",
    "…": "...", "—": "-", "–": "-",
}


def _strip_decor(s: str) -> str:
    try:
        for k, v in _DECOR_REPL.items():
            if k in s:
                s = s.replace(k, v)
        s = _DECOR_RE.sub("", s)
        s = re.sub(r"[ \t]{2,}", " ", s)   # tidy whitespace left by removed glyphs
        return s.strip()
    except Exception:
        return s


# ──────────────── Kometa-style boxed tables ──────────────── #
# A single renderer behind both log_table() and log_grid(). Borders use plain
# ASCII '|' / '=' because Unicode box-drawing is stripped by _DECOR_RE; ALL
# internal padding uses NON-BREAKING spaces so the column alignment survives
# _strip_decor's space-collapsing (re.sub(r"[ \t]{2,}"," "), which mangles
# ordinary space-padded tables). The title becomes a centred banner that
# doubles as the top border, mirroring Kometa's "==== Summary ====" look.
_NBSP = " "


def _looks_numeric(s: str) -> bool:
    s = str(s).strip().replace(",", "").replace("+", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _box_table(headers, rows, title="", cap=None, caption="") -> str:
    """Render an outlined, Kometa-style table as one multi-line string.

    Columns are sized to their own widest (capped) cell; numeric columns are
    right-aligned, everything else left-aligned. A long ``title`` widens the
    last column so it always fits inside the banner without breaking alignment.
    ``caption`` (optional) is word-wrapped to the table width and rendered as
    full-width lines just under the title — a one-line description of what the
    whole table is. Returns "" when ``rows`` is empty."""
    if not rows:
        return ""
    NBSP = _NBSP
    title = (title or "").strip()

    def _t(s):
        s = str(s)
        if cap and len(s) > cap:
            return s[: cap - 2] + ".."
        return s

    head = [_t(c) for c in (headers or [])]
    body = [[_t(c) for c in r] for r in rows]
    ncol = max([len(head)] + [len(r) for r in body])
    if ncol == 0:
        return ""
    head = head + [""] * (ncol - len(head))
    body = [r + [""] * (ncol - len(r)) for r in body]

    widths = [max([len(head[i])] + [len(r[i]) for r in body]) or 1 for i in range(ncol)]
    right = [all(_looks_numeric(r[i]) for r in body) for i in range(ncol)]

    col_w = [w + 2 for w in widths]                # +1 NBSP pad on each side
    inner_w = sum(col_w) + (ncol - 1)              # + interior '|' joints

    # Grow the last column so a long title fits inside the banner (>=1 '=' each side).
    if title and len(title) + 4 > inner_w:
        deficit = len(title) + 4 - inner_w
        widths[-1] += deficit
        col_w[-1] += deficit
        inner_w += deficit

    def _cell(text, i):
        pad = NBSP * (widths[i] - len(text))
        return NBSP + ((pad + text) if right[i] else (text + pad)) + NBSP

    def _row(cells):
        return "|" + "|".join(_cell(c, i) for i, c in enumerate(cells)) + "|"

    border = "|" + "=" * inner_w + "|"
    empty = "|" + NBSP * inner_w + "|"
    head_sep = "|" + "|".join("=" * w for w in col_w) + "|"

    if title:
        mid = NBSP + title.replace(" ", NBSP) + NBSP
        fill = inner_w - len(mid)
        left = fill // 2
        top = "|" + "=" * left + mid + "=" * (fill - left) + "|"
    else:
        top = border

    # Caption: word-wrapped to the table width, rendered as full-width lines under the title.
    # NBSP-pad like cells so _strip_decor's space-collapsing can't mangle it.
    cap_lines = []
    _cap = (caption or "").strip()
    if _cap:
        import textwrap
        for _w in (textwrap.wrap(_cap, width=max(1, inner_w - 2)) or [""]):
            _w = _w[: inner_w - 2]
            pad = NBSP * (inner_w - 1 - len(_w))
            cap_lines.append("|" + NBSP + _w.replace(" ", NBSP) + pad + "|")

    return "\n".join([top, *cap_lines, empty, _row(head), head_sep,
                      *(_row(r) for r in body), empty, border])


class _ColorFormatter(logging.Formatter):
    """Console formatter — colours the line by severity. No colour in log files."""

    def __init__(self, use_color: bool = True):
        super().__init__("%(message)s")
        self.use_color = use_color

    def format(self, record):
        msg = super().format(record)
        if not self.use_color:
            return msg
        lvl = record.levelno
        if lvl >= logging.ERROR:
            col = _ANSI["red"]
        elif lvl >= logging.WARNING:
            col = _ANSI["yellow"]
        elif lvl == SUCCESS_LEVEL:
            col = _ANSI["green"]
        else:
            return msg            # info / debug: neutral terminal colour
        return f"{col}{msg}{_ANSI['reset']}"


# Global accessor

def get_logger():
    return LoggerManager()


# ──────────────── Logger Singleton ──────────────── #
class LoggerManager:
    _instance = None
    _thread_local = threading.local()
    _file_lock = threading.Lock()
    _scrub_values: set = set()   # exact secret strings registered at config load

    # Below this length a value is treated as noise for the GENERAL register path
    # (an 8-char floor keeps common short words/ids from over-scrubbing every line).
    _SCRUB_MIN_LEN = 8

    @classmethod
    def register_secret(cls, value, allow_short=False):
        """Register ONE exact secret string so it is redacted from every subsequent
        log line, even outside a known pattern.

        ``allow_short=True`` bypasses the ``_SCRUB_MIN_LEN`` floor for a KNOWN short
        credential (e.g. a resolved 4-digit Plex profile PIN), so the exact value is
        value-scrubbed everywhere — in ``pin=1234`` form, in dict-repr, and bare in a
        sentence. Use it ONLY for confirmed credentials; the general
        :meth:`register_secrets` path keeps the floor to avoid over-scrubbing noise."""
        if not isinstance(value, str) or not value:
            return
        if allow_short or len(value) >= cls._SCRUB_MIN_LEN:
            cls._scrub_values.add(value)

    @classmethod
    def register_short_secrets(cls, values):
        """Register exact KNOWN short credentials (PINs, short codes) so the floor in
        :meth:`register_secrets` is bypassed and each value is redacted everywhere."""
        for v in values:
            cls.register_secret(v, allow_short=True)

    @classmethod
    def register_secrets(cls, values):
        """Register exact secret strings (api keys, tokens, webhook URLs) so they
        are redacted from every subsequent log line, even outside a known pattern.

        Values under ``_SCRUB_MIN_LEN`` chars are IGNORED here to avoid over-scrubbing
        short noise; register a known short credential via :meth:`register_short_secrets`
        (or ``register_secret(value, allow_short=True)``) instead."""
        for v in values:
            cls.register_secret(v, allow_short=False)

    def _scrub(self, message):
        """Redact credentials from a log message. Best-effort; never raises."""
        try:
            s = message if isinstance(message, str) else str(message)
            if LoggerManager._scrub_values:
                for val in LoggerManager._scrub_values:
                    if val and val in s:
                        s = s.replace(val, "<redacted>")
            for pat, repl in _SECRET_SCRUB_PATTERNS:
                s = pat.sub(repl, s)
            return s
        except Exception:
            return message

    def _present(self, message):
        """Scrub secrets, then strip decorative emoji/symbols, for display + files."""
        return _strip_decor(self._scrub(message))

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, log_name="default", level=logging.INFO):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.log_name = log_name
        self.level = level or logging.INFO
        self.logger = logging.getLogger(log_name)
        self.logger.setLevel(self.level)

        if not self.logger.handlers:
            log_path = LOG_DIR / f"{log_name}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Kometa-style: roll the previous run's log aside before opening this run's fresh
            # file. Skipped under pytest so test runs never churn the real run logs.
            if "pytest" not in sys.modules:
                _rotate_run_logs(log_path)

            file_handler = logging.FileHandler(log_path)
            file_handler.setFormatter(JsonFormatter())
            file_handler.setLevel(self.level)  # 🔧 Apply user-specified level
            self.logger.addHandler(file_handler)

            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(_ColorFormatter(use_color=_supports_color()))
            stream_handler.setLevel(self.level)  # Apply user-specified level
            self.logger.addHandler(stream_handler)

        self.logger.propagate = False


    def _setup_json_logger(self, base_name, max_runs=5):
        pattern = os.path.join(LOG_DIR, f"{base_name}.run-*.jsonl")
        existing_logs = sorted(glob.glob(pattern))

        def extract_run_number(filename):
            match = re.search(r'run-(\d{3})\.jsonl', filename)
            return int(match.group(1)) if match else 0

        run_numbers = [extract_run_number(f) for f in existing_logs]
        next_run_number = max(run_numbers, default=0) + 1
        run_id = f"{next_run_number:03d}"

        # Delete oldest logs beyond limit
        if len(existing_logs) >= max_runs:
            for old_file in sorted(existing_logs)[:len(existing_logs) - max_runs + 1]:
                os.remove(old_file)

        run_filename = f"{base_name}.run-{run_id}.jsonl"
        full_path = os.path.join(LOG_DIR, run_filename)

        logger = logging.getLogger(full_path)
        logger.setLevel(logging.DEBUG)

        handler = logging.FileHandler(full_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter('%(message)s'))

        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False

        return logger

    def _rename_with_gz(self, default_name):
        return f"{default_name}.gz"

    def _compress_rotated_log(self, source, dest):
        with open(source, 'rb') as sf, gzip.open(dest, 'wb') as df:
            shutil.copyfileobj(sf, df)
        os.remove(source)

    def _log_json(self, logger, level, message, extra=None):
        # Grab the name of the caller function two frames up
        caller = inspect.stack()[2]
        method_name = caller.function
        class_context = caller.frame.f_locals.get('self', None)
        class_name = class_context.__class__.__name__ if class_context else None

        full_method = f"{class_name}.{method_name}" if class_name else method_name
        prefixed_message = self._present(f"[{full_method}] {message}")

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level.upper(),
            "message": prefixed_message
        }
        if extra:
            log_entry.update(extra)
        logger.log(getattr(logging, level.upper(), logging.INFO), json.dumps(log_entry))

        # Also log to console for top-level output
        if logger == self.recommender_logger:
            self.console_logger.log(getattr(logging, level.upper(), logging.INFO), prefixed_message)

    def get_log_context(self, depth=2):
        """
        Returns a string like [ClassName.method_name] for logging context.
        depth=2 ensures it traces to the caller of the public log_* method.
        """
        import inspect

        stack = inspect.stack()
        if len(stack) <= depth:
            return "[unknown]"

        frame_info = stack[depth]
        method_name = frame_info.function
        cls = frame_info.frame.f_locals.get('self', None)
        class_name = cls.__class__.__name__ if cls else None

        return f"[{class_name}.{method_name}]" if class_name else f"[{method_name}]"

    def get_current_call_stack(self):
        return getattr(LoggerManager._thread_local, "call_stack", [])

    def log_init_once(self, class_name, expected_count=1):
        """
        Tracks initialization count for a class.
        Logs info when count is within expected; warns if it exceeds.
        """
        count = self._init_tracker.get(class_name, 0) + 1
        self._init_tracker[class_name] = count

        if count == expected_count:
            self.log_info(
                f"[{class_name}.__init__] ✅ {class_name} initialized exactly {expected_count} time(s) as expected.")
        elif count < expected_count:
            self.log_info(f"[{class_name}.__init__] ✅ {class_name} initialized {count}/{expected_count} time(s).")
        else:
            self.log_warning(
                f"[{class_name}.__init__] ⚠️ {class_name} initialized {count} times (expected {expected_count}).")

    # ──────────────── Public Logging API ──────────────── #

    def enable_concise_logging(self):
        self.concise_mode = True

    def disable_concise_logging(self):
        self.concise_mode = False

    @property
    def is_concise(self):
        return self.concise_mode

    def log_error(self, message):
        context = self.get_log_context()
        self.logger.error(self._present(f"{context} {message}"))

    def log_debug(self, message):
        context = self.get_log_context()
        self.logger.debug(self._present(f"{context} {message}"))

    def log_exception(self, e: Exception, context: str = ""):
        import traceback
        log_context = self.get_log_context()
        msg = f"{log_context} Exception{f' in {context}' if context else ''}: {e}\n{traceback.format_exc()}"
        self.logger.error(self._present(msg))

    def log_function_entry(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            trace_id = str(uuid.uuid4())
            func_name = getattr(func, "__qualname__", func.__name__)
            try:
                caller = inspect.stack()[1]
                location = f"{caller.filename}:{caller.lineno}"
            except Exception:
                location = "unknown location"

            # Initialize or safely reference thread-local call stack
            try:
                if not hasattr(LoggerManager._thread_local, "call_stack"):
                    LoggerManager._thread_local.call_stack = []
                LoggerManager._thread_local.call_stack.append(func_name)
            except Exception as e:
                self._log_trace_to_file(trace_id, func_name, location, f"Thread-local error: {e}")
                raise RuntimeError(f"Thread-local call stack inaccessible: {e}")

            # self.logger.debug(f"TRACE [{trace_id}] ▶ Entering {func_name} at {location}")
            start = time.time()

            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start
                # self.logger.debug(f"TRACE [{trace_id}] ✅ Exiting {func_name} (took {elapsed:.2f}s)")
                return result
            except Exception as e:
                self.logger.error(self._present(f"TRACE [{trace_id}] Exception in {func_name}: {e}"))
                self._log_trace_to_file(trace_id, func_name, location, str(e))
                raise
            finally:
                try:
                    self._thread_local.call_stack.pop()
                except Exception:
                    pass

        return wrapper

    def log_profiled_run(self, profile_path="support/logs/tmp_profile.json"):
        profile_file = Path(profile_path)

        # If missing, create a placeholder
        if not profile_file.exists():
            profile_file.parent.mkdir(parents=True, exist_ok=True)
            profile_file.write_text(json.dumps({"calls": [], "summary": "No profiled calls recorded."}, indent=2))
            self.log_warning(f"⚠️ Profiling file missing — generated placeholder at: {profile_path}")

        # Determine next available run number
        logs_dir = Path("support/logs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        existing = list(logs_dir.glob("timings.run-*.json"))
        run_numbers = [int(f.stem.split("-")[-1]) for f in existing if f.stem.split("-")[-1].isdigit()]
        next_run = max(run_numbers, default=0) + 1

        target_path = logs_dir / f"timings.run-{next_run:03d}.json"
        profile_file.rename(target_path)

        self.log_info(f"📊 Call graph profiler saved to: {target_path}")

    def log_decision(self, msg):
        self._log_json(self.recommender_logger, "info", f"🧠 Decision: {msg}")

    def log_api_failure(self, service_name):
        timestamp = datetime.now().isoformat()
        log_entry = {"service": service_name, "timestamp": timestamp}
        with open(LOG_FILES["api_failures"], "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(log_entry) + "\n")
        self._log_json(self.api_logger, "error", f"API failure recorded for {service_name} at {timestamp}.")

    def log_to_all(self, message):
        for logger in [self.recommender_logger, self.api_logger, self.function_logger]:
            self._log_json(logger, "info", message)

    def log_table(self, headers, data, title="", descriptions=None, caption=""):
        """Render a boxed table.

        ``caption`` (optional) is a one-line description of WHAT THE TABLE IS, word-wrapped under
        the title. ``descriptions`` (optional) is a sequence parallel to ``data``: when given, a
        final ``Description`` column is appended explaining what each row's item IS, so terse
        metric labels (``acquired``, ``no-space``, …) are self-documenting in the log. Rows past
        the end of ``descriptions`` (or a ``None`` entry) get a blank description cell. Pass
        plain-ASCII text — the cp1252 console/log can't encode ≤ / … / —."""
        if not data:
            self.log_info("⚠️ No data available to display.")
            return
        _headers, _data = headers, data
        if descriptions is not None:
            _headers = list(headers) + ["Description"]
            _data = [
                list(row) + [
                    "" if (i >= len(descriptions) or descriptions[i] is None)
                    else str(descriptions[i])
                ]
                for i, row in enumerate(data)
            ]
        block = _box_table(_headers, _data, title=title, caption=caption)
        if block:
            self.log_info(f"\n{block}\n")

    def log_grid(self, headers, rows, title="", cap=16):
        """Log an outlined, Kometa-style table — borders boxed with ASCII ``|`` / ``=`` and the
        title rendered as a centred banner. Each column is sized to its OWN widest (capped) cell so
        one wide column never forces every column wide; numeric columns are right-aligned. Cells are
        padded with a NON-BREAKING space so the alignment survives ``_strip_decor``'s space-collapsing
        (``re.sub(r"[ \\t]{2,}"," ")``, which mangles ordinary space-padded tables). Cells longer than
        ``cap`` are truncated with '..'. Pass PLAIN-ASCII cells (avoid ≤ / … / — — the cp1252
        console/log file can't encode them); a no-op when ``rows`` is empty."""
        block = _box_table(headers, rows, title=title, cap=cap)
        if block:
            self.log_info(f"\n{block}\n")

    def log_redacted_config(self, config):
        self.log_info("📜 Loaded Configuration (Redacted):")
        redacted_keys = {
            "api", "apikey", "api_key", "token", "password", "secret",
            "auth", "client_id", "client_secret", "authorization", "pin", "plex_token"
        }

        def redact(k, v):
            return "✅" if k.lower() in redacted_keys else v

        for top_key, value in config.items():
            if top_key in {"cache_expiration", "compressed_cache_keys", "memory_cache_keys",
                           "preserve_dict_cache_keys"}:
                if isinstance(value, dict):
                    self.log_info(f"🔹 {top_key} ({len(value)} keys):")
                    for k, v in list(value.items())[:6]:
                        self.log_info(f"   - {k}: {v}")
                    if len(value) > 6:
                        self.log_info("   ... (truncated)")
                elif isinstance(value, list):
                    self.log_info(f"🔹 {top_key} ({len(value)} items): {value[:3]} ...")
                continue

            if isinstance(value, dict):
                self.log_info(f"🔹 {top_key}:")
                for subkey, subval in value.items():
                    self.log_info(f"   - {subkey}: {redact(subkey, subval)}")
            else:
                self.log_info(f"🔹 {top_key}: {redact(top_key, value)}")

    def log_validation_results(self, validation_results):
        table_data = [[svc, "✅ Yes" if ok else "❌ No"] for svc, ok in validation_results.items()]
        self.log_table(["Service", "Status"], table_data, title="Validation Summary")
        self.log_info("🚀 Startup Complete. All critical services validated.\n")

    def log_startup_header(self):
        self.log_info("🧠 Glidearr Starting Up...")
        self.log_info("══════════════════════════════════════════════════════")

    def log_audit(self, message):
        audit_path = LOG_DIR / "audit.log"
        with LoggerManager._file_lock:
            with open(audit_path, "a", encoding="utf-8") as f:
                f.write(f"[AUDIT] {message}\n")
        self.log_info(f"[AUDIT] {message}")

    def log_structured(self, category, details):
        path = LOG_DIR / f"{category}.jsonl"
        with LoggerManager._file_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(details) + "\n")
        self.log_debug(f"[STRUCTURED] {category}: {details}")

    def log_to_file(self, category, message, *, reset=False):
        """Append a plain line to a DEDICATED log file (``support/logs/{category}.log``) WITHOUT
        echoing to the main run log or the console — for high-volume drill-down output (e.g. the
        re-organizer's per-title relocation plan, which can be thousands of titles) that would
        otherwise flood the run log. ``reset=True`` truncates first (one fresh plan per run).
        Secrets are scrubbed and decorative glyphs stripped, exactly like the console path.
        Thread-safe; best-effort (never raises)."""
        try:
            path = LOG_DIR / f"{category}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with LoggerManager._file_lock:
                with open(path, "w" if reset else "a", encoding="utf-8") as f:
                    f.write(self._present(message) + "\n")
        except Exception:
            pass

    def _log_trace_to_file(self, trace_id, func_name, location, error_message):
        trace_log_path = LOG_DIR / "tvdb_enrichment_trace.log"
        trace_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(trace_log_path, "a", encoding="utf-8") as f:
            f.write(self._present(
                f"[{datetime.utcnow().isoformat()}] TRACE_ID={trace_id} | Function={func_name} | Location={location} | Error={error_message}\n"))

    def set_level(self, new_level):
        """
        Set the logging level dynamically (e.g., logging.DEBUG, logging.INFO).
        """
        self.level = new_level
        self.logger.setLevel(new_level)
        for handler in self.logger.handlers:
            handler.setLevel(new_level)

    def log_info(self, message):
        context = self.get_log_context()
        self.logger.info(self._present(f"{context} {message}"))

    def log_success(self, message):
        context = self.get_log_context()
        # SUCCESS level → rendered green on the console.
        self.logger.log(SUCCESS_LEVEL, self._present(f"{context} {message}"))

    def log_warning(self, message):
        context = self.get_log_context()
        self.logger.warning(self._present(f"{context} {message}"))
