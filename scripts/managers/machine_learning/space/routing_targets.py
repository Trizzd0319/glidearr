"""
routing_targets.py — gating for the library re-organizer (file relocation).
================================================================================
The re-organizer reclassifies owned media and can MOVE files between root folders
(same-instance) and, eventually, between *arr instances (cross-instance). Moving
files on disk is destructive-adjacent — a mid-move failure can leave split state —
so actuation is gated exactly like deletion: an explicit operator consent flag PLUS
an explicit mode that turns actuation on. This module is the single source of truth
for those gates (pure config reads, mirroring ``space_targets``).

    routing.reorg_mode:
        "off"           → the re-organizer does nothing.
        "log_only"      → classify owned media + LOG misplacements; move NOTHING.
                          (default — safe, non-destructive, needs no consent.)
        "same_instance" → actuate same-instance root-folder moves; cross-instance
                          candidates are still only logged.

    relocation_consent: explicit "yes, move my files" opt-in (off by default).

``relocation_enabled`` requires BOTH consent AND ``reorg_mode == "same_instance"``,
so an install can never relocate a file without an informed opt-in. Cross-instance
migration is deferred and stays log-only regardless of this gate.
"""
from __future__ import annotations

import os

# Mirror the deletions consent env-var pattern (space_targets._CONSENT_ENV_VARS).
_CONSENT_ENV_VARS = ("RECOMMENDARR_RELOCATION_CONSENT", "GLIDEARR_RELOCATION_CONSENT")
# Cross-instance reconcile carries TWO further, independent consents (FORK 1 + FORK 4): physically
# relocating a 2160p file standard→4K (a MOVE), and reclaiming the worse copy when both instances own
# the same title (a DELETE). Each is its own informed opt-in, separate from the same-instance
# folder-move consent above, so an operator can arm one cross-instance behaviour without the other.
_MOVE_CONSENT_ENV_VARS = ("RECOMMENDARR_CROSS_INSTANCE_MOVE_CONSENT", "GLIDEARR_CROSS_INSTANCE_MOVE_CONSENT")
_DEDUP_CONSENT_ENV_VARS = ("RECOMMENDARR_CROSS_INSTANCE_DEDUP_CONSENT", "GLIDEARR_CROSS_INSTANCE_DEDUP_CONSENT")
_CONSENT_TRUTHY = {"1", "true", "yes", "on", "y"}

# "cross_instance" is a PEER mode to "same_instance" (FORK 1A): it un-conflates moving a file BETWEEN
# *arr instances from moving a file between root folders on ONE instance. reorg_mode is single-valued,
# so an install actuates EITHER same-instance folder moves OR the cross-instance reconcile — not both
# at once (a future "all" mode could lift that if ever needed).
_REORG_MODES = ("off", "log_only", "same_instance", "cross_instance")
DEFAULT_REORG_MODE = "log_only"


def _env_consent(env_vars, config, config_key) -> bool:
    """Shared consent reader: a non-empty env var (truthy/falsey) overrides config; otherwise the
    config flag, default False. Mirrors :func:`relocation_consented` for the cross-instance consents."""
    for var in env_vars:
        raw = os.environ.get(var)
        if raw is not None and raw.strip() != "":
            return raw.strip().lower() in _CONSENT_TRUTHY
    return bool(_cfg_get(config, config_key, False))


def _cfg_get(config, key, default):
    """Read a key from a ConfigManager OR a plain dict OR None (mirrors space_targets)."""
    if config is None:
        return default
    try:
        return config.get(key, default)
    except Exception:
        return default


def relocation_consented(config) -> bool:
    """Explicit operator consent to MOVE owned media files on disk — the informed-consent
    switch for the re-organizer, separate from which moves it plans. Captured during
    onboarding (the 'routing' step, which explains that files are physically relocated and
    Plex must re-scan) or via the ``RECOMMENDARR_RELOCATION_CONSENT`` /
    ``GLIDEARR_RELOCATION_CONSENT`` env var (headless / Docker). Defaults to False — no file
    is moved until the operator has opted in. A non-empty env var overrides config, so a
    container can force consent on (=true) or off (=false) regardless of config.json. Mirrors
    ``space_targets.deletions_consented`` exactly."""
    for var in _CONSENT_ENV_VARS:
        raw = os.environ.get(var)
        if raw is not None and raw.strip() != "":
            return raw.strip().lower() in _CONSENT_TRUTHY
    return bool(_cfg_get(config, "relocation_consent", False))


