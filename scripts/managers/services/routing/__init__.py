"""
services/routing/__init__.py — the in-run library re-organizer (RoutingManager).
================================================================================
Reconciles ALREADY-OWNED movies + shows to the correct library FOLDER when a late
signal (a Common Sense age arriving, anime detection, or a changed routing preference)
means a title's classified bucket no longer matches where it sits on disk.

Gated three ways, so it is inert until the operator opts in:
  • routing.configured — the routing onboarding step has run (else: skip entirely, so a
                         never-onboarded install does nothing).
  • routing.reorg_mode  — off (skip) / log_only (classify + LOG misplacements, move
                          NOTHING) / same_instance (actuate same-instance folder moves) /
                          cross_instance (a PEER mode — see below).
  • relocation_enabled  — same_instance ALSO requires explicit move consent; and even then
                          a dry_run never PUTs.

ON cross_instance — A PEER MODE, NOT A SUPERSET.
``reorg_mode`` is single-valued and the two actuating modes are mutually exclusive by
design (``routing_targets.py``: *"an install actuates EITHER same-instance folder moves
OR the cross-instance reconcile — not both at once"*). They move files along different
axes:

    same_instance   content bucket, one instance   kids / anime / standard   -> THIS manager
    cross_instance  resolution tier, two instances standard -> ultra (4K)    -> uhd_reconcile

So with ``cross_instance`` set, this manager still classifies and writes the full plan to
support/logs/routing.log, and moves nothing — that is correct, not a bug. Wanting both at
once means adding an ``all`` mode to ``_REORG_MODES``; it is not a matter of loosening the
test here, and ``relocation_enabled`` enforces the same exclusivity independently.

Classification + the move plan are the shared, pure ``library_router`` / ``library_classifier``
(identical to the add-time resolver, so add-time and re-org never disagree). This manager is
only the driver: fetch owned items via the engine's arr API, plan, then LOG or APPLY.
"""
from __future__ import annotations

from scripts.managers.machine_learning.classification import library_router
from scripts.managers.machine_learning.space.routing_targets import reorg_mode, relocation_enabled
from scripts.managers.services.mdblist import age_cache
from scripts.support.utilities.library_classifier import classify_movie, classify_show, is_anime_media


