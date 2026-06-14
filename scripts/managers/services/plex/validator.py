"""
plex/validator.py — PlexValidatorManager (stub).
================================================================================
Mirrors TautulliValidatorManager: a no-op ``validate() -> True`` so the component
splitter can introspect/load it like every other submanager. Real reachability +
token-scope gating happens in ``PlexManager._is_reachable`` / the scope probe at
``PlexManager.run()`` top — not here.
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager


class PlexValidatorManager(BaseManager):
    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)

    def validate(self) -> bool:
        return True