def reorg_mode(config) -> str:
    """The re-organizer mode: ``off`` | ``log_only`` | ``same_instance``. Reads
    ``routing.reorg_mode``; anything unrecognised (or missing) falls back to the safe
    default ``log_only`` (classify + log, never move)."""
    routing = _cfg_get(config, "routing", None) or {}
    try:
        mode = str(routing.get("reorg_mode", DEFAULT_REORG_MODE)).strip().lower()
    except Exception:
        return DEFAULT_REORG_MODE
    return mode if mode in _REORG_MODES else DEFAULT_REORG_MODE


def relocation_enabled(config) -> bool:
    """HARD SAFETY GATE for moving owned files on disk. BOTH are required before the
    re-organizer may relocate any file:
      1. ``reorg_mode == "same_instance"`` (the operator turned actuation on), AND
      2. explicit operator consent (``relocation_consented`` — onboarding/env opt-in).
    With either missing, the re-organizer may still classify and LOG misplacements
    (``log_only``) but must never move a file."""
    return reorg_mode(config) == "same_instance" and relocation_consented(config)


def cross_instance_move_consented(config) -> bool:
    """Explicit operator consent to physically MOVE a movie's file from one *arr instance to another
    (e.g. relocate a 2160p file off the standard instance onto the dedicated 4K instance). Separate
    from :func:`relocation_consented` (same-instance folder moves) so the two can be armed apart.
    Captured during onboarding (the routing step) or the
    ``RECOMMENDARR_/GLIDEARR_CROSS_INSTANCE_MOVE_CONSENT`` env var (headless). Default False."""
    return _env_consent(_MOVE_CONSENT_ENV_VARS, config, "cross_instance_move_consent")


def cross_instance_dedup_consented(config) -> bool:
    """Explicit operator consent to RECLAIM the redundant copy when BOTH instances own the same title
    — the lower-quality copy's FILE is deleted (its Radarr record is kept). This is a DELETION, so it
    carries its own consent on top of the move consent. Captured during onboarding or the
    ``RECOMMENDARR_/GLIDEARR_CROSS_INSTANCE_DEDUP_CONSENT`` env var. Default False."""
    return _env_consent(_DEDUP_CONSENT_ENV_VARS, config, "cross_instance_dedup_consent")


def cross_instance_move_enabled(config) -> bool:
    """HARD GATE for actuating a cross-instance FILE MOVE. BOTH required:
      1. ``reorg_mode == "cross_instance"`` (the operator armed the cross-instance reconcile), AND
      2. :func:`cross_instance_move_consented` (explicit move opt-in).
    With either missing the reconcile may still classify + LOG move candidates, but moves nothing.
    The backup gate (degrade-to-dry-run) and a shared-storage pre-flight are enforced separately at
    actuation time. Default False — existing installs unchanged."""
    return reorg_mode(config) == "cross_instance" and cross_instance_move_consented(config)


def cross_instance_dedup_enabled(config) -> bool:
    """HARD GATE for actuating cross-instance DEDUP (reclaim the worse of two copies). BOTH required:
      1. ``reorg_mode == "cross_instance"`` (the cross-instance reconcile is armed), AND
      2. :func:`cross_instance_dedup_consented` (explicit dedup/delete opt-in).
    Because dedup deletes a file, the actuator additionally honours the backup gate
    (``effective_dry_run``) — a real run whose backup pre-flight failed reclaims nothing. Same-path
    duplicates (two records, one physical file) are NEVER auto-acted regardless of this gate. Default
    False — existing installs unchanged."""
    return reorg_mode(config) == "cross_instance" and cross_instance_dedup_consented(config)


