# Package marker for the Plex HTTP client.
#
# Plex is a FLAT single-instance service ({url, port, plex_token, ...}) — unlike
# Tautulli/Sonarr/Radarr there is NO multi-instance manager here. The canonical
# HTTP client lives at ``plex/instances/api.py`` (re-exported by ``plex/api.py``)
# only to mirror the Tautulli package layout the rest of the codebase expects.
from scripts.managers.services.plex.instances.api import PlexAPI

__all__ = ["PlexAPI"]
