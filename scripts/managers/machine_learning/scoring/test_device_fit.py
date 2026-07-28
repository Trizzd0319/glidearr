"""scoring/test_device_fit.py — Group D v2: transcode risk as a penalty.
================================================================================
The regression this file exists for is MEASURED, not hypothetical. Group D v1 gave
1,840 of 1,997 movies (92%) EXACTLY 12.0 — 12 of the median movie's 21 points, carrying
no ranking information at all and silently invalidating every absolute threshold anchored
on the score. These tests pin the four properties that failure violated:

  * a clean direct-play title scores ~0 and a risky one scores NEGATIVE (the shape);
  * subtitle burn-in, audio mismatch and over-bitrate each move the number on their own
    (the group discriminates on the causes the household ACTUALLY transcodes for);
  * a household with no transcode evidence gets neutral 0, never a free bonus;
  * with the flag off, the legacy path is reachable and byte-identical.

Pure: no cache reads, no service imports. The household platform mix is inlined.
"""
from __future__ import annotations

from scripts.managers.machine_learning.scoring._shared import (
    _DEVICE_CAPABILITIES,
    audio_transcode_share,
    normalize_audio_codec,
    resolve_device_capabilities,
    resolve_device_capability,
)
from scripts.managers.machine_learning.scoring.device_fit import (
    CAUSE_AXES,
    PRIOR_CAUSE_WEIGHTS,
    DeviceFitSettings,
    bitrate_mbps,
    blend_cause_weights,
    build_transcode_profile,
    container_from_path,
    device_fit_penalty,
    observed_cause_weights,
    observed_transcode_rates,
    resolve_device_fit,
    summarise_group_d,
)
from scripts.managers.machine_learning.scoring.movie_scorer import score_movie
from scripts.managers.machine_learning.scoring.show_scorer import score_show

# The six platforms this household's Tautulli history reports, with real play counts.
HOUSEHOLD = {"Windows": 369, "Tizen": 361, "Android": 156,
             "iOS": 35, "Chrome": 14, "PlayStation": 3}

# Two real cells of tautulli/transcode_fingerprint (only the NETWORK slot is populated
# on a real cache; the codec/subtitle slots read "unknown").
FINGERPRINT = [
    {"device": "Windows", "fingerprint": ["unknown", "unknown", "none", "unknown", "lan"],
     "direct": 318, "transcode": 48, "n": 366},
    {"device": "Chrome", "fingerprint": ["unknown", "unknown", "none", "unknown", "wan"],
     "direct": 0, "transcode": 6, "n": 6},
]

# Four real shapes out of tautulli/stream_decisions, one per cause the classifier emits.
DECISIONS = {
    "1": {"video_decision": "transcode", "audio_decision": "copy", "subtitle_decision": "burn",
          "container_decision": "transcode", "video_codec": "hevc", "stream_video_codec": "hevc"},
    "2": {"video_decision": "copy", "audio_decision": "transcode", "subtitle_decision": "copy",
          "container_decision": "transcode", "video_codec": "h264", "stream_video_codec": "h264"},
    "3": {"video_decision": "transcode", "audio_decision": "transcode", "subtitle_decision": "copy",
          "container_decision": "transcode", "video_codec": "h264", "stream_video_codec": "h264"},
    "4": {"video_decision": "transcode", "audio_decision": "transcode", "subtitle_decision": None,
          "container_decision": "transcode", "video_codec": "h264", "stream_video_codec": "hevc"},
}


def _profile(**kw):
    kw.setdefault("platform_usage", HOUSEHOLD)
    kw.setdefault("stream_decisions", DECISIONS)
    kw.setdefault("transcode_fingerprint", FINGERPRINT)
    kw.setdefault("capabilities", dict(_DEVICE_CAPABILITIES))
    return build_transcode_profile(**kw)