def proactive_4k_enabled(config) -> bool:
    """HARD GATE for the proactive-4K dual-version behaviour: (a) give ANY owned movie whose
    watch-likelihood warrants 4K a copy on the 4K instance, and (b) CAP the standard-instance
    quality upgrade so it never bumps that title to 4K on standard (otherwise the two paths
    double-grab the same 2160p). Requires ``routing.movies.proactive_4k`` AND
    ``routing.movies.4k_policy == "both"`` AND a move-actuation gate — EITHER ``relocation_enabled``
    (same_instance) OR ``cross_instance_move_enabled`` (cross_instance). Tying it to a move gate is
    deliberate: the standard upgrade cap and the 4K-instance acquire MUST move together, so the
    standard 4K upgrade is never disabled without the 4K-instance replacement actually being
    actuated. Default OFF (existing installs unchanged)."""
    routing = _cfg_get(config, "routing", None) or {}
    if not isinstance(routing, dict):
        return False
    mv = routing.get("movies", {}) or {}
    if not isinstance(mv, dict) or not mv.get("proactive_4k") or mv.get("4k_policy") != "both":
        return False
    return relocation_enabled(config) or cross_instance_move_enabled(config)


_SHARED_STORAGE_MODES = ("auto", "true", "false")
DEFAULT_SHARED_STORAGE_MODE = "auto"


def shared_storage_mode(config) -> str:
    """How the dual-version acquire gets the 4K instance its 2160p — a hardlink-RELOCATE of the
    standard instance's EXISTING file (no re-download) vs a fresh DOWNLOAD. Reads
    ``routing.movies.shared_storage``:
      • ``auto`` (default) — probe (``shared_storage_confirmed``: common mount ancestor + equal
        backing capacity, then a per-title 'can the 4K instance actually see the file' check) and
        relocate only when both instances share one filesystem; otherwise download.
      • ``true``  — force relocate (instances KNOWN to share storage; skip the coarse probe, the
        per-title visibility check in the actuator still applies and falls back to download if it
        can't see the file).
      • ``false`` — always download (the portable default for households on separate storage).
    Anything unrecognised falls back to ``auto``."""
    routing = _cfg_get(config, "routing", None) or {}
    if not isinstance(routing, dict):
        return DEFAULT_SHARED_STORAGE_MODE
    mv = routing.get("movies", {}) or {}
    if not isinstance(mv, dict):
        return DEFAULT_SHARED_STORAGE_MODE
    try:
        mode = str(mv.get("shared_storage", DEFAULT_SHARED_STORAGE_MODE)).strip().lower()
    except Exception:
        return DEFAULT_SHARED_STORAGE_MODE
    return mode if mode in _SHARED_STORAGE_MODES else DEFAULT_SHARED_STORAGE_MODE


def transcode_gate_enabled(config) -> bool:
    """Gate for the transcode/remote-play capability check on the 4K BONUS copy: only acquire
    the 2160p companion when a likely household device can DIRECT-PLAY it (else the 4K would
    just force a transcode and the 1080p baseline already covers playback). Requires
    ``routing.movies.transcode_gate`` AND ``4k_policy == "both"`` (the gate only affects the
    dual-version 4K add, the one place ``can_remote_play`` is consumed). DELIBERATELY independent
    of relocation/move consent: this gate only SUPPRESSES an acquire, it never moves a file, so
    it carries no move-actuation dependency (unlike ``proactive_4k_enabled``). Default OFF — with
    it off ``can_remote_play`` stays the hardcoded True and 4K behaviour is byte-for-byte unchanged."""
    routing = _cfg_get(config, "routing", None) or {}
    if not isinstance(routing, dict):
        return False
    mv = routing.get("movies", {}) or {}
    if not isinstance(mv, dict):
        return False
    return bool(mv.get("transcode_gate")) and mv.get("4k_policy") == "both"


