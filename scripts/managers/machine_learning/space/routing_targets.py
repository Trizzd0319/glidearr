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
