"""
scoring/device_fit.py — Group-D v2: per-title transcode RISK, scored as a penalty (pure).
================================================================================
WHAT WENT WRONG WITH v1 (measured, not hypothesised)
----------------------------------------------------
Group D v1 was three BONUSES: D1 "the primary device can play this resolution" (+6),
D2 "this codec has never been seen transcoding" (+5), D3 "most plays happen on devices
that support this resolution" (+4). Once the movie path was finally wired up, 1,840 of
1,997 movies (92%) scored EXACTLY 12.0 — sd 1.92, the whole tail being 66 titles at 8.0,
43 at 5.0 and 38 at 1.0. For the median movie that was 12 of 21 points: **57% of the
entire watchability score carrying almost zero ranking information**, compressing the
useful dynamic range and silently invalidating every absolute threshold anchored on it.

The cause is structural, not a bug. D1 and D3 ask *"can the household's devices play
this?"*, which is YES for every modern device × file combination this decade produces —
a question whose answer is a constant is not a signal. D2 asks *"has this CODEC ever
transcoded?"*, which addresses only a small slice of what actually transcodes.

WHAT THE HOUSEHOLD'S OWN DATA SAYS
----------------------------------
133 ground-truth per-stream decisions (``tautulli/stream_decisions``), classified by the
SAME function the operator-facing report uses (``quality_analytics.transcode_causes``):

    audio                51   38.3%      <- the biggest cause, and v1 modelled NONE of it
    video: bitrate/res   48   36.1%
    subtitle             19   14.3%
    video: codec         15   11.3%      <- the only slice v1's D2 addressed
    container             0    0.0%      (never the PRIMARY cause here; see below)

and ``tautulli/transcode_fingerprint`` gives 938 plays across 11 (device, network) cells:
806 direct / 132 transcode = a 14.1% household base rate, 13.4% on LAN vs 25.5% on WAN.

THE v2 SHAPE
------------
Group D is now a PENALTY, the same convention Group G already uses:

    "will direct-play"      →  ~0    (neutral: playing correctly is the expectation,
                                      not an achievement worth 12 points)
    "will likely transcode" →  <0    scaled by how likely AND by how much it matters

    D4_transcode_risk = -round(magnitude * Σ_c w_c · r_c(title), 2)

``r_c`` is a per-title risk in [0, 1] on each CAUSE axis; ``w_c`` are the cause weights,
learned from the household's own observed decisions and shrunk toward a shipped prior so
a fresh install is not fitting four weights to three data points. Video codec is now ONE
input among five rather than the whole signal, and it is weighted by its OBSERVED share
(11%) instead of by guess.

NO DATA → NEUTRAL 0, NEVER A FREE BONUS
---------------------------------------
:func:`build_transcode_profile` returns ``None`` when the household has neither platform
usage nor observed decisions, and a ``None`` profile makes the scorers fall back to the
legacy path; :func:`device_fit_penalty` itself returns 0.0 for a title whose file
characteristics are all unknown (a Sonarr pilot STUB, which has no file at all). An
unmeasurable axis contributes 0.0 risk and is renormalised out of the weighted sum, so
"we cannot tell" never becomes either a penalty or a credit.

PURE — no I/O, no cache, no logging, no service imports. The service reads the two
Tautulli cache buckets once per pass and hands them to :func:`build_transcode_profile`.

Public API
----------
  * PRIOR_CAUSE_WEIGHTS                                  — the shipped cold-start mix
  * DeviceFitSettings / resolve_device_fit(config)       — ``scoring.device_fit_v2``
  * TranscodeProfile
  * observed_cause_weights(stream_decisions)             -> ({cause: share}, n)
  * observed_transcode_rates(fingerprint)                -> {base_rate, lan/wan, n}
  * build_transcode_profile(...)                         -> TranscodeProfile | None
  * device_fit_penalty(profile, ...)                     -> (float, {axis: risk})
  * summarise_group_d(values)                            -> {n, mean, sd, mode, ...}
"""
from __future__ import annotations

from dataclasses import dataclass, field

from scripts.managers.machine_learning.scoring._shared import (
    audio_transcode_share,
    codec_direct_play_share,
    device_resolution_ceiling,
    normalize_codec,
    preferred_language_available,
)

