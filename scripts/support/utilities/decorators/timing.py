import functools
import json
import threading
import time
from pathlib import Path

from scripts.support.utilities.logger.logger import get_logger

# ──────────────────────────────── #
# 🧵 Thread-local profiler context
# ──────────────────────────────── #
_thread_state = threading.local()

# Minimum elapsed seconds before a completed call is logged.
# Set to 0 to log every decorated call; raise to reduce noise.
TIMEIT_LOG_THRESHOLD_S: float = 1.0

# Whether to capture CPU time alongside wall-clock time.
# Uses time.process_time() which measures CPU seconds consumed by this process.
# Set False to reduce overhead on hot inner loops.
TIMEIT_CAPTURE_CPU: bool = True


def _ensure_thread_state():
    """Ensure thread-local stack and completed list exist."""
    if not hasattr(_thread_state, "call_stack"):
        _thread_state.call_stack = []
    if not hasattr(_thread_state, "completed_profiles"):
        _thread_state.completed_profiles = []


# ──────────────────────────────── #
# 📦 Profile Node Structure
# ──────────────────────────────── #
class ProfileNode:
    def __init__(self, name: str):
        self.name      = name
        self.wall_start = time.perf_counter()
        self.cpu_start  = time.process_time() if TIMEIT_CAPTURE_CPU else 0.0
        self.wall_end: float | None = None
        self.cpu_end:  float | None = None
        self.children: list["ProfileNode"] = []

    def finish(self):
        self.wall_end = time.perf_counter()
        if TIMEIT_CAPTURE_CPU:
            self.cpu_end = time.process_time()

    @property
    def duration(self) -> float:
        """Wall-clock elapsed seconds."""
        return (self.wall_end or time.perf_counter()) - self.wall_start

    @property
    def cpu_seconds(self) -> float:
        """CPU time consumed (user + system) in seconds."""
        if not TIMEIT_CAPTURE_CPU or self.cpu_end is None:
            return 0.0
        return self.cpu_end - self.cpu_start

    @property
    def cpu_ratio(self) -> float:
        """CPU seconds / wall seconds.  > 1.0 means multi-core parallelism;
        < 0.1 means mostly I/O-bound (waiting on network/disk)."""
        wall = self.duration
        return self.cpu_seconds / wall if wall > 0 else 0.0

    def to_dict(self) -> dict:
        d = {
            "function":    self.name,
            "wall_s":      round(self.duration, 4),
            "cpu_s":       round(self.cpu_seconds, 4),
            "cpu_ratio":   round(self.cpu_ratio, 3),
            "children":    [c.to_dict() for c in self.children],
        }
        return d


# ──────────────────────────────── #
# ⏱ Decorator: timeit
# ──────────────────────────────── #
def timeit(label=None):
    """
    Decorator that measures wall-clock time AND CPU time for a function.

    Wall time:  how long the call took end-to-end (includes I/O waits).
    CPU time:   how many CPU seconds were actually consumed.
    CPU ratio:  cpu / wall — low ratio (<0.1) means I/O-bound; high (>0.9)
                means compute-bound.

    Logs when elapsed >= TIMEIT_LOG_THRESHOLD_S or the call is top-level.
    Format:  ⏱️ [name] → 1.23s  (wall=1.23s cpu=0.04s ratio=0.03 — I/O bound)

    Data is also stored in the call-graph profiler for dump_profile().

    Args:
        label: Optional display name override.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _ensure_thread_state()
            name  = label or func.__name__
            node  = ProfileNode(name)
            depth = len(_thread_state.call_stack)

            if _thread_state.call_stack:
                _thread_state.call_stack[-1].children.append(node)
            _thread_state.call_stack.append(node)

            try:
                return func(*args, **kwargs)
            finally:
                node.finish()
                _thread_state.call_stack.pop()
                if not _thread_state.call_stack:
                    _thread_state.completed_profiles.append(node)

                # ── Live timing log ──────────────────────────────────────────
                wall    = node.duration
                cpu     = node.cpu_seconds
                ratio   = node.cpu_ratio
                is_top  = not _thread_state.call_stack

                if wall >= TIMEIT_LOG_THRESHOLD_S or is_top:
                    try:
                        logger = get_logger()
                        indent = "  " * depth

                        # Format wall time
                        if wall >= 60:
                            wall_str = f"{wall / 60:.1f}m"
                        elif wall >= 1:
                            wall_str = f"{wall:.2f}s"
                        else:
                            wall_str = f"{wall * 1000:.0f}ms"

                        # CPU annotation — only meaningful if wall ≥ 0.1s
                        if TIMEIT_CAPTURE_CPU and wall >= 0.1:
                            if cpu >= 1:
                                cpu_str = f"{cpu:.2f}s"
                            else:
                                cpu_str = f"{cpu * 1000:.0f}ms"

                            if ratio < 0.05:
                                bound = "I/O-bound"
                            elif ratio < 0.5:
                                bound = "mixed"
                            else:
                                bound = "CPU-bound"

                            annotation = f"  │ cpu={cpu_str}  ratio={ratio:.2f}  {bound}"
                        else:
                            annotation = ""

                        logger.log_debug(
                            f"⏱️ {indent}[{name}] → {wall_str}{annotation}"
                        )
                    except Exception:
                        pass  # never let the timer crash the actual call

        return wrapper
    return decorator


# ──────────────────────────────── #
# 📤 Profiler Output Dump
# ──────────────────────────────── #
def dump_profile(output_path="support/logs/tmp_profile.json", suppress_console=False):
    """
    Writes collected profiling data to a JSON file.

    Each node now includes wall_s, cpu_s, and cpu_ratio so post-run
    analysis can identify both slow and CPU-intensive methods.
    """
    logger = get_logger()
    _ensure_thread_state()
    completed = _thread_state.completed_profiles

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if not completed:
        placeholder = {
            "calls": [],
            "summary": "No profiled calls recorded (placeholder generated)."
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(placeholder, f, indent=2)
        if not suppress_console:
            logger.log_warning(f"⚠️ No profiled calls detected. Wrote placeholder to: {output_path}")
        return output_path

    profile_data = [node.to_dict() for node in completed]

    # ── Summary: top 10 slowest by wall time (flattened) ────────────────────
    def flatten(nodes, depth=0):
        result = []
        for n in nodes:
            result.append((n["function"], n["wall_s"], n["cpu_s"], n["cpu_ratio"], depth))
            result.extend(flatten(n["children"], depth + 1))
        return result

    all_calls = flatten(profile_data)
    top_wall = sorted(all_calls, key=lambda x: x[1], reverse=True)[:10]
    top_cpu  = sorted(all_calls, key=lambda x: x[2], reverse=True)[:10]

    summary = {
        "top_10_by_wall_s": [
            {"fn": f, "wall_s": w, "cpu_s": c, "cpu_ratio": r}
            for f, w, c, r, _ in top_wall
        ],
        "top_10_by_cpu_s": [
            {"fn": f, "wall_s": w, "cpu_s": c, "cpu_ratio": r}
            for f, w, c, r, _ in top_cpu
        ],
    }

    output = {"calls": profile_data, "summary": summary}

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    if not suppress_console:
        logger.log_info(f"⏱ Call graph profiler data written to: {output_path}")

    _thread_state.completed_profiles.clear()
    return output_path
