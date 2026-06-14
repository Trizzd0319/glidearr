# scripts/support/tools/test.py — dev harness: probe Trakt progress endpoints.

# Standalone bootstrap: put the repo root on sys.path so `scripts.*` imports
# resolve from any invocation cwd.
import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Use global logger instance
from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.factories.config import ConfigManager
from scripts.managers.services.trakt.api import TraktAPIManager
from scripts.support.utilities.logger.logger import get_logger

logger = get_logger()

# Pass logger to config + cache
config = ConfigManager(logger=logger)
cache = GlobalCacheManager(logger=logger, config=config)

# Initialize Trakt API
api = TraktAPIManager(logger=logger, config=config, global_cache=cache)

# TEST SHOW
show_id = "breaking-bad"  # Can be trakt ID, slug, etc.

# Call watched and collected progress
watched = api.get_progress_watched(show_id)
collected = api.get_progress_collected(show_id)

# Output the results
logger.log_info("🔍 Watched Progress:")
logger.log_info(watched)

logger.log_info("🎞️ Collected Progress:")
logger.log_info(collected)
