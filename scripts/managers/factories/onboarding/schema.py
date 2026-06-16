"""
schema.py — canonical config skeleton + shape builders for onboarding.
================================================================================
This mirrors the *LIVE* ``config.json`` shapes that the running readers actually
consume — NOT the stale ``default_config.json`` / ``reference_keys.json`` (which
still describe the old flat ``radarr-4k`` / flat ``tautulli`` layout).

The shapes onboarding writes:
  * sonarr_instances / radarr_instances:
        {"default_instance": {"name": <name>}, "<name>": {url, port, api, base_url}}
    Readers iterate the dict and skip the ``default_instance`` key
    (auth_validator.py, base_instance_manager._parse_host).
  * tautulli:  {"default": {url, port, api, base_url}}
    (tautulli/__init__.py collapses ``.default`` / the first sub-dict.)
  * trakt:     {client_id, client_secret, authorization{...}, username}

Keeping the skeleton here means every step writes a known, complete structure and
the file never ends up with half-populated nested dicts.
"""
from __future__ import annotations

import copy

# Discord notification defaults — mirrors the live config.json block.
_DISCORD_DEFAULTS = {
    "enabled": False,
    "webhook_url": "",
    "username": "Glidearr",
    "avatar_url": "",
    "color_success": 3066993,
    "color_warning": 16776960,
    "color_error": 15158332,
    # Delete the previous run-summary message on the next run so only the latest
    # summary remains in the channel.
    "replace_previous": True,
}

# Default Trakt token lifetime (90 days) used when the OAuth response omits it.
DEFAULT_TRAKT_EXPIRES_IN = 7_776_000


