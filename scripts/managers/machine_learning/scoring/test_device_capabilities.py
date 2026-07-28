"""scoring/test_device_capabilities.py — the Group-D device capability matrix.
================================================================================
Guards the contract of ``_shared._DEVICE_CAPABILITIES`` and the three things that read
it (D1 primary-device capability, D2 transcode avoidance, D3 platform ceiling):

  * the platform strings THIS household's Tautulli cache actually reports all resolve
    (the bug class that once left 42% of plays scoring as "unknown device");
  * a name-matched 4K model gets 2160 while the bare family name stays conservative;
  * **AV1 is never transcode-safe**, on any hardware — Plex transcodes it regardless of
    what the silicon can decode, so the direct-play rung must be unreachable;
  * an unknown device still degrades to today's neutral behaviour (never guessed at);
  * OBSERVED transcode events still dominate the static prior;
  * ``_DEVICE_RESOLUTION_CEILING`` back-compat readers — which walk the dict and take
    the FIRST fuzzy hit — see exactly what they saw before.

Everything here is pure: no cache reads, no service imports. The household platform
strings are inlined (see ``scripts/support/cache/tautulli/platforms.json``) so the test
does not depend on a cache file that a run may rewrite.
"""
from __future__ import annotations

from scripts.managers.machine_learning.scoring._shared import (
    _AVC,
    _DEVICE_CAPABILITIES,
    _DEVICE_RESOLUTION_CEILING,
    _PLEX_NEVER_DIRECT_PLAY,
    DeviceCapability,
    codec_direct_play_share,
    codec_transcode_prior,
    device_resolution_ceiling,
    normalize_codec,
    resolve_device_capabilities,
    resolve_device_capability,
)
from scripts.managers.machine_learning.scoring.movie_scorer import score_movie

# The six platforms this household's Tautulli history actually reports, with their real
# play counts (937 plays total) — cache/tautulli/platforms.json.
HOUSEHOLD = {"Windows": 368, "Tizen": 361, "Android": 156,
             "iOS": 35, "Chrome": 14, "PlayStation": 3}


def _d(**kw):
    """score_movie's Group-D breakdown for a minimal movie."""
    base = dict(
        movie={"tmdbId": 1}, completion_pct=0.0, completion_threshold=0.9,
        collection_members={}, watched_tmdb_ids=set(), genre_affinity={}, credits={},
    )
    base.update(kw)
    _, bd = score_movie(**base, return_breakdown=True)
    return (bd["D1_device_capability"], bd["D2_transcode_avoidance"],
            bd["D3_platform_ceiling"])


# ── the platform strings this household really reports ───────────────────────

def test_every_real_household_platform_resolves():
    """No entry in the household's own platform mix may fall through to "unknown".
    A platform that resolves to None contributes to D3's denominator but never to its
    capable count, which is exactly how 396 of 937 plays once became invisible."""
    for platform in HOUSEHOLD:
        assert resolve_device_capability(platform) is not None, platform


def test_real_household_platform_ceilings():
    assert device_resolution_ceiling("Windows") == 2160      # Plex desktop
    assert device_resolution_ceiling("Tizen") == 2160        # Samsung smart TV
    assert device_resolution_ceiling("Android") == 1080      # handheld
    assert device_resolution_ceiling("iOS") == 1080          # handheld
    assert device_resolution_ceiling("Chrome") == 1080       # Plex Web
    assert device_resolution_ceiling("PlayStation") == 2160


def test_the_whole_household_is_hevc_capable_but_not_via_chrome():
    """Plex Web on Chrome cannot direct-play HEVC (and Plex cannot direct STREAM it —
    it is direct play or a full transcode), so Chrome is the one hole in this household's
    HEVC coverage. 920 of 937 plays are still covered → the top rung."""
    assert "hevc" not in resolve_device_capability("Chrome").direct_play
    assert "hevc" in resolve_device_capability("Safari").direct_play
    share = codec_direct_play_share("hevc", HOUSEHOLD)
    assert 0.97 < share < 1.0
    assert codec_transcode_prior("hevc", HOUSEHOLD) == 5.0


