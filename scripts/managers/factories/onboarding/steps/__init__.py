"""
steps/__init__.py — ordered onboarding step registry.
================================================================================
Order matters: Sonarr/Radarr run first so the Library step can offer the root
folders discovered from those instances. The remaining services follow.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.arr import RadarrStep, SonarrStep
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

# Ordered classes — instantiated per run with the active logger.
# DaemonsStep follows Trakt (it depends on Trakt being configured to be useful).
# NextEpisodeStep / EnglishDubStep are config-only (no service dependency) — they run late,
# before notifications. EnglishDubStep records Radarr English-dub preferences (applied via
# the support/tools/_english_*.py setup tools).
STEP_CLASSES = [
    SonarrStep,
    RadarrStep,
    LibraryStep,
    # Routing follows Library so it can offer the instances + root folders the
    # Sonarr/Radarr/Library steps have already discovered (and detect a 4K/anime instance).
    RoutingStep,
    TraktStep,
    TautulliStep,
    PlexStep,
    TvdbStep,
    MalStep,
    MdblistStep,
    DaemonsStep,
    NextEpisodeStep,
    EnglishDubStep,
    DeletionsStep,
    NotificationsStep,
]


def build_steps(logger=None, only_service: str | None = None):
    """Instantiate the ordered steps, optionally filtered to one service name."""
    steps = [cls(logger=logger) for cls in STEP_CLASSES]
    if only_service:
        steps = [s for s in steps if s.name == only_service]
    return steps


def step_names() -> list[str]:
    return [cls.name for cls in STEP_CLASSES]
