# beta/managers/factories/cache/config_constants.py

from enum import Enum


# 🔗 Cache file suffixes for serialization formats
class CacheSuffix(str, Enum):
    JSON = ".json"
    PARQUET = ".parquet"
    LAST_UPDATED = ".last_updated"
    CSV = ".csv"
    YAML = ".yaml"


# 📁 Subdirectory mappings for each service
class CacheDirectory(str, Enum):
    BASE = "support/cache"
    SONARR = "support/cache/sonarr"
    RADARR = "support/cache/radarr"
    TAUTULLI = "support/cache/tautulli"
    TRAKT = "support/cache/trakt"
    PLEX = "support/cache/plex"
    TVDB = "support/cache/tvdb"


# 🧩 Enriched DataFrame suffixes for serialization
class EnrichedSuffix(str, Enum):
    SERIES = "_series_enriched.parquet"
    EPISODES = "_episodes_enriched.parquet"
    MOVIES = "_movies_enriched.parquet"
    PEOPLE = "_people_enriched.parquet"


# ⏱ Timestamp file keys
class TimestampKeys(str, Enum):
    LAST_UPDATE = "last_updated"


# 📦 File formats supported
class FileFormat(str, Enum):
    JSON = "json"
    PARQUET = "parquet"
    CSV = "csv"


# ⚙️ Fallbacks and serialization options
class FallbackSettings:
    DEFAULT_ENCODING = "utf-8"
    DEFAULT_INDENT = 2
    DEFAULT_COMPRESSED = False
    DEFAULT_PARQUET_ENGINE = "pyarrow"
    SUPPORTED_PARQUET_ENGINES = ["pyarrow", "fastparquet"]


# 🧠 Cache key templates used across services
class CacheKeyTemplate:
    SERIES_LIBRARY = "{service}/{instance}/library"
    SERIES_ENRICHED = "{service}/{instance}/library" + EnrichedSuffix.SERIES
    EPISODES_LIBRARY = "{service}/{instance}/episodes"
    EPISODES_ENRICHED = "{service}/{instance}/episodes" + EnrichedSuffix.EPISODES
    TAGS = "{service}/{instance}/tags"
    QUALITY = "{service}/{instance}/quality"
    MONITORING = "{service}/{instance}/monitoring"
    STORAGE = "{service}/{instance}/storage"
    HISTORY = "{service}/{instance}/history"
    TIMESTAMP = "{service}/{instance}/{name}" + CacheSuffix.LAST_UPDATED


# 🛠 Export paths for serialized artifacts
class ExportPaths:
    EXPORT_DIR = "exports"
    SONARR_EXPORT = "exports/sonarr_{instance}_series_enriched.parquet"
    RADARR_EXPORT = "exports/radarr_{instance}_movies_enriched.parquet"
    TAUTULLI_USERS = "exports/tautulli_users.json"
    TVDB_EXPORT = "exports/tvdb_{tvdb_id}_full.json"
