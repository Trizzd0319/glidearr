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


# ── the listing race, the stale fallback, and naming the cause ─────────────────
# All three come from one live incident (2026-08-20 20:33): a real run DEGRADED to
# dry-run because radarr:standard "failed", with no reason given. It had not failed -
# the 218.8 MB backup was written moments after the manager looked for it.

_BIG = ServiceBackupManager.MIN_BACKUP_BYTES * 4


def _stamp(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _one(*, listings, wait_ok=True, config=None, dry_run=False):
    """A manager wired to a scripted sequence of ``_list_backups`` results."""
    m = ServiceBackupManager.__new__(ServiceBackupManager)
    m.logger, m.config, m.global_cache, m.dry_run = _Log(), (config or {}), _GC(), dry_run
    m.POLL_INTERVAL_S = 0.01                       # keep the test instant
    m.LISTING_SETTLE_S = getattr(m, "LISTING_SETTLE_S", 90.0)
    seq = list(listings)
    m._conn = lambda s, i: ("http://x", "k")
    m._list_backups = lambda b, k: (seq.pop(0) if len(seq) > 1 else seq[0])
    m._api_post = lambda b, k, p, pl: {"id": 1}
    m._wait_command = lambda b, k, c: wait_ok
    m._download = lambda b, p, k: None
    return m


def test_survives_the_listing_race_that_degraded_a_live_run():
    """The *arr marks the Backup command completed BEFORE the zip is listed, so the
    first look finds nothing. Polling must find it rather than declare failure."""
    new = [{"name": "new.zip", "path": "/b/new", "size": _BIG, "time": _stamp(0)}]
    m = _one(listings=[[], [], new])
    m.LISTING_SETTLE_S = 5.0
    res = m._backup_one("radarr", "standard")
    assert res["ok"] is True and res["name"] == "new.zip"


def test_settle_window_expiry_names_the_cause():
    """`ensure_backups` used to report only WHICH instance failed, discarding the
    `detail` `_backup_one` had already computed (P-A). The operator was told the run
    degraded and given no way to find out why."""
    m = _one(listings=[[]])
    m.LISTING_SETTLE_S = 0.02
    res = m._backup_one("radarr", "standard")
    assert res["ok"] is False
    assert "no backup file appeared within" in res["detail"]


def test_command_timeout_names_the_cause():
    m = _one(listings=[[]], wait_ok=False)
    res = m._backup_one("radarr", "standard")
    assert res["ok"] is False and "did not complete within" in res["detail"]


def test_ensure_backups_surfaces_the_detail():
    m = _one(listings=[[]])
    m._instances = lambda s: ["standard"] if s == "radarr" else []
    m._backup_one = lambda s, i: {"ok": False, "detail": "Backup command did not complete within 300s"}
    m.ensure_backups()
    warn = " ".join(str(w) for w in m.logger.warns)
    assert "did not complete within 300s" in warn
    assert "FAILED or not loadable" not in warn          # the old ambiguous either/or


def test_stale_fallback_arms_on_a_valid_recent_backup_but_warns():
    """The gate guarantees a restorable ROLLBACK POINT, not a brand-new one. A valid
    40h-old backup still is one; disarming over it costs a live run for no safety."""
    old = [{"name": "b40.zip", "path": "/b/40", "size": _BIG, "time": _stamp(40)}]
    m = _one(listings=[old], wait_ok=False)
    res = m._backup_one("radarr", "standard")
    assert res["ok"] is True and res["stale_fallback"] is True
    assert any("falling back" in str(w) for w in m.logger.warns)


def test_stale_fallback_refuses_an_invalid_backup_however_recent():
    """Accepting a 0-byte file because it is recent would defeat the gate entirely."""
    tiny = [{"name": "empty.zip", "path": "/b/e", "size": 10, "time": _stamp(1)}]
    m = _one(listings=[tiny], wait_ok=False)
    assert m._backup_one("radarr", "standard")["ok"] is False


def test_stale_fallback_refuses_beyond_the_window():
    ancient = [{"name": "old.zip", "path": "/b/o", "size": _BIG, "time": _stamp(200)}]
    m = _one(listings=[ancient], wait_ok=False)
    assert m._backup_one("radarr", "standard")["ok"] is False


def test_fallback_window_can_be_disabled():
    old = [{"name": "b40.zip", "path": "/b/40", "size": _BIG, "time": _stamp(40)}]
    m = _one(listings=[old], wait_ok=False, config={"backup_fallback_max_age_hours": 0})
    assert m._backup_one("radarr", "standard")["ok"] is False


def test_fresh_reuse_still_takes_precedence_over_the_fallback():
    """Under the 24h freshness window nothing is created and no fallback is involved."""
    fresh = [{"name": "b6.zip", "path": "/b/6", "size": _BIG, "time": _stamp(6)}]
    m = _one(listings=[fresh])
    res = m._backup_one("radarr", "standard")
    assert res["ok"] is True and res.get("reused") is True
    assert res.get("stale_fallback") is not True