# ── cause vocabulary ─────────────────────────────────────────────────────────
#: The five axes a title is scored on. These are exactly the cause labels
#: ``quality_analytics.transcode_causes._classify`` emits (its two video labels collapse
#: onto ``codec`` and ``bitrate_res``), so the weights below are directly comparable with
#: the operator-facing "transcode causes household-wide" report — one vocabulary, not two.
CAUSE_AXES: tuple[str, ...] = ("audio", "bitrate_res", "subtitle", "codec", "container")

#: Map ``transcode_causes._classify``'s labels onto :data:`CAUSE_AXES`.
_CAUSE_LABEL_TO_AXIS: dict[str, str] = {
    "audio": "audio",
    "video: bitrate/res": "bitrate_res",
    "subtitle": "subtitle",
    "video: codec": "codec",
    "container": "container",
    # Labels that name a CIRCUMSTANCE rather than a fixable property of the file
    # ("remote (bandwidth)", "other", "audio/other") are deliberately absent: they are
    # counted in the denominator of the observation but attributed to no axis, so they
    # dilute every weight equally instead of being pinned on whichever axis looks closest.
}

#: Shipped cold-start cause mix, used verbatim by a household with no stream decisions and
#: blended with the observed mix as evidence arrives. NOT this household's numbers — it is
#: the general shape Plex transcode-cause surveys and the Plex forums report, deliberately
#: chosen so a fresh install is not seeded with one library's idiosyncrasies:
#: audio (Atmos/DTS to TVs and browsers) and bitrate/bandwidth dominate, subtitles are a
#: solid third, video codec is a minority, container remux is rarely the sole cause.
PRIOR_CAUSE_WEIGHTS: dict[str, float] = {
    "audio": 0.35,
    "bitrate_res": 0.30,
    "subtitle": 0.15,
    "codec": 0.15,
    "container": 0.05,
}

#: Empirical-Bayes pooling constant for the cause weights: ``w = n/(n+K)`` of the observed
#: mix plus ``K/(n+K)`` of the prior. K=60 means ~60 classified decisions buy half the
#: weight — the same "some evidence, not none" philosophy as the ml.thresholds shrinkage
#: (k=150 there, lower here because a 5-bucket multinomial converges faster than an
#: isotonic curve). This household's 133 decisions therefore carry 69% of the mix.
CAUSE_WEIGHT_PRIOR_STRENGTH: float = 60.0

#: Minimum classified decisions before the observed mix is allowed to move the weights at
#: all. Below this the shrinkage would be dominated by sampling noise in a 5-way split.
MIN_DECISIONS_FOR_FIT: int = 10

# ── per-axis shape constants ─────────────────────────────────────────────────
#: Reference video bitrate (Mbps) at which a file of a given resolution is "normal". Risk
#: is zero at or below the reference and saturates at :data:`BITRATE_SATURATION` × it.
#: Anchored on what streaming/Blu-ray sources actually deliver per tier, NOT on this
#: library's own percentiles — a self-normalising threshold would guarantee a fixed share
#: of the library at maximum risk regardless of whether that library is bitrate-heavy.
BITRATE_REFERENCE_MBPS: dict[int, float] = {
    480: 3.0, 576: 4.0, 720: 8.0, 1080: 16.0, 2160: 50.0,
}
BITRATE_SATURATION: float = 2.5          # 2.5x the reference = full bitrate risk
#: Remote plays bind on the WAN link long before the LAN one, so the same file is far
#: riskier off-network. The knee for a remote play is this fraction of the LAN reference.
REMOTE_BITRATE_FRACTION: float = 0.35
#: Video's share of a file's total bitrate, used when ``video_bitrate`` is absent and the
#: rate has to be derived from size ÷ runtime (45% of this library's movie rows and 93%
#: of its episode rows report no video bitrate, so without this the axis would be blind
#: on most of the library).
VIDEO_BITRATE_FRACTION_OF_TOTAL: float = 0.9

#: Containers whose subtitle tracks are typically IMAGE-based (PGS / VOBSUB) and whose
#: streams often need a full remux. Text subtitles in MP4 are rarely burned in.
_IMAGE_SUB_CONTAINERS: frozenset = frozenset({"mkv", "m2ts", "ts", "iso", "img", "vob",
                                              "mpls", "bdmv"})