def test_encoder_names_normalise_to_codec_names():
    # 1,584 of this library's 2,003 movie files report the ENCODER ("x264"), not the codec.
    assert normalize_codec("x264") == "h264"
    assert normalize_codec("X265") == "hevc"
    assert normalize_codec("AVC") == "h264"
    assert normalize_codec("XviD") == "mpeg4"
    assert normalize_codec("mpeg2video") == "mpeg2"
    # A codec the matrix has no opinion about → None ("no prior"), never "unsupported".
    assert normalize_codec("theora") is None
    assert normalize_codec("") is None and normalize_codec(None) is None


# ── specificity: a 4K model beats the conservative family name ───────────────

def test_named_4k_models_beat_the_conservative_family_name():
    # Roku and Fire TV both still ship 1080p sticks, so the BARE family name is the safe
    # value and only a name-matched 4K SKU claims 2160.
    assert device_resolution_ceiling("Roku") == 1080
    assert device_resolution_ceiling("Roku Ultra") == 2160
    assert device_resolution_ceiling("Roku Streaming Stick 4K") == 2160
    assert device_resolution_ceiling("Fire TV") == 1080
    assert device_resolution_ceiling("Fire TV Stick 4K") == 2160
    assert device_resolution_ceiling("Fire TV Cube") == 2160
    assert device_resolution_ceiling("Chromecast") == 1080
    assert device_resolution_ceiling("Chromecast Ultra") == 2160


def test_specific_key_is_not_shadowed_by_a_shorter_one():
    """The old matcher took the FIRST insertion-order hit, so a model-specific key could
    never win. resolve_device_capability picks the most specific entry instead."""
    # exact match wins over a longer key that merely contains the platform
    assert device_resolution_ceiling("Android") == 1080
    assert device_resolution_ceiling("Android TV") == 2160
    # longest key contained IN the platform wins over the catch-all "tv"
    assert device_resolution_ceiling("Samsung Smart TV") == 2160
    assert device_resolution_ceiling("Roku TV") == 1080          # -> "roku", not "tv"
    # "chrome" must not swallow "chromecast"
    assert resolve_device_capability("Chromecast") is _DEVICE_CAPABILITIES["chromecast"]
    assert resolve_device_capability("Chrome") is _DEVICE_CAPABILITIES["chrome"]
    # console generations
    assert device_resolution_ceiling("PlayStation 4") == 1080
    assert device_resolution_ceiling("PlayStation 5") == 2160


def test_the_public_release_platform_families_are_all_covered():
    """The device names a public release will actually meet."""
    for platform, expected in [
        ("Apple TV", 2160), ("Apple TV 4K", 2160), ("tvOS", 2160),
        ("iOS", 1080), ("iPadOS", 1080), ("iPhone", 1080), ("iPad", 1080),
        ("Android", 1080), ("Android TV", 2160), ("Nvidia Shield", 2160),
        ("Google TV", 1080), ("Chromecast with Google TV 4K", 2160),
        ("Tizen", 2160), ("webOS", 2160), ("Vizio", 2160), ("SmartCast", 2160),
        ("Hisense", 2160), ("Sony", 2160), ("TCL", 2160),
        ("Xbox One", 2160), ("Xbox Series X", 2160), ("PlayStation", 2160),
        ("Windows", 2160), ("macOS", 2160), ("Linux", 2160),
        ("Plex HTPC", 2160), ("Plex Media Player", 2160), ("Kodi", 2160),
        ("Chrome", 1080), ("Safari", 1080), ("Firefox", 1080), ("Edge", 1080),
    ]:
        assert device_resolution_ceiling(platform) == expected, platform


def test_only_safari_among_the_browsers_direct_plays_hevc():
    assert "hevc" in resolve_device_capability("Safari").direct_play
    for browser in ("Chrome", "Firefox", "Edge", "Web"):
        assert "hevc" not in resolve_device_capability(browser).direct_play, browser


# ── AV1: never transcode-safe, whatever the hardware ─────────────────────────