#: A title nothing in the house has to work for: H.264 in MP4, stereo AAC, English
#: audio, no subtitle tracks, an ordinary 720p bitrate.
CLEAN = dict(resolution=720, video_codec="x264", video_bitrate=5_000_000,
             audio_codec="AAC", audio_channels=2.0, audio_languages="eng",
             subtitles="", relative_path="Clean (2019) - [WEBDL-720p].mp4")


# ── the SHAPE: neutral for direct play, negative for risk ────────────────────

def test_a_clean_direct_play_title_scores_about_zero():
    p = _profile()
    d, detail = device_fit_penalty(p, **CLEAN)
    assert d <= 0.0                      # the term can never be a bonus
    assert d > -1.0, (d, detail)         # ...and a clean title is essentially neutral
    assert detail["risk_total"] < 0.07


def test_the_group_can_never_award_points():
    """The whole point of the redesign: 'will play correctly' is the expectation, not an
    achievement. v1 handed out +12 for it to 92% of the library."""
    p = _profile()
    for kw in (CLEAN,
               dict(CLEAN, audio_codec="DTS-HD MA", audio_channels=7.1),
               dict(CLEAN, video_codec="XviD", relative_path="x.avi"),
               dict(CLEAN, resolution=2160, video_bitrate=90_000_000)):
        assert device_fit_penalty(p, **kw)[0] <= 0.0


def test_a_subtitle_burn_title_scores_negative():
    p = _profile()
    clean = device_fit_penalty(p, **CLEAN)[0]
    # A foreign-language film with no English audio: subtitles are selected on EVERY
    # play, and the container is MKV, where they are typically image-based (PGS).
    burn = device_fit_penalty(p, **dict(
        CLEAN, subtitles="eng/eng/fre", audio_languages="fre",
        relative_path="Foreign (2019) - [Bluray-720p].mkv"))[0]
    assert burn < clean
    assert burn < 0.0


def test_an_audio_mismatch_title_scores_negative():
    p = _profile()
    clean = device_fit_penalty(p, **CLEAN)[0]
    # DTS-HD MA 7.1: only the desktop class decodes it; the TVs, handhelds and browsers
    # (61% of this household's plays) must all transcode.
    lossless = device_fit_penalty(p, **dict(CLEAN, audio_codec="DTS-HD MA",
                                            audio_channels=7.1))[0]
    assert lossless < clean
    assert device_fit_penalty(p, **dict(CLEAN, audio_codec="DTS", audio_channels=5.1))[0] < clean
    # ...while Dolby Digital, which every class passes through, is not punished.
    assert device_fit_penalty(p, **dict(CLEAN, audio_codec="AC3", audio_channels=5.1))[0] \
        == device_fit_penalty(p, **dict(CLEAN, audio_codec="EAC3", audio_channels=5.1))[0]


def test_an_over_bitrate_title_scores_negative():
    p = _profile()
    clean = device_fit_penalty(p, **CLEAN)[0]
    fat = device_fit_penalty(p, **dict(CLEAN, video_bitrate=40_000_000))[0]
    assert fat < clean
    # ...and the ramp is monotone in bitrate at a fixed resolution.
    ladder = [device_fit_penalty(p, **dict(CLEAN, video_bitrate=b))[0]
              for b in (5_000_000, 10_000_000, 16_000_000, 25_000_000, 40_000_000)]
    assert ladder == sorted(ladder, reverse=True)


