"""Unit tests for LoggerManager secret-scrubbing — the short-credential register
path and the broadened ``pin=`` pattern (PR-6 logger pin / short-token hardening).

The scrubber's ``_scrub_values`` set is class-level (the logger is a singleton), so
each test snapshots and restores it to stay isolated from registrations elsewhere."""
from __future__ import annotations

import pytest

from scripts.support.utilities.logger import logger as logger_mod
from scripts.support.utilities.logger.logger import (
    LoggerManager, _rotate_run_logs, rotate_run_artifacts,
)


# ──────────────── per-run log rotation (Kometa-style) ──────────────── #
def test_run_log_rotation_keeps_backups_and_trashes_rest(tmp_path):
    (tmp_path / "default.log").write_text("CUR")
    for n in range(1, 6):
        (tmp_path / f"default-{n}.log").write_text(f"P{n}")
    (tmp_path / "default-7.log").write_text("STRAGGLER")          # leftover from a larger setting

    _rotate_run_logs(tmp_path / "default.log", backups=5)

    assert not (tmp_path / "default.log").exists()               # the handler recreates it fresh
    assert (tmp_path / "default-1.log").read_text() == "CUR"     # last run rolled to -1
    assert (tmp_path / "default-2.log").read_text() == "P1"      # each previous shifted up one
    assert (tmp_path / "default-5.log").read_text() == "P4"      # oldest kept = old -4
    assert not (tmp_path / "default-6.log").exists()             # old -5 aged out (trashed)
    assert not (tmp_path / "default-7.log").exists()             # straggler trashed
    assert sorted(p.name for p in tmp_path.glob("default-*.log")) == [
        f"default-{n}.log" for n in range(1, 6)]                 # exactly 5 backups remain


def test_run_log_rotation_first_run_no_existing_files(tmp_path):
    # Nothing to roll yet — must not raise and must create nothing.
    _rotate_run_logs(tmp_path / "default.log", backups=5)
    assert list(tmp_path.glob("*.log")) == []


# ── unified run-artifact rotation (logger + routing + timings on the same -N) ── #
def test_rotate_run_artifacts_rolls_all_three_with_same_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_mod, "LOG_DIR", tmp_path)
    for name in ("default.log", "routing.log", "timings.json"):
        (tmp_path / name).write_text(f"CUR-{name}")
    # a prior run already at slot -1 for each family …
    (tmp_path / "default-1.log").write_text("D1")
    (tmp_path / "routing-1.log").write_text("R1")
    (tmp_path / "timings-1.json").write_text("T1")
    # … plus a leftover from the OLD unbounded profiler naming, which must be swept.
    (tmp_path / "timings.run-007.json").write_text("LEGACY")

    rotate_run_artifacts(backups=5)

    # this run's file rolled to -1 for ALL three, sharing the same suffix
    assert (tmp_path / "default-1.log").read_text() == "CUR-default.log"
    assert (tmp_path / "routing-1.log").read_text() == "CUR-routing.log"
    assert (tmp_path / "timings-1.json").read_text() == "CUR-timings.json"
    # the prior -1 shifted up to -2 for all three
    assert (tmp_path / "default-2.log").read_text() == "D1"
    assert (tmp_path / "routing-2.log").read_text() == "R1"
    assert (tmp_path / "timings-2.json").read_text() == "T1"
    # current slots cleared so the fresh run opens new files
    assert not (tmp_path / "default.log").exists()
    assert not (tmp_path / "routing.log").exists()
    assert not (tmp_path / "timings.json").exists()
    # legacy unbounded profiler file is gone
    assert not (tmp_path / "timings.run-007.json").exists()


# ── daemon must never touch / rotate the orchestrator's default.log ── #
def test_default_logger_redirected_off_default_in_daemon(monkeypatch):
    monkeypatch.setenv("GLIDEARR_DAEMON", "1")
    assert LoggerManager._effective_log_name("default") == "enrich_daemon_run"
    assert LoggerManager._effective_log_name("routing") == "routing"   # non-default untouched


def test_default_logger_not_redirected_outside_daemon(monkeypatch):
    monkeypatch.delenv("GLIDEARR_DAEMON", raising=False)
    assert LoggerManager._effective_log_name("default") == "default"


