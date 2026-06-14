import json
import os

from scripts.managers.factories.cache import make_json_safe


class TautulliMLDatasetBuilder:
    def __init__(self, watch_history_path, metadata_path, output_path):
        self.watch_history_path = watch_history_path
        self.metadata_path = metadata_path
        self.output_path = output_path

    def load_json(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"File not found: {path}")
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def build_dataset(self):
        watch_data = self.load_json(self.watch_history_path)
        metadata = self.load_json(self.metadata_path)

        raw_entries = watch_data.get("data") or ((watch_data.get("response") or {}).get("data") or {}).get("data", [])
        if not isinstance(raw_entries, list):
            raise ValueError("Watch history format invalid or not a list")

        dataset = []
        for entry in raw_entries:
            rk = str(entry.get("rating_key"))
            meta = metadata.get(rk)
            if not meta:
                continue

            record = {
                "user_id": entry.get("user_id"),
                "username": entry.get("user"),
                "title": entry.get("full_title") or entry.get("title"),
                "genres": meta.get("genres", []),
                "actors": meta.get("actors", []),
                "directors": meta.get("directors", []),
                "composers": meta.get("composers", []),
                "producers": meta.get("producers", []),
                "studios": meta.get("studios", []),
                "labels": meta.get("labels", []),
                "collections": meta.get("collections", []),
                "video_codec": meta.get("video_codec"),
                "audio_codec": meta.get("audio_codec"),
                "audio_language": meta.get("audio_language"),
                "play_duration": entry.get("play_duration"),
                "watched_status": entry.get("watched_status"),
                "timestamp": entry.get("started") or entry.get("date")
            }
            dataset.append(record)

        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        with open(self.output_path, 'w', encoding='utf-8') as out:
            json.dump(make_json_safe(dataset), out, indent=2)

        print(f"✅ ML dataset built with {len(dataset)} records → {self.output_path}")


if __name__ == "__main__":
    builder = TautulliMLDatasetBuilder(
        watch_history_path="cache/tautulli/watch_history/watch_history_default.json",
        metadata_path="cache/tautulli/metadata_libraries/metadata_libraries_default.json",
        output_path="machine_learning/tautulli/user_media_viewing_data.json"
    )
    builder.build_dataset()