def test_bitrate_risk_is_relative_to_the_resolution():
    """20 Mbps is fat for 720p and thin for 4K — an absolute knee would punish every
    UHD file and excuse every bloated SD one."""
    p = _profile()
    # At 480p, 20 Mbps saturates the ramp on its own.
    assert device_fit_penalty(
        p, **dict(CLEAN, resolution=480, video_bitrate=20_000_000))[1]["risk_bitrate_res"] == 1.0
    # At 2160p the SAME bitrate is thin, so the axis is unmoved from the same title at
    # 5 Mbps. (Both carry the shared 2160-over-ceiling component — 205 of this
    # household's 938 plays are on 1080p-capped devices — which is the OTHER half of
    # this axis and must not be confused with the bitrate ramp.)
    thin = device_fit_penalty(p, **dict(CLEAN, resolution=2160, video_bitrate=5_000_000))
    fat = device_fit_penalty(p, **dict(CLEAN, resolution=2160, video_bitrate=20_000_000))
    assert abs(fat[1]["risk_bitrate_res"] - thin[1]["risk_bitrate_res"]) < 0.01
    assert 0.2 < thin[1]["risk_bitrate_res"] < 0.25       # the resolution component alone


def test_a_legacy_codec_scores_worse_than_h264():
    p = _profile()
    assert device_fit_penalty(p, **dict(CLEAN, video_codec="XviD"))[0] < \
        device_fit_penalty(p, **CLEAN)[0]
    # AV1 too — Plex transcodes it on effectively every client whatever the hardware.
    assert device_fit_penalty(p, **dict(CLEAN, video_codec="av1"))[0] < \
        device_fit_penalty(p, **CLEAN)[0]


def test_container_moves_the_number_but_only_a_little():
    """Container is 1.6% of the blended weight on this household's evidence — it has
    never been the PRIMARY cause of a transcode here. It must still not be zero."""
    p = _profile()
    avi = device_fit_penalty(p, **dict(CLEAN, relative_path="Old (1998).avi"))
    mp4 = device_fit_penalty(p, **CLEAN)
    assert avi[1]["risk_container"] == 1.0 and mp4[1]["risk_container"] == 0.0
    assert avi[0] < mp4[0]


def test_the_group_actually_discriminates():
    """The property v1 lost. Twelve plausible library titles must not collapse onto one
    value — that is the regression summarise_group_d exists to surface."""
    p = _profile()
    titles = [
        CLEAN,
        dict(CLEAN, audio_codec="AC3", audio_channels=5.1),
        dict(CLEAN, audio_codec="DTS", audio_channels=5.1),
        dict(CLEAN, audio_codec="DTS-HD MA", audio_channels=7.1),
        dict(CLEAN, audio_codec="TrueHD Atmos", audio_channels=7.1),
        dict(CLEAN, subtitles="eng"),
        dict(CLEAN, subtitles="eng/fre/ger/spa/ita/rus"),
        dict(CLEAN, subtitles="eng/jpn", audio_languages="jpn"),
        dict(CLEAN, video_codec="XviD", relative_path="a.avi"),
        dict(CLEAN, video_bitrate=30_000_000),
        dict(CLEAN, resolution=2160, video_bitrate=60_000_000),
        dict(CLEAN, resolution=1080, video_codec="x265", audio_codec="EAC3 Atmos",
             audio_channels=5.1, subtitles="eng/eng"),
    ]
    values = [device_fit_penalty(p, **t)[0] for t in titles]
    s = summarise_group_d(values)
    assert s["distinct"] >= 10, values
    assert s["share_at_mode"] <= 0.25, s
    assert s["sd"] > 0.5, s


# ── NO DATA → NEUTRAL 0, never a free bonus ─────────────────────────────────

def test_no_household_evidence_at_all_disables_v2():
    """No recognised device, no decisions, no fingerprint → None → the scorers fall back
    to the path this household's behaviour was already calibrated against."""
    assert build_transcode_profile(platform_usage=None, stream_decisions=None,
                                   transcode_fingerprint=None) is None
    assert build_transcode_profile(platform_usage={}, stream_decisions={},
                                   transcode_fingerprint=[]) is None
    # An UNRECOGNISED device is not evidence either — it must never be guessed at.
    assert build_transcode_profile(platform_usage={"Nonesuch OS": 900},
                                   stream_decisions=None,
                                   transcode_fingerprint=None) is None