def uhd_remote_play_ok(config, fingerprint_records, platform_weights, *, hdr: bool = False) -> bool:
    """Should the 4K BONUS copy be acquired given the household's transcode habits? The single
    wiring authority shared by the add-time resolver and the proactive reconcile, so they decide
    IDENTICALLY (computing it in only one place would split add-time vs reconcile behaviour).

    Returns ``True`` — no change — when the transcode gate is OFF (``transcode_gate_enabled``).
    When ON, it rebuilds the cached capability matrix and asks the ``can_remote_play`` policy
    authority whether a likely device can direct-play a representative 2160p HEVC file (the codec
    and resolution the 4K bonus lands in; the file's exact audio/HDR are unknown until grab, and
    the predictor's graded fallback coarsens those axes away). ``hdr`` lets a caller that knows the
    candidate is HDR ask the tone-mapping-aware cell; the default SDR read leans on the dominant
    codec signal. Inputs are PASSED IN (the caller reads ``tautulli/transcode_fingerprint`` and
    ``tautulli/platforms``), so this stays pure and unit-testable."""
    if not transcode_gate_enabled(config):
        return True
    from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
        can_remote_play, deserialize_fingerprint_matrix, source_fingerprint,
    )
    matrix = deserialize_fingerprint_matrix(fingerprint_records)
    fp = source_fingerprint(video_codec="hevc", height=2160, hdr=hdr, location="unknown")
    return can_remote_play(matrix, fp, platform_weights or {})


def evict_uhd_first(config) -> bool:
    """Gate for evicting dual-version 4K BONUS copies FIRST under space pressure — each has a
    surviving 1080p baseline on the standard instance, so reclaiming it loses no title (pure
    reclaim) and it should go before any whole title. Requires ``routing.movies.evict_uhd_first``
    AND ``4k_policy == "both"`` AND the cross-service coordinator owning deletion
    (``coordinator_owns_deletion`` — space_coordinator_enabled + free_space_limit). DELIBERATELY
    independent of relocation/move consent: eviction is a DELETION path with its own consent (the
    space floor), NOT a file move. Default OFF (existing installs unchanged)."""
    from scripts.managers.machine_learning.space.space_targets import coordinator_owns_deletion
    routing = _cfg_get(config, "routing", None) or {}
    if not isinstance(routing, dict):
        return False
    mv = routing.get("movies", {}) or {}
    if not isinstance(mv, dict) or not mv.get("evict_uhd_first") or mv.get("4k_policy") != "both":
        return False
    return coordinator_owns_deletion(config)


def demote_4k_on_watchability_enabled(config) -> bool:
    """Gate for the WATCHABILITY-driven demote of dual-version 4K BONUS copies — the
    pressure-INDEPENDENT companion to :func:`evict_uhd_first` (which only fires under disk
    pressure). When a title's saga-aware watch-likelihood falls below the UHD threshold, its
    4K FILE is deleted + its 4K record unmonitored — but ONLY while a 1080p baseline file
    SURVIVES on a standard-tier instance (never the last copy), and never for a keep/universe
    pin. The 4K record is kept (fileless), so the proactive acquire / recover path re-adds the
    companion if the score climbs back. Requires ``routing.movies.demote_4k_on_watchability``
    AND ``4k_policy == "both"`` (it only touches the dual-version bonus) AND explicit deletion
    consent (``deletions_consented`` — it deletes a file). DELIBERATELY independent of
    ``free_space_limit`` / the coordinator: the whole point is to demote on watchability even
    when space is fine (space pressure is still handled by ``evict_uhd_first``). The backup gate
    (``effective_dry_run``) is enforced at actuation time. Default OFF."""
    from scripts.managers.machine_learning.space.space_targets import deletions_consented
    routing = _cfg_get(config, "routing", None) or {}
    if not isinstance(routing, dict):
        return False
    mv = routing.get("movies", {}) or {}
    if not isinstance(mv, dict) or not mv.get("demote_4k_on_watchability") or mv.get("4k_policy") != "both":
        return False
    return deletions_consented(config)


