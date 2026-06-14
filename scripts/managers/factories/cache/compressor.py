# beta/managers/factories/cache/compressor.py

import gzip
import shutil
from pathlib import Path
from typing import Union

from scripts.support.utilities.logger.logger import LoggerManager


class CacheCompressor:
    """
    Compresses or decompresses cache files (typically .json ⇄ .json.gz)
    to reduce disk footprint or restore from archive.
    """

    def __init__(self, logger=None):
        self.logger = logger or LoggerManager()

    def compress(self, path: Union[str, Path]) -> bool:
        """
        Compresses a .json file into .json.gz and deletes the original.
        """
        path = Path(path)
        if not path.exists() or path.suffix != ".json":
            self.logger.log_warning(f"⚠️ Skipped compression — not a valid .json file: {path}")
            return False

        compressed_path = path.with_suffix(".json.gz")

        try:
            with open(path, "rb") as f_in, gzip.open(compressed_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            path.unlink()
            self.logger.log_info(f"🗜 Compressed: {path} → {compressed_path}")
            return True
        except Exception as e:
            self.logger.log_warning(f"❌ Failed to compress {path}: {e}")
            return False

    def decompress(self, path: Union[str, Path]) -> bool:
        """
        Decompresses a .json.gz file into .json and deletes the original.
        """
        path = Path(path)
        if not path.exists() or path.suffix != ".gz":
            self.logger.log_warning(f"⚠️ Skipped decompression — not a .json.gz file: {path}")
            return False

        decompressed_path = path.with_suffix("")  # Remove .gz to get .json

        try:
            with gzip.open(path, "rb") as f_in, open(decompressed_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            path.unlink()
            self.logger.log_info(f"📤 Decompressed: {path} → {decompressed_path}")
            return True
        except Exception as e:
            self.logger.log_warning(f"❌ Failed to decompress {path}: {e}")
            return False

    def compress_all_in_dir(self, folder: Union[str, Path]) -> int:
        """
        Compresses all .json files under a folder recursively.
        Returns the number of files compressed.
        """
        folder = Path(folder)
        count = 0

        for path in folder.rglob("*.json"):
            if self.compress(path):
                count += 1

        self.logger.log_info(f"🗜 Compressed {count} .json files in {folder}")
        return count

    def decompress_all_in_dir(self, folder: Union[str, Path]) -> int:
        """
        Decompresses all .json.gz files under a folder recursively.
        Returns the number of files decompressed.
        """
        folder = Path(folder)
        count = 0

        for path in folder.rglob("*.json.gz"):
            if self.decompress(path):
                count += 1

        self.logger.log_info(f"📤 Decompressed {count} .json.gz files in {folder}")
        return count
