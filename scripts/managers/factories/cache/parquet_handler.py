# beta/managers/factories/cache/parquet_handler.py

from pathlib import Path
from typing import Optional, Union

import pandas as pd

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.managers.factories.cache.constants import FallbackSettings


class CacheParquetManager:
    """
    Handles saving and loading of DataFrames in Parquet format,
    with graceful fallback to CSV when Parquet support is unavailable.
    """

    def __init__(self, logger: Optional[LoggerManager] = None):
        self.logger = logger or LoggerManager()
        self.default_engine = FallbackSettings.DEFAULT_PARQUET_ENGINE
        self.supported_engines = FallbackSettings.SUPPORTED_PARQUET_ENGINES

    # ─────────────────────────────────────────────────────────────
    # 💾 Save Operations
    # ─────────────────────────────────────────────────────────────

    def save_dataframe(
        self,
        path: Path,
        df: pd.DataFrame,
        fallback_to_csv: bool = True,
        index: bool = False,
        engine: Optional[str] = None,
    ) -> bool:
        """
        Save a DataFrame to disk as a Parquet file. If Parquet fails, optionally fallback to CSV.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=index, engine=engine or self.default_engine)
            self.logger.log_debug(f"💾 Saved DataFrame to Parquet: {path}")
            return True
        except ImportError:
            if fallback_to_csv:
                fallback = path.with_suffix(".csv")
                return self._save_as_csv(fallback, df, index=index)
            self.logger.log_warning(f"⚠️ Parquet engine not available and CSV fallback disabled.")
            return False
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to save DataFrame to {path}: {e}")
            if fallback_to_csv:
                fallback = path.with_suffix(".csv")
                return self._save_as_csv(fallback, df, index=index)
            return False

    def _save_as_csv(self, path: Path, df: pd.DataFrame, index: bool = False) -> bool:
        """Fallback: save as CSV if Parquet fails."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=index)
            self.logger.log_warning(f"⚠️ Saved fallback CSV to {path}")
            return True
        except Exception as e:
            self.logger.log_warning(f"❌ Failed to save fallback CSV at {path}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # 📂 Load Operations
    # ─────────────────────────────────────────────────────────────

    def load_dataframe(
        self,
        path: Path,
        fallback_csv: bool = True,
        engine: Optional[str] = None
    ) -> Optional[pd.DataFrame]:
        """
        Load a DataFrame from a Parquet file. If loading fails, optionally try fallback CSV.
        """
        try:
            df = pd.read_parquet(path, engine=engine or self.default_engine)
            self.logger.log_info(f"📂 Loaded DataFrame from Parquet: {path}")
            return df
        except FileNotFoundError:
            self.logger.log_warning(f"⚠️ Parquet file not found: {path}")
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to load Parquet from {path}: {e}")

        if fallback_csv:
            return self._load_fallback_csv(path.with_suffix(".csv"))

        return None

    def _load_fallback_csv(self, path: Path) -> Optional[pd.DataFrame]:
        """Fallback: load from CSV if Parquet fails or is unavailable."""
        try:
            df = pd.read_csv(path)
            self.logger.log_info(f"📂 Loaded fallback CSV from {path}")
            return df
        except FileNotFoundError:
            self.logger.log_warning(f"⚠️ CSV fallback not found: {path}")
        except Exception as e:
            self.logger.log_warning(f"❌ Failed to load fallback CSV from {path}: {e}")
        return None