def test_a_none_profile_is_exactly_zero():
    assert device_fit_penalty(None, **CLEAN) == (0.0, {})


def test_a_title_with_no_file_facts_is_exactly_zero():
    """A Sonarr pilot STUB owns no episode file, so every axis is unmeasurable. It must
    score a hard 0.0 — not a small penalty, and (the v1 bug) not a free +2.0 either."""
    p = _profile()
    d, detail = device_fit_penalty(p)
    assert d == 0.0
    assert detail["weighted_axes"] == 0
    assert all(detail[f"risk_{a}"] is None for a in CAUSE_AXES)


def test_an_unmeasurable_axis_is_renormalised_out_not_scored_as_zero_risk():
    """Half-known titles must not be flattered. Dropping an axis renormalises the rest,
    so the answer is 'the risk among what we can see', not 'zero risk over there'."""
    p = _profile()
    full = device_fit_penalty(p, **dict(CLEAN, audio_codec="DTS", audio_channels=5.1))
    # Same title with the AUDIO unknown: the surviving axes keep their relative shares.
    partial = device_fit_penalty(p, **dict(CLEAN, audio_codec=None, audio_channels=None))
    assert partial[1]["risk_audio"] is None
    assert partial[1]["weighted_axes"] == full[1]["weighted_axes"] - 1
    assert abs(sum(partial[1]["weights_used"].values()) - 1.0) < 1e-6
    assert partial[0] >= full[0]        # not punished for what we cannot see...
    assert partial[0] <= 0.0            # ...and not credited for it either


def test_an_unknown_codec_or_audio_format_contributes_no_risk():
    p = _profile()
    assert device_fit_penalty(p, **dict(CLEAN, video_codec="theora"))[1]["risk_codec"] is None
    assert device_fit_penalty(p, **dict(CLEAN, audio_codec="NeverHeardOfIt"))[1]["risk_audio"] is None


# ── the cause weights are LEARNED, not guessed ──────────────────────────────

def test_observed_cause_weights_use_the_report_s_own_classifier():
    weights, n = observed_cause_weights(DECISIONS)
    assert n == 4
    assert weights["subtitle"] == 0.25          # decision 1: burn wins on priority
    assert weights["audio"] == 0.25             # decision 2: video copied, audio didn't
    assert weights["bitrate_res"] == 0.25       # decision 3: video transcode, same codec
    assert weights["codec"] == 0.25             # decision 4: video transcode, codec changed
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_zero_observations_returns_the_prior_bit_identically():
    """A fresh install must behave exactly as the documented cold start, not as a fit
    to nothing."""
    assert blend_cause_weights({}, 0) == {a: PRIOR_CAUSE_WEIGHTS[a] for a in CAUSE_AXES}
    assert blend_cause_weights(None, 0) == {a: PRIOR_CAUSE_WEIGHTS[a] for a in CAUSE_AXES}
    # ...and too FEW observations are ignored rather than fitted (5-way split, tiny n).
    assert blend_cause_weights({"codec": 1.0}, 3) == \
        {a: PRIOR_CAUSE_WEIGHTS[a] for a in CAUSE_AXES}


def test_evidence_moves_the_weights_toward_the_observation():
    obs = {"audio": 1.0, "bitrate_res": 0.0, "subtitle": 0.0, "codec": 0.0, "container": 0.0}
    weak = blend_cause_weights(obs, 20)
    strong = blend_cause_weights(obs, 2000)
    assert PRIOR_CAUSE_WEIGHTS["audio"] < weak["audio"] < strong["audio"]
    assert strong["audio"] > 0.9
    for w in (weak, strong):
        assert abs(sum(w.values()) - 1.0) < 1e-6


