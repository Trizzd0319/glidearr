# Deprecated — TraktAPI has been consolidated into TraktAPIManager.
# This module is retained for import compatibility only.
from scripts.managers.services.trakt.api import TraktAPIManager as TraktAPI  # noqa: F401

__all__ = ["TraktAPI"]
