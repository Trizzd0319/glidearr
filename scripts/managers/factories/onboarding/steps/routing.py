"""
steps/routing.py — library re-organizer + 4K/anime routing preferences.
================================================================================
Captures HOW owned media is routed across *arr instances and library folders, and
whether the re-organizer may MOVE misfiled files. Re-runnable on its own via
``--service routing``. Every default reproduces today's behaviour, so an install is
unchanged until the operator opts in.

  * movies.4k_policy / 4k_dual_min_score — keep BOTH a 4K + an HD copy (remote play)
    or just the best one. Only asked when a DISTINCT 4K Radarr instance is mapped.
  * movies.anime_policy — route anime movies to the dedicated anime instance (and
    whether to also keep a standard copy). Only asked when an anime instance is mapped.
  * tv.anime_policy / tv.4k_enabled / tv.dual_version — the TV equivalents; the 4K-TV
    branch needs a second Sonarr session to be meaningful.
  * reorg_mode — off / log_only (classify + LOG, move nothing) / same_instance (actuate
    same-instance folder moves). Picking same_instance asks for relocation_consent — the
    destructive "yes, move my files" opt-in (headless: RECOMMENDARR_RELOCATION_CONSENT).

None of the preferences are secrets → plaintext config.json under ``routing`` +
``relocation_consent``. The runtime gates live in
machine_learning/space/routing_targets (reorg_mode / relocation_enabled).
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.base import Step, StepResult


def _real_instances(cfg, service) -> list:
    """Configured instance names for a service, excluding the ``default_instance`` marker
    (every enumerator skips it — see schema.py / base_instance_manager)."""
    insts = cfg.get(f"{service}_instances", {}) or {}
    return [k for k in insts if k != "default_instance" and isinstance(insts.get(k), dict)]


def _default_name(cfg, service) -> str:
    return ((cfg.get(f"{service}_instances", {}) or {}).get("default_instance", {}) or {}).get("name") or ""


class RoutingStep(Step):
    name = "routing"
    title = "File-move routing (4K & anime)"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("File-move routing (4K & anime)")
        prompter.notice(
            "   These choices decide which *arr instance and library folder each title lands\n"
            "   in, and whether the re-organizer may MOVE misfiled owned files. They never\n"
            "   delete anything (deletion stays governed by the deletions step), and every\n"
            "   default keeps today's behaviour. Re-run anytime: --service routing."
        )

        block = cfg.setdefault("routing", {})
        mb = block.setdefault("movies", {})
        tv = block.setdefault("tv", {})

        radarr_cat = cfg.get("radarr_instances_categorized", {}) or {}
        sonarr_cat = cfg.setdefault("sonarr_instances_categorized", {})
        default_radarr = _default_name(cfg, "radarr")
        sonarr_names = _real_instances(cfg, "sonarr")
        movie_rf = cfg.get("movieRootFolders", {}) or {}
        root_folders = cfg.get("rootFolders", {}) or {}

        # ── MOVIES: 4K (dual-version only with a DISTINCT 4K Radarr instance) ──
        fourk_inst = radarr_cat.get("4K") or radarr_cat.get("4k")
        if fourk_inst and fourk_inst != default_radarr:
            prompter.notice(
                "   Dual-version keeps BOTH a 4K copy (4K instance, local direct-play) AND an HD\n"
                "   copy (standard instance) so remote / low-bandwidth clients play the HD file\n"
                "   directly instead of transcoding the 4K. Cost: ~2x disk for those titles."
            )
            mb["4k_policy"] = prompter.choice(
                "routing.movies.4k_policy",
                "For 4K movies, keep BOTH 4K and HD (remote play) or only the best copy?",
                options=["highest_only", "both"],
                default=mb.get("4k_policy", "highest_only"),
            )
            if mb["4k_policy"] == "both":
                mb["4k_dual_min_score"] = int(prompter.integer(
                    "routing.movies.4k_dual_min_score",
                    "Only keep the second (HD) copy when the movie's score is at least (0 = always)",
                    default=int(mb.get("4k_dual_min_score", 0) or 0),
                ) or 0)
            else:
                mb.setdefault("4k_dual_min_score", 0)
        else:
            mb.setdefault("4k_policy", "highest_only")
            mb.setdefault("4k_dual_min_score", 0)
            prompter.notice(
                "   No dedicated 4K Radarr instance is mapped — 4K is handled as a same-instance\n"
                "   folder move. Map one via --service radarr to enable dual-version."
            )

        # ── MOVIES: anime instance (only when a dedicated one is mapped) ──────
        anime_inst = radarr_cat.get("anime")
        if anime_inst and anime_inst != default_radarr:
            mb["anime_policy"] = prompter.choice(
                "routing.movies.anime_policy",
                "Anime movies: keep on the dedicated anime instance only (dedicated), also keep a "
                "standard copy (dedicated_plus_standard), or ignore the anime split (standard_only)?",
                options=["dedicated", "dedicated_plus_standard", "standard_only"],
                default=mb.get("anime_policy", "dedicated"),
            )
        else:
            mb.setdefault("anime_policy", "dedicated")
            prompter.notice(
                "   No dedicated anime Radarr instance is mapped — anime movies route to the\n"
                "   default instance. Map one via --service radarr to split them out."
            )

        mb["kids_bucket_enabled"] = bool(prompter.confirm(
            "routing.movies.kids_bucket_enabled",
            "Route kid-safe movies to a separate kids folder?",
            default=bool(mb.get("kids_bucket_enabled", bool(movie_rf.get("kids")))),
        ))

        # ── TV: anime folder/tag ──────────────────────────────────────────────
        tv["anime_policy"] = prompter.choice(
            "routing.tv.anime_policy",
            "Anime series keep seriesType=anime for parsing. Also route them to a dedicated anime "
            "FOLDER (series_type_plus_folder), or keep them in the series folder with just the tag "
            "(series_type)?",
            options=["series_type", "series_type_plus_folder"],
            default=tv.get("anime_policy",
                           "series_type_plus_folder" if root_folders.get("anime") else "series_type"),
        )

        # ── TV: 4K instance (needs a second Sonarr session) ───────────────────
        tv["4k_enabled"] = bool(prompter.confirm(
            "routing.tv.4k_enabled",
            "Do you have (or want) a dedicated 4K Sonarr instance for TV?",
            default=bool(tv.get("4k_enabled", False)),
        ))
        if tv["4k_enabled"] and len(sonarr_names) >= 2:
            chosen = prompter.choice(
                "sonarr_instances_categorized.4k",
                "Which Sonarr session holds 4K TV?",
                options=sonarr_names + ["— skip —"],
                default=sonarr_cat.get("4k", sonarr_names[0]),
            )
            if chosen and chosen != "— skip —":
                sonarr_cat["4k"] = chosen
            prompter.notice(
                "   Dual-version TV keeps BOTH 4K and HD episodes in parallel instances — same\n"
                "   remote-play benefit as movies, ~2x disk for those series."
            )
            tv["dual_version"] = prompter.choice(
                "routing.tv.dual_version",
                "Keep BOTH 4K and HD episodes, or only the best?",
                options=["highest_only", "both"],
                default=tv.get("dual_version", "highest_only"),
            )
        else:
            tv.setdefault("dual_version", "highest_only")
            if tv["4k_enabled"]:
                prompter.notice(
                    "   Only one Sonarr session is configured — add a second via --service sonarr,\n"
                    "   then re-run --service routing to map it as the 4K TV instance."
                )

        tv["kids_bucket_enabled"] = bool(prompter.confirm(
            "routing.tv.kids_bucket_enabled",
            "Route kid-safe shows to a separate kids folder?",
            default=bool(tv.get("kids_bucket_enabled", bool(root_folders.get("kids")))),
        ))

        # ── RE-ORGANIZER MODE (+ destructive-move consent) ────────────────────
        prompter.notice(
            "   The re-organizer reconciles ALREADY-OWNED titles to the correct library when a\n"
            "   late signal (CSM age, anime detection, a UHD upgrade) changes their bucket:\n"
            "     off           = do nothing.\n"
            "     log_only      = classify owned media + LOG misplacements; move NOTHING (safe).\n"
            "     same_instance = actuate same-instance folder moves (cross-instance stays log-only)."
        )
        block["reorg_mode"] = prompter.choice(
            "routing.reorg_mode",
            "How should the library re-organizer behave for owned media?",
            options=["off", "log_only", "same_instance"],
            default=block.get("reorg_mode", "log_only"),
        )

        if block["reorg_mode"] == "same_instance":
            prompter.notice(
                "   same_instance MOVES files on disk (Sonarr/Radarr relocate them; Plex must\n"
                "   re-scan). Destructive-adjacent, so it needs explicit consent — OFF until you\n"
                "   opt in (or set RECOMMENDARR_RELOCATION_CONSENT=true for headless / Docker)."
            )
            if getattr(prompter, "is_interactive", False):
                cfg["relocation_consent"] = bool(prompter.confirm(
                    "relocation_consent",
                    "Allow the re-organizer to MOVE owned files between folders to fix placement?",
                    default=bool(cfg.get("relocation_consent", False)),
                ))

        return [self._summary(block, cfg)]

    @staticmethod
    def _summary(block, cfg) -> StepResult:
        mode = block.get("reorg_mode", "log_only")
        mv = block.get("movies", {})
        tail = f"movies 4k={mv.get('4k_policy')} anime={mv.get('anime_policy')}"
        if mode == "same_instance" and cfg.get("relocation_consent"):
            return StepResult("routing", ok=True, detail=f"re-org ARMED (same-instance moves) · {tail}")
        if mode == "same_instance":
            return StepResult("routing", ok=False,
                              detail=f"same_instance chosen but relocation_consent off — moves stay off (log only) · {tail}")
        return StepResult("routing", ok=(True if mode == "log_only" else None),
                          detail=f"reorg_mode={mode} · {tail}")