#: Container risk: how often the container alone forces work. MP4 is the streaming-native
#: format; MKV needs a cheap remux on browsers; the legacy AVI/WMV/VOB family forces a
#: full transcode on essentially every modern client.
_CONTAINER_RISK: dict[str, float] = {
    "mp4": 0.0, "m4v": 0.0, "mov": 0.1,
    "mkv": 0.3, "webm": 0.3,
    "ts": 0.6, "m2ts": 0.6, "mpls": 0.6,
    "avi": 1.0, "wmv": 1.0, "vob": 1.0, "mpg": 1.0, "mpeg": 1.0,
    "iso": 1.0, "img": 1.0, "divx": 1.0, "flv": 1.0, "asf": 1.0,
}

#: Subtitle risk by TRACK COUNT. More tracks = more chance the one Plex auto-selects is
#: image-based (which forces burn-in, which forces a full video transcode). The WEIGHT of
#: this axis is empirical; this within-axis shape is an explicit prior.
_SUBTITLE_COUNT_RISK: tuple[tuple[int, float], ...] = ((0, 0.0), (2, 0.4), (5, 0.7))
_SUBTITLE_MANY_RISK: float = 1.0
#: A title with NO preferred-language audio can only be watched WITH subtitles, so a
#: subtitle track will be selected on every single play rather than occasionally.
_SUBTITLE_FORCED_USE_RISK: float = 0.9

#: Default total magnitude of the group. Chosen to match v1's cap (15) rather than v1's
#: effective constant (12) so the group's dynamic RANGE does not lurch: a title at full
#: risk on every axis loses 15, the same span the group could theoretically award before.
#: In practice the realised range on this library is ~0 to -8 (see the module tests).
DEFAULT_MAGNITUDE: float = 15.0


# ── config gate ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeviceFitSettings:
    """``scoring.device_fit_v2`` — resolved.

    ``enabled`` DEFAULTS TO **TRUE**, deliberately. The legacy path is not a neutral
    alternative that happens to be older: it is measurably broken on real data (92% of
    movies pinned to one value, 57% of the median score carrying no information), and the
    quality ladder + delete-floor thresholds shipped alongside this change are anchored on
    the v2 distribution. Defaulting to OFF would ship thresholds calibrated for a
    distribution the default code path does not produce — the worse of the two failure
    modes. Setting the key to ``false`` restores the legacy terms byte-for-byte.
    """
    enabled: bool = True
    magnitude: float = DEFAULT_MAGNITUDE
    cause_weights: "dict[str, float] | None" = None   # operator override, pre-normalised
    prior_strength: float = CAUSE_WEIGHT_PRIOR_STRENGTH


def resolve_device_fit(config=None) -> DeviceFitSettings:
    """``config.scoring.device_fit_v2`` → :class:`DeviceFitSettings`.

    Accepts the bare bool shorthand (``"device_fit_v2": false``) as well as the dict form::

        "scoring": {"device_fit_v2": {
            "enabled": true,
            "magnitude": 15.0,
            "cause_weights": {"audio": 0.4, "bitrate_res": 0.3, "subtitle": 0.15,
                              "codec": 0.1, "container": 0.05}
        }}

    Any malformed value falls back to the shipped defaults rather than raising — a typo in
    config must never take a run down. Pure."""
    try:
        raw = ((config or {}).get("scoring", {}) or {}).get("device_fit_v2")
    except Exception:
        return DeviceFitSettings()
    if raw is None:
        return DeviceFitSettings()
    if isinstance(raw, bool):
        return DeviceFitSettings(enabled=raw)
    if not isinstance(raw, dict):
        return DeviceFitSettings()
    enabled = bool(raw.get("enabled", True))
    try:
        magnitude = float(raw.get("magnitude", DEFAULT_MAGNITUDE))
    except (TypeError, ValueError):
        magnitude = DEFAULT_MAGNITUDE
    if magnitude < 0:
        magnitude = DEFAULT_MAGNITUDE
    try:
        strength = float(raw.get("prior_strength", CAUSE_WEIGHT_PRIOR_STRENGTH))
    except (TypeError, ValueError):
        strength = CAUSE_WEIGHT_PRIOR_STRENGTH
    weights = _normalise_weights(raw.get("cause_weights"))
    return DeviceFitSettings(enabled=enabled, magnitude=magnitude,
                             cause_weights=weights, prior_strength=max(0.0, strength))


