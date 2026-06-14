"""
tautulli/api.py — package-level re-export shim
================================================
``TautulliManager.__init__`` imports ``TautulliAPI`` from this module.
The canonical implementation lives in ``tautulli.instances.api``; this
file simply re-exports it so the package ``__init__`` loads cleanly.

Do NOT add logic here.  Any Tautulli API work should go in
``scripts.managers.services.tautulli.instances.api``.
"""

from scripts.managers.services.tautulli.instances.api import TautulliAPI

__all__ = ["TautulliAPI"]
