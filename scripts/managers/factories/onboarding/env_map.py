"""
env_map.py — the headless / Docker / unraid environment-variable contract.
================================================================================
Every config leaf maps to a ``RECOMMENDARR_*`` environment variable using the
SAME convention the SecretStore already uses for secrets
(``secret_store.env_name`` — e.g. ``trakt.client_secret`` ->
``RECOMMENDARR_TRAKT_CLIENT_SECRET``). Onboarding extends that convention to the
NON-secret leaves too (urls, ports, root folders, genres …) when running headless,
so a container/unraid template can supply the entire config via env vars.

Instance counts are expressed as a comma list (Sonarr is single-instance; Radarr may list several):
    RECOMMENDARR_SONARR_INSTANCE_NAMES="sonarr"   ·   RECOMMENDARR_RADARR_INSTANCE_NAMES="standard,4k"
and each instance's fields hang off the dotted path, e.g.:
    RECOMMENDARR_SONARR_INSTANCES_SONARR_URL / _PORT / _API / _BASE_URL

``generate_env_example`` / ``generate_markdown_table`` emit a ready-to-ship
``.env.example`` and a docs table — the eventual unraid Docker template source.
"""
from __future__ import annotations

import os

from scripts.managers.factories.config.secret_store import env_name, is_secret_key


def env_for(path: str) -> str:
    """Dotted config path -> RECOMMENDARR_* env var name."""
    return env_name(path)


def get_env(path: str):
    """Return the env value for a config path, or None if unset/blank."""
    val = os.environ.get(env_name(path))
    return val if val not in (None, "") else None


def split_list(value: str) -> list[str]:
    """Parse a comma/semicolon/whitespace-separated env value into a clean list."""
    if not value:
        return []
    for sep in (",", ";", "\n"):
        value = value.replace(sep, " ")
    return [tok.strip() for tok in value.split(" ") if tok.strip()]


def instance_names(service: str) -> list[str]:
    """Read RECOMMENDARR_<SERVICE>_INSTANCE_NAMES into an ordered name list."""
    raw = os.environ.get(env_name(f"{service}.instance_names"))
    return split_list(raw or "")