def test_no_table_entry_claims_av1():
    """Shield / Roku Ultra / Fire TV Stick 4K / Chromecast with Google TV all have AV1
    HARDWARE decode. It does not matter: Plex transcodes AV1 to HEVC/H.264 on almost
    every client anyway, so no entry may advertise it as direct-play."""
    assert _PLEX_NEVER_DIRECT_PLAY == frozenset({"av1"})
    for key, cap in _DEVICE_CAPABILITIES.items():
        assert "av1" not in cap.direct_play, key


def test_av1_is_never_transcode_safe_even_on_av1_capable_hardware():
    for platforms in (
        {"Nvidia Shield": 100},                 # full AV1 hardware decode
        {"Roku Ultra": 100},                    # recent models decode AV1
        {"Fire TV Stick 4K Max": 100},          # 2022+ decodes AV1
        {"Chromecast with Google TV 4K": 100},  # decodes AV1
        HOUSEHOLD,
        None,                                   # no device data at all
    ):
        prior = codec_transcode_prior("av1", platforms)
        assert prior < 5.0, platforms
        assert prior == 1.0, platforms


def test_av1_never_reaches_the_direct_play_rung_through_the_scorer():
    d2_h264 = _d(platform_usage={"Nvidia Shield": 100}, transcode_stats={},
                 video_codec="h264", target_resolution=2160)[1]
    d2_av1 = _d(platform_usage={"Nvidia Shield": 100}, transcode_stats={},
                video_codec="av1", target_resolution=2160)[1]
    assert d2_h264 == 5.0          # the safe rung is reachable...
    assert d2_av1 == 1.0           # ...but never by AV1


def test_an_operator_cannot_configure_av1_as_direct_play():
    """The AV1 constraint is Plex's, not the device's, so it is not operator-tunable."""
    caps = resolve_device_capabilities({"scoring": {"device_capabilities": {
        "my av1 box": {"max_resolution": 2160, "codecs": ["h264", "hevc", "av1"]},
    }}})
    assert caps["my av1 box"].direct_play == frozenset({"h264", "hevc"})
    assert codec_transcode_prior("av1", {"My AV1 Box": 50}, caps) == 1.0


# ── legacy codecs stop scoring as "never transcoded, +5" ─────────────────────

def test_legacy_codecs_lose_the_direct_play_credit():
    # XviD/DivX/MPEG-2/VC-1 transcode on every modern client — the same judgement the
    # Sonarr legacy_regrab pass already makes. 104 of this library's movie files.
    for codec in ("xvid", "divx", "mpeg2", "vc1"):
        assert codec_direct_play_share(codec, HOUSEHOLD) == 0.0
        assert codec_transcode_prior(codec, HOUSEHOLD) == 1.0
    # ...while the codecs the household really direct-plays are untouched.
    for codec in ("x264", "h264", "hevc", "x265"):
        assert codec_transcode_prior(codec, HOUSEHOLD) == 5.0


def test_partial_device_support_earns_a_middle_rung():
    # VP9: Windows/Android/Chrome direct-play it, Tizen/iOS/PlayStation do not → 57%.
    share = codec_direct_play_share("vp9", HOUSEHOLD)
    assert 0.5 <= share < 0.75
    assert codec_transcode_prior("vp9", HOUSEHOLD) == 3.0
    # A single 1080p handheld household: VP9 on Android is fully covered.
    assert codec_transcode_prior("vp9", {"Android": 10}) == 5.0
    # ...and not at all on an Apple-only one.
    assert codec_transcode_prior("vp9", {"Apple TV": 10}) == 1.0


# ── unknown devices / codecs degrade to today's neutral behaviour ────────────

def test_an_unknown_device_is_never_guessed_at():
    assert resolve_device_capability("Nonesuch OS") is None
    assert device_resolution_ceiling("Nonesuch OS") is None
    # D1 and D3 stay 0.0 rather than inventing a ceiling.
    d1, _, d3 = _d(platform_usage={"Nonesuch OS": 50}, target_resolution=2160,
                   transcode_stats={}, video_codec="h264")
    assert (d1, d3) == (0.0, 0.0)


