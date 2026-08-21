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
    ("owned_monitor_score_threshold", "30", "Min watchability score (0-100) to monitor an owned movie"),
    ("owned_demote_enabled", "true", "Prune owned movies that stay low-watchability (unmonitor then delete)"),
    # 20, NOT the 17 the space-pressure ceilings below carry. This pass scores through
    # repair/anomaly.py::_score_owned (no transcode_profile → Group D v2 never reaches it),
    # so its axis was not translated by SCORER_REVISION 4 and its floor was not re-anchored.
    # It is also the hysteresis partner of owned_restore_score_threshold — they move together
    # or a movie in the gap flaps. See machine_learning/thresholds/registry.py's delete block.
    ("owned_demote_score_threshold", "20", "Demote floor (hysteresis vs the 30 monitor threshold)"),
    ("owned_demote_dwell_days", "30", "Days below the floor before unmonitoring"),
    ("owned_delete_enabled", "true", "DELETE the file after a longer sustained low-watchability window"),
    ("owned_delete_dwell_days", "90", "Days below the floor before deleting the file (restorable)"),
    ("owned_restore_score_threshold", "20", "Re-acquire a deleted movie if its score recovers above this"),
    ("owned_restore_min_age_days", "0", "Min days since deletion before a recovered title may be re-acquired (0 = off; stops delete/re-grab thrash)"),
    ("owned_delete_min_dwell_days", "7", "Most-expedited delete dwell when free space is at the floor"),
    ("space_pressure_headroom_ratio", "0.10", "Pressure band above free_space_limit (0.10 = up to +10%)"),
    ("space_pressure_delete_enabled", "true", "Delete lowest-rated movies to free space below free_space_limit"),
    ("space_pressure_include_unwatched", "true", "Allow deleting unwatched low-watchability movies under pressure"),
    # 17: these two read the PERSISTED watchability_score column, the axis Group D v2
    # translated down ~13 points (SCORER_REVISION 4). Re-anchored from 20 to preserve the
    # selectivity that was actually reviewed — see machine_learning/thresholds/registry.py.
    ("space_pressure_score_ceiling", "17", "Max watchability score eligible for space-pressure deletion (unwatched)"),
    ("space_coordinator_enabled", "false", "Centralise movie+TV deletion into one ranked pool (downgrade both, then delete)"),
    ("tv_downgrade_enabled", "true", "Allow downgrading low-watchability series to 720p under space pressure"),
    ("tv_space_pressure_score_ceiling", "17", "Max series watchability score eligible for TV space deletion"),
    ("tv_restore_score_threshold", "17", "Re-acquire coordinator-deleted episodes once their series' score recovers above this (Sonarr twin of owned_restore_score_threshold; separate key because it reads the persisted score axis)"),
    ("large_file_gb", "30", "Flag movies larger than this (GB) in the storage report"),
    ("backup_before_destructive", "true", "Native Radarr/Sonarr backup before any destructive change (real runs); on failure the run degrades to dry-run"),
    ("backup_deep_validate", "false", "Also CRC-check the downloaded backup zip (only when the *arr /backup route isn't UI-auth-gated; else creation is size-verified)"),
    ("backup_max_age_hours", "24", "Reuse a backup younger than this instead of making a new one every run (0 = always fresh)"),
    ("size_anomaly.enabled", "true", "Report files wildly out of size profile for their graded quality (e.g. a 45 GB '720p')"),
    ("size_anomaly.remediate", "false", "ACT on size anomalies: rescan mis-graded files; search bloated ones for a right-sized replacement (the file is KEPT until the replacement imports and then retired to the recycle bin - nothing deleted up front; needs a valid backup)"),
    ("size_anomaly.over_ratio", "3.0", "Flag oversized at >= this multiple of the expected size for the quality"),
    ("size_anomaly.max_regrab_attempts", "3", "Stop re-searching a bloated file after this many attempts with its size unchanged (a size change resets the budget); 0 = unbounded"),
    ("size_anomaly.regrab_retry_days", "7", "Cooldown between re-search attempts for the same bloated file"),
    ("acquisition.people_affinity_weight", "0.08", "Weighted share of cast/crew (people) overlap in the ADD score — elevates candidates sharing cast/crew with your watched titles. Renormalizes on present signals (no-overlap candidates unaffected); 0 disables (byte-identical)"),
    ("acquisition.space_budget.enabled", "false", "Govern acquisition by BYTES instead of a count: fund adds in priority order out of max(0, free-U) minus committed-but-unlanded GB (cross-run ledger). Off = legacy max_adds_per_run slice, byte-identical. On failure to read free space or the ledger it falls back to the BOUNDED count cap, never unlimited"),
    ("acquisition.space_budget.hard_max_adds", "0", "Optional count ceiling ON TOP of the byte budget (0 = off) — a tester seatbelt against surprise-large add waves on roomy arrays"),
    ("acquisition.quality_caps.enabled", "false", "Write a grab-time size ceiling into Radarr/Sonarr quality definitions: maxSize (MB/min) = this library's own measured rate x over_ratio, so a mislabelled disc image (a 50 GiB '720p') is refused BEFORE downloading instead of flagged after import. Writes *arr CONFIGURATION — gated by dry_run and the backup gate; caps only ever TIGHTEN and thin tiers are left alone"),
    ("acquisition.quality_caps.min_samples", "30", "Measured files a quality tier needs before a ceiling may be derived from it — too LOW a cap starves a tier silently, so tiers below this are skipped and reported, never capped"),
    ("household_affinity.family_only", "false", "Scope the HOUSEHOLD genre/actor/director affinity to Plex Home members only — shared friends streaming remotely stop steering family taste (acquisition genre scoring, playlists, watch-likelihood) while still building their own per-user matrices. Fail-open: an absent identity map (first run after enabling) skips scoping for that run with a warning"),
    ("acquisition.demand.enabled", "false", "Demand-aware ordering: as free space nears the floor, prioritise titles MORE of the household would watch over single-user picks (a shared file → value-per-GB). Default off → score-desc, byte-identical"),
    ("acquisition.demand.band", "0.30", "How far above the free-space floor (as a fraction) demand-weighting ramps in (0 at the band top → full at the floor)"),
    ("acquisition.demand.threshold", "0.15", "Per-user genre-match floor (0–1) for a user to count toward a title's demand"),
    ("pilot_interactive.enabled", "true", "Pilot search via ONE Sonarr interactive search per stub (grab the lowest available resolution in one shot, flag UNACQUIRABLE when nothing is found) instead of the blind tier-by-tier climb. false = legacy climb"),
    ("pilot_interactive.recheck_days", "7", "How long an UNACQUIRABLE pilot stays blocked before re-searching (it also re-checks immediately when a new indexer is added). Days"),
    ("pilot_interactive.floor_res", "0", "Minimum resolution (px height) a pilot grab will consider; 0 = grab the lowest available at any resolution"),
    ("pilot_interactive.search_no_resolution", "true", "When releases exist but report NO resolution (likely SD-only), search at the floor tier so Sonarr can grab them; false = flag UNACQUIRABLE"),
    ("pilot_interactive.skip_hard_rejects", "true", "Skip + flag a pilot when EVERY release is rejected for a profile-independent reason (size/blocklist/incomplete) a profile flip can't fix; false = search anyway"),
    ("pilot_interactive.anime_ladder", "true", "Route anime (seriesType=anime) stubs onto the [Anime] quality-profile ladder so they're never flipped onto an x265-penalising live-action profile; false = use the regular ladder"),
    ("pilot_interactive.report", "true", "Emit the read-only 'Pilots below 720' audit each run (count + table of on-disk sub-720 pilots split into upgradable vs held: full-series/watched/scored/keep); false = silent"),
    ("acquisition.next_episode.mode", "recommended", "Next-episode prefetch tuning: recommended | customize | off (set =off to keep it disabled headlessly)"),
    ("acquisition.universe.enabled", "false", "Hybrid universe acquisition: once the household watches part of a saga (MCU, Star Trek, Arrowverse, One Chicago…), grab its remaining films (Radarr) + shows (Sonarr) in timeline order — START-first. Default off; honours dry_run + free-space band. Needs plex.playlists.universe_timeline on"),
    ("acquisition.universe.max_per_run", "5", "Per-run cap on universe backfill grabs (bypasses acquisition.max_adds_per_run / min_score — explicit intent, own budget)"),
    ("acquisition.universe.cold_start", "false", "false = extend only sagas the household has already watched ≥1 member of; true = also cold-start universes you own none of (aggressive). NOTE: true is coordinator-pending (Phase 7) — currently inert, always extend-only"),
    ("acquisition.universe.movies", "true", "Acquire unowned FILM members of an engaged saga via Radarr"),
    ("acquisition.universe.tv", "true", "Acquire unowned SHOW members of an engaged saga via Sonarr"),
    # Per-viewer episode retention (lifecycle.viewer_retention) — ON by default. The
    # behaviour it replaces is a bug (an episode is delete-eligible 3h after ANYONE
    # watched it, with no backward cushion and no protection for a viewer further
    # behind), so this ships enabled and is turned OFF, not on.
    ("episode_retention.enabled", "true", "Per-VIEWER episode retention (ON by default): each account holds [position − backward_buffer, position + pace × horizon_days] on every series it watches; an episode inside ANY account's window is never marked for deletion. false = legacy behaviour (delete 3h after ANYONE watched it, no backward cushion, no protection for a viewer further behind)"),
    ("episode_retention.backward_buffer", "2", "Episodes kept BEHIND each viewer's furthest-watched episode — the rewatch cushion ('in case the viewer wants to go backwards'). Counted in real episodes, so it crosses a season boundary correctly. 0 = hold only the resume point itself"),
    ("episode_retention.horizon_days", "14", "How many days of each viewer's measured pace to protect AHEAD of their position ('if someone will approach the episode within a decent timeframe, don't delete it')"),
    ("episode_retention.pace_window_days", "30", "Window used to measure a viewer's episodes/day on a series. Anchored on THAT VIEWER'S last play, not on today, so a paused viewer keeps the pace they were actually going at"),
    ("episode_retention.default_pace", "1.0", "Episodes/day assumed when pace is undefined (a viewer with a single play on the series). A viewer one episode in is at their most likely to continue"),
    ("episode_retention.dormant_days", "", "Days with no play on a series before that viewer stops projecting FORWARD (position + backward_buffer are still held). BLANK = inherit acquisition.next_episode.recency_gate.cold_days (90) so 'cold' has one definition; set a number to split the two"),
    ("episode_retention.watched_percent", "85", "LEGACY ALIAS of watched_threshold.percent — kept so an existing config keeps working. Set watched_threshold.percent instead; it wins when both are present"),
    ("watched_threshold.percent", "85", "WHAT COUNTS AS WATCHED, system-wide. A play counts as a WATCH when Tautulli's own per-row watched_status says so (its verdict always wins — it already reflects the threshold configured in Tautulli/Plex), else when percent_complete >= this. Drives is_watched/watch_count in BOTH the movie and episode caches, the 3h grace clock, the A2/A3 engagement+rewatch signals, the watch_likelihood engagement floors and the per-viewer retention intervals. 0 = the old behaviour where a 30-second sample counted as a watch and queued the file for deletion"),
    ("saga_retention.enabled", "false", "Catch-up (trailing-viewer) retention: never delete a saga title a behind viewer still needs to reach (the gate = viewers who WATCHED or WATCHLISTED any saga member, derived from data — no hardcoded users). At the free-space floor held titles are DOWNGRADED not deleted. Default off → legacy deletion unchanged"),
    ("saga_retention.dormancy_window_days", "90", "Drop a viewer from a saga's gate after this many days of no saga activity (the primary disk-safety knob; prevents an abandoned saga pinning disk forever)"),
    ("saga_retention.completion_threshold", "0.8", "A play reaching this fraction counts as a 'meaningful watch' = engaged with the saga"),
    ("saga_retention.engagement_grace_days", "7", "A STARTED but sub-threshold play still counts as engaged for this many days, so a real interruption (kid/work/life) keeps the hold; expires after if never finished"),
    ("saga_retention.watchlist_hold_policy", "windowed", "How a watchlist-only member (watchlisted but never started) gates: until_start | windowed (expire after dormancy) | indefinite"),
    ("saga_retention.expiry_boost_days", "30", "Final N days before a held title is released: lift it to the top of that viewer's playlists + a 'Leaving Soon' collection ('use it or lose it')"),
    ("saga_retention.downgrade_at_floor", "true", "At free<free_space_limit, downgrade (shrink) held titles instead of deleting them"),
    ("saga_retention.leaving_soon_collection", "true", "Surface the about-to-be-released set as a 'Leaving Soon' Plex collection promoted first on Home"),
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
    ("cross_instance_move_consent", "false", "Allow the cross-instance reconcile to physically MOVE a file between *arr instances (e.g. 2160p off standard onto the 4K instance); needs routing.reorg_mode=cross_instance + a shared mount; off by default"),
    ("cross_instance_dedup_consent", "false", "Allow the cross-instance reconcile to DELETE the worse copy when both instances own a title (the better copy is kept); needs routing.reorg_mode=cross_instance + the backup gate; off by default"),
    ("routing.reorg_mode", "log_only", "Library re-organizer for owned media: off | log_only | same_instance (MOVES files between root folders) | cross_instance (MOVES files between instances + dedup)"),
    ("routing.movies.4k_policy", "both", "Movie 4K policy when a DISTINCT 4K Radarr exists: both | uhd_only | hd_only"),
    ("routing.movies.4k_dual_min_score", "75", "Min watchability score to keep BOTH a 4K and an HD copy of a movie (0 = unset → the shipped DEFAULT_UHD_SCORE, which is also 75)"),
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
    ("ignored_users", "", "Comma-separated profile titles / safe_users IGNORED across ALL per-user runs EXCEPT affinity (dropped from the tracked roster; their watches still feed the affinity signal)"),
    ("plex.playlists.recency_boost.enabled", "false", "Lift a show/saga you're caught up on to the top of Up Next the moment its freshest item lands within window_days (e.g. a finished show whose new episode just aired); applies to the TV, movie, and combined per-user ordering"),
    ("plex.playlists.fresh_arrivals.enabled", "false", "Build a per-profile 'Fresh Arrivals' playlist of genuinely-new movie acquisitions (churn-immune; off until enabled)"),
    ("plex.playlists.fresh_arrivals.acquired_window_days", "45", "How many days back counts as a 'fresh arrival' for that playlist"),
    ("plex.playlists.home_collections.enabled", "false", "Build the age-tiered 'Up Next - <Tier>' collections and pin them to Home (REAL Plex writes; off until enabled, also needs dry_run=false)"),
    ("plex.playlists.home_collections.promote_home", "true", "Promote the tier collections to the OWNER's Home screen"),
    ("plex.playlists.home_collections.promote_shared", "false", "Also promote the tier collections to managed/friends' Home screens"),
    ("plex.playlists.cold_start_kids_prior", "true", "Seed a kid profile's empty playlist from the household's kid-show taste (cold-start prior)"),
    ("plex.playlists.this_week_in_history.enabled", "false", "Build per-profile 'Anniversary Picks' (movies) + 'On This Week' (shows) shelves of titles released/aired this calendar week in any past year (off until enabled)"),
    ("plex.playlists.this_week_in_history.cap", "7", "Max NET-NEW (to-acquire) picks per anniversary shelf — the already-owned freebies are UNCAPPED and listed below the net-new finds"),
    ("plex.playlists.this_week_in_history.min_votes", "0", "TMDb-vote popularity floor on anniversary MOVIE picks (owned AND net-new; the show shelf is never floored) — drops any movie below this vote count, or with no vote data (0 = off)"),
    ("plex.playlists.this_week_in_history.popularity_weight", "0.30", "How heavily a title's all-time vote volume boosts its anniversary-shelf rank — reuses the scorer's log-scaled popularity, just re-weighted (0.30 default vs the add pipeline's 0.10, so a notable old title beats a recent obscure one; 0 = popularity ignored)"),
    ("plex.playlists.this_week_in_history.timezone", "", "IANA timezone that pins the household week, e.g. America/New_York (blank = PMS/local)"),
    ("plex.playlists.this_week_in_history.opt_in_users", "", "Comma-separated profile titles/keys to build the shelf for (blank + enabled = all tracked users)"),
    ("plex.playlists.this_week_in_history.trust_home_managed", "false", "When a managed profile's library grant can't be resolved, default it to ALL libraries (still age-gated) instead of an empty shelf"),
    ("plex.playlists.hidden_gems.enabled", "false", "Build a per-profile 'Hidden Gems' shelf of movies you OWN, have NEVER played, and that match your taste — ranked on a TASTE-ONLY score (engagement signals excluded, so it surfaces the backlog instead of re-ranking favourites); off until enabled"),
    ("plex.playlists.hidden_gems.size", "25", "How many picks a Hidden Gems shelf holds"),
    ("plex.playlists.hidden_gems.max_per_franchise", "2", "Max picks from one collection/universe, so a single saga can't fill the shelf (0 = no cap)"),
    ("plex.playlists.hidden_gems.max_per_person", "3", "Max picks sharing one credited director/lead actor (0 = no cap)"),
    ("plex.playlists.hidden_gems.window_days", "30", "Days a Hidden Gems pick has to be played before it counts as a miss — also how long it stays off the shelf after being shown (one knob, so a play is never ambiguous between two recommendations)"),
    ("plex.playlists.hidden_gems.play_min_pct", "85", "Completion percentage that counts as 'played' when scoring a Hidden Gems pick — keep it equal to the watched-set floor (85) or a successful pick will keep re-surfacing"),
    ("plex.playlists.hidden_gems.opt_in_users", "", "Comma-separated profile titles/keys to build the Hidden Gems shelf for (blank + enabled = all tracked users)"),
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
    ("daemons.pilot_search.enabled", "true", "Spill large Sonarr pilot interactive-search batches to a background daemon so big sprees never hang the run"),
    ("daemons.pilot_search.threshold", "10", "Batches LARGER than this many stub pilots go to the daemon; <= stay in-process"),
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
