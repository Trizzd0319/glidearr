"""Regression test for item B: the 'Radarr universe quality actions' grid must render
the QUALITY NAME (edition) in its From->To column, not the resolution.

Before the fix, From->To rendered ``_profile_max_resolution`` (an int) on both sides.
Radarr reports resolution 2160 for *every* 4K tier, so a genuine ladder upgrade between
two 2160p profiles (e.g. WEBDL-2160p -> Remux-2160p) printed the useless '2160p->2160p'
and was indistinguishable from a 'hold'.

Fix:
- FROM = the file's current edition (the ``quality_name`` parquet column), falling back to
  the resolution string when no file/edition is known.
- TO   = the target profile's CUTOFF quality name (the edition Radarr aims for).

Covers the two new pure helpers directly AND drives the real dry-run apply path,
capturing the actual grid rows via a fake logger.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.universe import RadarrQualityUniverseManager as U


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_cutoff_quality_name_leaf():
    """cutoff referencing a leaf quality id -> that quality's name."""
    p = {"id": 11, "name": "ultra", "cutoff": 19, "items": [
        {"allowed": True,  "quality": {"id": 18, "name": "WEBDL-2160p",  "resolution": 2160}},
        {"allowed": True,  "quality": {"id": 19, "name": "Bluray-2160p", "resolution": 2160}},
        {"allowed": False, "quality": {"id": 17, "name": "HDTV-2160p",   "resolution": 2160}},
    ]}
    assert U._profile_cutoff_quality_name(p) == "Bluray-2160p"


def test_cutoff_quality_name_group():
    """cutoff referencing a quality GROUP id -> the group's name."""
    p = {"id": 5, "name": "web-4k", "cutoff": 1000, "items": [
        {"id": 1000, "name": "WEB 2160p", "allowed": True, "items": [
            {"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p",  "resolution": 2160}},
            {"allowed": True, "quality": {"id": 14, "name": "WEBRip-2160p", "resolution": 2160}},
        ]},
    ]}
    assert U._profile_cutoff_quality_name(p) == "WEB 2160p"


def test_cutoff_quality_name_fallback_to_best_allowed():
    """Unmatchable cutoff id -> highest-resolution ALLOWED edition (not a disallowed one)."""
    p = {"id": 3, "name": "x", "cutoff": 999, "items": [
        {"allowed": True,  "quality": {"id": 7,  "name": "Bluray-1080p", "resolution": 1080}},
        {"allowed": False, "quality": {"id": 19, "name": "Bluray-2160p", "resolution": 2160}},
    ]}
    assert U._profile_cutoff_quality_name(p) == "Bluray-1080p"


def test_cutoff_quality_name_empty():
    assert U._profile_cutoff_quality_name({}) is None


def test_quality_label():
    assert U._quality_label("WEBDL-2160p", 2160) == "WEBDL-2160p"
    assert U._quality_label(None, 2160) == "2160p"
    assert U._quality_label(float("nan"), 2160) == "2160p"   # missing parquet cell
    assert U._quality_label("   ", 1080) == "1080p"          # whitespace-only -> fallback
    assert U._quality_label(None, 0) == "-"                  # nothing known


# ── Real dry-run render ───────────────────────────────────────────────────────

class _FakeMfm:
    def __init__(self, df): self._df = df
    def load(self, instance): return self._df.copy()
    def save(self, instance, df): pass


class _CapLogger:
    """Captures the From->To grid; ignores all other log calls."""
    def __init__(self, sink): self.sink = sink
    def log_grid(self, headers, rows, **k):
        self.sink["headers"] = headers
        self.sink["rows"] = rows
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _GC:
    def __init__(self, run_summary): self.run_summary = run_summary


def _mk_apply_mgr(df, ranked, target, sink, *, hold_qp, run_summary=None):
    m = object.__new__(U)                       # skip __init__/registry/base
    m.config = {}
    m.dry_run = True
    m.radarr_api = object()                      # non-None sentinel; dry_run never calls it
    m.logger = _CapLogger(sink)
    m.global_cache = _GC(run_summary) if run_summary is not None else None
    m._get_movie_files_manager = lambda: _FakeMfm(df)          # type: ignore[attr-defined]
    m._resolve_instance = lambda i: i                          # type: ignore[attr-defined]
    m._fetch_ranked_profiles = lambda inst: ranked             # type: ignore[attr-defined]
    # Return None (=> hold) for the hold profile id, else the upgrade target.
    m._get_target_profile = (                                  # type: ignore[attr-defined]
        lambda ranked_profiles, direction, current_profile_id, **kw:
        None if current_profile_id == hold_qp else target
    )
    m._stamp_universe_plan = lambda *a, **k: None              # type: ignore[attr-defined]
    return m


