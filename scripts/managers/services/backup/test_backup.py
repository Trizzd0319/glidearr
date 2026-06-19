"""Tests for the pre-destructive service backup manager + the write gate it arms."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

from scripts.managers.services.backup import GATE_KEY, ServiceBackupManager
from scripts.support.utilities.backup_gate import effective_dry_run, writes_armed


def _zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, b"x" * 32)
    return buf.getvalue()


class _Log:
    def __init__(self): self.infos = []; self.warns = []; self.oks = []
    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): self.warns.append(m)
    def log_success(self, m): self.oks.append(m)
    def log_error(self, m): pass
    def log_debug(self, *a, **k): pass


class _GC:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


# ── pure loadability validation ───────────────────────────────────────────────
def test_validate_backup_zip_accepts_db_plus_config():
    assert ServiceBackupManager.validate_backup_zip(_zip(["radarr.db", "config.xml"])) is True
    assert ServiceBackupManager.validate_backup_zip(_zip(["sonarr.db", "config.xml"])) is True


def test_validate_backup_zip_rejects_missing_pieces_and_non_zip():
    assert ServiceBackupManager.validate_backup_zip(_zip(["radarr.db"])) is False        # no config.xml
    assert ServiceBackupManager.validate_backup_zip(_zip(["config.xml"])) is False       # no db
    assert ServiceBackupManager.validate_backup_zip(b"<html>login</html>") is False      # login page
    assert ServiceBackupManager.validate_backup_zip(b"") is False
    assert ServiceBackupManager.validate_backup_zip(None) is False


def test_looks_like_zip():
    assert ServiceBackupManager._looks_like_zip(b"PK\x03\x04....") is True
    assert ServiceBackupManager._looks_like_zip(b"<!DOCTYPE html>") is False


def test_pick_newest_is_chronological_and_excludes():
    backups = [
        {"path": "/b/a.zip", "time": "2026-06-19T01:00:00Z"},
        {"path": "/b/b.zip", "time": "2026-06-19T03:00:00Z"},
        {"path": "/b/c.zip", "time": "2026-06-19T02:00:00Z"},
    ]
    assert ServiceBackupManager._pick_newest(backups)["path"] == "/b/b.zip"
    # excluding the newest falls back to the next newest
    got = ServiceBackupManager._pick_newest(backups, exclude_paths={"/b/b.zip"})
    assert got["path"] == "/b/c.zip"


# ── gate arming via ensure_backups ────────────────────────────────────────────
def _mgr(config, dry_run, per_instance):
    m = ServiceBackupManager(_Log(), config, _GC(), dry_run=dry_run)
    m._instances = lambda service: list(per_instance.get(service, {}))
    m._backup_one = lambda service, inst: per_instance[service][inst]
    return m


def test_dry_run_arms_gate_without_backing_up():
    m = _mgr({}, dry_run=True, per_instance={})
    m.ensure_backups()
    assert m.global_cache.get(GATE_KEY)["armed"] is True
    assert m.global_cache.get(GATE_KEY)["reason"] == "dry_run"


def test_disabled_arms_gate():
    m = _mgr({"backup_before_destructive": False}, dry_run=False, per_instance={})
    m.ensure_backups()
    assert m.global_cache.get(GATE_KEY)["armed"] is True
    assert m.global_cache.get(GATE_KEY)["reason"] == "disabled"


def test_all_backups_ok_arms_gate():
    per = {"radarr": {"standard": {"ok": True, "size_mb": 192.0}},
           "sonarr": {"standard": {"ok": True, "size_mb": 109.0}}}
    m = _mgr({}, dry_run=False, per_instance=per)
    m.ensure_backups()
    assert m.global_cache.get(GATE_KEY)["armed"] is True
    assert m.logger.oks                                   # success logged


def test_any_backup_failure_disarms_gate_and_warns():
    per = {"radarr": {"standard": {"ok": True}},
           "sonarr": {"standard": {"ok": False, "detail": "no backup file appeared"}}}
    m = _mgr({}, dry_run=False, per_instance=per)
    m.ensure_backups()
    assert m.global_cache.get(GATE_KEY)["armed"] is False
    assert any("DEGRADING" in w for w in m.logger.warns)


# ── freshness window: don't dump a new backup every run ───────────────────────
def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _backup(hours_ago: float, *, size: int = 200_000_000, name: str = "b.zip") -> dict:
    return {"name": name, "path": f"/backup/manual/{name}", "size": size, "time": _iso(hours_ago)}


def test_fresh_enough_window():
    m = ServiceBackupManager(_Log(), {"backup_max_age_hours": 24}, _GC(), dry_run=False)
    assert m._fresh_enough(_backup(1)) is True              # 1h old → reuse
    assert m._fresh_enough(_backup(48)) is False            # 48h old → too stale
    assert m._fresh_enough(_backup(1, size=1000)) is False  # present but trivially small → don't trust
    m0 = ServiceBackupManager(_Log(), {"backup_max_age_hours": 0}, _GC(), dry_run=False)
    assert m0._fresh_enough(_backup(1)) is False            # 0 disables reuse (fresh every run)


def test_reuses_recent_backup_without_creating_a_new_one():
    m = ServiceBackupManager(_Log(), {"backup_max_age_hours": 24}, _GC(), dry_run=False)
    posts: list = []
    m._conn = lambda s, i: ("http://x", "k")
    m._list_backups = lambda base, key: [_backup(2, name="recent.zip")]
    m._api_post = lambda *a, **k: posts.append(a) or {"id": 1}
    res = m._backup_one("radarr", "standard")
    assert res["ok"] is True and res["reused"] is True
    assert res["name"] == "recent.zip"
    assert posts == []                                      # NO new Backup command was triggered


def test_creates_new_when_newest_backup_is_stale():
    m = ServiceBackupManager(_Log(), {"backup_max_age_hours": 24}, _GC(), dry_run=False)
    state = {"n": 0}

    def _list(base, key):
        state["n"] += 1
        old = _backup(72, name="old.zip")
        return [old] if state["n"] == 1 else [old, _backup(0.0, name="new.zip")]

    posts: list = []
    m._conn = lambda s, i: ("http://x", "k")
    m._list_backups = _list
    m._api_post = lambda *a, **k: posts.append(a) or {"id": 1}
    m._wait_command = lambda base, key, cid: True
    res = m._backup_one("radarr", "standard")
    assert res["ok"] is True and res["reused"] is False
    assert res["name"] == "new.zip"
    assert len(posts) == 1                                   # exactly one Backup command


# ── the write gate destructive primitives read ────────────────────────────────
def test_effective_dry_run_semantics():
    gc = _GC()
    # unset gate → armed by default → only the bare dry_run matters
    assert effective_dry_run(True, gc) is True
    assert effective_dry_run(False, gc) is False
    # explicit disarm (real run, backup failed) → real run degrades to dry-run
    gc.set(GATE_KEY, {"armed": False})
    assert effective_dry_run(False, gc) is True
    assert writes_armed(gc) is False
    # armed again → real writes allowed
    gc.set(GATE_KEY, {"armed": True})
    assert effective_dry_run(False, gc) is False
