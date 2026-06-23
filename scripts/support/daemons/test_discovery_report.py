"""The --status anniversary-shelf block — a pure disk read of discovery/this_week/preview."""
from __future__ import annotations

import json

import scripts.support.daemons.enrich_daemon as ed


def test_discovery_report_renders_preview(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ed, "CACHE_TRAKT", tmp_path / "trakt")
    d = tmp_path / "discovery" / "this_week"
    d.mkdir(parents=True)
    (d / "preview.json").write_text(json.dumps({
        "users": 2, "owned_movies": 3, "owned_shows": 1,
        "net_new_movies": 5, "net_new_shows": 0,
        "movies": [{"title": "The Matrix", "years_ago": 25}], "shows": []}), encoding="utf-8")
    ed._discovery_shelf_report()
    out = capsys.readouterr().out
    assert "Anniversary shelf (This Week in History)" in out
    assert "owned movies" in out and "The Matrix" in out and "25y ago" in out


def test_discovery_report_none_yet_when_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ed, "CACHE_TRAKT", tmp_path / "trakt")
    ed._discovery_shelf_report()
    assert "(none yet" in capsys.readouterr().out