def test_an_unknown_device_does_not_drag_the_d2_prior():
    """An unrecognised platform enters NEITHER the numerator nor the denominator, so it
    dilutes nothing — a household of only unknown devices keeps today's full credit."""
    assert codec_direct_play_share("h264", {"Nonesuch OS": 999}) is None
    assert codec_transcode_prior("hevc", {"Nonesuch OS": 999}) == 5.0
    # mixed: the share speaks only about the devices the matrix recognises
    assert codec_direct_play_share("hevc", {"Nonesuch OS": 999, "Tizen": 1}) == 1.0


def test_an_unknown_codec_keeps_todays_full_credit():
    assert codec_direct_play_share("theora", HOUSEHOLD) is None
    assert codec_transcode_prior("theora", HOUSEHOLD) == 5.0
    assert _d(platform_usage=HOUSEHOLD, transcode_stats={},
              video_codec="theora", target_resolution=1080)[1] == 5.0


def test_no_platform_data_keeps_todays_full_credit():
    assert codec_transcode_prior("h264", None) == 5.0
    assert codec_transcode_prior("h264", {}) == 5.0


# ── observed transcode events still dominate the static prior ────────────────

def test_observed_transcodes_dominate_the_prior():
    """The static matrix is a COLD-START prior. A codec the household has actually been
    caught transcoding is scored from that observation, and the prior is not consulted —
    even when the matrix would have said the codec is perfectly safe here."""
    # h264 is universal, so the prior says +5 (see below)...
    assert codec_transcode_prior("h264", HOUSEHOLD) == 5.0
    assert _d(platform_usage=HOUSEHOLD, transcode_stats={},
              video_codec="h264", target_resolution=1080)[1] == 5.0
    # ...but an OBSERVED h264 transcode overrides it with the friendly-codec rung.
    assert _d(platform_usage=HOUSEHOLD, transcode_stats={"h264/aac": 12},
              video_codec="h264", target_resolution=1080)[1] == 2.0
    # An observed transcode of a non-friendly codec still earns nothing at all.
    assert _d(platform_usage=HOUSEHOLD, transcode_stats={"vp9/aac": 3},
              video_codec="vp9", target_resolution=1080)[1] == 0.0


def test_the_prior_only_fires_where_there_is_no_observation():
    """Two calls differing ONLY in whether the codec appears in transcode_stats."""
    seen = _d(platform_usage=HOUSEHOLD, transcode_stats={"mpeg2/ac3": 4},
              video_codec="mpeg2", target_resolution=1080)[1]
    unseen = _d(platform_usage=HOUSEHOLD, transcode_stats={"h264/ac3": 4},
                video_codec="mpeg2", target_resolution=1080)[1]
    assert seen == 0.0        # observed: mpeg2 is not on the friendly list
    assert unseen == 1.0      # unobserved: the device prior speaks instead


def test_d2_still_neutral_without_a_codec_or_without_transcode_data():
    assert _d(platform_usage=HOUSEHOLD, transcode_stats={},
              video_codec=None, target_resolution=1080)[1] == 2.0
    # transcode_stats=None (no Tautulli transcode data at all) → the whole term is 0.0,
    # exactly as before; the prior does not sneak credit in through that door.
    assert _d(platform_usage=HOUSEHOLD, transcode_stats=None,
              video_codec="h264", target_resolution=1080)[1] == 0.0


# ── config extensibility ─────────────────────────────────────────────────────

def test_config_can_add_a_device_the_table_has_never_heard_of():
    caps = resolve_device_capabilities({"scoring": {"device_capabilities": {
        "Weird Projector": {"max_resolution": 1080, "codecs": ["h264", "hevc"]},
    }}})
    assert device_resolution_ceiling("Weird Projector 900X", caps) == 1080
    assert resolve_device_capability("weird projector", caps).direct_play == \
        frozenset({"h264", "hevc"})
    # ...and it reaches the scorer through the device_capabilities kwarg.
    d1, d2, d3 = _d(platform_usage={"Weird Projector 900X": 10}, transcode_stats={},
                    video_codec="hevc", target_resolution=1080,
                    device_capabilities=caps)
    assert (d1, d2, d3) == (6.0, 5.0, 4.0)
    # without the override the same platform is simply unknown
    assert _d(platform_usage={"Weird Projector 900X": 10}, transcode_stats={},
              video_codec="hevc", target_resolution=1080)[0] == 0.0