def test_codec_is_one_input_not_the_whole_signal():
    """v1's entire Group D turned on the codec question, which this household's own data
    says is 11% of the problem. v2 must weight it like an 11% cause."""
    p = _profile(stream_decisions=None, transcode_fingerprint=None)
    assert p.cause_weights["codec"] == PRIOR_CAUSE_WEIGHTS["codec"]
    assert p.cause_weights["codec"] < p.cause_weights["audio"]
    assert p.cause_weights["codec"] < p.cause_weights["bitrate_res"]


def test_observed_rates_split_lan_from_wan():
    r = observed_transcode_rates(FINGERPRINT)
    assert r["n"] == 372
    assert abs(r["base_rate"] - 54 / 372) < 1e-9
    assert abs(r["lan_rate"] - 48 / 366) < 1e-9
    assert r["wan_rate"] == 1.0
    assert abs(r["remote_share"] - 6 / 372) < 1e-9
    assert observed_transcode_rates([])["n"] == 0
    assert observed_transcode_rates(None)["base_rate"] == 0.0


def test_the_model_reproduces_the_households_observed_transcode_rate():
    """A calibration check, not a fit: the per-title risks are built bottom-up from file
    facts and device capabilities and are never shown the base rate. Their library mean
    should still land near the rate the household actually experiences."""
    p = _profile()
    library = [CLEAN,
               dict(CLEAN, audio_codec="AC3", audio_channels=5.1),
               dict(CLEAN, audio_codec="EAC3", audio_channels=5.1),
               dict(CLEAN, audio_codec="DTS", audio_channels=5.1, subtitles="eng/eng"),
               dict(CLEAN, subtitles="eng")]
    risks = [device_fit_penalty(p, **t)[1]["risk_total"] for t in library]
    assert 0.0 < sum(risks) / len(risks) < 0.5


# ── audio capability (the biggest cause, and the half v1 had no model for) ───

def test_audio_codec_display_names_normalise():
    assert normalize_audio_codec("EAC3 Atmos") == "eac3"
    assert normalize_audio_codec("TrueHD Atmos") == "truehd"
    assert normalize_audio_codec("DTS-HD MA") == "dtshd"
    assert normalize_audio_codec("DTS-ES") == "dts"
    assert normalize_audio_codec("AC3") == "ac3"
    assert normalize_audio_codec("") is None and normalize_audio_codec(None) is None
    assert normalize_audio_codec("NeverHeardOfIt") is None


def test_the_household_audio_classes_are_what_the_devices_really_do():
    assert "dts" not in resolve_device_capability("Tizen").direct_play_audio
    assert "dts" in resolve_device_capability("Windows").direct_play_audio
    assert resolve_device_capability("Chrome").max_audio_channels == 2
    assert resolve_device_capability("Tizen").max_audio_channels == 8
    # Multichannel in a browser is a downmix, which IS an audio transcode.
    assert audio_transcode_share("AAC", 5.1, {"Chrome": 10}) == 1.0
    assert audio_transcode_share("AAC", 2.0, {"Chrome": 10}) == 0.0


def test_every_shipped_device_has_an_audio_class():
    """A device added to the video table without an audio class would silently fall back
    to the stereo baseline and be accused of transcoding everything."""
    for key, cap in _DEVICE_CAPABILITIES.items():
        assert cap.direct_play_audio, key
        assert "aac" in cap.direct_play_audio, key
        assert cap.max_audio_channels in (2, 8), (key, cap.max_audio_channels)


def test_an_operator_can_describe_a_devices_audio():
    caps = resolve_device_capabilities({"scoring": {"device_capabilities": {
        "my receiver": {"max_resolution": 2160, "codecs": ["h264", "hevc"],
                        "audio_codecs": ["AAC", "DTS-HD MA", "TrueHD"],
                        "max_audio_channels": 8},
    }}})
    cap = resolve_device_capability("My Receiver", caps)
    assert cap.direct_play_audio == frozenset({"aac", "dtshd", "truehd"})
    assert audio_transcode_share("DTS-HD MA", 7.1, {"My Receiver": 10}, caps) == 0.0
    # Omitting the audio keys is CONSERVATIVE, not permissive.
    caps2 = resolve_device_capabilities({"scoring": {"device_capabilities": {"box": 1080}}})
    assert audio_transcode_share("DTS", 5.1, {"box": 10}, caps2) == 1.0


