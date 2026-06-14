# scripts/support/tools/main_trakt.py — dev harness: drive TraktManager standalone.

# Standalone bootstrap: put the repo root on sys.path so `scripts.*` imports
# resolve from any invocation cwd.
import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.factories.config import ConfigManager
from scripts.managers.services.trakt import TraktManager
from scripts.support.utilities.logger.logger import LoggerManager

# Minimal mocks or pass-throughs
logger = LoggerManager()
config = ConfigManager(logger)
cache = GlobalCacheManager(config=config, logger=logger)

trakt = TraktManager(
    logger=logger,
    config=config,
    global_cache=cache,
    sonarr_apis={},  # You can fill this in with mocks or real connections later
    ml_manager=None,  # Pass actual ML manager when ready
    tautulli_api=None,
    plex_api=None
)

trakt.process_all_data()