# ── profiler joins the rotation window as timings.json (no unbounded run-NNN) ── #
def test_log_profiled_run_writes_timings_json_no_run_number(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_mod, "LOG_DIR", tmp_path)
    prof = tmp_path / "tmp_profile.json"
    prof.write_text('{"calls": [1, 2, 3]}')

    LoggerManager().log_profiled_run(profile_path=str(prof))

    assert (tmp_path / "timings.json").read_text() == '{"calls": [1, 2, 3]}'
    assert not prof.exists()                                   # moved, not copied
    assert list(tmp_path.glob("timings.run-*.json")) == []     # old unbounded naming gone


@pytest.fixture
def clean_scrub():
    """Isolate ``_scrub_values`` so registrations don't leak across tests."""
    saved = set(LoggerManager._scrub_values)
    LoggerManager._scrub_values.clear()
    try:
        yield LoggerManager()
    finally:
        LoggerManager._scrub_values.clear()
        LoggerManager._scrub_values.update(saved)


# ──────────────── short-credential register path ──────────────── #

def test_short_pin_scrubbed_in_pin_equals_form(clean_scrub):
    LoggerManager.register_short_secrets(["1234"])
    s = clean_scrub._scrub("POST /home/users/abc/switch?pin=1234 ok")
    assert "1234" not in s


def test_short_pin_scrubbed_in_dict_repr_form(clean_scrub):
    LoggerManager.register_short_secrets(["1234"])
    s = clean_scrub._scrub("payload was {'username': 'kid', 'pin': '1234'}")
    assert "1234" not in s and "<redacted>" in s


def test_short_pin_scrubbed_as_bare_value_in_sentence(clean_scrub):
    LoggerManager.register_short_secrets(["1234"])
    s = clean_scrub._scrub("the resolved pin is 1234 for this profile")
    assert "1234" not in s and "<redacted>" in s


def test_register_secret_allow_short_equivalent(clean_scrub):
    """``register_secret(value, allow_short=True)`` is the single-value spelling."""
    LoggerManager.register_secret("1234", allow_short=True)
    assert "1234" in LoggerManager._scrub_values
    s = clean_scrub._scrub("bare 1234 leaks")
    assert "1234" not in s


def test_non_numeric_short_pin_scrubbed_when_registered(clean_scrub):
    LoggerManager.register_short_secrets(["ab9z"])
    s = clean_scrub._scrub("switch?pin=ab9z&continue=1")
    assert "ab9z" not in s


# ──────────────── general register path keeps the >=8 floor ──────────────── #

def test_register_secrets_still_ignores_under_8_char_values(clean_scrub):
    LoggerManager.register_secrets(["1234"])
    assert "1234" not in LoggerManager._scrub_values
    # an unregistered bare short value is left untouched (no over-scrubbing of noise)
    s = clean_scrub._scrub("order count was 1234 items")
    assert "1234" in s


def test_register_secret_default_keeps_floor(clean_scrub):
    LoggerManager.register_secret("1234")          # allow_short defaults False
    assert "1234" not in LoggerManager._scrub_values


def test_register_secrets_still_accepts_long_values(clean_scrub):
    LoggerManager.register_secrets(["minted-token-9f8e7d6c"])
    s = clean_scrub._scrub("authToken was minted-token-9f8e7d6c for the switch")
    assert "minted-token-9f8e7d6c" not in s and "<redacted>" in s


# ──────────────── broadened pin= pattern (no registration needed) ──────────────── #

def test_pin_pattern_numeric_still_redacted(clean_scrub):
    s = clean_scrub._scrub("POST switch?pin=4821 ok")
    assert "4821" not in s and "pin=<redacted>" in s


def test_pin_pattern_non_numeric_redacted(clean_scrub):
    s = clean_scrub._scrub("POST switch?pin=4a8z1 ok")
    assert "4a8z1" not in s and "pin=<redacted>" in s


def test_pin_pattern_ampersand_terminated_redacted(clean_scrub):
    s = clean_scrub._scrub("switch?pin=4821&X-Plex-Product=glidearr")
    assert "4821" not in s and "pin=<redacted>" in s
    # the trailing param survives — the redaction stops at the '&'
    assert "X-Plex-Product=glidearr" in s
