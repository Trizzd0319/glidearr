# scripts/support/tools/main_trakt.py — dev harness: drive TraktManager standalone.

# Standalone bootstrap: put the repo root on sys.path so `scripts.*` imports
# resolve from any invocation cwd.
import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.factories.config.__Init__ import ConfigManager
from scripts.managers.factories.registry import RegistryManager
from scripts.managers.services.trakt import TraktManager
from scripts.support.utilities.logger.logger import LoggerManager

# Mirror main.py's construction so the harness stays in step with the real
# TraktManager API (it takes logger/config/global_cache/validator/registry/dry_run,
# not the old sonarr_apis/ml_manager/tautulli_api/plex_api kwargs).
logger = LoggerManager()
config = ConfigManager(logger=logger)
config.reload()
cache = GlobalCacheManager(logger=logger, config=config)
registry = RegistryManager()

trakt = TraktManager(
    logger=logger,
    config=config,
    global_cache=cache,
    validator=None,
    registry=registry,
    dry_run=True,
)

trakt.run()
