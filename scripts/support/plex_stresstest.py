#!/usr/bin/env python3
"""
plex_headless_stress.py

Headless Plex playback/transcode stress tester.

Modes:
  transcode  - forces Plex HLS transcoding and discards received segments
  direct     - pulls original media bytes and discards them; useful for disk/network testing

Install:
  pip install requests

Example:
  export PLEX_URL="http://192.168.1.50:32400"
  export PLEX_TOKEN="YOUR_TOKEN"

  python plex_headless_stress.py \
    --mode transcode \
    --streams 6 \
    --duration 900 \
    --bitrate 4000 \
    --resolution 1280x720 \
    --libraries "Movies,TV Shows-Series,TV Shows-Anime"
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests


STOP = threading.Event()


@dataclass(frozen=True)
class PlexItem:
    rating_key: str
    title: str
    item_type: str
    library: str


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bytes_read = 0
        self.segments = 0
        self.errors = 0
        self.sessions_started = 0

    def add_bytes(self, amount: int) -> None:
        with self.lock:
            self.bytes_read += amount

    def add_segment(self) -> None:
        with self.lock:
            self.segments += 1

    def add_error(self) -> None:
        with self.lock:
            self.errors += 1

    def add_session(self) -> None:
        with self.lock:
            self.sessions_started += 1

    def snapshot(self) -> tuple[int, int, int, int]:
        with self.lock:
            return self.bytes_read, self.segments, self.errors, self.sessions_started


def install_signal_handlers() -> None:
    def handler(signum, frame):
        print("\n[STOP] Caught interrupt. Stopping workers...")
        STOP.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def append_token(url: str, token: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["X-Plex-Token"] = [token]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def plex_headers(client_id: str) -> dict[str, str]:
    return {
        "X-Plex-Product": "GlidearrHeadlessStress",
        "X-Plex-Version": "1.0",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Platform": "Python",
        "X-Plex-Platform-Version": sys.version.split()[0],
        "X-Plex-Device": "HeadlessStressClient",
        "X-Plex-Device-Name": f"HeadlessStress-{client_id[:8]}",
        "X-Plex-Provides": "player",
        "Accept": "application/xml,text/xml,*/*",
        "User-Agent": "GlidearrHeadlessStress/1.0",
    }


def get_xml(
    session: requests.Session,
    base_url: str,
    path: str,
    token: str,
    params: dict | None = None,
    extra_headers: dict | None = None,
    timeout: int = 30,
) -> ET.Element:
    url = f"{base_url}{path}"
    p = {"X-Plex-Token": token}
    if params:
        p.update(params)

    headers = {}
    if extra_headers:
        headers.update(extra_headers)

    r = session.get(url, params=p, headers=headers, timeout=timeout)
    r.raise_for_status()
    return ET.fromstring(r.content)


def iter_sections(session: requests.Session, base_url: str, token: str) -> Iterable[ET.Element]:
    root = get_xml(session, base_url, "/library/sections", token)
    yield from root.findall(".//Directory")


def fetch_library_items(
    session: requests.Session,
    base_url: str,
    token: str,
    section_key: str,
    section_title: str,
    section_type: str,
    page_size: int = 500,
    max_items_per_library: int = 0,
) -> list[PlexItem]:
    """
    Movies use Plex type=1.
    Episodes use Plex type=4.

    For show libraries, this pulls episodes directly so each stress worker can
    start a real playable item.
    """
    if section_type == "movie":
        media_type = "1"
    elif section_type == "show":
        media_type = "4"
    else:
        return []

    items: list[PlexItem] = []
    start = 0

    while not STOP.is_set():
        headers = {
            "X-Plex-Container-Start": str(start),
            "X-Plex-Container-Size": str(page_size),
        }

        root = get_xml(
            session,
            base_url,
            f"/library/sections/{section_key}/all",
            token,
            params={"type": media_type},
            extra_headers=headers,
        )

        videos = root.findall(".//Video")
        if not videos:
            break

        for video in videos:
            rating_key = video.attrib.get("ratingKey")
            if not rating_key:
                continue

            item_type = video.attrib.get("type", section_type)
            title = video.attrib.get("title", "Unknown")

            if item_type == "episode":
                show = video.attrib.get("grandparentTitle", "Unknown Show")
                season = video.attrib.get("parentIndex", "?")
                episode = video.attrib.get("index", "?")
                title = f"{show} - S{season}E{episode} - {title}"

            items.append(
                PlexItem(
                    rating_key=rating_key,
                    title=title,
                    item_type=item_type,
                    library=section_title,
                )
            )

            if max_items_per_library and len(items) >= max_items_per_library:
                return items

        total_size = int(root.attrib.get("totalSize", "0") or 0)
        start += page_size

        if total_size and start >= total_size:
            break

    return items


def discover_items(
    base_url: str,
    token: str,
    libraries: list[str] | None,
    max_items_per_library: int,
) -> list[PlexItem]:
    client_id = str(uuid.uuid4())
    session = requests.Session()
    session.headers.update(plex_headers(client_id))

    wanted = {x.strip().lower() for x in libraries or [] if x.strip()}
    all_items: list[PlexItem] = []

    print("[DISCOVER] Reading Plex libraries...")

    for section in iter_sections(session, base_url, token):
        section_type = section.attrib.get("type")
        section_title = section.attrib.get("title", "")
        section_key = section.attrib.get("key")

        if not section_key or section_type not in {"movie", "show"}:
            continue

        if wanted and section_title.lower() not in wanted:
            continue

        print(f"[DISCOVER] Scanning {section_title!r} ({section_type})...")
        items = fetch_library_items(
            session=session,
            base_url=base_url,
            token=token,
            section_key=section_key,
            section_title=section_title,
            section_type=section_type,
            max_items_per_library=max_items_per_library,
        )
        print(f"[DISCOVER]   found {len(items)} playable items")
        all_items.extend(items)

    random.shuffle(all_items)
    return all_items


def parse_playlist_uris(text: str, playlist_url: str) -> list[str]:
    uris: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        uris.append(urljoin(playlist_url, line))

    return uris


def read_and_discard(
    session: requests.Session,
    url: str,
    token: str,
    stats: Stats,
    timeout: int = 30,
    max_bytes: int = 0,
) -> int:
    url = append_token(url, token)
    total = 0

    with session.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if STOP.is_set():
                break
            if not chunk:
                continue

            total += len(chunk)
            stats.add_bytes(len(chunk))

            if max_bytes and total >= max_bytes:
                break

    return total


def start_hls_transcode(
    session: requests.Session,
    base_url: str,
    token: str,
    item: PlexItem,
    client_id: str,
    bitrate: int,
    resolution: str,
    offset: int,
) -> tuple[str, str]:
    """
    Starts a Plex universal HLS transcode and returns:
      session_id, playlist_url
    """
    session_id = f"headless-{client_id[:8]}-{uuid.uuid4()}"

    params = {
        "path": f"/library/metadata/{item.rating_key}",
        "mediaIndex": "0",
        "partIndex": "0",
        "protocol": "hls",
        "offset": str(offset),
        "fastSeek": "1",
        "directPlay": "0",
        "directStream": "0",
        "subtitleSize": "100",
        "audioBoost": "100",
        "location": "lan",
        "session": session_id,
        "maxVideoBitrate": str(bitrate),
        "videoResolution": resolution,
        "X-Plex-Token": token,
    }

    url = f"{base_url}/video/:/transcode/universal/start.m3u8"
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()

    return session_id, r.url


def stop_hls_transcode(
    session: requests.Session,
    base_url: str,
    token: str,
    session_id: str,
) -> None:
    try:
        session.get(
            f"{base_url}/video/:/transcode/universal/stop",
            params={"session": session_id, "X-Plex-Token": token},
            timeout=10,
        )
    except Exception:
        pass


def run_hls_reader(
    session: requests.Session,
    playlist_url: str,
    token: str,
    stats: Stats,
    end_time: float,
    playlist_interval: float,
) -> None:
    seen: set[str] = set()
    active_playlist = playlist_url

    while not STOP.is_set() and time.time() < end_time:
        playlist_with_token = append_token(active_playlist, token)
        r = session.get(playlist_with_token, timeout=20)
        r.raise_for_status()

        uris = parse_playlist_uris(r.text, active_playlist)

        # Master playlist can point to a media playlist.
        nested_playlists = [u for u in uris if ".m3u8" in u]
        if nested_playlists:
            active_playlist = nested_playlists[0]
            time.sleep(0.2)
            continue

        new_segments = [u for u in uris if u not in seen]

        if not new_segments:
            time.sleep(playlist_interval)
            continue

        for segment_url in new_segments:
            if STOP.is_set() or time.time() >= end_time:
                break

            seen.add(segment_url)
            try:
                read_and_discard(session, segment_url, token, stats, timeout=30)
                stats.add_segment()
            except Exception:
                stats.add_error()

        time.sleep(playlist_interval)


def get_direct_part_url(
    session: requests.Session,
    base_url: str,
    token: str,
    item: PlexItem,
) -> str | None:
    root = get_xml(session, base_url, f"/library/metadata/{item.rating_key}", token)
    part = root.find(".//Part")
    if part is None:
        return None

    key = part.attrib.get("key")
    if not key:
        return None

    return urljoin(base_url, key)


def worker(
    worker_id: int,
    args: argparse.Namespace,
    items: list[PlexItem],
    stats: Stats,
    global_end_time: float,
) -> None:
    client_id = str(uuid.uuid4())
    session = requests.Session()
    session.headers.update(plex_headers(client_id))

    rng = random.Random(uuid.uuid4().int)

    while not STOP.is_set() and time.time() < global_end_time:
        item = rng.choice(items)

        try:
            if args.mode == "transcode":
                offset = rng.randint(0, max(args.random_offset_max, 0))
                transcode_session_id, playlist_url = start_hls_transcode(
                    session=session,
                    base_url=args.base_url,
                    token=args.token,
                    item=item,
                    client_id=client_id,
                    bitrate=args.bitrate,
                    resolution=args.resolution,
                    offset=offset,
                )

                stats.add_session()
                print(f"[W{worker_id}] TRANSCODE start: {item.title}")

                try:
                    run_hls_reader(
                        session=session,
                        playlist_url=playlist_url,
                        token=args.token,
                        stats=stats,
                        end_time=min(global_end_time, time.time() + args.item_seconds),
                        playlist_interval=args.playlist_interval,
                    )
                finally:
                    stop_hls_transcode(session, args.base_url, args.token, transcode_session_id)

            else:
                direct_url = get_direct_part_url(session, args.base_url, args.token, item)
                if not direct_url:
                    raise RuntimeError(f"No playable Part key found for {item.title}")

                stats.add_session()
                print(f"[W{worker_id}] DIRECT start: {item.title}")

                direct_end = min(global_end_time, time.time() + args.item_seconds)
                while not STOP.is_set() and time.time() < direct_end:
                    read_and_discard(
                        session=session,
                        url=direct_url,
                        token=args.token,
                        stats=stats,
                        timeout=60,
                        max_bytes=args.direct_chunk_bytes,
                    )
                    time.sleep(args.direct_pause)

        except Exception as e:
            stats.add_error()
            print(f"[W{worker_id}] ERROR: {e}")

        time.sleep(args.between_items)


def reporter(stats: Stats, start_time: float) -> None:
    last_bytes = 0
    last_time = start_time

    while not STOP.is_set():
        time.sleep(5)

        now = time.time()
        bytes_read, segments, errors, sessions = stats.snapshot()

        delta_bytes = bytes_read - last_bytes
        delta_time = max(now - last_time, 0.001)
        mbps = (delta_bytes * 8) / delta_time / 1_000_000

        total_gb = bytes_read / 1024 / 1024 / 1024

        print(
            f"[STATS] sessions={sessions} "
            f"segments={segments} "
            f"errors={errors} "
            f"read={total_gb:.2f} GiB "
            f"rate={mbps:.1f} Mbps"
        )

        last_bytes = bytes_read
        last_time = now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Headless Plex playback/transcode stress tester")

    parser.add_argument("--base-url", default=os.getenv("PLEX_URL"), help="Plex base URL, e.g. http://server:32400")
    parser.add_argument("--token", default=os.getenv("PLEX_TOKEN"), help="Plex token")

    parser.add_argument("--mode", choices=["transcode", "direct"], default="transcode")
    parser.add_argument("--streams", type=int, default=4, help="Concurrent playback sessions")
    parser.add_argument("--duration", type=int, default=600, help="Total test duration in seconds")
    parser.add_argument("--item-seconds", type=int, default=300, help="How long each worker stays on one item before picking another")

    parser.add_argument("--libraries", default="", help='Comma-separated library names, e.g. "Movies,TV Shows-Series"')
    parser.add_argument("--max-items-per-library", type=int, default=0, help="0 means no limit")

    parser.add_argument("--bitrate", type=int, default=4000, help="Transcode target max video bitrate in Kbps")
    parser.add_argument("--resolution", default="1280x720", help="Transcode target resolution")
    parser.add_argument("--random-offset-max", type=int, default=600, help="Max random playback offset in seconds")

    parser.add_argument("--playlist-interval", type=float, default=1.0)
    parser.add_argument("--between-items", type=float, default=2.0)

    parser.add_argument("--direct-chunk-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--direct-pause", type=float, default=0.25)

    args = parser.parse_args()

    if not args.base_url:
        parser.error("Missing --base-url or PLEX_URL")
    if not args.token:
        parser.error("Missing --token or PLEX_TOKEN")

    args.base_url = normalize_base_url(args.base_url)
    args.libraries = [x.strip() for x in args.libraries.split(",") if x.strip()]

    return args


def main() -> int:
    install_signal_handlers()
    args = parse_args()

    items = discover_items(
        base_url=args.base_url,
        token=args.token,
        libraries=args.libraries,
        max_items_per_library=args.max_items_per_library,
    )

    if not items:
        print("[ERROR] No playable movie/episode items discovered.")
        return 2

    print(f"[READY] Found {len(items)} playable items.")
    print(f"[READY] Starting {args.streams} {args.mode} worker(s) for {args.duration}s.")

    stats = Stats()
    start_time = time.time()
    end_time = start_time + args.duration

    reporter_thread = threading.Thread(target=reporter, args=(stats, start_time), daemon=True)
    reporter_thread.start()

    with ThreadPoolExecutor(max_workers=args.streams) as executor:
        futures = [
            executor.submit(worker, i + 1, args, items, stats, end_time)
            for i in range(args.streams)
        ]

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                stats.add_error()
                print(f"[WORKER ERROR] {e}")

    STOP.set()

    bytes_read, segments, errors, sessions = stats.snapshot()
    print(
        "\n[DONE] "
        f"sessions={sessions}, "
        f"segments={segments}, "
        f"errors={errors}, "
        f"read={bytes_read / 1024 / 1024 / 1024:.2f} GiB"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())