# Package shim — the canonical Plex HTTP client lives at plex/instances/api.py.
# This re-export mirrors tautulli/api.py so callers can ``from
# scripts.managers.services.plex.api import PlexAPI`` exactly as they do for Tautulli.
from scripts.managers.services.plex.instances.api import PlexAPI, build_base_url, scrub_url

__all__ = ["PlexAPI", "build_base_url", "scrub_url"]