class RoutingManager:
    def __init__(self, config=None, logger=None, *, radarr=None, sonarr=None, dry_run=False, **kwargs):
        self.config = config or {}
        self.logger = logger
        self.dry_run = bool(dry_run)
        self._radarr_im = self._im(radarr)
        self._sonarr_im = self._im(sonarr)
        self._routing = self.config.get("routing", {}) or {}
        self._root_folders = self.config.get("rootFolders", {}) or {}
        self._movie_root_folders = self.config.get("movieRootFolders", {}) or {}
        self._anime_genres = {str(g).lower() for g in (self.config.get("animeGenres", []) or []) if g}
        self._kids_genres = [str(g) for g in (self.config.get("kidsGenres", []) or []) if g]
        self._kids_certs = [str(c) for c in (self.config.get("kidsCertifications", []) or []) if c]
        self._kids_networks = [str(n) for n in (self.config.get("kidsNetworks", []) or []) if n]
        self._reality_genres = [str(g) for g in (self.config.get("realityGenres", []) or []) if g]
        self._doc_genres = [str(g) for g in (self.config.get("documentaryGenres", []) or []) if g]
        self._news_genres = [str(g) for g in (self.config.get("newsGenres", []) or []) if g]
        self._preschool_genres = [str(g) for g in (self.config.get("preschoolGenres", []) or []) if g]
        self._non_kids_genres = [str(g) for g in (self.config.get("nonKidsGenres", []) or []) if g]
        # CSM AGE GATE (GLD-ROU-11). The oldest Common Sense Media recommended age that
        # still counts as "kids". A title rated OVER this is demoted OUT of the kids
        # bucket; a low or absent age never promotes one INTO it ("never trust Common
        # Sense alone" -- Star Trek: DS9 is CSM ~10 and is an adult drama).
        #
        # This was hard-pinned at the classifier's default of 11 because the router
        # never passed it, so the single most consequential number in kids routing was
        # invisible to the operator and unchangeable without editing library_classifier.
        # It is a HOUSEHOLD decision -- it depends on the actual children -- so it
        # belongs in config beside the other kids_* keys.
        #
        # Out-of-range or non-numeric falls back to the classifier default rather than
        # clamping silently: a typo that widened the gate would put age-inappropriate
        # content in front of a child, so an unusable value must not be quietly coerced
        # into a usable one.
        self._kids_age_max = self._read_kids_age_max()
        self._movie_ages = None       # CSM caches, lazy-loaded once
        self._show_ages = None

    #: Bounds for ``kidsAgeMax``. 2 is the youngest CSM rating in practice; 17 is the
    #: point above which "kids" is meaningless. A value outside this is treated as a
    #: mistake, not an intention.
    _KIDS_AGE_MIN, _KIDS_AGE_MAX_CEIL, _KIDS_AGE_DEFAULT = 2, 17, 11

    def _read_kids_age_max(self) -> int:
        """The CSM age ceiling, read from where ONBOARDING writes it.

        ``plex.playlists.kids_age_max`` is the canonical location: the onboarding
        schema asks for it there, beside ``profile_ages``, because both answer the
        same household question and a parent setting up their children's profiles is
        the person who knows. A top-level ``kidsAgeMax`` is accepted as an alias so a
        hand-edited config keeps working.

        Reading only one of the two would make the other a value that is written and
        never consumed (P-A) — and on a parental control that failure is silent and
        looks exactly like the setting having no effect.
        """
        raw = (((self.config.get("plex") or {}).get("playlists") or {})
               .get("kids_age_max"))
        if raw is None:
            raw = self.config.get("kidsAgeMax")
        if raw is None:
            return self._KIDS_AGE_DEFAULT
        try:
            val = int(raw)
        except (TypeError, ValueError):
            self._log("log_warning",
                      f"[Routing] kidsAgeMax={raw!r} is not a number — using the default "
                      f"{self._KIDS_AGE_DEFAULT}. Kids routing is age-gated, so an unreadable "
                      f"value is NOT silently accepted.")
            return self._KIDS_AGE_DEFAULT
        if not (self._KIDS_AGE_MIN <= val <= self._KIDS_AGE_MAX_CEIL):
            self._log("log_warning",
                      f"[Routing] kidsAgeMax={val} is outside {self._KIDS_AGE_MIN}-"
                      f"{self._KIDS_AGE_MAX_CEIL} — using the default {self._KIDS_AGE_DEFAULT}. "
                      f"A too-high gate would route age-inappropriate titles into the kids "
                      f"library, so the value is refused rather than clamped.")
            return self._KIDS_AGE_DEFAULT
        return val

    @staticmethod
    def _im(mgr):
        """The instance-manager / arr-api off a service manager (exposes _get_apis + _make_request)."""
        if mgr is None:
            return None
        return (getattr(mgr, "instance_manager", None) or getattr(mgr, "radarr_api", None)
                or getattr(mgr, "sonarr_api", None))

    def _log(self, level, msg):
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    # ── entry ─────────────────────────────────────────────────────────────────
    def run(self):
        if not self._routing.get("configured"):
            # SILENT NO-OP WAS THE DEFECT (GLD-ROU-10). A never-onboarded install
            # must do nothing -- that part is right. But an operator who has SET an
            # actuating reorg_mode, granted relocation consent AND disabled dry_run
            # has stated an intention, and returning here without a word means the
            # config contradicts itself in silence. Measured: `reorg_mode` sat at
            # `all` with every consent true for hours across two full runs, and the
            # router never ran once -- no routing output at all, and relocation.log
            # untouched for two days. The 121 same-instance misplacements were not
            # merely unactuated, they were never even classified.
            #
            # Every OTHER refusal on this path announces itself (log_only logs the
            # plan; a consent miss logs the plan and names the missing consent), so
            # this was the one exit that told the operator nothing. Warn ONCE, and
            # only when the config is self-contradictory -- a genuinely un-onboarded
            # install with no consents set stays silent, exactly as before.
            try:
                # `relocation_enabled` is the single source of truth for "this mode
                # actuates AND consent is granted" (GLD-ROU-07 made it so, precisely
                # to stop two places disagreeing). Reusing it here means this warning
                # can never drift from the gate it is describing.
                if relocation_enabled(self.config):
                    self._log("log_warning",
                              f"[Routing] SKIPPED entirely: reorg_mode is "
                              f"'{reorg_mode(self.config)}' and relocation consent is granted, "
                              f"but routing.configured is not set — the routing onboarding step "
                              f"has never run, so nothing is classified or moved. Set "
                              f"routing.configured=true to act on it.")
            except Exception:
                pass                            # a diagnostic must never cost the run
            return                              # never-onboarded → today's behaviour (nothing)
        mode = reorg_mode(self.config)
        if mode == "off":
            return
        # The full per-title plan can be thousands of lines, so it goes to a DEDICATED file
        # (support/logs/routing.log) instead of flooding the run log/console - the main log gets
        # only a per-instance count. Fresh plan each run.
        #
        # The header states this manager's EFFECTIVE capability, not just the mode string. A
        # header reading "relocation plan" above hundreds of move-shaped lines, in a mode where
        # this manager moves nothing, is the kind of log that gets trusted and shouldn't be.
        _armed = relocation_enabled(self.config) and not self.dry_run
        if _armed:
            _cap = "same-instance folder moves ARMED"
        elif not relocation_enabled(self.config):
            _cap = ("CLASSIFY-ONLY - nothing below will move. To actuate: "
                    "routing.reorg_mode = same_instance (or all) AND relocation_consent = true")
        else:
            _cap = "CLASSIFY-ONLY - dry run (or backup gate disarmed); nothing below will move"
        self._detail(f"==== routing plan - SAME-INSTANCE folder moves (mode={mode}) ====", reset=True)
        self._detail(f"     {_cap}")
        self._detail("     Cross-INSTANCE moves (standard -> ultra 4K) are a different axis, "
                     "handled by uhd_reconcile - see support/logs/relocation.log.")
        self._reorg(is_show=False, im=self._radarr_im, get_ep="movie",
                    put_ep="movie/editor", id_key="movieIds", mode=mode)
        self._reorg(is_show=True, im=self._sonarr_im, get_ep="series",
                    put_ep="series/editor", id_key="seriesIds", mode=mode)

    def _detail(self, msg, *, reset=False):
        """Write a per-title relocation line to the dedicated routing log file (NOT the main run
        log). No-op when the logger lacks the file sink (e.g. a None logger in tests)."""
        if self.logger and hasattr(self.logger, "log_to_file"):
            self.logger.log_to_file("routing", msg, reset=reset)

    # ── per-service ───────────────────────────────────────────────────────────
    def _reorg(self, *, is_show, im, get_ep, put_ep, id_key, mode):
        if im is None or not hasattr(im, "_get_apis") or not hasattr(im, "_make_request"):
            return
        classify = self._classifier(is_show)
        anime_media_fn = (lambda it: self._anime_media(it)) if is_show else None
        # This axis needs BOTH the mode and relocation_consent, and a live (non-dry) run.
        # relocation_enabled() is the SINGLE source of truth for the first two -- the old
        # code re-tested `mode == "same_instance"` alongside it, which meant two places had
        # to agree about which modes actuate. Widening one and not the other was a live
        # hazard; there is now one gate, in one place.
        apply = relocation_enabled(self.config) and not self.dry_run
        for name in list((im._get_apis() or {}).keys()):
            try:
                items = im._make_request(name, get_ep, fallback=[]) or []
            except Exception as e:
                self._log("log_warning", f"[Routing] {get_ep} fetch failed for '{name}': {e}")
                continue
            # The configured folder maps are GLOBAL, but this instance owns only
            # some roots. Fetch its real ones so plan_moves can refuse a
            # cross-library target (the 4K instance being told to move a
            # kids-classified film into the 1080p kids root).
            #
            # FAILS TOWARD INACTION. A failed rootfolder fetch used to leave the set
            # empty, which DISABLED THE GUARD rather than the pass -- so a transient API
            # error let same-instance moves proceed with the cross-root check switched
            # off, in the one place that MOVES FILES. The two outcomes are not
            # symmetric: skipping the pass costs one run of re-organisation (the plan is
            # still logged, and the next run redoes it), while moving without the guard
            # can put a kids film in the 4K instance's root, where its audience cannot
            # reach it and only a manual move gets it back. Per the fail-direction rule
            # (machine_learning/discovery), on unknown input prefer the outcome that
            # changes nothing.
            allowed_roots = set()
            try:
                allowed_roots = {
                    library_router._norm(r.get("path"))
                    for r in (im._make_request(name, "rootfolder", fallback=[]) or [])
                    if isinstance(r, dict) and r.get("path")
                }
            except Exception as e:
                self._log("log_debug", f"[Routing] {name}: root-folder list "
                                       f"unavailable, cross-root guard off: {e}")
            if apply and not allowed_roots:
                self._log("log_warning",
                          f"[Routing] {name}: root-folder list unavailable, so the cross-root "
                          f"guard cannot run -- PLANNING ONLY this run rather than moving files "
                          f"unguarded. The plan is still written to support/logs/routing.log; "
                          f"the next run with a readable root list will apply it.")
                apply_here = False
            else:
                apply_here = apply
            plans = library_router.plan_moves(
                items, is_show=is_show, routing=self._routing,
                root_folders=self._root_folders, movie_root_folders=self._movie_root_folders,
                classify=classify, anime_media=anime_media_fn,
                allowed_roots=allowed_roots)
            if not plans:
                continue
            kind = "show" if is_show else "movie"
            self._log("log_info", f"[Routing] {name}: {len(plans)} {kind}(s) misplaced "
                                  f"({'applying same-instance moves' if apply_here else 'log only'}) "
                                  f"— full plan in support/logs/routing.log")
            self._detail(f"-- {name} ({kind}s): {len(plans)} misplaced --")
            for p in plans:
                self._detail(f"[{name}] {p['title']}: {p['current_root'] or '?'} -> "
                             f"{p['target_root'] or '(stay)'}  [{p['reason']}]")
            if apply_here:
                self._apply(im, name, put_ep, id_key, plans, is_show)

    def _apply(self, im, name, put_ep, id_key, plans, is_show):
        groups: dict = {}
        for p in plans:
            if p.get("id") is None:
                continue
            groups.setdefault((p.get("target_root"), p.get("new_series_type")), []).append(p["id"])
        for (target, stype), ids in groups.items():
            payload = {id_key: ids, "moveFiles": bool(target)}
            if target:
                payload["rootFolderPath"] = target
            if is_show and stype:
                payload["seriesType"] = stype
            try:
                im._make_request(name, put_ep, method="PUT", payload=payload)
                self._log("log_success", f"[Routing] {name}: relocated {len(ids)} -> {target or '(seriesType only)'}")
            except Exception as e:
                self._log("log_warning", f"[Routing] {name}: editor batch failed ({len(ids)}): {e}")

    # ── classification helpers (mirror the add-time resolver) ─────────────────
    def _movie_age(self, tmdb):
        if self._movie_ages is None:
            self._movie_ages = age_cache.load(age_cache.AGE_CACHE_PATH)
        return age_cache.age_for(tmdb, cache=self._movie_ages)

    def _show_age(self, tmdb):
        if self._show_ages is None:
            self._show_ages = age_cache.load(age_cache.TV_AGE_CACHE_PATH)
        return age_cache.age_for(tmdb, cache=self._show_ages)

    @staticmethod
    def _olang(it):
        ol = it.get("originalLanguage")
        return ol.get("name") if isinstance(ol, dict) else ol

    def _classifier(self, is_show):
        """A ``classify(item) -> category`` closure over the live arr object, matching the
        resolver's classify call (CSM-primary). is_uhd is left False — the same-instance
        re-organizer routes by content; the 4K/anime INSTANCE split is the deferred path."""
        if is_show:
            def classify(it):
                return classify_show(
                    genres=it.get("genres"), certification=it.get("certification"),
                    series_type=it.get("seriesType"), original_language=self._olang(it),
                    network=it.get("network"),
                    recommended_age=self._show_age(it.get("tmdbId")),
                    kids_age_max=self._kids_age_max,
                    anime_genres=self._anime_genres, kids_genres=self._kids_genres,
                    kids_certs=self._kids_certs, kids_networks=self._kids_networks,
                    reality_genres=self._reality_genres,
                    documentary_genres=self._doc_genres, news_genres=self._news_genres,
                    preschool_genres=self._preschool_genres,
                    non_kids_genres=self._non_kids_genres)
            return classify

        def classify(it):
            return classify_movie(
                genres=it.get("genres"), certification=it.get("certification"),
                original_language=self._olang(it), studio=it.get("studio"),
                recommended_age=self._movie_age(it.get("tmdbId")), is_uhd=False,
                kids_age_max=self._kids_age_max,
                anime_genres=self._anime_genres, kids_genres=self._kids_genres,
                kids_certs=self._kids_certs, preschool_genres=self._preschool_genres,
                non_kids_genres=self._non_kids_genres)
        return classify

    def _anime_media(self, it):
        return is_anime_media(
            genres=it.get("genres"), series_type=it.get("seriesType"),
            original_language=self._olang(it), anime_genres=self._anime_genres,
            studio=it.get("network"))
