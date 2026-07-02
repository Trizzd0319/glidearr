"""GlobalCacheManager.set accepts a ``compressed`` kwarg for parity with its siblings
set_json / set_with_pretty_output. Regression guard: the Radarr sibling cache refreshers
(refresh_tag_cache/refresh_history/refresh_instance_*/refresh_monitoring_rules/
cache_orchestration_summary) call ``set(key, data, compressed=True)``; before the fix the shim's
signature was ``set(key, data, pretty=True)`` and that raised TypeError. ``compressed`` is a no-op at
the JSON storage layer (save_json writes plain .json), so it only needs to be accepted + forwarded."""
from __future__ import annotations

from scripts.managers.factories.cache import GlobalCacheManager


class _Rec:
    """Records which sibling writer the shim forwards to, and with what compressed flag."""
    def __init__(self):
        self.calls = []
    def set_with_pretty_output(self, key, data, compressed=False):
        self.calls.append(("pretty", key, data, compressed)); return True
    def set_json(self, key, data, compressed=False, pretty=True):
        self.calls.append(("json", key, data, compressed, pretty)); return True


def test_set_accepts_compressed_true_no_typeerror():
    # the exact call the Radarr tag/history/instance/monitoring refreshers make
    rec = _Rec()
    assert GlobalCacheManager.set(rec, "radarr.tags.standard", [1, 2], compressed=True) is True
    assert rec.calls == [("pretty", "radarr.tags.standard", [1, 2], True)]


def test_set_pretty_false_forwards_to_set_json_with_compressed():
    rec = _Rec()
    assert GlobalCacheManager.set(rec, "k", {"a": 1}, pretty=False, compressed=True) is True
    assert rec.calls == [("json", "k", {"a": 1}, True, False)]


def test_set_defaults_compressed_false():
    rec = _Rec()
    GlobalCacheManager.set(rec, "k", {"a": 1})
    assert rec.calls == [("pretty", "k", {"a": 1}, False)]
