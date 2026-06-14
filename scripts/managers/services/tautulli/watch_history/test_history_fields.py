"""The cached history projection must capture the unix `date` (drives temporal
affinity decay) while still dropping PII (ip/machine/friendly_name)."""
from __future__ import annotations

from scripts.managers.services.tautulli.watch_history import (
    TautulliWatchHistoryManager,
    _CACHED_HISTORY_FIELDS,
)


def test_date_is_captured_pii_is_dropped():
    assert "date" in _CACHED_HISTORY_FIELDS
    raw = {
        "rating_key": "55", "user_id": 7, "date": 1749513600, "percent_complete": 95,
        "ip_address": "192.168.1.5", "friendly_name": "Rob", "machine_id": "abc",
    }
    proj = TautulliWatchHistoryManager._project_record(raw)
    assert proj["date"] == 1749513600
    assert proj["rating_key"] == "55" and proj["user_id"] == 7
    assert "ip_address" not in proj and "friendly_name" not in proj and "machine_id" not in proj