# ── small pure helpers ──────────────────────────────────────────────────────

def test_bitrate_falls_back_to_size_over_runtime():
    """45% of this library's movie rows and 93% of its episode rows report a zero video
    bitrate — without the fallback the axis would be blind on most of the library."""
    assert bitrate_mbps(video_bitrate=8_000_000) == 8.0
    # 4 GB over 100 minutes ≈ 5.33 Mbps total, 90% of it video.
    derived = bitrate_mbps(video_bitrate=0, size_bytes=4e9, runtime_seconds=6000)
    assert 4.7 < derived < 4.9
    assert bitrate_mbps(0, None, None) is None
    assert bitrate_mbps(None, 4e9, 0) is None


def test_container_from_path():
    assert container_from_path("/movies/A (2019) - [Bluray-1080p].mkv") == "mkv"
    assert container_from_path("C:\\m\\B.MP4") == "mp4"
    assert container_from_path("no-extension") is None
    assert container_from_path(None) is None


def test_summarise_group_d_reports_what_a_constant_looks_like():
    flat = summarise_group_d([12.0] * 92 + [8.0] * 4 + [5.0] * 2 + [1.0] * 2)
    assert flat["mode"] == 12.0 and flat["share_at_mode"] == 0.92 and flat["distinct"] == 4
    assert summarise_group_d([])["n"] == 0
    assert summarise_group_d(None)["n"] == 0
    assert summarise_group_d([1.0, None, "x", 2.0])["n"] == 2


# ── the config gate + legacy byte-identity ──────────────────────────────────

def test_the_flag_defaults_to_on():
    """Deliberate: the legacy path is measurably broken, and the ladder + delete floor
    shipped in the same change are anchored on the v2 distribution."""
    for cfg in (None, {}, {"scoring": {}}, {"scoring": {"device_fit_v2": None}}):
        assert resolve_device_fit(cfg).enabled is True


def test_the_flag_turns_it_off_and_the_profile_disappears():
    for cfg in ({"scoring": {"device_fit_v2": False}},
                {"scoring": {"device_fit_v2": {"enabled": False}}}):
        assert resolve_device_fit(cfg).enabled is False
        assert build_transcode_profile(platform_usage=HOUSEHOLD,
                                       stream_decisions=DECISIONS,
                                       transcode_fingerprint=FINGERPRINT,
                                       settings=resolve_device_fit(cfg)) is None


def test_malformed_config_falls_back_to_the_shipped_defaults():
    for bad in ("nonsense", 7, [], {"magnitude": "oops"}, {"cause_weights": "nope"}):
        s = resolve_device_fit({"scoring": {"device_fit_v2": bad}})
        assert s.enabled is True
        assert s.magnitude > 0


def test_operator_cause_weights_are_normalised():
    s = resolve_device_fit({"scoring": {"device_fit_v2": {
        "cause_weights": {"audio": 2, "codec": 2, "nonsense": 99}}}})
    assert abs(sum(s.cause_weights.values()) - 1.0) < 1e-9
    assert s.cause_weights["audio"] == 0.5 and s.cause_weights["codec"] == 0.5
    assert s.cause_weights["subtitle"] == 0.0


def test_magnitude_scales_the_penalty_linearly():
    a = device_fit_penalty(_profile(settings=DeviceFitSettings(magnitude=15.0)),
                           **dict(CLEAN, audio_codec="DTS", audio_channels=5.1))[0]
    b = device_fit_penalty(_profile(settings=DeviceFitSettings(magnitude=30.0)),
                           **dict(CLEAN, audio_codec="DTS", audio_channels=5.1))[0]
    assert abs(b - 2 * a) < 0.02