def rehome_4k_only_enabled(config) -> bool:
    """Gate for FORK-D: rehome a cold 4K-ONLY film (2160p on the dedicated 4K instance with
    NO 1080p baseline on standard) down to a watchability-matched (≤1080p) copy on the
    standard instance, then defer-evict the 4K copy only AFTER the standard copy imports.
    Requires ``routing.movies.rehome_4k_only`` AND the cross-service coordinator owning
    deletion (``coordinator_owns_deletion`` — space_coordinator_enabled + consent +
    free_space_limit). Unlike :func:`evict_uhd_first` it does NOT require dual-version
    ``4k_policy == "both"``: its precondition is a SPLIT 4K instance, checked at runtime via
    ``_uhd_instance``. This is an acquisition+deletion path governed by the space-floor
    consent, NOT a file move — independent of relocation consent. Default OFF."""
    from scripts.managers.machine_learning.space.space_targets import coordinator_owns_deletion
    routing = _cfg_get(config, "routing", None) or {}
    if not isinstance(routing, dict):
        return False
    mv = routing.get("movies", {}) or {}
    if not isinstance(mv, dict) or not mv.get("rehome_4k_only"):
        return False
    return coordinator_owns_deletion(config)


# ── category-aware 4K roots, and the kids-visibility gate ──────────────────────────
#
# THE PROBLEM THESE SOLVE. ``classify_movie`` puts CONTENT above RESOLUTION on purpose:
# its order is anime -> kids -> 4k -> standard, so a 2160p Pixar film classifies as
# "kids", never "4k" ("CONTENT WINS: a 4K kids/anime film routes to kids/anime, so the 4k
# library holds only non-kids, non-anime UHD movies"). The dual-version reconcile does NOT
# honour that: it relocates every 4K companion into ONE 4K root. A kids film upgraded to
# 2160p therefore leaves the Kids library, and a child's Plex profile — which only has
# access to Kids — can no longer see its own film.
#
# The fix is per-category roots on the 4K instance (``routing.movies.uhd_root_folders``),
# mirroring ``movieRootFolders`` on the standard side, so /4k/kids can be added to the
# EXISTING Kids Plex library and nothing has to traverse libraries.

def uhd_root_folders(config) -> dict:
    """Per-CATEGORY root folders on the 4K instance, e.g.::

        "routing": {"movies": {"uhd_root_folders": {
            "kids":     "/data/media/movies/4k/kids",
            "anime":    "/data/media/movies/4k/anime",
            "standard": "/data/media/movies/4k"}}}

    Empty (the default) means the reconcile keeps today's single-root behaviour — existing
    installs unchanged. Keys are the ``classify_movie`` categories, so the same category a
    title has on the standard instance decides its 4K root too.
    """
    routing = _cfg_get(config, "routing", None) or {}
    if not isinstance(routing, dict):
        return {}
    mv = routing.get("movies", {}) or {}
    if not isinstance(mv, dict):
        return {}
    roots = mv.get("uhd_root_folders") or {}
    return roots if isinstance(roots, dict) else {}


def _norm_path(p) -> str:
    """Case-folded, forward-slashed, trailing-slash-stripped. Plex reports library paths as
    the container sees them, which may differ from the *arr root in separator style or case
    (bind mounts, Windows hosts, SMB), so a raw string compare is too brittle for a gate
    whose failure hides a child's film."""
    s = str(p or "").strip().replace("\\", "/").rstrip("/")
    return s.casefold()


def _path_within(child, parent) -> bool:
    """True when *child* is *parent* or sits beneath it. Compares whole segments, so
    ``/movies/4k-adult`` is NOT treated as inside ``/movies/4k``."""
    c, p = _norm_path(child), _norm_path(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + "/")


#: Categories whose titles live in their OWN Plex library rather than the general movie
#: shelf. For these, sending a 4K companion to the flat 4K root moves it OUT of the library
#: its audience browses -- so they get the visibility check below. "standard" is absent on
#: purpose: the flat 4K root IS where a standard-category 4K copy belongs.
CONTENT_CATEGORIES: tuple[str, ...] = ("kids", "anime")

