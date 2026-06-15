"""
steps/routing.py — library re-organizer + 4K/anime routing preferences.
================================================================================
Captures HOW owned media is routed across *arr instances and library folders, and
whether the re-organizer may MOVE misfiled files. Re-runnable on its own via
``--service routing``. Every default reproduces today's behaviour, so an install is
unchanged until the operator opts in.

Each policy toggle that needs a concrete destination INLINE-CAPTURES it on the spot
(the kids / anime / 4K MOVIE root folders are never asked for anywhere else), and the
prompt PRE-SELECTS an existing folder whose name looks right (a "…/kids", "…/anime",
"…/uhd" or "…/4k" folder discovered on any of your instances) as the default — you
still confirm or change it. The step ends with a resolved-routing summary so you can
see exactly where every bucket lands, with a clear flag on any that has no folder.

  * movies.4k_policy / 4k_dual_min_score — keep BOTH a 4K + an HD copy (remote play) or
    just the best one. Only asked when a DISTINCT 4K Radarr instance is mapped; captures
    the 4K movie root folder.
  * movies.anime_policy — route anime movies to the dedicated anime instance; captures
    the anime movie folder. Only asked when an anime instance is mapped.
  * movies.kids_bucket_enabled — send kid-safe movies to a Kids folder; captures it.
  * tv.anime_policy / tv.4k_enabled / tv.dual_version — the TV equivalents.
  * reorg_mode — off / log_only (classify + LOG, move nothing) / same_instance (actuate
    same-instance folder moves). Picking same_instance asks for relocation_consent.

None of the preferences are secrets → plaintext config.json under ``routing`` +
``relocation_consent`` + the captured ``movieRootFolders`` / ``rootFolders``. Runtime
gates live in machine_learning/space/routing_targets (reorg_mode / relocation_enabled).
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.base import Step, StepResult


def _real_instances(cfg, service) -> list:
    """Configured instance names for a service, excluding the ``default_instance`` marker."""
    insts = cfg.get(f"{service}_instances", {}) or {}
    return [k for k in insts if k != "default_instance" and isinstance(insts.get(k), dict)]


def _default_name(cfg, service) -> str:
    return ((cfg.get(f"{service}_instances", {}) or {}).get("default_instance", {}) or {}).get("name") or ""


def _leaf(path) -> str:
    """Lower-cased last path component of a root-folder path (for name matching)."""
    return str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def _auto_match(folders, keywords, prefer=()):
    """Best discovered root folder for a bucket — used to PRE-SELECT a default while still
    prompting. Prefers a folder whose PATH carries a media-context hint (``prefer``, e.g.
    'tv'/'movie') AND whose leaf matches a ``keyword`` (so a Kids-TV prompt picks
    ``…/tv/kids`` over ``…/movies/kids``); falls back to the first keyword-leaf match
    regardless of context."""
    kw, pref = tuple(keywords), tuple(prefer)

    def leaf_match(p):
        return any(k in _leaf(p) for k in kw)

    if pref:
        for p in folders:
            low = str(p or "").replace("\\", "/").lower()
            if leaf_match(p) and any(x in low for x in pref):
                return p
    for p in folders:
        if leaf_match(p):
            return p
    return None


def _discover_root_folders(cfg, ctx) -> list:
    """Live root-folder paths across ALL configured Sonarr + Radarr instances. Prefers the
    list the arr steps stashed in ``ctx`` (full onboarding flow); on a standalone
    ``--service routing`` run (empty ctx) it best-effort re-probes each configured instance
    (API key from the config block or the keyring) so the kids/anime/4K auto-detect still has
    folders to offer. Failures are skipped silently. Cached back into ``ctx``."""
    found = sorted({p for p in (ctx.get("root_folders") or []) if p})
    if found:
        return found
    try:
        from scripts.managers.factories.onboarding import validators
    except Exception:
        return []
    try:
        from scripts.managers.factories.config.secret_store import SecretStore
        store = SecretStore()
    except Exception:
        store = None
    paths = []
    for service in ("radarr", "sonarr"):
        for nm, block in (cfg.get(f"{service}_instances", {}) or {}).items():
            if nm == "default_instance" or not isinstance(block, dict):
                continue
            base = block.get("base_url") or ""
            api = block.get("api") or (store.get(f"{service}_instances.{nm}.api") if store else "") or ""
            if not base or not api:
                continue
            try:
                res = validators.arr_status(base, api, kind=service)
                if res.get("ok"):
                    paths.extend(res.get("root_folders") or [])
            except Exception:
                continue
    out = sorted({p for p in paths if p})
    ctx["root_folders"] = list(out)
    return out


def _folder_prompt(prompter, path, label, folders, keywords, current, prefer=()) -> str:
    """Inline-capture a root-folder binding. PRE-SELECTS an existing folder whose name
    matches ``keywords`` (e.g. kids / anime / uhd), biased to the media context (``prefer``,
    e.g. 'tv' vs 'movie'), as the default while still prompting: a choice over the discovered
    folders, or a text field when none were discovered. ``""`` means "no folder"."""
    detected = _auto_match(folders, keywords, prefer)
    default = current or detected or ""
    if detected and not current:
        prompter.notice(f"   Auto-detected a matching folder: {detected}  (default — press Enter to keep, or change)")
    if prompter.is_interactive and folders:
        chosen = prompter.choice(path, label, options=list(folders) + ["— none —"],
                                 default=default or (folders[0] if folders else "— none —"))
        return "" if chosen == "— none —" else chosen
    return prompter.text(path, label, default=default, required=False)


class RoutingStep(Step):
    name = "routing"
    title = "File-move routing (4K & anime)"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("File-move routing (4K & anime)")
        prompter.notice(
            "   This decides WHICH library folder (and which *arr instance) each title lands in,\n"
            "   and whether the re-organizer may MOVE already-owned files that ended up in the\n"
            "   wrong place. Nothing here deletes anything (that's the deletions step), and every\n"
            "   default keeps your current behaviour. Re-run anytime: --service routing.\n"
            "   Where a choice needs a folder, you'll be asked for it right here — and if you\n"
            "   already have a kids / anime / 4K folder on any instance, it's offered as the default."
        )

        block = cfg.setdefault("routing", {})
        mb = block.setdefault("movies", {})
        tv = block.setdefault("tv", {})
        movie_rf = cfg.setdefault("movieRootFolders", {})
        root_folders = cfg.setdefault("rootFolders", {})

        radarr_cat = cfg.get("radarr_instances_categorized", {}) or {}
        sonarr_cat = cfg.setdefault("sonarr_instances_categorized", {})
        default_radarr = _default_name(cfg, "radarr")
        sonarr_names = _real_instances(cfg, "sonarr")
        folders = _discover_root_folders(cfg, ctx)

        # ── MOVIES: 4K (dual-version only with a DISTINCT 4K Radarr instance) ──
        fourk_inst = radarr_cat.get("4K") or radarr_cat.get("4k")
        if fourk_inst and fourk_inst != default_radarr:
            prompter.notice(
                f"   You have a dedicated 4K Radarr instance ({fourk_inst}). DUAL-VERSION keeps BOTH a\n"
                "   4K copy there AND a 1080p copy on your standard instance, so remote / low-bandwidth\n"
                "   clients direct-play the smaller HD file instead of transcoding the 4K in real time.\n"
                "   'highest_only' keeps just the one best copy. Cost of 'both': ~2x disk for those titles."
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
                    "Only keep the second (HD) copy when the movie's watchability score is at least (0 = always)",
                    default=int(mb.get("4k_dual_min_score", 0) or 0),
                ) or 0)
                movie_rf["4k"] = _folder_prompt(
                    prompter, "movieRootFolders.4k", "   4K MOVIE root folder (on the 4K instance)",
                    folders, ("4k", "uhd", "2160"), movie_rf.get("4k", ""), prefer=("movie", "film"))
            else:
                mb.setdefault("4k_dual_min_score", 0)
        else:
            mb.setdefault("4k_policy", "highest_only")
            mb.setdefault("4k_dual_min_score", 0)
            prompter.notice(
                "   No dedicated 4K Radarr instance is mapped, so there's no second instance to hold a\n"
                "   separate 4K copy — 4K is handled as a same-instance folder split instead. Map a 4K\n"
                "   instance via --service radarr if you want true dual-version remote play."
            )

        # ── MOVIES: anime — instance policy (instance-gated) + folder (instance-independent) ─
        anime_inst = radarr_cat.get("anime")
        if anime_inst and anime_inst != default_radarr:
            prompter.notice(
                f"   You have a dedicated anime Radarr instance ({anime_inst}). 'dedicated' sends anime\n"
                "   movies there only; 'dedicated_plus_standard' also keeps a standard copy; 'standard_only'\n"
                "   ignores the split and treats anime like any other movie."
            )
            mb["anime_policy"] = prompter.choice(
                "routing.movies.anime_policy",
                "How should anime movies be routed across instances?",
                options=["dedicated", "dedicated_plus_standard", "standard_only"],
                default=mb.get("anime_policy", "dedicated"),
            )
        else:
            mb.setdefault("anime_policy", "dedicated")
            prompter.notice(
                "   No dedicated anime Radarr instance is mapped — anime movies stay on the default\n"
                "   instance (map one via --service radarr to split them out), but they can still go to\n"
                "   their own anime FOLDER below."
            )
        # The anime MOVIE folder is independent of the instance — anime movies route to it on
        # whichever instance holds them. Offer it whenever anime content is evident (an existing
        # value, an anime-looking folder, or a dedicated anime instance), unless standard_only.
        if mb.get("anime_policy") != "standard_only" and (
                movie_rf.get("anime") or _auto_match(folders, ("anime", "donghua")) or
                (anime_inst and anime_inst != default_radarr)):
            movie_rf["anime"] = _folder_prompt(
                prompter, "movieRootFolders.anime", "   Anime MOVIE root folder",
                folders, ("anime", "donghua"), movie_rf.get("anime", ""), prefer=("movie", "film"))

        # ── MOVIES: kids folder (captured inline — asked for nowhere else) ────
        mb["kids_bucket_enabled"] = bool(prompter.confirm(
            "routing.movies.kids_bucket_enabled",
            "Send kid-safe MOVIES to their own Kids library folder (vs mixing them into the standard folder)?",
            default=bool(mb.get("kids_bucket_enabled", bool(movie_rf.get("kids")))),
        ))
        if mb["kids_bucket_enabled"]:
            movie_rf["kids"] = _folder_prompt(
                prompter, "movieRootFolders.kids", "   Kids MOVIE root folder",
                folders, ("kids", "child", "family"), movie_rf.get("kids", ""), prefer=("movie", "film"))
            if not movie_rf.get("kids"):
                prompter.warn("   No kids folder set — kid-safe movies will fall back to the standard folder.")

        # ── TV: anime folder/tag ──────────────────────────────────────────────
        prompter.notice(
            "   Anime SERIES always keep seriesType=anime (correct episode parsing). The choice is\n"
            "   whether they ALSO move into a dedicated anime FOLDER, or stay in the series folder."
        )
        tv["anime_policy"] = prompter.choice(
            "routing.tv.anime_policy",
            "Route anime series to a dedicated anime FOLDER (series_type_plus_folder), or keep them in "
            "the series folder with just the tag (series_type)?",
            options=["series_type", "series_type_plus_folder"],
            default=tv.get("anime_policy",
                           "series_type_plus_folder" if root_folders.get("anime") else "series_type"),
        )
        if tv["anime_policy"] == "series_type_plus_folder":
            root_folders["anime"] = _folder_prompt(
                prompter, "rootFolders.anime", "   Anime TV root folder",
                folders, ("anime", "donghua"), root_folders.get("anime", ""), prefer=("tv", "series", "show"))
            if not root_folders.get("anime"):
                prompter.warn("   No anime TV folder — anime shows stay in the series folder (the tag is still set).")

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
                "   Dual-version TV keeps BOTH 4K and HD episodes in parallel instances — same remote-play\n"
                "   benefit as movies, ~2x disk for those series."
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
                    "   Only one Sonarr session is configured — add a second via --service sonarr, then\n"
                    "   re-run --service routing to map it as the 4K TV instance."
                )

        # ── TV: kids folder (captured inline — LibraryStep never asks for it) ─
        tv["kids_bucket_enabled"] = bool(prompter.confirm(
            "routing.tv.kids_bucket_enabled",
            "Send kid-safe SHOWS to their own Kids library folder (vs the series folder)?",
            default=bool(tv.get("kids_bucket_enabled", bool(root_folders.get("kids")))),
        ))
        if tv["kids_bucket_enabled"]:
            root_folders["kids"] = _folder_prompt(
                prompter, "rootFolders.kids", "   Kids TV root folder",
                folders, ("kids", "child", "family"), root_folders.get("kids", ""), prefer=("tv", "series", "show"))
            if not root_folders.get("kids"):
                prompter.warn("   No kids TV folder set — kid-safe shows will fall back to the series folder.")

        # ── RE-ORGANIZER MODE (+ destructive-move consent) ────────────────────
        prompter.notice(
            "   The re-organizer reconciles ALREADY-OWNED titles to the correct library when a late\n"
            "   signal (a Common Sense age arriving, anime detected, a UHD upgrade) changes their bucket:\n"
            "     off           = do nothing.\n"
            "     log_only      = classify owned media + LOG what's misplaced; move NOTHING (safe — start here).\n"
            "     same_instance = actuate same-instance folder moves (cross-instance migration stays log-only)."
        )
        block["reorg_mode"] = prompter.choice(
            "routing.reorg_mode",
            "How should the library re-organizer behave for owned media?",
            options=["off", "log_only", "same_instance"],
            default=block.get("reorg_mode", "log_only"),
        )

        if block["reorg_mode"] == "same_instance":
            prompter.notice(
                "   same_instance MOVES files on disk (Sonarr/Radarr relocate them; Plex must re-scan).\n"
                "   Destructive-adjacent, so it needs explicit consent — OFF until you opt in (or set\n"
                "   RECOMMENDARR_RELOCATION_CONSENT=true for headless / Docker)."
            )
            if getattr(prompter, "is_interactive", False):
                cfg["relocation_consent"] = bool(prompter.confirm(
                    "relocation_consent",
                    "Allow the re-organizer to MOVE owned files between folders to fix placement?",
                    default=bool(cfg.get("relocation_consent", False)),
                ))

        # Stamp that routing has been configured, so the resolver honours these preferences
        # (a never-onboarded install with the schema defaults keeps today's add-time routing).
        block["configured"] = True
        return [self._summary(prompter, cfg)]

    # ── resolved-routing summary ──────────────────────────────────────────────
    @staticmethod
    def _summary(prompter, cfg) -> StepResult:
        block = cfg.get("routing", {})
        mb, tv = block.get("movies", {}), block.get("tv", {})
        mrf = cfg.get("movieRootFolders", {}) or {}
        rf = cfg.get("rootFolders", {}) or {}
        rcat = cfg.get("radarr_instances_categorized", {}) or {}
        di = _default_name(cfg, "radarr") or "default"
        si = _default_name(cfg, "sonarr") or "sonarr"
        missing = []           # enabled buckets with no destination folder

        def row(bucket, folder, inst, note=""):
            if folder:
                return f"     {bucket:<10} -> {inst:<12} {folder}   {note}".rstrip()
            missing.append(bucket)
            return f"     {bucket:<10} -> {inst:<12} (no folder) -- falls back to standard/series  [!]"

        lines = ["   Resolved MOVIE routing:",
                 row("standard", mrf.get("standard"), di) if mrf.get("standard")
                 else f"     {'standard':<10} -> {di:<12} (your *arr default root folder)"]
        if mb.get("kids_bucket_enabled"):
            lines.append(row("kids", mrf.get("kids"), di))
        if mb.get("anime_policy") != "standard_only" and (mrf.get("anime") or rcat.get("anime")):
            lines.append(row("anime", mrf.get("anime"), rcat.get("anime") or di, f"({mb.get('anime_policy', 'dedicated')})"))
        fourk = rcat.get("4K") or rcat.get("4k")
        if fourk and fourk != di:
            lines.append(row("4k", mrf.get("4k"), fourk, f"({mb.get('4k_policy', 'highest_only')})"))
        lines.append("   Resolved TV routing:")
        lines.append(f"     {'series':<10} -> {si:<12} {rf.get('series') or '(your Sonarr default root folder)'}")
        if tv.get("anime_policy") == "series_type_plus_folder":
            lines.append(row("anime", rf.get("anime"), si, "(series_type_plus_folder)"))
        if tv.get("kids_bucket_enabled"):
            lines.append(row("kids", rf.get("kids"), si))
        mode = block.get("reorg_mode", "log_only")
        relo = "armed" if (mode == "same_instance" and cfg.get("relocation_consent")) else "off"
        lines.append(f"   Re-organizer: {mode}" + (f" (moves {relo})" if mode == "same_instance" else ""))
        prompter.notice("\n".join(lines))

        if missing:
            return StepResult("routing", ok=False,
                              detail=f"saved, but no folder for: {', '.join(missing)} (falls back). "
                                     f"Set them via --service routing or --service library.")
        if mode == "same_instance" and not cfg.get("relocation_consent"):
            return StepResult("routing", ok=False,
                              detail="same_instance chosen but relocation_consent off — moves stay off (log only)")
        return StepResult("routing", ok=(True if mode != "off" else None),
                          detail=f"reorg_mode={mode}; every bucket has a destination")