def test_config_can_override_a_shipped_entry():
    caps = resolve_device_capabilities({"scoring": {"device_capabilities": {
        "roku": {"max_resolution": 2160, "codecs": ["h264", "hevc"]},
    }}})
    assert device_resolution_ceiling("Roku", caps) == 2160
    assert device_resolution_ceiling("Roku") == 1080      # shipped table untouched


def test_config_shorthand_and_codec_aliases():
    caps = resolve_device_capabilities({"scoring": {"device_capabilities": {
        "old plasma": 720,                                    # bare int shorthand
        "htpc box": {"max_resolution": 2160, "codecs": ["X265"]},   # alias-normalised
    }}})
    assert caps["old plasma"] == DeviceCapability(720, _AVC)   # codecs default to h264
    assert caps["htpc box"].direct_play == frozenset({"hevc"})


def test_a_malformed_entry_is_skipped_without_taking_the_table_down():
    caps = resolve_device_capabilities({"scoring": {"device_capabilities": {
        "good box": 1080,
        "bad box": {"max_resolution": "not a number"},
        "worse box": ["nonsense"],
        "zero box": 0,
        "": 2160,
    }}})
    assert caps["good box"].max_resolution == 1080
    for bad in ("bad box", "worse box", "zero box", ""):
        assert bad not in caps
    assert len(caps) == len(_DEVICE_CAPABILITIES) + 1


def test_absent_or_broken_config_returns_the_shipped_table():
    for cfg in (None, {}, {"scoring": {}}, {"scoring": {"device_capabilities": None}},
                {"scoring": {"device_capabilities": []}}, "not a dict", 7):
        assert resolve_device_capabilities(cfg) == dict(_DEVICE_CAPABILITIES)


# ── back-compat: _DEVICE_RESOLUTION_CEILING still works ──────────────────────

def _legacy_ceiling(platform):
    """EXACTLY what a pre-existing reader does: walk the dict, take the first fuzzy hit."""
    p = platform.lower().strip()
    return next((c for k, c in _DEVICE_RESOLUTION_CEILING.items()
                 if k in p or p in k), None)


def test_back_compat_dict_is_derived_from_the_matrix():
    assert _DEVICE_RESOLUTION_CEILING == {
        k: cap.max_resolution for k, cap in _DEVICE_CAPABILITIES.items()
    }
    # same keys, SAME ORDER — legacy readers depend on insertion order
    assert list(_DEVICE_RESOLUTION_CEILING) == list(_DEVICE_CAPABILITIES)


def test_back_compat_first_match_readers_see_what_they_always_saw():
    """The 24 shipped keys stay FIRST, in their original order, so a first-hit reader
    resolves the household's platforms to the same ceilings as before the matrix landed."""
    assert _legacy_ceiling("Tizen") == 2160
    assert _legacy_ceiling("iOS") == 1080
    assert _legacy_ceiling("Windows") == 2160
    assert _legacy_ceiling("Android") == 1080       # not shadowed by "android tv"
    assert _legacy_ceiling("Chrome") == 1080
    assert _legacy_ceiling("PlayStation") == 2160   # not shadowed by "playstation 4"


def test_back_compat_reader_never_over_estimates_a_new_device():
    """A legacy first-hit reader cannot see the appended model-specific keys, so it lands
    on the conservative family value — an UNDER-estimate, never an over-estimate."""
    assert _legacy_ceiling("Roku Ultra") == 1080          # new resolver says 2160
    assert device_resolution_ceiling("Roku Ultra") == 2160


def test_the_shipped_matrix_is_internally_consistent():
    for key, cap in _DEVICE_CAPABILITIES.items():
        assert key == key.lower().strip() and key, key
        assert cap.max_resolution in (720, 1080, 2160), (key, cap.max_resolution)
        assert "h264" in cap.direct_play, key      # the universal baseline
        assert isinstance(cap.direct_play, frozenset), key