# ── the two scorers ─────────────────────────────────────────────────────────

_MOVIE = dict(movie={"tmdbId": 1}, completion_pct=0.0, completion_threshold=0.9,
              collection_members={}, watched_tmdb_ids=set(), genre_affinity={}, credits={})


def _movie_bd(**kw):
    return score_movie(**_MOVIE, **kw, return_breakdown=True)[1]


def _show_bd(**kw):
    return score_show({"genres": ["Drama"]}, **kw, return_breakdown=True)[1]


def test_movie_legacy_path_is_byte_identical_when_the_flag_is_off():
    """No profile → the v1 terms, and D4 is a hard 0.0. This is the same call the golden
    corpus fixture makes, which is what proves the equality across the whole corpus."""
    legacy = dict(platform_usage=HOUSEHOLD, transcode_stats={}, video_codec="h264",
                  target_resolution=2160)
    bd = _movie_bd(**legacy)
    assert (bd["D1_device_capability"], bd["D2_transcode_avoidance"],
            bd["D3_platform_ceiling"]) == (6.0, 5.0, 4.0)
    assert bd["D4_transcode_risk"] == 0.0


def test_movie_v2_path_replaces_the_bonuses_with_one_penalty():
    bd = _movie_bd(platform_usage=HOUSEHOLD, transcode_stats={}, video_codec="h264",
                   target_resolution=2160, transcode_profile=_profile(),
                   audio_codec="DTS-HD MA", audio_channels=7.1, subtitles="eng/eng/fre",
                   audio_languages="fre", video_bitrate=90_000_000,
                   relative_path="X.mkv")
    assert (bd["D1_device_capability"], bd["D2_transcode_avoidance"],
            bd["D3_platform_ceiling"]) == (0.0, 0.0, 0.0)
    assert bd["D4_transcode_risk"] < -2.0


def test_show_path_has_the_same_two_paths():
    legacy = _show_bd(platform_usage=HOUSEHOLD, transcode_stats={},
                      video_codec="h264", target_resolution=2160)
    assert legacy["D1_device_capability"] == 6.0 and legacy["D4_transcode_risk"] == 0.0
    v2 = _show_bd(platform_usage=HOUSEHOLD, transcode_stats={}, video_codec="h264",
                  target_resolution=2160, transcode_profile=_profile(),
                  audio_codec="DTS", audio_channels=5.1, audio_languages="jpn",
                  subtitles="eng/jpn", container="mkv")
    assert v2["D1_device_capability"] == 0.0
    assert v2["D4_transcode_risk"] < 0.0


def test_a_stub_series_scores_zero_on_group_d_under_v2():
    """7,600 of this library's 11,973 series own no episode file. Under v1 they collected
    a flat +2.0 from D2's 'unknown codec' branch; under v2 they must score a hard 0.0."""
    stub = _show_bd(platform_usage=HOUSEHOLD, transcode_stats={}, transcode_profile=_profile())
    assert stub["D4_transcode_risk"] == 0.0
    assert stub["D1_device_capability"] == 0.0
    # ...and the v1 branch it replaces, for the record.
    assert _show_bd(platform_usage=HOUSEHOLD,
                    transcode_stats={})["D2_transcode_avoidance"] == 2.0


def test_group_d_never_raises_the_score_under_v2():
    p = _profile()
    for kw in ({}, dict(video_codec="x264", target_resolution=720),
               dict(video_codec="av1", target_resolution=2160, audio_codec="TrueHD",
                    audio_channels=7.1, subtitles="a/b/c/d/e/f", audio_languages="jpn",
                    video_bitrate=99_000_000, relative_path="x.avi")):
        with_v2 = score_movie(**_MOVIE, **kw, transcode_profile=p)
        without = score_movie(**_MOVIE, **kw, transcode_profile=None,
                              platform_usage=None, transcode_stats=None)
        assert with_v2 <= without