def empty_config() -> dict:
    """A fresh, fully-keyed config skeleton with blank/secret-free values."""
    return copy.deepcopy({
        "trakt": {
            "client_id": "",
            "client_secret": "",
            "authorization": {
                "access_token": "",
                "token_type": "bearer",
                "expires_in": 604800,
                "refresh_token": "",
                "scope": "public",
                "created_at": 0,
            },
            "username": "",
        },
        "tvdb": {"api": "", "pin": "", "token": ""},
        "rootFolders": {"series": "", "anime": "", "documentary": ""},
        # Movie library buckets (classify_movie: kids/anime/4k/standard). Read by
        # resolver._pick_root_folder + router_movie.py; absent in older configs (then
        # movie placement falls back to the *arr's first reported root folder).
        "movieRootFolders": {"standard": "", "anime": "", "kids": "", "4k": ""},
        # Library re-organizer + 4K/anime routing preferences, captured by the `routing`
        # onboarding step (re-runnable via --service routing). Every default reproduces
        # today's behaviour, so existing installs are unchanged until the user opts in.
        #   movies.4k_policy: "both" keeps a 4K + an HD copy (remote play); "highest_only" keeps one.
        #   movies.proactive_4k: with 4k_policy=="both", proactively give ANY owned movie whose
        #     watch-likelihood warrants 4K a copy on the 4K instance (not just glidearr-acquired /
        #     universe / recently-watched). Off by default; only actuates with the relocation gate.
        #   movies.evict_uhd_first: under space pressure, reclaim dual-version 4K BONUS copies
        #     (1080p baseline survives → no title lost) BEFORE deleting any whole title. Off by
        #     default; gated on the deletion coordinator owning deletion (NOT relocation consent).
        #   movies.anime_policy: "dedicated" routes anime movies to the dedicated anime instance;
        #     "dedicated_plus_standard" keeps an extra standard copy; "standard_only" ignores the split.
        #   tv.anime_policy: "series_type_plus_folder" (anime FOLDER + seriesType) | "series_type".
        #   reorg_mode: "off" (no re-org) | "log_only" (classify owned + LOG misplacements, move
        #     nothing) | "same_instance" (actuate same-instance root-folder moves; cross-instance
        #     migration stays log-only). Actuation also requires relocation_consent (below).
        "routing": {
            "movies": {
                "4k_policy": "highest_only",
                "4k_dual_min_score": 0,
                "proactive_4k": False,
                "evict_uhd_first": False,
                "anime_policy": "dedicated",
                "kids_bucket_enabled": False,
            },
            "tv": {
                "anime_policy": "series_type",
                "4k_enabled": False,
                "dual_version": "highest_only",
                "kids_bucket_enabled": False,
            },
            "reorg_mode": "log_only",
        },
        # Explicit consent to MOVE owned files on disk between root folders / instances —
        # destructive-adjacent (a mid-move failure can split state), so gated exactly like
        # deletions_consent: off by default; RECOMMENDARR_RELOCATION_CONSENT /
        # GLIDEARR_RELOCATION_CONSENT env override. Read by routing_targets.relocation_enabled;
        # log_only / off never move files so never need consent.
        "relocation_consent": False,
        "animeGenres": [],
        "documentaryGenres": [],
        "free_space_limit": 0,
        # Owned-movie monitoring: how the repair pass decides which has-file but
        # unmonitored movies to monitor. "watchability" (default) monitors only
        # keep-tagged / watched / score>=threshold; "all" = monitor everything;
        # "off" = leave monitoring untouched.
        "owned_monitor_policy": "watchability",
        "owned_monitor_score_threshold": 30,
        # Two-stage prune of owned movies that stay below the demote floor (20):
        #   unmonitor after owned_demote_dwell_days (30), DELETE FILE after
        #   owned_delete_dwell_days (90). Deletions are restored (re-monitor + search)
        #   if the score later recovers above owned_restore_score_threshold.
        #   keep/universe-tagged or ever-watched movies are never touched; movies
        #   without cached Trakt credits are deferred (never deleted on missing data).
        "owned_demote_enabled": True,
        "owned_demote_score_threshold": 20,
        "owned_demote_dwell_days": 30,
        "owned_delete_enabled": True,
        "owned_delete_dwell_days": 90,
        "owned_restore_score_threshold": 20,
        "owned_delete_min_dwell_days": 7,
        # Space-pressure deletion: free_space_limit (T) is the floor to keep. As free
        # space falls into the band [T, U=T*(1+headroom)] the time-based delete dwell
        # is expedited; below T a target loop deletes the lowest-rated owned movies
        # (watchability + critic ratings) until free >= U. All restorable.
        "space_pressure_headroom_ratio": 0.10,
        "space_pressure_delete_enabled": True,
        "space_pressure_include_unwatched": True,
        "space_pressure_score_ceiling": 20,
        # Cross-service space coordinator: when enabled, deletion of movies AND TV is
        # centralised into one ranked, lowest-watchability-first pool (downgrade both
        # first, then delete to free_space_limit). Default OFF until fully rolled out.
        "space_coordinator_enabled": True,
        "tv_downgrade_enabled": True,
        "tv_space_pressure_score_ceiling": 20,
        # Watch-likelihood-gated quality upgrades (Radarr universe + active-watcher,
        # Sonarr JIT). Likelihood = max(engagement floor, affinity propensity):
        # engagement floors it (rewatch 90 / watched 50 / started 40 / abandoned <=25)
        # and AFFINITY (cast/crew/studio/genre, via watchability_score * gain, boosted
        # by affinity_boost) raises it — capped at affinity_cap=75 so affinity alone
        # reaches high-4K but never TOP-4K (reserved for rewatched). Radarr maps the
        # likelihood onto radarr_quality_ladder (explicit profile ids, below); Sonarr
        # JIT uses the resolution cutoffs (uhd/fhd/hd -> 2160/1080/720).
        "watch_likelihood": {
            "rewatch_floor": 90, "watched_floor": 50, "started_floor": 40,
            "abandoned_ceiling": 25,
            # untouched_mode: "percentile" ranks each title within the library so
            # affinity SPREADS across tiers (Option 1); "absolute" = base+score*gain.
            # untouched_pct_floor: percentiles <= this contribute 0, so only the top
            # (100-floor)% of untouched titles climb above the floor profile — the
            # "only the top X% upgrade" knob (raise it to upgrade fewer titles).
            "untouched_mode": "absolute", "untouched_pct_floor": 0,
            "untouched_base": 12, "untouched_score_gain": 1.0,
            "affinity_cap": 75, "affinity_boost": 1.8,
            "uhd_cutoff": 70, "fhd_cutoff": 40, "hd_cutoff": 20,
            "uhd_res": 2160, "fhd_res": 1080, "hd_res": 720, "floor_res": 720,
        },
        # Radarr explicit profile ladder: ascending [min_likelihood%, profile_id].
        # Distinguishes sub-tiers sharing a resolution (low/high-1080p, low/high-4K).
        #  3=HD-720p 4=HD-1080p 6=HD-720p/1080p 7=HD Bluray+WEB 8=Remux+WEB-1080p
        #  5=Ultra-HD(low-4K) 9=Remux 2160p(high-4K) 10=UHD Bluray+WEB(top-4K).
        "radarr_quality_ladder": [
            [0, 3], [20, 4], [30, 6], [40, 7], [55, 8], [65, 5], [70, 9], [85, 10],
        ],
        # Watchability-score (score_show / score_movie) tunables.
        #   show_user_rating: the Group-A4 declared-rating term for SHOWS only. A
        #   series rating is a stickier, weaker signal than revealed episode
        #   engagement (A2), so it is gentler than the movie term (fixed at slope 2,
        #   +10/-5). It is also CONFIDENCE-GATED: the bump is scaled by
        #   max(watched_episodes / conf_divisor, fraction_watched), capped at 1.0 —
        #   a 10/10 after two episodes is trusted far less than after four seasons.
        #   slope=points per rating-point above 5/10; pos_cap/neg_cap clamp the bump;
        #   conf_divisor=episodes watched for full confidence from raw count.
        #   related_graph: Group-C3 collaborative "related-graph affinity" — how many
        #   of a title's Trakt-RELATED neighbours the household has watched, generalising
        #   collection/universe completeness (C1/C2) onto Trakt's similarity graph. Reads
        #   the daemon-cached movie_related bucket (and show_related once enabled). cap =
        #   max points (mirrors C2's +4); enabled toggles the term.
        "scoring": {
            "show_user_rating": {
                "slope": 1.5, "pos_cap": 8.0, "neg_cap": -3.0, "conf_divisor": 8.0,
            },
            "related_graph": {"enabled": True, "cap": 4.0},
        },
        # JIT next-episode quality: per_episode_tiers (default ON) lets each upcoming episode earn
        # its OWN best-that-fits tier, so one series can mix tiers (e.g. one 2160p next to four
        # 1080p). The background search groups episodes by target tier and flips the series profile
        # one group at a time, so a lower-target episode is never grabbed at a higher tier. OFF =
        # one profile per series (byte-identical to the pre-per-episode behavior).
        "jit_per_episode_tiers": {"enabled": True},
        # Pilot search: best_tier_first (default ON) makes a stub pilot target the HIGHEST tier
        # whose grab keeps the space reserve, diverting DOWN one rung per empty run for availability
        # (never likelihood-gated — a pilot earns max tier on space alone). OFF = legacy
        # floor-first/step-up. force_floor (default FALSE): when even the floor would breach the
        # reserve, grab at the floor anyway vs skip + re-probe next run. A pilot is NEVER deleted.
        "pilot_best_tier_first": {"enabled": True, "force_floor": False},
        "large_file_gb": 30,
        "firstRunCompleted": False,
        "radarr_instances": {"default_instance": {"name": ""}},
        "sonarr_instances": {"default_instance": {"name": ""}},
        "radarr_instances_categorized": {},   # {tier_label: instance_name} — 720p/1080p/4K/anime → which Radarr session
        # Symmetric twin for Sonarr (e.g. {"4k": "sonarr4k"}). gateway.categorized_instance is
        # already generic (reads f"{service}_instances_categorized"), so a dedicated 4K/anime TV
        # session needs no gateway change — only this key + populating it.
        "sonarr_instances_categorized": {},
        "tautulli": {"default": {"url": "", "port": "", "api": "", "base_url": ""}},
        "mal": {
            "client_id": "",
            "client_secret": "",
            # MAL requires a real, pre-registered redirect URL (it does NOT support
            # the OOB urn). Register this exact value as the app's App Redirect URL.
            "redirect_uri": "http://localhost/oauth",
            "authorization": {
                "access_token": "",
                "token_type": "bearer",
                "expires_in": 0,
                "refresh_token": "",
                "created_at": 0,
            },
            "username": "",
        },
        # episodes.enabled / movies.enabled (default OFF): build the owned-episode
        # tvdb→ratingKey and owned-movie tmdb→ratingKey maps + coverage probes
        # (plex/episodes/*, plex/movies/*) that personal TV + MOVIE playlists need.
        # Heavy local-PMS scans, so opt-in; a fresh install gets the keys (discoverable)
        # but off.
        "plex": {"url": "", "port": 32400, "plex_token": "", "plex_media_path": "",
                 "episodes": {"enabled": False}, "movies": {"enabled": False}},
        "mdblist": {"apikey": ""},   # opt-in: aggregated ratings + lists. apikey -> keyring.
        "dry_run": True,
        "notifications": {"discord": dict(_DISCORD_DEFAULTS)},
        # Phase-3 capabilities — all OFF by default; honour dry_run when enabled.
        "acquisition": {
            "enabled": False,
            "sources": {"trakt_recommendations": True, "trakt_watchlist": True, "mal": True},
            "monitored": True,
            "search_on_add": False,
            "max_adds_per_run": 10,
            "recommendation_limit": 20,
            "quality_profile": "",
            "min_score": 0,
            # Next-episode (stay-ahead) prefetch tuning — RECOMMENDED ON. Each sub-key
            # is opt-OUT: write {} or {"enabled": False} to disable just that feature.
            # Keep these values IN SYNC with the DEFAULT_* constants in
            # machine_learning/acquisition/next_episode_planner.py.
            "next_episode": {
                "graduated_cap": {"enabled": True, "reference_minutes": 45, "base_cap": 6, "hard_cap": 24},
                "recency_gate":  {"enabled": True, "cold_days": 90},
                "budget_ramp":   {"enabled": True, "low_mult": 0.5, "high_mult": 1.5},
            },
        },
        "trakt_writeback": {"enabled": False, "collection": True, "history": True},
        "mal_writeback": {"enabled": False},
        # calendar.mal: include the MAL seasonal chart in the calendar, gated by
        # watchability (score_show on genres x household affinity + the MAL mean);
        # unowned upcoming entries realistically score 0-25 → low threshold default.
        "calendar": {"enabled": False, "ensure_monitored": True, "search": False,
                     "mal": True, "mal_min_watchability": 20},
        # Background workers — the Trakt enrichment daemon is OFF by default. When
        # enabled, main.py (re)spawns it and runs become cache-only.
        "daemons": {"enrich": {"enabled": False, "scope": [], "owned_first": True}},
    })


def build_base_url(host: str, port) -> str:
    """Compose a ``http(s)://host:port`` base URL from a bare host + port.

    Accepts a host that is already a full URL (returned as-is) so users can paste
    either ``192.168.1.110`` or ``https://sonarr.example.com``.
    """
    host = (host or "").strip().rstrip("/")
    port = str(port or "").strip()
    if not host:
        return ""
    if host.startswith(("http://", "https://")):
        return host
    return f"http://{host}:{port}" if port else f"http://{host}"


def instance_block(host: str, port, api: str) -> dict:
    """Build a single Sonarr/Radarr instance block in the LIVE shape."""
    return {
        "url": (host or "").strip(),
        "port": str(port or "").strip(),
        "api": api or "",
        "base_url": build_base_url(host, port),
    }


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` onto ``base`` (overlay values win).

    Used to lay an existing (possibly partial) config over the full skeleton so
    every expected key exists while keeping whatever the user already had.
    """
    out = copy.deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out
