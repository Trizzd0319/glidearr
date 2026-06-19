"""The 6 grouped onboarding phases over the 15 leaf steps: coverage, --service resolution,
and the per-child isolation / ctx-forwarding contract of the phase wrapper."""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps import (
    LEAF_CLASSES, STEP_CLASSES, _Phase, build_steps, phase_names, step_names,
)
from scripts.managers.factories.onboarding.steps.base import Step, StepResult


def test_six_phases_cover_every_leaf_exactly_once():
    assert len(STEP_CLASSES) == 6
    members = [m for ph in STEP_CLASSES for m in ph.members]
    assert set(members) == set(LEAF_CLASSES)          # every leaf reachable
    assert len(members) == len(LEAF_CLASSES) == 15    # and exactly once (no leaf in two phases)


def test_step_names_are_leaf_names_phase_names_are_six():
    assert len(phase_names()) == 6
    names = step_names()
    assert len(names) == 15
    for n in ("sonarr", "radarr", "library", "routing", "trakt", "tautulli", "plex",
              "tvdb", "mal", "mdblist", "daemons", "next_episode", "english_dub",
              "deletions", "notifications"):
        assert n in names


def test_full_run_is_six_phases_only_service_resolves_one_leaf():
    assert len(build_steps()) == 6
    for leaf in step_names():
        sel = build_steps(only_service=leaf)
        assert len(sel) == 1 and sel[0].name == leaf


# ── phase wrapper: a failing child doesn't abort the phase; ctx is shared ──────
class _OK(Step):
    name = "ok"
    def run(self, prompter, cfg, ctx):
        ctx.setdefault("ran", []).append("ok")
        return [StepResult("ok", ok=True)]


class _Boom(Step):
    name = "boom"
    def run(self, prompter, cfg, ctx):
        raise RuntimeError("kaboom")


class _OK2(Step):
    name = "ok2"
    def run(self, prompter, cfg, ctx):
        ctx.setdefault("ran", []).append("ok2")
        return [StepResult("ok2", ok=True)]


class _P(_Phase):
    name = "p"
    title = "P"
    members = [_OK, _Boom, _OK2]


class _Prompter:
    is_interactive = False
    def section(self, *a, **k): pass


def test_phase_isolates_child_failures_and_shares_ctx():
    ctx: dict = {}
    res = _P(logger=None).run(_Prompter(), {}, ctx)
    assert ctx["ran"] == ["ok", "ok2"]                # the boom did not abort the phase
    assert [r.service for r in res] == ["ok", "boom", "ok2"]
    boom = next(r for r in res if r.service == "boom")
    assert boom.ok is False and "error" in boom.detail


def test_phase_propagates_user_abort():
    class _Abort(Step):
        name = "abort"
        def run(self, prompter, cfg, ctx):
            raise KeyboardInterrupt

    class _PA(_Phase):
        name = "pa"; title = "PA"; members = [_Abort]

    try:
        _PA(logger=None).run(_Prompter(), {}, {})
        assert False, "should have propagated KeyboardInterrupt"
    except KeyboardInterrupt:
        pass
