"""
sizing/file_comparison.py — upgrade / keep / downgrade from file size.
================================================================================
Pure decision (ML-migration Step 1d): given a file's actual size vs the expected
size for its quality, decide whether it's under-sized (upgrade), over-sized
(downgrade), or fine (keep). No HTTP, no service imports — the service fetches
the movie/expected size and calls this.

Pulled from ``radarr/quality/file_size.compare_file_size`` (the 0.6 / 1.4
threshold decision); ``sonarr/quality/filesizes.compare_file_sizes`` will delegate
here too (Step 1d follow-on). The service keeps the Radarr/Sonarr fetch + the
expected-size estimate (which itself comes from ``sizing.size_model``).
"""
from __future__ import annotations

# Default acceptance band as a fraction of the expected size. Below LOW the file
# is suspiciously small (probably a worse source → upgrade); above HIGH it is
# bloated (downgrade); in between, keep.
DEFAULT_LOW = 0.6
DEFAULT_HIGH = 1.4


def classify_file_size(
    actual_bytes: "float | int | None",
    expected_bytes: "float | int | None",
    *,
    low: float = DEFAULT_LOW,
    high: float = DEFAULT_HIGH,
) -> str:
    """Return 'upgrade' | 'downgrade' | 'keep'.

    A zero/missing/under-LOW actual size → 'upgrade'; over-HIGH → 'downgrade';
    otherwise 'keep'. A non-positive ``expected`` (unknown) → 'keep' (no opinion).
    """
    try:
        actual = float(actual_bytes or 0)
        expected = float(expected_bytes or 0)
    except (TypeError, ValueError):
        return "keep"
    if expected <= 0:
        return "keep"
    if actual == 0 or actual < expected * low:
        return "upgrade"
    if actual > expected * high:
        return "downgrade"
    return "keep"