def test_from_to_renders_quality_name_end_to_end():
    df = pd.DataFrame([
        dict(keep_policy="universe", title="2 Fast 2 Furious", quality_action="upgrade",
             quality_profile_id=5,  quality_name="WEBDL-2160p", quality_profile_name="WEB-4K",
             movie_id=101, universe_name="ff"),
        dict(keep_policy="universe", title="Fast Five",        quality_action="upgrade",
             quality_profile_id=11, quality_name="WEBDL-2160p", quality_profile_name="WEB-4K",
             movie_id=102, universe_name="ff"),
        dict(keep_policy="universe", title="No File Movie",    quality_action="upgrade",
             quality_profile_id=5,  quality_name=None,         quality_profile_name="WEB-4K",
             movie_id=103, universe_name="ff"),
    ])
    ranked = [
        {"id": 5,  "items": [{"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}}]},
        {"id": 11, "items": [{"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}}]},
    ]
    target = {"id": 99, "name": "ultra", "cutoff": 19, "items": [
        {"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}},
        {"allowed": True, "quality": {"id": 19, "name": "Remux-2160p", "resolution": 2160}},
    ]}

    sink: dict = {}
    mgr = _mk_apply_mgr(df, ranked, target, sink, hold_qp=11)
    mgr.apply_quality_actions("standard")

    assert sink["headers"] == ["Title", "Action", "From->To", "Profile"]
    fromto  = {r[0]: r[2] for r in sink["rows"]}
    profile = {r[0]: r[3] for r in sink["rows"]}

    # A genuine same-resolution ladder upgrade now shows the edition delta, not 2160p->2160p.
    assert fromto["2 Fast 2 Furious"] == "WEBDL-2160p->Remux-2160p", fromto
    # No edition on the file (null quality_name) -> graceful resolution fallback on the FROM side.
    assert fromto["No File Movie"] == "2160p->Remux-2160p", fromto
    # Hold (no target) -> current edition shown ONCE (no X->X repeat).
    assert fromto["Fast Five"] == "WEBDL-2160p", fromto
    # New Profile column: from->to on a real change, single value on a hold.
    assert profile["2 Fast 2 Furious"] == "WEB-4K->ultra", profile
    assert profile["Fast Five"] == "WEB-4K", profile


def test_universe_actions_route_into_run_summary():
    """D.2: with a run_summary collector present, the per-title detail goes to the
    consolidated end-of-run report (one radarr block, Instance column) and NOT the live grid."""
    from scripts.support.utilities.logger.run_summary import RunSummaryManager

    df = pd.DataFrame([
        dict(keep_policy="universe", title="2 Fast 2 Furious", quality_action="upgrade",
             quality_profile_id=5,  quality_name="WEBDL-2160p", movie_id=101, universe_name="ff"),
        dict(keep_policy="universe", title="Fast Five",        quality_action="upgrade",
             quality_profile_id=11, quality_name="WEBDL-2160p", movie_id=102, universe_name="ff"),
    ])
    ranked = [
        {"id": 5,  "items": [{"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}}]},
        {"id": 11, "items": [{"allowed": True, "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160}}]},
    ]
    target = {"id": 99, "name": "ultra", "cutoff": 19, "items": [
        {"allowed": True, "quality": {"id": 19, "name": "Remux-2160p", "resolution": 2160}},
    ]}

    rs = RunSummaryManager()
    sink: dict = {}
    mgr = _mk_apply_mgr(df, ranked, target, sink, hold_qp=11, run_summary=rs)
    mgr.apply_quality_actions("ultra")

    assert "headers" not in sink                  # inline grid NOT used when collector present

    class _RenderCap:
        def __init__(self): self.grids = []
        def log_grid(self, headers, rows, title="", cap=None):
            self.grids.append((title, list(headers), [list(r) for r in rows]))
        def log_info(self, *a, **k): pass

    rcap = _RenderCap()
    rs.render(rcap)
    assert len(rcap.grids) == 1
    title, headers, rows = rcap.grids[0]
    assert title == "Universe quality actions"
    assert headers == ["Instance", "Title", "Action", "From->To", "Profile"]
    assert all(r[0] == "ultra" for r in rows)     # instance column
    fromto = {r[1]: r[3] for r in rows}
    assert fromto["2 Fast 2 Furious"] == "WEBDL-2160p->Remux-2160p"
    assert fromto["Fast Five"] == "WEBDL-2160p"   # hold -> current edition shown once
