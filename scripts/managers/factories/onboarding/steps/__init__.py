"""
steps/__init__.py — onboarding step registry (6 grouped phases over 15 leaf steps).
================================================================================
Onboarding is presented as SIX coherent phases instead of fifteen separate steps,
so a new operator (and the Docker headless config-generator) sees a short, sensible
flow. Each phase is a thin COMPOSITE that runs its member leaf steps in order under
one section banner — the leaf steps (and their tests) are unchanged; only their
grouping is. The six phases, in order:

  1. Servers & connections   — Sonarr + Radarr instances (HARD-required producers;
                               discover root folders into ctx for phases 1-2).
  2. Library & routing        — root folders, genres, dry-run, free-space floor,
                               4K/anime/kids placement + relocation consent.
  3. Accounts & metadata      — Trakt, MAL, TVDB, MDBList (external credential sources).
  4. Viewing data & playlists — Tautulli + Plex (per-user taste, playlists, age gating).
  5. Automation & tuning      — enrich daemon, next-episode prefetch, English-dub,
                               Discord notifications (config-only, optional).
  6. Space, safety & consent  — deletion consent, free-space floor, pre-destructive
                               backup gate, size-anomaly. Deliberately ISOLATED:
                               informed consent for the one irreversible action must
                               never be defaulted-on without a TTY (its headless path
                               early-exits by design).

Hard ordering constraints (real in-process data flow via the transient ``ctx``):
Sonarr/Radarr must precede Library/Routing (they feed ``ctx['root_folders']``); Trakt
must precede the enrich daemon. The phase order above preserves both.

Headless/Docker config-generation is INDEPENDENT of step count: OnboardingManager.run
builds the full skeleton via deep_merge(schema.empty_config(), existing) once, then each
leaf writes only its own keys from RECOMMENDARR_* env — so grouping leaves changes nothing
about the generated config. ``--service <leaf>`` still targets a single leaf (sonarr,
radarr, library, routing, trakt, tautulli, plex, tvdb, mal, mdblist, daemons, next_episode,
english_dub, deletions, notifications) via the flat LEAF_CLASSES registry below.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.arr import RadarrStep, SonarrStep
from scripts.managers.factories.onboarding.steps.base import Step, StepResult
from scripts.managers.factories.onboarding.steps.daemons import DaemonsStep
from scripts.managers.factories.onboarding.steps.deletions import DeletionsStep
from scripts.managers.factories.onboarding.steps.english_dub import EnglishDubStep
from scripts.managers.factories.onboarding.steps.extras import TvdbStep
from scripts.managers.factories.onboarding.steps.library import LibraryStep
from scripts.managers.factories.onboarding.steps.mal import MalStep
from scripts.managers.factories.onboarding.steps.mdblist import MdblistStep
from scripts.managers.factories.onboarding.steps.media import PlexStep, TautulliStep
from scripts.managers.factories.onboarding.steps.nextep import NextEpisodeStep
from scripts.managers.factories.onboarding.steps.notifications import NotificationsStep
from scripts.managers.factories.onboarding.steps.routing import RoutingStep
from scripts.managers.factories.onboarding.steps.trakt import TraktStep

# Flat registry of the underlying leaf steps, in their canonical run order. Used to
# resolve a single ``--service <name>`` reconfigure and to enumerate addressable
# service names — the names CALLERS know are the leaf names, not the phase names.
LEAF_CLASSES = [
    SonarrStep, RadarrStep,
    LibraryStep, RoutingStep,
    TraktStep, MalStep, TvdbStep, MdblistStep,
    TautulliStep, PlexStep,
    DaemonsStep, NextEpisodeStep, EnglishDubStep, NotificationsStep,
    DeletionsStep,
]


class _Phase(Step):
    """A grouped onboarding phase: runs its member leaf steps in order under one banner.

    Per-CHILD try/except keeps the warn-and-continue contract — one unreachable service
    still emits its own summary row and the phase carries on. KeyboardInterrupt/EOFError
    (user abort / no stdin) propagate so OnboardingManager can cancel without saving."""
    members: list = []

    def run(self, prompter, cfg, ctx) -> list:
        if getattr(prompter, "is_interactive", False):
            prompter.section(self.title)
        results: list = []
        for cls in self.members:
            leaf = cls(logger=self.logger)
            try:
                results.extend(leaf.run(prompter, cfg, ctx) or [])
            except (KeyboardInterrupt, EOFError):
                raise
            except Exception as e:
                if self.logger:
                    self.logger.log_warning(f"[Onboarding] step '{leaf.name}' error: {e}")
                results.append(StepResult(leaf.name, ok=False, detail=f"error: {e}"[:60]))
        return results


class ConnectionsStep(_Phase):
    name = "connections"
    title = "1. Servers & connections — Sonarr & Radarr"
    members = [SonarrStep, RadarrStep]


class LibraryRoutingStep(_Phase):
    name = "library_routing"
    title = "2. Library & routing — folders, genres, placement"
    members = [LibraryStep, RoutingStep]


class AccountsStep(_Phase):
    name = "accounts"
    title = "3. Accounts & metadata sources — Trakt, MAL, TVDB, MDBList"
    members = [TraktStep, MalStep, TvdbStep, MdblistStep]


class MediaStep(_Phase):
    name = "media"
    title = "4. Viewing data & playlists — Tautulli & Plex"
    members = [TautulliStep, PlexStep]


class AutomationStep(_Phase):
    name = "automation"
    title = "5. Automation & feature tuning — daemon, next-episode, dubs, notifications"
    members = [DaemonsStep, NextEpisodeStep, EnglishDubStep, NotificationsStep]


class SpaceSafetyStep(_Phase):
    name = "space_safety"
    title = "6. Space, safety & deletion consent"
    members = [DeletionsStep]


# The six grouped phases, in order.
STEP_CLASSES = [
    ConnectionsStep,
    LibraryRoutingStep,
    AccountsStep,
    MediaStep,
    AutomationStep,
    SpaceSafetyStep,
]


def build_steps(logger=None, only_service: str | None = None):
    """The ordered phases for a full run; or, for a ``--service <name>`` reconfigure, the
    single matching LEAF step (so per-service reconfigure still targets one service)."""
    if only_service:
        return [cls(logger=logger) for cls in LEAF_CLASSES if cls.name == only_service]
    return [cls(logger=logger) for cls in STEP_CLASSES]


def step_names() -> list[str]:
    """Addressable service names for ``--service`` — the LEAF names, not the phase names."""
    return [cls.name for cls in LEAF_CLASSES]


def phase_names() -> list[str]:
    return [cls.name for cls in STEP_CLASSES]