def is_truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# ── Documentation / template generation ───────────────────────────────────────
# (path, example, note) — non-instance leaves. Instance fields are templated
# separately because their count is user-defined.
_DOC_LEAVES = [
    ("dry_run", "true", "Plan only; make no changes to your services"),
    ("free_space_limit", "2500", "Minimum free space (GB) to keep per disk"),
    ("deletions_consent", "false", "REQUIRED to allow DELETING media files (DESTRUCTIVE; with free_space_limit it arms space reclamation). Off = scoring/profiles/playlists only, pause acquisition at the floor"),
    ("owned_monitor_policy", "watchability", "Monitor owned movies by: watchability | all | off"),
    ("owned_monitor_score_threshold", "35", "Min watchability score (0-100) to monitor an owned movie"),
    ("owned_demote_enabled", "true", "Prune owned movies that stay low-watchability (unmonitor then delete)"),
    ("owned_demote_score_threshold", "20", "Demote floor (hysteresis vs the 35 monitor threshold)"),
    ("owned_demote_dwell_days", "30", "Days below the floor before unmonitoring"),
    ("owned_delete_enabled", "true", "DELETE the file after a longer sustained low-watchability window"),
    ("owned_delete_dwell_days", "90", "Days below the floor before deleting the file (restorable)"),
    ("owned_restore_score_threshold", "20", "Re-acquire a deleted movie if its score recovers above this"),
    ("owned_delete_min_dwell_days", "7", "Most-expedited delete dwell when free space is at the floor"),
    ("space_pressure_headroom_ratio", "0.10", "Pressure band above free_space_limit (0.10 = up to +10%)"),
    ("space_pressure_delete_enabled", "true", "Delete lowest-rated movies to free space below free_space_limit"),
    ("space_pressure_include_unwatched", "true", "Allow deleting unwatched low-watchability movies under pressure"),
    ("space_pressure_score_ceiling", "20", "Max watchability score eligible for space-pressure deletion (unwatched)"),
    ("space_coordinator_enabled", "false", "Centralise movie+TV deletion into one ranked pool (downgrade both, then delete)"),
    ("tv_downgrade_enabled", "true", "Allow downgrading low-watchability series to 720p under space pressure"),
    ("tv_space_pressure_score_ceiling", "20", "Max series watchability score eligible for TV space deletion"),
    ("large_file_gb", "30", "Flag movies larger than this (GB) in the storage report"),
    ("backup_before_destructive", "true", "Native Radarr/Sonarr backup before any destructive change (real runs); on failure the run degrades to dry-run"),
    ("backup_deep_validate", "false", "Also CRC-check the downloaded backup zip (only when the *arr /backup route isn't UI-auth-gated; else creation is size-verified)"),
    ("backup_max_age_hours", "24", "Reuse a backup younger than this instead of making a new one every run (0 = always fresh)"),
    ("size_anomaly.enabled", "true", "Report files wildly out of size profile for their graded quality (e.g. a 45 GB '720p')"),
    ("size_anomaly.remediate", "false", "ACT on size anomalies: rescan mis-graded + re-grab bloated at profile target (DESTRUCTIVE; needs a valid backup)"),
    ("size_anomaly.over_ratio", "3.0", "Flag oversized at >= this multiple of the expected size for the quality"),
    ("acquisition.next_episode.mode", "recommended", "Next-episode prefetch tuning: recommended | customize | off (set =off to keep it disabled headlessly)"),
    ("english_dub.mode", "recommended", "English-audio (dub) prioritization: recommended (all five pieces on) | customize | off"),
    ("jit_per_episode_tiers.enabled", "true", "JIT: per-episode quality tiers (one series may mix tiers); false = one profile per series"),
    ("pilot_floor_climb.enabled", "true", "Pilot search grabs each pilot at its LOWEST available resolution (climbs an ascending floor→widest ladder in one background pass, stops at the first tier with a release); false = use the legacy strategies below"),
    ("pilot_best_tier_first.enabled", "false", "Legacy (only when pilot_floor_climb=false): target the highest tier that fits the space reserve, diverting down; false = legacy floor-first/step-up across runs"),
    ("pilot_best_tier_first.force_floor", "false", "Legacy: grab the pilot at the floor even when no tier fits the reserve (vs skip + re-probe). Pilots are never deleted"),
    ("rootFolders.series", "/data/tv/series", ""),
    ("rootFolders.anime", "/data/tv/anime", ""),
    ("rootFolders.documentary", "/data/tv/documentary", ""),
    ("rootFolders.reality", "/data/tv/reality", ""),
    ("movieRootFolders.standard", "/data/media/movies/standard", "Movie bucket folder (kids/anime/4k/standard)"),
    ("movieRootFolders.anime", "/data/media/movies/anime", ""),
    ("movieRootFolders.kids", "/data/media/movies/kids", ""),
    ("movieRootFolders.4k", "/data/media/movies/4k", ""),
    ("relocation_consent", "false", "Allow the re-organizer to MOVE owned files between root folders (acts only at run-time when routing.reorg_mode=same_instance AND dry_run=false; destructive-adjacent)"),
    ("routing.reorg_mode", "log_only", "Library re-organizer for owned media: off | log_only | same_instance (same_instance MOVES files on disk)"),
    ("routing.movies.4k_policy", "both", "Movie 4K policy when a DISTINCT 4K Radarr exists: both | uhd_only | hd_only"),
    ("routing.movies.4k_dual_min_score", "70", "Min watchability score to keep BOTH a 4K and an HD copy of a movie"),
    ("routing.movies.anime_policy", "dedicated", "Anime movie routing: dedicated (anime folder) | standard"),
    ("routing.movies.kids_bucket_enabled", "false", "Route kid-safe movies to the kids movie folder"),
    ("routing.tv.anime_policy", "series_type_plus_folder", "Anime series routing: series_type_plus_folder | folder_only | off"),
    ("routing.tv.4k_enabled", "false", "Allow a separate 4K tier for TV"),
    ("routing.tv.dual_version", "highest_only", "TV dual-version handling: highest_only | both"),
    ("routing.tv.kids_bucket_enabled", "true", "Route kid-safe series to the kids TV folder"),
    ("animeGenres", "anime", "Comma-separated genre list"),
    ("documentaryGenres", "documentary", "Comma-separated genre list (documentary only — keep TIGHT; broad story genres like crime/war/history sweep scripted dramas into Documentaries)"),
    ("realityGenres", "reality", "Comma-separated genre list (its own bucket — not folded into documentary)"),
    ("sonarr.instance_names", "sonarr", "Sonarr session label (single instance)"),
    ("radarr.instance_names", "standard,4k", "Radarr session labels"),
    # Tier→session role map (which Radarr instance holds each tier); omit any tier you don't split out.
    # (Sonarr is single-instance — no categorization.)
    ("radarr_instances_categorized.720p", "standard", "Which Radarr session holds each tier (omit if not split)"),
    ("radarr_instances_categorized.1080p", "standard", ""),
    ("radarr_instances_categorized.4K", "4k", ""),
    ("radarr_instances_categorized.anime", "anime", "Optional dedicated anime Radarr instance"),
    ("trakt.client_id", "<from trakt.tv/oauth/applications>", "SECRET"),
    ("trakt.client_secret", "<secret>", "SECRET"),
    ("trakt.authorization.refresh_token", "<optional pre-seeded token>", "SECRET — set to skip the device flow"),
    ("tautulli.default.url", "192.168.1.110", ""),
    ("tautulli.default.port", "8181", ""),
    ("tautulli.default.api", "<secret>", "SECRET"),
    ("plex.url", "192.168.1.110", ""),
    ("plex.port", "32400", ""),
    ("plex.plex_token", "<secret>", "SECRET"),
    ("plex.plex_media_path", "/storage/media/", ""),
    ("plex.episodes.enabled", "false", "Owned-episode Plex scan: build the tvdb->ratingKey map + coverage probe for personal playlists"),
    ("plex.movies.enabled", "false", "Owned-movie Plex scan: build the tmdb->ratingKey map + coverage probe for personal MOVIE playlists"),
    ("plex.playlists.writeback.enabled", "false", "WRITES per-user playlists into Plex (create/update real playlists). Off until you opt in; ALSO requires dry_run=false"),
    ("plex.playlists.max_items", "100", "Max items per per-user playlist"),
    ("plex.playlists.exclude_users", "", "Comma-separated profile titles / safe_users to skip when building playlists"),
    ("plex.playlists.recency_boost.enabled", "false", "Lift recently-aired/added items in the per-user ordering (inert until enabled)"),
    ("plex.playlists.fresh_arrivals.enabled", "false", "Build a per-profile 'Fresh Arrivals' playlist of genuinely-new movie acquisitions (churn-immune; off until enabled)"),
    ("plex.playlists.fresh_arrivals.acquired_window_days", "45", "How many days back counts as a 'fresh arrival' for that playlist"),
    ("plex.playlists.cold_start_kids_prior", "true", "Seed a kid profile's empty playlist from the household's kid-show taste (cold-start prior)"),
    ("tvdb.api", "<secret>", "SECRET — optional"),
    ("mal.client_id", "<secret>", "SECRET — optional"),
    ("mal.client_secret", "<secret>", "SECRET — optional"),
    ("mal.redirect_uri", "http://localhost/oauth", "Must match your MAL app (blank = app default)"),
    ("mal.authorization.refresh_token", "<optional pre-seeded token>", "SECRET — set to refresh MAL headless"),
    ("mdblist.apikey", "<from mdblist.com/preferences>", "SECRET — optional; aggregated ratings + lists"),
    ("notifications.discord.enabled", "false", ""),
    ("notifications.discord.webhook_url", "<secret>", "SECRET — optional"),
    ("daemons.enrich.enabled", "false", "Run the background Trakt enrichment daemon; main runs go cache-only"),
    ("daemons.enrich.owned_first", "true", "Enrich in-library (owned) movies before unowned"),
    ("daemons.enrich.scope", "summary,people,ratings,related,aliases,studios", "Comma-separated Trakt buckets per movie"),
]

