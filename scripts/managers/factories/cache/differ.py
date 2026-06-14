# beta/managers/factories/cache/differ.py

import pandas as pd
from typing import Optional

from scripts.support.utilities.logger.logger import LoggerManager


class CacheDiffer:
    """
    Computes delta differences between two DataFrames:
    - Added rows
    - Removed rows
    - Changed rows based on specified fields
    """

    def __init__(self, logger=None):
        self.logger = logger or LoggerManager()

    def diff(
        self,
        new_df: pd.DataFrame,
        old_df: pd.DataFrame,
        primary_key: str,
        comparison_fields: Optional[list[str]] = None
    ) -> dict:
        """
        Returns dict with 'added', 'removed', and 'changed' DataFrames.
        """
        if primary_key not in new_df.columns or primary_key not in old_df.columns:
            self.logger.log_error(f"❌ Primary key '{primary_key}' missing in one of the DataFrames.")
            return {"added": new_df, "removed": pd.DataFrame(), "changed": pd.DataFrame()}

        new_df = new_df.set_index(primary_key)
        old_df = old_df.set_index(primary_key)

        # New rows not in old cache
        added = new_df.loc[new_df.index.difference(old_df.index)].reset_index()

        # Old rows no longer present
        removed = old_df.loc[old_df.index.difference(new_df.index)].reset_index()

        # Changed rows
        changed = pd.DataFrame()
        if comparison_fields:
            shared_idx = new_df.index.intersection(old_df.index)
            diffs = new_df.loc[shared_idx, comparison_fields] != old_df.loc[shared_idx, comparison_fields]
            changed = new_df.loc[diffs.any(axis=1)].reset_index()

        self.logger.log_info(f"🧮 Diff summary: +{len(added)} added, -{len(removed)} removed, ~{len(changed)} changed")

        return {
            "added": added,
            "removed": removed,
            "changed": changed
        }
