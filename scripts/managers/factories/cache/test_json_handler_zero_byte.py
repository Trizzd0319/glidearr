"""CacheJsonManager.load_json: a 0-byte cache file (killed write / partial restore /
file-sync dehydration) is a QUIET miss, not a flood of decode warnings."""
from __future__ import annotations

from scripts.managers.factories.cache.json_handler import CacheJsonManager


class _Log:
    def __init__(self): self.warnings, self.debugs = [], []
    def log_debug(self, m): self.debugs.append(m)
    def log_warning(self, m): self.warnings.append(m)
    def log_info(self, m): pass


def _mgr(tmp_path):
    return CacheJsonManager(logger=_Log(), base_dir=str(tmp_path))


def test_zero_byte_is_quiet_miss(tmp_path):
    m = _mgr(tmp_path)
    p = tmp_path / "e.json"; p.write_bytes(b"")
    assert m.load_json(p) == {}
    assert not m.logger.warnings                      # no scary warning for 0-byte
    assert any("0-byte" in d for d in m.logger.debugs)


def test_valid_json_loads(tmp_path):
    m = _mgr(tmp_path)
    p = tmp_path / "v.json"; p.write_text('{"a": 1}', encoding="utf-8")
    assert m.load_json(p) == {"a": 1}


def test_malformed_nonzero_still_warns(tmp_path):
    m = _mgr(tmp_path)
    p = tmp_path / "b.json"; p.write_text("not json", encoding="utf-8")
    assert m.load_json(p) == {}
    assert m.logger.warnings                          # genuine corruption is still loud


def test_missing_is_quiet(tmp_path):
    m = _mgr(tmp_path)
    assert m.load_json(tmp_path / "nope.json") == {}
    assert not m.logger.warnings