#: Alias-aware labels that identify the dedicated 4K/UHD Radarr instance.
#:
#: SINGLE SOURCE OF TRUTH. This tuple was duplicated verbatim in
#: ``services/routing/uhd_reconcile.py`` and ``services/radarr/repair/anomaly.py``, the
#: second carrying the comment "MUST match UhdReconcileManager._UHD_LABELS" in capitals --
#: a requirement stated in prose with nothing enforcing it. If the two ever drifted, the
#: monitored-missing triage and the dual-version reconcile would disagree about WHICH
#: RADARR SESSION IS THE 4K ONE, silently and in opposite directions: one routing titles to
#: an instance the other does not consider 4K at all.
#:
#: The aliases exist because the vocabulary is genuinely inconsistent upstream -- the role
#: map writes "4K" while the folder bucket is "4k", and operators reasonably use "uhd" or
#: "2160p". Accepting all of them means a casing or naming choice never silently disables
#: the 4K path.
UHD_INSTANCE_LABELS: tuple[str, ...] = ("4K", "4k", "uhd", "UHD", "2160p", "2160")


def category_uhd_root(config, category: str) -> str:
    """The 4K-instance root folder for *category*, or "" when none is configured."""
    return str(uhd_root_folders(config).get(str(category or "")) or "").strip()


def kids_uhd_root(config) -> str:
    """The 4K-instance root folder for KIDS movies. Thin alias over
    :func:`category_uhd_root` — kids is the case with real access consequences, so it keeps a
    named accessor."""
    return category_uhd_root(config, "kids")


def category_uhd_visible(config, category: str, library_paths) -> bool:
    """Is *category*'s configured 4K root inside one of the given Plex library locations?

    ``library_paths`` is the on-disk locations of the libraries that serve this category (the
    caller reads them — the Plex service knows which sections those are; this stays pure).
    Returns False when no root is configured or none of the paths cover it.

    This is the VERIFICATION half. Use :func:`category_uhd_allowed` for the decision — it
    also handles "the paths could not be read at all", which is a different answer from
    "read them and found no match".
    """
    root = category_uhd_root(config, category)
    if not root:
        return False
    for lib in (library_paths or ()):
        if _path_within(root, lib):
            return True
    return False


def kids_uhd_visible(config, kids_library_paths) -> bool:
    """Is the configured kids 4K root inside a Plex library a kid profile reaches?"""
    return category_uhd_visible(config, "kids", kids_library_paths)


def plex_sections_covering(sections, path, *, media_type: str = "movie") -> list:
    """The Plex sections whose on-disk locations CONTAIN *path*.

    ``sections`` is the ``plex/sections`` inventory written by
    ``PlexLibrarySectionsManager.run()`` — a dict keyed by section id::

        {"3": {"title": "Kids Movies", "type": "movie",
               "locations": ["/data/media/movies/kids", ...]}}

    Returns the matching section dicts (each with its ``title`` and ``locations``), or []
    when none cover the path. Filtered by ``media_type`` so a "Kids TV" show library never
    answers a question about a MOVIE root.
    """
    out = []
    if not isinstance(sections, dict) or not path:
        return out
    for sec in sections.values():
        if not isinstance(sec, dict):
            continue
        if media_type and str(sec.get("type") or "").strip().lower() != media_type:
            continue
        for loc in (sec.get("locations") or []):
            if _path_within(path, loc):
                out.append(sec)
                break
    return out


def category_library_paths(sections, category_root, *, media_type: str = "movie"):
    """Every location of the Plex libraries that already hold *category_root*.

    THIS IS HOW THE CATEGORY'S LIBRARY IS IDENTIFIED — by the folder attached to it, not by
    its name. The operator's existing standard-side root (``movieRootFolders["kids"]``) is
    already inside exactly one Plex library; that library IS the kids library, whatever it
    happens to be called. Matching on section TITLES ("kid"/"child"/"family") was a guess
    that breaks on any local naming — "Little Ones", "Family Movies (4K)", a non-English
    install — and breaks silently, by capping content it should have allowed.

    Returns the union of those libraries' locations, so a caller can then ask whether some
    OTHER path (the 4K root) is in the same library. Returns:

        None  the inventory is unreadable/cold          -> caller reports "unverifiable"
        []    readable, but nothing holds category_root -> the category's own movies are not
              in any Plex movie library, which is itself worth surfacing
    """
    if not isinstance(sections, dict) or not sections:
        return None
    if not category_root:
        return []
    paths = []
    for sec in plex_sections_covering(sections, category_root, media_type=media_type):
        paths.extend(str(p) for p in (sec.get("locations") or []) if p)
    return paths