def _normalise_weights(raw) -> "dict[str, float] | None":
    """A ``{axis: weight}`` mapping → non-negative weights over :data:`CAUSE_AXES` summing
    to 1.0, or None when the input is absent/unusable. Unknown axis names are ignored."""
    if not raw or not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for axis in CAUSE_AXES:
        try:
            v = float(raw.get(axis, 0.0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        out[axis] = max(0.0, v)
    total = sum(out.values())
    if total <= 0:
        return None
    return {k: v / total for k, v in out.items()}


# ── the household profile ────────────────────────────────────────────────────

@dataclass(frozen=True)
class TranscodeProfile:
    """Everything :func:`device_fit_penalty` needs about the HOUSEHOLD, built once per
    scoring pass. Frozen + hashable-by-content so a caller can fold it into a memo key.

    ``cause_weights``  — the blended axis weights (sum 1.0).
    ``platform_usage`` — ``{platform: plays}``; the play-weighting behind every axis.
    ``capabilities``   — the resolved device matrix (shipped ∪ operator overrides).
    ``base_rate``      — observed transcode share of plays, reported for diagnostics.
    ``remote_share``   — observed WAN share of plays; scales the bandwidth axis.
    ``n_decisions``    — classified stream decisions behind ``cause_weights``.
    ``n_plays``        — plays behind ``base_rate`` / ``remote_share``.
    """
    cause_weights: dict = field(default_factory=lambda: dict(PRIOR_CAUSE_WEIGHTS))
    platform_usage: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    magnitude: float = DEFAULT_MAGNITUDE
    base_rate: float = 0.0
    remote_share: float = 0.0
    n_decisions: int = 0
    n_plays: int = 0
    preferred_languages: tuple = ("en",)

    def memo_key(self) -> list:
        """A stable, JSON-serialisable digest for the score memos' CONTEXT hash. Without
        it a household whose transcode evidence shifts keeps being served scores computed
        from the old cause weights (the memos are keyed on inputs, and the profile is an
        input that lives outside the per-row data)."""
        return [
            sorted((k, round(float(v), 6)) for k, v in (self.cause_weights or {}).items()),
            sorted((str(k), float(v)) for k, v in (self.platform_usage or {}).items()),
            round(float(self.magnitude), 6), round(float(self.base_rate), 6),
            round(float(self.remote_share), 6),
            int(self.n_decisions), int(self.n_plays),
            sorted(self.preferred_languages or ()),
            sorted((k, cap.max_resolution, sorted(cap.direct_play),
                    sorted(cap.direct_play_audio), cap.max_audio_channels)
                   for k, cap in (self.capabilities or {}).items()),
        ]


def observed_cause_weights(stream_decisions) -> "tuple[dict[str, float], int]":
    """``tautulli/stream_decisions`` → ``({axis: share}, n_classified)``.

    Classifies every cached per-stream decision with the SAME ``_classify`` the
    operator-facing transcode-cause report uses, so the weights and the report can never
    disagree about what caused what. Decisions whose cause is a circumstance rather than a
    file property ("remote (bandwidth)", "other") count toward ``n`` but toward no axis,
    which correctly dilutes every weight rather than inventing an attribution.

    ``({}, 0)`` when there is nothing to classify. Pure."""
    from scripts.managers.machine_learning.quality_analytics.transcode_causes import _classify

    counts: dict[str, float] = {axis: 0.0 for axis in CAUSE_AXES}
    n = 0
    for record in (stream_decisions or {}).values():
        if not isinstance(record, dict):
            continue
        try:
            cause, _gt = _classify(
                record.get("video_decision"), record.get("audio_decision"),
                record.get("subtitle_decision"), record.get("container_decision"),
                record.get("video_codec"), record.get("stream_video_codec"),
                record.get("location"), True,
            )
        except Exception:
            continue
        n += 1
        axis = _CAUSE_LABEL_TO_AXIS.get(cause)
        if axis:
            counts[axis] += 1.0
    if n <= 0:
        return {}, 0
    return {axis: counts[axis] / n for axis in CAUSE_AXES}, n


def observed_transcode_rates(fingerprint) -> dict:
    """``tautulli/transcode_fingerprint`` → ``{base_rate, lan_rate, wan_rate,
    remote_share, n}``.

    The fingerprint is a list of ``{device, fingerprint[codec, ?, subtitle, ?, network],
    direct, transcode, n}`` cells. Only the NETWORK slot of the fingerprint is reliably
    populated on a real cache (the codec/subtitle slots read "unknown" until Tautulli has
    per-stream detail), so this reads the direct/transcode counts and the lan/wan marker
    and nothing else. All-zero defaults when the bucket is empty. Pure."""
    direct = transcode = 0.0
    lan_d = lan_t = wan_d = wan_t = 0.0
    for cell in (fingerprint or []):
        if not isinstance(cell, dict):
            continue
        try:
            d = float(cell.get("direct") or 0)
            t = float(cell.get("transcode") or 0)
        except (TypeError, ValueError):
            continue
        fp = cell.get("fingerprint") or []
        network = str(fp[-1]).strip().lower() if fp else ""
        direct += d
        transcode += t
        if network == "wan":
            wan_d += d
            wan_t += t
        else:
            lan_d += d
            lan_t += t
    total = direct + transcode
    if total <= 0:
        return {"base_rate": 0.0, "lan_rate": 0.0, "wan_rate": 0.0,
                "remote_share": 0.0, "n": 0}
    lan_n, wan_n = lan_d + lan_t, wan_d + wan_t
    return {
        "base_rate": transcode / total,
        "lan_rate": (lan_t / lan_n) if lan_n else 0.0,
        "wan_rate": (wan_t / wan_n) if wan_n else 0.0,
        "remote_share": wan_n / total,
        "n": int(total),
    }


def blend_cause_weights(observed, n, prior=None,
                        strength: float = CAUSE_WEIGHT_PRIOR_STRENGTH) -> dict:
    """Empirical-Bayes blend of the OBSERVED cause mix with the shipped prior:
    ``w = n/(n+K)·observed + K/(n+K)·prior``.

    ``n = 0`` returns the prior BIT-IDENTICALLY, so a fresh install behaves exactly as a
    documented cold start rather than as a fit to nothing. Below
    :data:`MIN_DECISIONS_FOR_FIT` the observation is ignored entirely — shrinkage is for
    "some evidence", not for "almost none". Delegates the pooling arithmetic to
    ``foundation.formulas.empirical_bayes_pool`` so this file owns no duplicated math,
    falling back to the closed form if that import is unavailable. Pure."""
    prior = dict(prior or PRIOR_CAUSE_WEIGHTS)
    if not observed or n < MIN_DECISIONS_FOR_FIT or strength <= 0:
        return {axis: float(prior.get(axis, 0.0)) for axis in CAUSE_AXES}
    try:
        from scripts.managers.machine_learning.foundation.formulas import empirical_bayes_pool
        out = {axis: float(empirical_bayes_pool(
            observed.get(axis, 0.0), float(n), prior.get(axis, 0.0), strength))
            for axis in CAUSE_AXES}
    except Exception:
        w = float(n) / (float(n) + strength)
        out = {axis: w * float(observed.get(axis, 0.0)) +
               (1.0 - w) * float(prior.get(axis, 0.0)) for axis in CAUSE_AXES}
    total = sum(out.values())
    if total <= 0:
        return {axis: float(prior.get(axis, 0.0)) for axis in CAUSE_AXES}
    return {axis: v / total for axis, v in out.items()}


def build_transcode_profile(
    *,
    platform_usage=None,
    stream_decisions=None,
    transcode_fingerprint=None,
    capabilities=None,
    settings: "DeviceFitSettings | None" = None,
    preferred_languages=None,
) -> "TranscodeProfile | None":
    """The household transcode profile, or **None** when v2 must not run.

    Returns None — which makes both scorers fall back to the legacy D1/D2/D3 terms — when
    the feature is config-disabled, OR when the household has NO evidence of any kind: no
    recognised platform in ``platform_usage`` and no observed decisions/fingerprint. That
    second case is the "degrade to neutral, not to a free bonus" rule at the profile
    level; :func:`device_fit_penalty` enforces the same rule per title.

    A household with platform data but no decision data still gets v2 — every axis except
    the weights is computable from the device matrix alone, and the weights fall back to
    the documented shipped prior. Pure."""
    settings = settings or DeviceFitSettings()
    if not settings.enabled:
        return None
    caps = dict(capabilities or {})
    if not caps:
        from scripts.managers.machine_learning.scoring._shared import _DEVICE_CAPABILITIES
        caps = dict(_DEVICE_CAPABILITIES)

    usage: dict = {}
    for platform, plays in (platform_usage or {}).items():
        try:
            n = float(plays or 0)
        except (TypeError, ValueError):
            continue
        if n > 0:
            usage[str(platform)] = n
    recognised = any(device_resolution_ceiling(p, caps) is not None for p in usage)

    observed, n_dec = observed_cause_weights(stream_decisions)
    rates = observed_transcode_rates(transcode_fingerprint)

    # NO EVIDENCE AT ALL → None → the legacy path. Not "v2 with zero risk everywhere":
    # a household we know nothing about must be scored by the code path whose behaviour
    # it was already calibrated against, not by a new one running blind.
    if not recognised and n_dec <= 0 and rates["n"] <= 0:
        return None

    weights = settings.cause_weights or blend_cause_weights(
        observed, n_dec, strength=settings.prior_strength)
    return TranscodeProfile(
        cause_weights=weights,
        platform_usage=usage,
        capabilities=caps,
        magnitude=float(settings.magnitude),
        base_rate=float(rates["base_rate"]),
        remote_share=float(rates["remote_share"]),
        n_decisions=int(n_dec),
        n_plays=int(rates["n"]),
        preferred_languages=tuple(preferred_languages or ("en",)),
    )


# ── per-axis risk ────────────────────────────────────────────────────────────

def _clamp(v: float) -> float:
    return 0.0 if v <= 0.0 else (1.0 if v >= 1.0 else v)


def _union(a: float, b: float) -> float:
    """Probability that AT LEAST ONE of two independent causes fires. Used instead of
    ``max`` so two moderate risks on the same axis compound instead of one hiding the
    other, and instead of a sum so the result stays in [0, 1]."""
    return 1.0 - (1.0 - _clamp(a)) * (1.0 - _clamp(b))


def _subtitle_track_count(subtitles) -> int:
    """Number of subtitle tracks in the parquet's slash/comma-joined language string."""
    if not subtitles:
        return 0
    return len([t for t in str(subtitles).replace(",", "/").split("/") if t.strip()])


def container_from_path(path) -> "str | None":
    """``"Movie (2016) - [Bluray-720p].mkv"`` → ``"mkv"``. None when there is no path or
    no extension — the parquet has no container column, so the extension IS the container."""
    if not path:
        return None
    tail = str(path).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in tail:
        return None
    ext = tail.rsplit(".", 1)[-1].strip().lower()
    return ext or None


def bitrate_mbps(video_bitrate=None, size_bytes=None, runtime_seconds=None) -> "float | None":
    """The file's video bitrate in Mbps, from ``video_bitrate`` when present and non-zero,
    else derived from ``size_bytes ÷ runtime`` × :data:`VIDEO_BITRATE_FRACTION_OF_TOTAL`.

    The fallback is load-bearing, not a nicety: 45% of this library's movie rows and 93%
    of its episode rows report ``video_bitrate = 0``, so an axis that only read that
    column would be blind on most of the library and would collapse back toward a
    constant — the exact failure this redesign exists to fix. None when neither source
    is usable. Pure."""
    try:
        vb = float(video_bitrate or 0)
    except (TypeError, ValueError):
        vb = 0.0
    if vb > 0:
        return vb / 1e6
    try:
        size = float(size_bytes or 0)
        secs = float(runtime_seconds or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0 or secs <= 0:
        return None
    return (size * 8.0 / secs) * VIDEO_BITRATE_FRACTION_OF_TOTAL / 1e6


def _bitrate_reference(resolution) -> float:
    """Reference Mbps for a resolution, interpolated to the nearest known tier."""
    try:
        res = int(resolution or 0)
    except (TypeError, ValueError):
        res = 0
    if res <= 0:
        res = 1080          # unknown resolution → the library's commonest HD tier
    tiers = sorted(BITRATE_REFERENCE_MBPS)
    nearest = min(tiers, key=lambda t: abs(t - res))
    return BITRATE_REFERENCE_MBPS[nearest]


def _resolution_risk(resolution, profile: TranscodeProfile) -> "float | None":
    """Play-weighted share of the household's viewing that CANNOT render this resolution
    natively (the device must downscale, which is a video transcode).

    This is v1's D1/D3 question — but asked as a risk and folded into the bitrate/res axis
    at that axis's observed weight, instead of being 10 of the group's 15 points on its
    own. None when the resolution is unknown or no platform resolves."""
    try:
        res = int(resolution or 0)
    except (TypeError, ValueError):
        return None
    if res <= 0:
        return None
    known = over = 0.0
    for platform, plays in (profile.platform_usage or {}).items():
        ceiling = device_resolution_ceiling(platform, profile.capabilities)
        if ceiling is None:
            continue
        known += plays
        if res > ceiling:
            over += plays
    if known <= 0:
        return None
    return over / known


def _bitrate_risk(mbps, resolution, profile: TranscodeProfile) -> "float | None":
    """Risk that the file's BITRATE forces a transcode, on a ramp from the per-resolution
    reference to :data:`BITRATE_SATURATION` × it, split between LAN and remote plays at
    the household's OBSERVED remote share. Remote plays use a much lower knee
    (:data:`REMOTE_BITRATE_FRACTION`) because the WAN link binds long before the LAN does —
    which is also what this household's own fingerprint shows (25.5% transcode rate on
    WAN against 13.4% on LAN). None when the bitrate is unknown."""
    if mbps is None or mbps <= 0:
        return None
    reference = _bitrate_reference(resolution)
    span = max(1e-6, reference * (BITRATE_SATURATION - 1.0))
    lan = _clamp((mbps - reference) / span)
    remote_ref = max(0.5, reference * REMOTE_BITRATE_FRACTION)
    remote_span = max(1e-6, remote_ref * (BITRATE_SATURATION - 1.0))
    wan = _clamp((mbps - remote_ref) / remote_span)
    share = _clamp(profile.remote_share)
    return (1.0 - share) * lan + share * wan


def _subtitle_risk(subtitles, audio_languages, container, profile: TranscodeProfile) -> "float | None":
    """Risk that a SUBTITLE track is selected and burned in (which forces a full video
    transcode). None when the file carries no subtitle information at all.

    Zero tracks is a REAL zero, not a missing value: a file with no embedded subtitles
    cannot burn one in. Beyond that the shape is a documented prior — the parquet stores
    subtitle LANGUAGES, never formats, so PGS-vs-SRT is not observable and track count is
    the best available proxy for "one of these is image-based".

    The one hard signal here is language: a title with NO preferred-language AUDIO can
    only be watched WITH subtitles, so a subtitle track is selected on EVERY play rather
    than occasionally. That is the foreign-film / anime case, and it is exactly the sample
    entry in this household's own decision cache (hevc→hevc, subtitle_decision="burn")."""
    if subtitles is None and audio_languages is None:
        return None
    n = _subtitle_track_count(subtitles)
    if n <= 0:
        return 0.0
    risk = _SUBTITLE_MANY_RISK
    for threshold, value in _SUBTITLE_COUNT_RISK:
        if n <= threshold:
            risk = value
            break
    if audio_languages is not None and not preferred_language_available(
            audio_languages, None, list(profile.preferred_languages or ("en",))):
        risk = max(risk, _SUBTITLE_FORCED_USE_RISK)
    if container and container not in _IMAGE_SUB_CONTAINERS:
        # Text subtitles (mov_text/SRT) in a streaming-native container are converted, not
        # burned — the client renders them. Halve rather than zero: a forced/foreign track
        # can still be burned by a client that cannot render the format.
        risk *= 0.5
    return _clamp(risk)


def _container_risk(container) -> "float | None":
    """Risk that the CONTAINER alone forces work. None when unknown; an unrecognised
    extension is scored at the MKV rung rather than at zero (an exotic container is more
    likely to need a remux than less)."""
    if not container:
        return None
    return _CONTAINER_RISK.get(container, 0.3)


def _codec_risk(video_codec, profile: TranscodeProfile) -> "float | None":
    """1 − the play-weighted share of the household that direct-plays this video codec.
    The v1 D2 question, kept as ONE input at its observed 11% weight rather than as the
    whole group. None for an unrecognised codec or no resolved platform."""
    share = codec_direct_play_share(video_codec, profile.platform_usage, profile.capabilities)
    if share is None:
        return None
    return _clamp(1.0 - share)


def _audio_risk(audio_codec, audio_channels, profile: TranscodeProfile) -> "float | None":
    """Play-weighted share of the household that must transcode this AUDIO track — the
    biggest cause in the observed data (38%) and the one v1 modelled not at all."""
    return audio_transcode_share(audio_codec, audio_channels,
                                 profile.platform_usage, profile.capabilities)


# ── the penalty ──────────────────────────────────────────────────────────────

def device_fit_penalty(
    profile: "TranscodeProfile | None",
    *,
    resolution=None,
    video_codec=None,
    video_bitrate=None,
    size_bytes=None,
    runtime_seconds=None,
    audio_codec=None,
    audio_channels=None,
    audio_languages=None,
    subtitles=None,
    relative_path=None,
    container=None,
) -> "tuple[float, dict]":
    """``(penalty, detail)`` for one title. ``penalty`` is **<= 0.0**.

    ``detail`` carries the per-axis risk, the weight each axis actually received and the
    blended total, so a title's Group-D number can be explained without re-deriving it.

    NEUTRALITY RULES, in order:
      * ``profile is None``          → ``(0.0, {})``   (feature off / no household evidence)
      * every axis unmeasurable      → ``(0.0, ...)``  (a Sonarr pilot STUB: no file, so no
                                                        playback facts to be risky about)
      * some axes unmeasurable       → the measurable ones are RENORMALISED to sum to 1,
                                        so a missing axis neither penalises nor credits.

    Pure."""
    if profile is None:
        return 0.0, {}

    cont = container or container_from_path(relative_path)
    mbps = bitrate_mbps(video_bitrate, size_bytes, runtime_seconds)

    res_risk = _resolution_risk(resolution, profile)
    br_risk = _bitrate_risk(mbps, resolution, profile)
    if res_risk is None and br_risk is None:
        bitrate_res: "float | None" = None
    else:
        bitrate_res = _union(res_risk or 0.0, br_risk or 0.0)

    risks: dict[str, "float | None"] = {
        "audio": _audio_risk(audio_codec, audio_channels, profile),
        "bitrate_res": bitrate_res,
        "subtitle": _subtitle_risk(subtitles, audio_languages, cont, profile),
        "codec": _codec_risk(video_codec, profile),
        "container": _container_risk(cont),
    }

    weights = profile.cause_weights or PRIOR_CAUSE_WEIGHTS
    available = {a: w for a, w in weights.items()
                 if a in risks and risks[a] is not None and w > 0}
    total_w = sum(available.values())
    detail: dict = {f"risk_{a}": (None if risks[a] is None else round(risks[a], 4))
                    for a in CAUSE_AXES}
    if total_w <= 0:
        detail.update({"risk_total": 0.0, "weighted_axes": 0, "mbps": mbps,
                       "container": cont})
        return 0.0, detail

    risk = sum((risks[a] or 0.0) * (w / total_w) for a, w in available.items())
    risk = _clamp(risk)
    detail.update({
        "risk_total": round(risk, 4),
        "weighted_axes": len(available),
        "weights_used": {a: round(w / total_w, 4) for a, w in available.items()},
        "mbps": None if mbps is None else round(mbps, 3),
        "container": cont,
    })
    return -round(profile.magnitude * risk, 2), detail


# ── regression guard ─────────────────────────────────────────────────────────

def summarise_group_d(values) -> dict:
    """``{n, mean, sd, min, max, mode, share_at_mode, distinct}`` for a pass's Group-D
    contributions.

    THIS IS THE WHOLE POINT OF THE EXERCISE. v1 became a near-constant silently — nothing
    in the run output would have shown it, and every absolute threshold anchored on the
    score was invalidated without a word. Both scoring passes log this summary, so
    ``share_at_mode`` climbing back toward 1.0 (or ``distinct`` collapsing toward 1) is
    visible in the run log the first time it happens. Pure, stdlib-only."""
    # NOT ``values or []``: a pandas Series raises on truthiness, and the callers are
    # exactly the two scoring passes, which hold their Group-D contributions in one.
    if values is None:
        return {"n": 0, "mean": 0.0, "sd": 0.0, "min": 0.0, "max": 0.0,
                "mode": 0.0, "share_at_mode": 0.0, "distinct": 0}
    vals = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:          # NaN
            continue
        vals.append(round(f, 2))
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": 0.0, "sd": 0.0, "min": 0.0, "max": 0.0,
                "mode": 0.0, "share_at_mode": 0.0, "distinct": 0}
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    counts: dict[float, int] = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    mode, mode_n = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    return {
        "n": n,
        "mean": round(mean, 3),
        "sd": round(var ** 0.5, 3),
        "min": min(vals),
        "max": max(vals),
        "mode": mode,
        "share_at_mode": round(mode_n / n, 4),
        "distinct": len(counts),
    }