# Fields templated once per Sonarr/Radarr session.
_INSTANCE_FIELD_EXAMPLES = {"url": "192.168.1.110", "port": "8989", "api": "<secret>", "base_url": "http://192.168.1.110:8989"}


def _instance_lines(service: str, names: list[str]):
    for nm in names:
        for field, example in _INSTANCE_FIELD_EXAMPLES.items():
            path = f"{service}_instances.{nm}.{field}"
            note = "SECRET" if is_secret_key(field) else ""
            yield path, example, note
        yield f"{service}_instances.default_instance.name", names[0], "Which session is the default"


def generate_env_example(sonarr_names: list[str] | None = None,
                         radarr_names: list[str] | None = None) -> str:
    """Render a ``.env.example`` covering the full headless contract."""
    sonarr_names = sonarr_names or ["sonarr"]
    radarr_names = radarr_names or ["standard"]
    lines = [
        "# Recommendarr headless / Docker / unraid configuration.",
        "# Every value below maps to a config leaf via the RECOMMENDARR_* convention.",
        "# Secrets are read from these vars at runtime and never written to disk.",
        "",
    ]
    for path, example, note in _DOC_LEAVES:
        if note:
            lines.append(f"# {note}")
        lines.append(f"{env_name(path)}={example}")
    lines.append("")
    lines.append("# ── Sonarr sessions ──")
    for path, example, note in _instance_lines("sonarr", sonarr_names):
        if note:
            lines.append(f"# {note}")
        lines.append(f"{env_name(path)}={example}")
    lines.append("")
    lines.append("# ── Radarr sessions ──")
    for path, example, note in _instance_lines("radarr", radarr_names):
        if note:
            lines.append(f"# {note}")
        lines.append(f"{env_name(path)}={example}")
    lines.append("")
    return "\n".join(lines)


def generate_markdown_table(sonarr_names: list[str] | None = None,
                            radarr_names: list[str] | None = None) -> str:
    """Render a markdown table of the env contract (unraid template source)."""
    sonarr_names = sonarr_names or ["sonarr"]
    radarr_names = radarr_names or ["standard"]
    rows = [("Variable", "Secret", "Example / Note")]
    for path, example, note in _DOC_LEAVES:
        secret = "yes" if (is_secret_key(path.split(".")[-1]) or "SECRET" in note) else ""
        rows.append((f"`{env_name(path)}`", secret, note or example))
    for service, names in (("sonarr", sonarr_names), ("radarr", radarr_names)):
        for path, example, note in _instance_lines(service, names):
            secret = "yes" if (is_secret_key(path.split(".")[-1]) or note == "SECRET") else ""
            rows.append((f"`{env_name(path)}`", secret, note or example))
    out = ["| " + " | ".join(rows[0]) + " |", "| --- | --- | --- |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)
