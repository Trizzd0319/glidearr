"""quality_analytics/likely_viewers.py — who is likely to watch a title (pure).
==============================================================================
The codec-aware selector optimises each title for the device(s) of whoever actually
watches it. This module distils "who watches this title, and how much" into a weighted
viewer set ``{username: watch_share}`` (shares sum to 1 over the returned users) that
``profile_selector.choose_codec_profile`` weights its transcode-cost by.

Two regimes, blended by the caller's data availability:
  * OWNED-with-history — attribute from ACTUAL per-user plays of this title
    (``per_title_watchers``); the ground truth.
  * NEW / unwatched — predict from per-user AFFINITY propensity for this title
    (``per_user_propensity``); there is no play yet at acquisition time.

DESIGN NOTE — separation of concerns: the affinity *scoring* (a title's genres/cast
vs a user's taste) already lives in the feature pipeline (``features.*`` consume
``genre_affinity.per_user_affinity``). This module does NOT re-score; the service passes
the already-computed per-user propensity in, and this stays a pure normalise/blend so it
is trivially testable and never drifts from the scorer.

PURE — no HTTP, no cache, no logging.

Public API:
  * infer_likely_viewers(per_user_propensity, *, per_title_watchers=None, threshold=0.15)
        -> {username: watch_share}
  * platform_weights_for_viewers(likely_viewers, per_user_platform_usage)
        -> {username: {platform: share}}   (predict_transcode's per-user device weights)
"""
from __future__ import annotations


def _shares(counts: dict) -> dict:
    """Normalise positive counts/weights to shares summing to 1; {} when nothing positive."""
    pos = {k: float(v) for k, v in (counts or {}).items() if v and float(v) > 0.0}
    total = sum(pos.values())
    if total <= 0.0:
        return {}
    return {k: v / total for k, v in pos.items()}


def infer_likely_viewers(per_user_propensity, *, per_title_watchers=None, threshold: float = 0.15) -> dict:
    """``{username: watch_share}`` for a title, shares summing to 1 over the returned users.

    Regime (degradation ladder):
      1. ``per_title_watchers`` ({user: play_count}) present + positive → ACTUAL watch shares
         (owned title with history — ground truth, ignores propensity).
      2. else ``per_user_propensity`` ({user: affinity score}) → predicted shares; users whose
         share is below ``threshold`` are dropped and the rest renormalised, so a title is
         optimised for the handful who'll actually watch it, not the long tail. If EVERY user is
         below threshold (a flat/cold field), keep them all (don't drop everyone).
      3. neither has positive signal → ``{}`` (the caller falls back to the household matrix).

    Pure."""
    actual = _shares(per_title_watchers)
    if actual:
        return actual

    shares = _shares(per_user_propensity)
    if not shares:
        return {}
    kept = {u: s for u, s in shares.items() if s >= threshold}
    if not kept:
        return shares
    return _shares(kept)


def platform_weights_for_viewers(likely_viewers, per_user_platform_usage) -> dict:
    """``{username: {platform: share}}`` for the likely viewers — each user's platform play-counts
    (from :func:`affinity.platform_usage.per_user_platform_usage`) normalised to shares, the per-user
    ``platform_weights`` ``predict_transcode`` expects. A viewer with no recorded device usage maps to
    ``{}`` (the predictor then has no device read for them → the neutral none_p prior). Pure."""
    out: dict = {}
    for user in (likely_viewers or {}):
        out[user] = _shares((per_user_platform_usage or {}).get(user) or {})
    return out