def category_library_titles(sections, category_root, *, media_type: str = "movie") -> list:
    """Titles of the Plex libraries holding *category_root* — for operator-facing messages
    ("added to 'Kids Movies'"), so a log or an onboarding prompt can name the library rather
    than describe it."""
    return [str(s.get("title") or "?")
            for s in plex_sections_covering(sections, category_root, media_type=media_type)]


def category_uhd_allowed(config, category: str, library_paths=None) -> tuple[bool, str]:
    """May a title in *category* be given a 2160p companion? Returns ``(allowed, reason)``.

    The single authority for the question, so the add-time resolver and the proactive
    reconcile answer it IDENTICALLY (computing it twice is how add-time and reconcile drift
    apart — cf. the shared classify/plan_moves split).

    Categories outside :data:`CONTENT_CATEGORIES` are always allowed: a standard-category 4K
    copy belongs in the flat 4K root, so there is nothing to verify.

    For a content category, THREE states — all three CAP unless the root is positively
    confirmed reachable. "We checked and it is wrong" and "we could not check" are still
    reported separately, because they want different fixes:

      no root configured                   -> (False, "unconfigured")
            The DEFAULT. Nothing promises a 4K copy would stay in the right library, so cap
            at 1080p. Silent — this is the ordinary single-root install.

      configured, category's OWN root not in any Plex movie library -> (False, "category-not-in-plex")
            Distinct from the case below, and a bigger problem than 4K: the category's
            EXISTING films are not in any Plex movie library either. Either the standard root
            was never added to Plex, or ``movieRootFolders[category]`` does not match what
            Plex actually has. Surface it as its own fault — telling the operator to "add the
            4K root to the kids library" is useless when there is no kids library.

      configured, paths known, 4K root NOT covered -> (False, "not-in-library")
            The library exists and holds the standard root, but the 4K root was never added
            to it as a second folder — so the 4K copy would leave the shelf its audience
            browses. Cap, and say so; loudly for kids, where a restricted profile loses the
            film entirely.

      configured, paths unknown (None)     -> (False, "unverifiable")
            Plex's section inventory could not be read, so we cannot confirm the root is
            reachable. CAP — operator policy: if the path Radarr reports is not DETECTED in
            Plex, do not create a 4K copy there. The two outcomes are not symmetric, and this
            is the cheap one:

                wrongly capped -> the title stays 1080p. Watchable, and the next run with a
                                 warm Plex cache upgrades it.
                wrongly allowed -> a 2160p file lands where its audience cannot reach it. For
                                 kids that is a silent loss whose only symptom is a child
                                 saying a film is gone.

    ONE BIG MOVIE LIBRARY NEEDS NO SPECIAL CASE. A household running a single ``Movies``
    library on ``/data/media/movies`` passes automatically: that library holds the kids root
    (it is inside it), and its location also contains ``/data/media/movies/4k/kids``, so the
    containment test succeeds. Nothing to configure. And if the 4K root lives on a different
    mount, it correctly caps — Plex genuinely could not see it there.
    """
    cat = str(category or "").strip().lower()
    if cat not in CONTENT_CATEGORIES:
        return True, "not-a-content-category"
    if not category_uhd_root(config, cat):
        return False, "unconfigured"
    if library_paths is None:
        return False, "unverifiable"
    if not library_paths:
        return False, "category-not-in-plex"
    if category_uhd_visible(config, cat, library_paths):
        return True, "visible"
    return False, "not-in-library"


def kids_uhd_allowed(config, kids_library_paths=None) -> tuple[bool, str]:
    """May a KIDS title take a 2160p companion? See :func:`category_uhd_allowed`.

    Kids is the case where the failure has real consequences rather than merely untidy ones:
    a child's profile reaches only the Kids library, so a 4K copy outside it does not look
    misfiled — the film is simply gone, and the only symptom is a child saying so.
    """
    return category_uhd_allowed(config, "kids", kids_library_paths)
