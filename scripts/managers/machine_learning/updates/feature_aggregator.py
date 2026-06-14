import json
from collections import defaultdict
from pathlib import Path

import pandas as pd


class TautulliFeatureAggregator:
    def __init__(self, history_path, metadata_path, output_dir):
        self.history_path = Path(history_path)
        self.metadata_path = Path(metadata_path)
        self.output_dir = Path(output_dir)
        self.history = self._load_json(self.history_path)
        self.metadata = self._load_json(self.metadata_path)
        self.user_features = defaultdict(lambda: defaultdict(int))

    def _load_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def extract_features(self):
        entries = ((self.history.get("response") or {}).get("data") or {}).get("data", [])

        for entry in entries:
            user = entry.get("user") or entry.get("friendly_name")
            rk = str(entry.get("rating_key"))
            meta = self.metadata.get(rk)
            if not user or not meta:
                continue

            for genre in meta.get("genres", []):
                self.user_features[user][f"genre_{genre}"] += 1

            for actor in meta.get("actors", []):
                self.user_features[user][f"actor_{actor}"] += 1

            for director in meta.get("directors", []):
                self.user_features[user][f"director_{director}"] += 1

            for composer in meta.get("composers", []):
                self.user_features[user][f"composer_{composer}"] += 1

            for studio in meta.get("studios", []):
                self.user_features[user][f"studio_{studio}"] += 1

            for collection in meta.get("collections", []):
                self.user_features[user][f"collection_{collection}"] += 1

            vc = meta.get("video_codec")
            ac = meta.get("audio_codec")
            al = meta.get("audio_language")

            if vc:
                self.user_features[user][f"video_codec_{vc}"] += 1
            if ac:
                self.user_features[user][f"audio_codec_{ac}"] += 1
            if al:
                self.user_features[user][f"audio_lang_{al}"] += 1

    def save_feature_matrix(self):
        df = pd.DataFrame.from_dict(self.user_features, orient="index").fillna(0).astype(int)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_file = self.output_dir / "user_feature_matrix.csv"
        df.to_csv(output_file)
        return output_file


if __name__ == "__main__":
    aggregator = TautulliFeatureAggregator(
        history_path="cache/tautulli/watch_history/watch_history_default.json",
        metadata_path="cache/tautulli/metadata_libraries/metadata_libraries_default.json",
        output_dir="machine_learning"
    )
    aggregator.extract_features()
    path = aggregator.save_feature_matrix()
    print(f"✅ User feature matrix saved to {path}")
