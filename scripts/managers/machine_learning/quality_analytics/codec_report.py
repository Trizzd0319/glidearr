"""quality_analytics/codec_report.py — read-only codec-routing preview (pure).
==============================================================================
Assembles the "what codec would we pick, and would it change?" diagnostic for OWNED titles
that have watch history — so an operator can SEE the codec-aware-routing decisions in the
run summary before anything is wired to actuate. Read-only: computes + returns rows; a
service adapter logs them / adds them to the end-of-run report. Nothing here changes a profile.

For each owned title with watchers: infer the likely viewers from ACTUAL plays, pick the
transcode-minimising codec at the title's resolution tier (profile_selector.choose_codec_profile),
and compare to the file's current codec. Titles with no watch history are skipped (the report
shows what's actually viewed); the caller can still count them for the summary.

PURE — no HTTP, no cache, no logging. The history/profile/df inputs are assembled by the caller.

Public API:
  * normalize_title(t) -> str
  * build_per_title_watchers(history_entries, *, title_fields=...) -> {norm_title: {user: count}}
  * per_user_platform_usage_from_history(history_entries) -> {user: {platform: count}}
  * codec_report_rows(df, candidate_profiles, per_user_matrix, per_user_platform_usage,
                      per_title_watchers, *, ...) -> list[dict]
"""
from __future__ import annotations

from scripts.managers.machine_learning.affinity.platform_usage import platform_usage
from scripts.managers.machine_learning.quality_analytics.likely_viewers import (
    infer_likely_viewers,
    platform_weights_for_viewers,
)
from scripts.managers.machine_learning.quality_analytics.profile_selector import (
    candidate_fingerprint,
    choose_codec_profile,
    classify_profile_axes,
    viewer_transcode_cost,
)
from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import _norm_video


def normalize_title(t) -> str:
    """Collapse a title to a stable join key (lowercased, whitespace-normalised)."""
    return " ".join(str(t or "").strip().lower().split())


def _history_user(entry) -> str:
    """The user key used CONSISTENTLY across the per-user matrix, platform usage and per-title
    watchers (matches per_user_transcode_fingerprint_matrix): ``user`` then ``user_id``."""
    return str(entry.get("user") or entry.get("user_id") or "unknown")


def build_per_title_watchers(history_entries,
                             title_fields=("title", "full_title", "grandparent_title")) -> dict:
    """``{normalized_title: {user: play_count}}`` — who watched each title, how often. The first
    non-empty of ``title_fields`` is the title (Tautulli movie 'title' / episode 'grandparent_title')."""
    out: dict = {}
    for entry in (history_entries or []):
        title = next((entry.get(f) for f in title_fields if entry.get(f)), None)
        if not title:
            continue
        user = _history_user(entry)
        bucket = out.setdefault(normalize_title(title), {})
        bucket[user] = bucket.get(user, 0) + 1
    return out


def per_user_platform_usage_from_history(history_entries) -> dict:
    """``{user: {platform: count}}`` grouped by the SAME user key as the per-user matrix, so the
    selector's per-user lookups line up. (The user_list-driven variant lives in affinity.platform_usage.)"""
    by_user: dict = {}
    for entry in (history_entries or []):
        by_user.setdefault(_history_user(entry), []).append(entry)
    return {u: platform_usage(rows) for u, rows in by_user.items()}


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def codec_report_rows(df, candidate_profiles, per_user_matrix, per_user_platform_usage,
                      per_title_watchers, *, title_col="title", codec_col="video_codec",
                      res_col="resolution", none_p: float = 0.5, min_n: int = 3,
                      threshold: float = 0.15, gain_threshold: float = 0.1) -> list:
    """Per-title codec recommendations for owned, WATCHED titles. Returns a list of dicts sorted
    change-first then by predicted transcode reduction::

        {title, watchers: [user...], current_codec, resolution, recommended_codec,
         current_cost, recommended_cost, gain, recommended_pid, change: bool}

    ``current_cost``/``recommended_cost`` are the watch-share-weighted P(transcode) for the file's
    CURRENT codec vs the recommended one. ``change`` is True only when a DIFFERENT codec is predicted
    to REDUCE transcoding by more than ``gain_threshold`` — so a tie with no transcode evidence (cold
    matrix → both costs hit the neutral prior) never reads as a recommended change, and the size
    tie-break's cold default (AV1) doesn't masquerade as a real recommendation.

    ``df`` rows expose ``title_col``/``codec_col``/``res_col``; ``per_title_watchers`` is keyed by
    :func:`normalize_title`. Only titles whose resolution tier has >= 2 codec-variant candidate
    profiles produce a row (a single variant has nothing to choose). Pure."""
    by_tier: dict = {}
    for prof in (candidate_profiles or []):
        axes = classify_profile_axes(prof)
        if axes["res_tier"] > 0:
            by_tier.setdefault(axes["res_tier"], []).append(prof)

    rows = []
    for _idx, r in df.iterrows():
        title = r.get(title_col)
        if title is None:
            continue
        watchers = (per_title_watchers or {}).get(normalize_title(title))
        if not watchers:
            continue
        res = _to_int(r.get(res_col))
        cands = by_tier.get(res)
        if not cands or len(cands) < 2:
            continue
        likely = infer_likely_viewers({}, per_title_watchers=watchers, threshold=threshold)
        weights = platform_weights_for_viewers(likely, per_user_platform_usage)
        rec_pid, reason = choose_codec_profile(
            res, likely, per_user_matrix, cands,
            per_user_platform_weights=weights, none_p=none_p, min_n=min_n,
        )
        if rec_pid is None:
            continue
        cur_codec = _norm_video(r.get(codec_col))
        rec_codec = reason.get("codec")
        rec_cost = float(reason.get("cost") or 0.0)
        # The file's CURRENT codec's predicted transcode cost for these viewers — the baseline the
        # recommendation must actually beat for a 'change' to be meaningful (vs. a no-evidence tie).
        cur_cost = viewer_transcode_cost(
            candidate_fingerprint({"codec": cur_codec, "res_tier": res}),
            likely, per_user_matrix, weights, none_p=none_p, min_n=min_n,
        )
        gain = cur_cost - rec_cost
        rows.append({
            "title": str(title),
            "watchers": sorted(watchers),
            "current_codec": cur_codec,
            "resolution": res,
            "recommended_codec": rec_codec,
            "current_cost": round(cur_cost, 3),
            "recommended_cost": round(rec_cost, 3),
            "gain": round(gain, 3),
            "recommended_pid": rec_pid,
            "change": bool(rec_codec and rec_codec != "unknown"
                           and rec_codec != cur_codec and gain > gain_threshold),
        })
    rows.sort(key=lambda x: (not x["change"], -x["gain"]))
    return rows
