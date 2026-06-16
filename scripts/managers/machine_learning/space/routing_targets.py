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
_CONSENT_TRUTHY = {"1", "true", "yes", "on", "y"}

_REORG_MODES = ("off", "log_only", "same_instance")
DEFAULT_REORG_MODE = "log_only"


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


def proactive_4k_enabled(config) -> bool:
    """HARD GATE for the proactive-4K dual-version behaviour: (a) give ANY owned movie whose
    watch-likelihood warrants 4K a copy on the 4K instance, and (b) CAP the standard-instance
    quality upgrade so it never bumps that title to 4K on standard (otherwise the two paths
    double-grab the same 2160p). Requires ``routing.movies.proactive_4k`` AND
    ``routing.movies.4k_policy == "both"`` AND the move-actuation gate (``relocation_enabled``).
    Tying it to relocation_enabled is deliberate: the standard upgrade cap and the 4K-instance
    acquire MUST move together, so the standard 4K upgrade is never disabled without the 4K-instance
    replacement actually being actuated. Default OFF (existing installs unchanged)."""
    routing = _cfg_get(config, "routing", None) or {}
    mv = routing.get("movies", {}) or {}
    if not mv.get("proactive_4k") or mv.get("4k_policy") != "both":
        return False
    return relocation_enabled(config)
