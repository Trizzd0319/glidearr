"""
scoring/_shared.py — the cross-engine scoring commons (pure).
================================================================================
The constants AND pure helpers that the movie and show watchability engines BOTH
use. Extracted (ML Step 2.1) so neither scorer imports the other: previously
``show_scorer`` reached into ``movie_scorer`` for the device/cert tables, the
``score_to_profile`` mapping and a copy-pasted affinity helper, which coupled the
two engines and risked silent drift. Now ``movie_scorer`` and ``show_scorer`` are
siblings that each import from here; **this module imports neither of them**, so
there is exactly one definition of every shared symbol and no directional
dependency between the engines.

Folds in what MIGRATION.md Step 2 earmarked for ``scoring/constants.py`` — but a
plain ``constants.py`` could not host the three pure helpers below, so the single
shared space carries both the tables and the helpers that operate on them.

Contents (all pure — no I/O, no service imports, no global_cache):
  * QUALITY_PROFILE_THRESHOLDS  — score → profile-name-pattern ladder
  * _DEVICE_CAPABILITIES        — device platform → DeviceCapability(max_resolution,
                                  direct_play codecs); the cold-start prior D1/D2/D3 read
  * _DEVICE_RESOLUTION_CEILING  — device platform → max supported resolution (DERIVED
                                  from _DEVICE_CAPABILITIES; kept for back-compat readers)
  * _TRANSCODE_FRIENDLY_CODECS  — codecs that direct-play on typical devices
  * _KIDS_CERTS                 — certifications that mark kids content
  * normalize_codec(value)                              — x264/avc/h.264 → "h264" etc.
  * normalize_audio_codec(value)                        — "EAC3 Atmos" → "eac3",
                                                          "DTS-HD MA" → "dtshd" etc.
  * audio_transcode_share(codec, channels, platform_usage, caps)
                                                        — share of the household's PLAYS
                                                          on devices that must transcode
                                                          this audio track (v2 Group-D)
  * resolve_device_capability(platform, caps=None)      — Tautulli platform string →
                                                          DeviceCapability (fuzzy, exact →
                                                          longest-substring → shortest-reverse)
  * device_resolution_ceiling(platform, caps=None)      — the same match, resolution only
  * codec_direct_play_share(codec, platform_usage, caps) — share of the household's PLAYS on
                                                          devices that direct-play the codec
  * codec_transcode_prior(codec, platform_usage, caps)  — D2's cold-start credit for a codec
                                                          with NO observed transcode history
  * resolve_device_capabilities(config)                 — config.scoring.device_capabilities
                                                          merged over the shipped table
  * normalize_lang(value)                               — language NAME or ISO 639-2
                                                          code → ISO 639-1 code, so the
                                                          G1 penalty compares like with like
  * affinity_topk(names, aff_map, cap)                  — top-3 mean affinity → cap
  * user_rating_score(user_rating, *, slope, pos_cap, neg_cap, confidence)
                                                        — Group-A4 declared-rating bump
                                                          (movie defaults vs gentler show knobs)
  * intent_recency_factor(listed_at, now, …)            — Group-A5 staleness multiplier
                                                          (1.0 for an UNDATED feed)
  * watchlist_intent_score(entry, cap, *, now)          — Group-A5 explicit-intent bump,
                                                          graded by source x recency x members
  * intent_hold_active(entry, now, *, dormancy_days)    — is the watchlist DELETE SHIELD
                                                          still live for this title?
  * resolve_intent_inputs(config, intent_index)         — config.scoring.watchlist_intent →
                                                          (index, cap, half_life_days,
                                                          stale_floor); cap 0.0 = inert
  * intent_decay_kwargs(half_life_days, stale_floor)    — the two A5 decay kwargs, OMITTED
                                                          when unset so the scorer defaults
                                                          (the constants) still apply
  * related_graph_affinity(related_ids, watched_ids, *, cap)
                                                        — Group-C3 collaborative neighbour-watch
                                                          signal (generalises C1/C2 onto Trakt's
                                                          related graph)
  * resolve_quality_ladder(config)                      — config.scoring.quality_ladder →
                                                          validated ladder (or the default)
  * ladder_rung_for_resolution(token, ladder)           — lowest rung whose label mentions
                                                          a resolution token ("2160" → 46)
  * score_to_profile(score, ladder=None)                — score → profile-name pattern
  * select_profile_id(score, ranked_profiles, target_resolution=None, ladder=None) — score → id
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple


# ── Scorer revision token ────────────────────────────────────────────────────
# Bumped by hand whenever a change alters what ``score_movie``/``score_show``
# return for UNCHANGED inputs (a new signal wired in, a device-table entry, a
# formula tweak). BOTH per-row score memos (radarr ``movie_score_memo`` /
# sonarr ``show_score_memo``) fold this into their CONTEXT hash, so a scoring
# change forces exactly one full rescore instead of silently serving scores
# computed by the previous revision. Without it the memos are keyed only on the
# INPUTS — which do not change when the CODE does, so a stale score can survive
# indefinitely (only the 1% sampled parity audit would eventually notice).
#
#   2  D1/D2/D3 activated in the MOVIE path (target_resolution + video_codec now
#      reach score_movie) and the device ceiling table gained tizen/ios.
#   3  Device table became a CAPABILITY matrix (resolution ceiling + per-device
#      direct-play codec set). D1/D3 resolve platforms with a longest-match resolver
#      instead of first-insertion-order; "roku"/"fire tv" dropped to a conservative
#      1080 (only name-matched 4K SKUs get 2160); D2 gained a codec PRIOR for codecs
#      with no observed transcode history — AV1 can no longer reach the direct-play
#      rung and legacy codecs (XviD/DivX/MPEG-2/VC-1) stop scoring as "never
#      transcoded, +5". Observed transcode events still dominate, unchanged.
#   4  Group-D became a transcode-RISK PENALTY (``scoring.device_fit_v2``, default ON) —
#      see scoring/device_fit.py. D1/D2/D3 report 0.0 and the new ``D4_transcode_risk``
#      carries the group, negative. The device matrix gained an AUDIO half
#      (``direct_play_audio`` / ``max_audio_channels``). The quality LADDER and the
#      DELETE-floor family were re-anchored on the resulting distribution in the same
#      change. Flipping the flag OFF restores revision-3 behaviour byte-for-byte — the
#      token still has to move, because a household that flips it must rescore.
#   5  NEW SIGNAL: ``A5_intent`` (``scoring.watchlist_intent``, default ON, cap 8) — the
#      scorecard's first EXPLICIT-INTENT term. Graded by source strength (the acquisition
#      scorer's own feed ranking), by how many household members watchlisted the title,
#      and by staleness where the feed carries a real ``listed_at``. Group A only, so the
#      other six groups are untouched. The bump alone was NOT sufficient: the watchlist
#      union is a per-PASS household input that appears in NEITHER memo's row hash, so
#      both CONTEXT hashes now fold in a fingerprint of the intent index (see
#      radarr/quality/space_pressure + sonarr/cache/episode_files) — without that,
#      adding a title to your watchlist would never invalidate its memoized score.
#   6  TWO Group-A FEEDS, batched into ONE bump on purpose: a full memo reseed takes this
#      household's run from ~39s to ~211s, and paying that twice for two changes that land
#      in the same group on the same day is waste, not caution.
#        (a) A5 gains MAL ``plan_to_watch``. The blocker was never the scoring — the
#            ladder already ranked ``mal_plantowatch`` at 1.00 and ``DATED_SOURCES``
#            already contained it — it was that MAL carries no id that joins a library
#            row. ``services/mal/id_bridge`` closes that with an EXACT normalized-title
#            match against ``seriesType == "anime"`` Sonarr rows (and animation-genre
#            Radarr rows), ambiguity-dropped, cached at ``mal/{user}/id_map``. MAL is
#            attributed to the SAME household member as Trakt, so one human's three lists
#            stay one member on the ladder.
#        (b) A4 gains a MOVIE source. ``score_movie`` has accepted ``user_rating`` since
#            it was written and NOTHING ever passed it — every movie in this library
#            reported ``A4_user_rating: 0.0`` while the show scorer had read
#            ``trakt/{user}/ratings/shows`` from its first line. The movie twin
#            (``_build_user_movie_rating_map``) mirrors that implementation exactly, and
#            the Radarr CONTEXT hash now folds in the ratings map for the same reason the
#            Sonarr one always has: the rating lives outside the parquet, so without it a
#            re-rated film would keep serving its memoized score forever.
#      Neither the quality LADDER, the DELETE floor nor ``untouched_base`` moved: the
#      combined blast radius is small enough that the anchors still mean what they meant
#      (see thresholds/test_delete_floor_anchor + likelihood/test_untouched_anchor).
#
# NOT BUMPED for the GLOBAL WATCHED BAR (lifecycle.watched_definition), deliberately.
# That change moves score INPUTS (is_watched / watch_count / last_watched_at in both
# parquets), not scorer CODE, and both memos already key on exactly those inputs:
#   * radarr — the per-row key is ``_h(row)`` over the WHOLE row
#     (quality/space_pressure), so any changed watch column self-invalidates;
#   * sonarr — ``_SCORE_COLS`` (cache/episode_files) explicitly lists
#     ``is_watched``, ``last_watched_at`` and ``watch_count``, so a series whose
#     episodes move rehashes;
#   * both CONTEXT hashes fold in the watched-id set (``watched_tvdb_ids`` /
#     ``watched_tmdb_ids``), which shrinks — so the first run after the change
#     invalidates the whole memo anyway.
# Bumping would force a full rescore of ~12k series + ~2k movies for no correctness
# gain. Measured blast radius: 17 series and 102 movies move at all.
#
# NOT BUMPED for the A5 IDENTITY FIX (``trakt.household_member``), deliberately — and this
# one is worth stating precisely, because it LOOKS like a scoring change. Naming the
# household member the Trakt/MAL lists belong to collapses one human's two watchlist
# entries into ONE member, which moves A5 on 8 titles (5.76 → 4.80). But it moves an
# INPUT, not scorer code: ``watchlist_intent_score`` returns the identical number for an
# identical entry. The member COUNT is one of the fields ``intent_memo_fingerprint``
# digests (services/_intent_index), and that digest is in BOTH context hashes — so the
# memo invalidates on its own. Proven end to end, not asserted: see
# services/test_intent_identity + radarr/quality/test_intent_memo_invalidation.
#
# NOT BUMPED for THREADING THE A5 DECAY KNOBS either (``scoring.watchlist_intent
# .half_life_days`` / ``stale_floor`` now reach the scorers through
# ``resolve_intent_inputs``). This DOES change scorer code — the rule above is the test,
# and the rule is about OUTPUT for UNCHANGED INPUTS. At the pinned 365.0 / 0.25, which are
# exactly ``INTENT_HALF_LIFE_DAYS`` / ``INTENT_STALE_FLOOR``, the output is byte-identical:
# the 500-case golden fixture (scoring/test_score_golden) reproduces and both scorers are
# pinned against passing the values explicitly. A LATER edit to either knob does move
# scores — which is why both knobs are now in both CONTEXT hashes, so that edit
# invalidates the memos by itself and still needs no bump.
#
# Combined measured blast radius on the 6,449-title file-owning pool: 7 titles move their
# final integer score (0.109%), against 1.41% and 1.19% for the last two changes that
# needed no re-anchor. The quality LADDER (rung-2 admissions 49 → 49), the DELETE floor
# (5,357 → 5,358 below 17) and ``untouched_base`` (no mover crosses the score-20 boundary
# its 25 + fhd_cutoff 45 implies) all stand.
SCORER_REVISION: int = 6


# ── Quality profile thresholds (score → profile name pattern) ────────────────
# CALIBRATION, not policy invention. RE-ANCHORED TWICE, for two different reasons:
#
#   * 80/70/60/50/35 (shipped) — absolute guesses about a distribution that does not
#     exist here. Measured max watchability was 58 (radarr) / 69 (sonarr), so the top
#     two rungs were UNREACHABLE and every owned 4K title was slated to demote to 720p.
#   * 62/46/41/36/35 — anchored on percentiles of a 14,056-title pool. Two flaws, both
#     now fixed: (1) the pool was 64% FILE-LESS Sonarr pilot stubs, which sit at the
#     bottom of the score axis and dragged every percentile down ~6-8 points — a rung
#     calibrated on titles that own no file cannot say anything about what quality to
#     hold a file at; (2) it was measured against Group D v1, which added a near-constant
#     +12 to 92% of the library (see SCORER_REVISION 4 / scoring/device_fit.py), so the
#     whole score axis was translated upward by an amount carrying no information.
#
# The rungs below are anchored on the FILE-OWNING population ONLY — 6,449 titles
# (2,076 movies + the 4,373 series that own at least one episode file), scored with
# Group D v2:
#
#   rung  score  percentile  titles at/above  (movies / series)  meaning
#   ----  -----  ----------  ---------------  -----------------  --------------------
#     1     50      p99.9            7          5 /   2          very top tier
#     2     38      p99.5           34         25 /   9          4K entry
#     3     33      p99             67         47 /  20          best 1080p
#     4     29      p98            130         75 /  55          strong 1080p
#     5     25      p97            224        127 /  97          good 1080p (>=1080p entry)
#     6      0        —          6,449                           floor (SD → 720p)
#
# ⚠ THE 4K SANITY CHECK NO LONGER PASSES AT p99.5, AND THAT IS REPORTED, NOT HIDDEN.
# The household curates 67 titles at 2160p today (62 movies — 58 on the `ultra`
# instance plus 4 held at 2160p elsewhere — and 5 series). Rung 2 at p99.5 = 38 keeps
# only 12 of them; the rung that reproduces the household's own 4K judgement is p99
# (33), which admits 67 titles library-wide and keeps 17 of the owned 4K set. Two
# effects compound: the file-owning pool is 2.2x SMALLER than the stub-inclusive one
# (so a fixed percentile admits proportionally fewer TITLES — p99.5 of 6,449 is ~32
# titles where p99.5 of 14,056 was ~70), and Group D v2 compressed the top of the
# distribution (movie max 71 -> 58). An operator who wants the ladder to track the
# household's existing 4K shelf sets ``scoring.quality_ladder`` with rung 2 at 33
# (or lowers rung 2 alone); the rungs here are the literal percentiles, which is what
# "anchored" has to mean if the word is to keep any content.
#
# MITIGATION THAT ALREADY EXISTS: the ladder only PROPOSES a tier. Actual 4K
# acquisition is gated by ``watch_likelihood.uhd_cutoff`` (75) and
# ``routing.movies.4k_dual_min_score`` (75) — both on the DIFFERENT watch-likelihood
# scale, both untouched by this re-anchor — and 4K bonus copies are exempt from the
# space-pressure floor gate. So a low rung-2 count restricts NEW 4K, it does not by
# itself delete the existing shelf.
#
# THIS IS CALIBRATION TO *THIS* HOUSEHOLD'S DISTRIBUTION. The rungs are not universal
# constants and will drift as the library and watch history change; re-derive them
# when the distribution moves materially, and re-derive them whenever Group D's shape
# changes (that is precisely what invalidated the previous set). The long-term
# replacement is ``ml.thresholds`` — the calibrated-probability path already built
# (machine_learning/thresholds/), which anchors decisions on isotonic-calibrated
# P(watch within H) instead of on percentiles of an uncalibrated 0-100 score. Until
# that path leaves shadow mode, these rungs are the honest interim.
#
# Config-overridable via ``scoring.quality_ladder`` — see :func:`resolve_quality_ladder`.
QUALITY_PROFILE_THRESHOLDS: list[tuple[int, str]] = [
    (50, "Remux 2160p"),          # p99.9 — top tier          (was 62)
    (38, "Remux 2160p"),          # p99.5 — 4K entry          (was 46) — see the ⚠ above
    (33, "Remux 1080p"),          # p99   — strong affinity   (was 41)
    (29, "Bluray 1080p"),         # p98   — watched content   (was 36)
    (25, "WEBDL 1080p"),          # p97   — good affinity     (was 35)
    (0,  "HD 720p"),              # Minimum floor — SD absorbed into 720p
                                  # (older content without HD masters still
                                  #  benefits from 720p container/metadata)
]

# ── Device capability matrix ─────────────────────────────────────────────────
# THIS TABLE IS A **COLD-START PRIOR**, NOT A MEASUREMENT.
#
# It answers "what can a device of this name probably do?" for a household (or a
# device) with NO observed history. Wherever real Tautulli data exists it WINS:
#   * D2 consults this table ONLY for a codec that has never appeared in an observed
#     ``transcode_stats`` pair. A codec the household HAS been seen transcoding is
#     scored from that observation, exactly as before — the prior is never consulted.
#   * The richer observed structures (``tautulli/device_codec_matrix``,
#     ``tautulli/transcode_fingerprint``) are the long-term replacement for the codec
#     half of this table; it exists so a brand-new install, or a device that has only
#     just appeared in history, is not scored as "unknown → neutral" forever.
#
# CEILING CONVENTION (why a family name is sometimes conservative):
#   * A vendor that STILL SELLS a 1080p SKU under the family name gets the SAFE value
#     for the bare family name — "Roku" could be a Roku Express, "Fire TV" could be a
#     Stick Lite, "Chromecast"/"Google TV" could be the HD dongle. Only a NAME-MATCHED
#     4K model ("Roku Ultra", "Fire TV Stick 4K", "Chromecast Ultra") gets 2160.
#     Getting this wrong is not free in either direction: D1 pays −2 when a file is
#     ABOVE the primary device's ceiling, so an over-estimate silently blesses files
#     the household must downscale, and an under-estimate penalises 4K it can play.
#   * A SMART-TV OS name (Tizen, webOS, VIDAA, SmartCast) gets 2160: the name only
#     appears when the TV itself is the Plex client, and the connected-smart-TV base
#     for those OSes is overwhelmingly 4K. This also preserves the shipped tizen /
#     lg tv / samsung tv values.
#   * A SET-TOP BOX whose vendor no longer sells a sub-4K SKU (Apple TV/tvOS since
#     2022, Nvidia Shield) gets 2160 — likewise preserving the shipped "apple tv".
#   * HANDHELDS (iOS/iPadOS/Android/phones/tablets) stay 1080: the panel is smaller
#     than the ceiling question, and a 4K file is wasted bytes there.
#
# CODEC CONVENTION — ``direct_play`` is what **PLEX** DIRECT-PLAYS on that client,
# which is NOT the same as what the SILICON can decode:
#   * ``h264`` (AVC) is the universal baseline and is present on every entry.
#   * ``hevc``/H.265 direct-plays broadly on modern TVs, streamers, handhelds, native
#     desktop apps and Safari — but NOT in Plex Web on Chrome/Firefox/Edge, and not on
#     the base PS4. Note Plex cannot DIRECT STREAM HEVC at all: it is direct play or a
#     full transcode, so a client that cannot direct-play HEVC always transcodes it.
#   * ``av1`` appears on **NO** entry, on purpose. Nvidia Shield, recent Roku Ultra /
#     Streaming Stick 4K, Fire TV Stick 4K/4K Max (2022+), Fire TV Cube 3rd gen and
#     Chromecast with Google TV all have AV1 hardware decode (Apple TV is the outlier —
#     the A15 does H.264/HEVC in hardware and AV1 only in limited/software form). It
#     does not matter: **Plex itself transcodes AV1 to HEVC/H.264 on almost every
#     client regardless of what the hardware can decode.** Plex HTPC 1.30.1+ has AV1
#     decode; the mobile and smart-TV apps largely do not. That exception is version-
#     gated and cannot be inferred from the platform string Tautulli reports, so the
#     prior stays pessimistic — see :data:`_PLEX_NEVER_DIRECT_PLAY`. An AV1 file is a
#     transcode risk on this stack and D2 must never score it as safe.
#   * ``vp9`` is listed only on Google/Chromium-lineage clients and the mpv-based
#     desktop apps.
#   * Legacy codecs (MPEG-2, VC-1, XviD/DivX → ``mpeg4``) appear NOWHERE: they
#     transcode on every modern client. This is the same judgement the Sonarr
#     ``scoring.codec_profiles.legacy_regrab`` pass already makes.
#
# EXTENDING IT: an operator adds or overrides an entry via
# ``scoring.device_capabilities`` — see :func:`resolve_device_capabilities`.

# ── Audio direct-play sets (Group-D v2) ──────────────────────────────────────
# WHY AUDIO IS HERE AT ALL: on this household's 133 ground-truth stream decisions the
# AUDIO track is the single biggest cause of transcoding — 51 of 133 (38%), against 48
# bitrate/resolution (36%), 19 subtitle (14%) and only 15 video-codec (11%). The v1
# Group-D terms modelled the 11% slice and nothing else. See scoring/device_fit.py.
#
# Same convention as the VIDEO ``direct_play`` set: this is what **PLEX** direct-plays on
# that client, not what the silicon could decode.
_AUDIO_BASELINE = frozenset({"aac", "mp3"})            # every Plex client, always
_AUDIO_BROWSER  = _AUDIO_BASELINE                      # Plex Web: AAC/MP3 stereo, nothing else
_AUDIO_MOBILE   = frozenset({"aac", "mp3", "ac3", "eac3", "flac"})
_AUDIO_TV       = frozenset({"aac", "mp3", "ac3", "eac3", "flac"})
#: Desktop/HTPC-grade: the only class that decodes or passes through the lossless formats.
_AUDIO_FULL     = frozenset({"aac", "mp3", "ac3", "eac3", "dts", "dtshd", "truehd",
                             "flac", "pcm", "opus", "vorbis"})


class DeviceCapability(NamedTuple):
    """What a device FAMILY can be assumed to do before any history is observed.

    ``max_resolution``     — the resolution ceiling D1/D3 compare a target against.
    ``direct_play``        — the VIDEO codecs PLEX direct-plays on that client (see the
                             codec convention above; hardware decode support is NOT the
                             same thing and is deliberately not what this models).
    ``direct_play_audio``  — the AUDIO codecs Plex direct-plays on that client. Consulted
                             ONLY by the v2 device-fit engine (scoring/device_fit.py);
                             the legacy D1/D2/D3 terms never read it, which is why it is
                             a DEFAULTED field — every existing construction site and
                             every equality assertion against one is unchanged.
    ``max_audio_channels`` — the channel count above which the client must DOWNMIX (which
                             is an audio transcode). 2 for browsers, 8 everywhere else.
    """
    max_resolution: int
    direct_play: frozenset
    direct_play_audio: frozenset = _AUDIO_BASELINE
    max_audio_channels: int = 8


# Codec sets, shared by the entries below so a policy change lands in one place.
_AVC          = frozenset({"h264"})                          # universal baseline only
_AVC_HEVC     = frozenset({"h264", "hevc"})                  # modern TV / streamer / handheld
_AVC_VP9      = frozenset({"h264", "vp9"})                   # Chromium browsers (no HEVC in Plex Web)
_AVC_HEVC_VP9 = frozenset({"h264", "hevc", "vp9"})           # Google / Android lineage
_DESKTOP      = frozenset({"h264", "hevc", "vp9"})           # native mpv-based desktop / HTPC apps

# Codecs no entry may ever claim to direct-play, whatever the table (or an operator's
# config override) says. AV1 is here because the constraint is PLEX's, not the device's.
_PLEX_NEVER_DIRECT_PLAY: frozenset = frozenset({"av1"})

# The matrix. ORDER IS LOAD-BEARING for one reason only: the derived, back-compat
# ``_DEVICE_RESOLUTION_CEILING`` preserves it, and legacy readers of that dict walk it
# in order and take the FIRST fuzzy hit. The 24 entries the table shipped with are kept
# first, in their original order, so those readers see exactly what they saw before;
# every new entry is APPENDED. The scorers do NOT depend on this order — they call
# :func:`resolve_device_capability`, which prefers the most SPECIFIC key.
_DEVICE_CAPABILITIES: "dict[str, DeviceCapability]" = {
    # ── shipped entries (original order preserved for back-compat readers) ────
    "apple tv":          DeviceCapability(2160, _AVC_HEVC),
    "apple tv 4k":       DeviceCapability(2160, _AVC_HEVC),
    "tv":                DeviceCapability(2160, _AVC_HEVC),   # last-resort "some TV"
    "lg tv":             DeviceCapability(2160, _AVC_HEVC),
    "samsung tv":        DeviceCapability(2160, _AVC_HEVC),
    "chromecast":        DeviceCapability(1080, _AVC_HEVC_VP9),
    "chromecast ultra":  DeviceCapability(2160, _AVC_HEVC_VP9),
    # CHANGED (was 2160): "Roku" and "Fire TV" are the two families whose vendors still
    # sell 1080p sticks in volume, so the BARE family name is now the safe value and only
    # the name-matched 4K SKUs below claim 2160.
    "roku":              DeviceCapability(1080, _AVC_HEVC),
    "fire tv":           DeviceCapability(1080, _AVC_HEVC),
    "ipad":              DeviceCapability(1080, _AVC_HEVC),
    "iphone":            DeviceCapability(1080, _AVC_HEVC),
    # Tautulli reports Samsung smart TVs by their OS name ("Tizen"), not "Samsung TV",
    # and Apple handhelds as "iOS", not "iPhone"/"iPad" — neither string fuzzy-matched
    # anything above, so on this household's real platform mix 396 of 937 plays (42%)
    # scored as "unknown device" and D3 could never clear its 75% capable-share rung.
    "tizen":             DeviceCapability(2160, _AVC_HEVC),
    "ios":               DeviceCapability(1080, _AVC_HEVC),
    "android":           DeviceCapability(1080, _AVC_HEVC_VP9),
    "android tv":        DeviceCapability(2160, _AVC_HEVC_VP9),
    # PlayStation: the FAMILY keeps 2160 (shipped value, and what a legacy reader of
    # _DEVICE_RESOLUTION_CEILING still sees); the generation-specific keys below split
    # the base PS4 (1080p, no HEVC) from the Pro/PS5.
    "playstation":       DeviceCapability(2160, _AVC),
    "xbox":              DeviceCapability(2160, _AVC_HEVC),
    "web":               DeviceCapability(1080, _AVC),        # generic Plex Web
    "chrome":            DeviceCapability(1080, _AVC_VP9),    # Plex Web: VP9 yes, HEVC no
    "safari":            DeviceCapability(1080, _AVC_HEVC),   # the browser that DOES do HEVC
    "windows":           DeviceCapability(2160, _DESKTOP),
    "mac":               DeviceCapability(2160, _DESKTOP),
    "linux":             DeviceCapability(2160, _DESKTOP),
    "kodi":              DeviceCapability(2160, _DESKTOP),

    # ── appended: streaming sticks / boxes ────────────────────────────────────
    "roku ultra":                   DeviceCapability(2160, _AVC_HEVC),
    "roku streaming stick 4k":      DeviceCapability(2160, _AVC_HEVC),
    "roku express 4k":              DeviceCapability(2160, _AVC_HEVC),
    "roku streambar":               DeviceCapability(2160, _AVC_HEVC),
    "fire tv stick 4k":             DeviceCapability(2160, _AVC_HEVC),
    "fire tv stick 4k max":         DeviceCapability(2160, _AVC_HEVC),
    "fire tv cube":                 DeviceCapability(2160, _AVC_HEVC),
    "fireos":                       DeviceCapability(1080, _AVC_HEVC),
    "amazon fire tv":               DeviceCapability(1080, _AVC_HEVC),
    "chromecast with google tv":    DeviceCapability(1080, _AVC_HEVC_VP9),   # an HD SKU exists
    "chromecast with google tv 4k": DeviceCapability(2160, _AVC_HEVC_VP9),
    "google tv":                    DeviceCapability(1080, _AVC_HEVC_VP9),   # ditto
    "nvidia shield":                DeviceCapability(2160, _AVC_HEVC_VP9),
    "shield android tv":            DeviceCapability(2160, _AVC_HEVC_VP9),

    # ── appended: Apple ───────────────────────────────────────────────────────
    "tvos":                         DeviceCapability(2160, _AVC_HEVC),
    "ipados":                       DeviceCapability(1080, _AVC_HEVC),
    "macos":                        DeviceCapability(2160, _DESKTOP),

    # ── appended: smart TVs (OS names + the vendors that report a brand) ──────
    "samsung":                      DeviceCapability(2160, _AVC_HEVC),
    "webos":                        DeviceCapability(2160, _AVC_HEVC),
    "vidaa":                        DeviceCapability(2160, _AVC_HEVC),       # Hisense
    "hisense":                      DeviceCapability(2160, _AVC_HEVC),
    "vizio":                        DeviceCapability(2160, _AVC_HEVC),
    "smartcast":                    DeviceCapability(2160, _AVC_HEVC),       # Vizio's OS
    "panasonic":                    DeviceCapability(2160, _AVC_HEVC),
    "philips":                      DeviceCapability(2160, _AVC_HEVC_VP9),   # Android TV
    "sony":                         DeviceCapability(2160, _AVC_HEVC_VP9),   # Bravia = Google TV
    "tcl":                          DeviceCapability(2160, _AVC_HEVC_VP9),

    # ── appended: consoles ────────────────────────────────────────────────────
    "xbox one":                     DeviceCapability(2160, _AVC_HEVC),
    "xbox series":                  DeviceCapability(2160, _AVC_HEVC),
    "xbox series s":                DeviceCapability(2160, _AVC_HEVC),
    "xbox series x":                DeviceCapability(2160, _AVC_HEVC),
    "playstation 4":                DeviceCapability(1080, _AVC),
    "playstation 4 pro":            DeviceCapability(2160, _AVC),
    "playstation 5":                DeviceCapability(2160, _AVC_HEVC),
    "ps4":                          DeviceCapability(1080, _AVC),
    "ps5":                          DeviceCapability(2160, _AVC_HEVC),

    # ── appended: desktop / HTPC / browsers ───────────────────────────────────
    "plex htpc":                    DeviceCapability(2160, _DESKTOP),
    "plex media player":            DeviceCapability(2160, _DESKTOP),
    "firefox":                      DeviceCapability(1080, _AVC),            # no HEVC in Plex Web
    "edge":                         DeviceCapability(1080, _AVC),            # ditto (extension-gated)
    "opera":                        DeviceCapability(1080, _AVC_VP9),
}

# ── the AUDIO half of the matrix ─────────────────────────────────────────────
# Assigned by CLASS rather than inline on each of the 65 entries above, for two reasons:
# the video table stays byte-identical to the reviewed version (its literal entries are
# untouched), and an audio-policy change lands in ONE place instead of 65. Every key must
# appear in exactly one class — ``test_device_capabilities`` asserts total coverage, so a
# device added to the video table without an audio class is a test failure, not a silent
# fallback to the stereo baseline.
#
# CLASS RATIONALE (what PLEX direct-plays, not what the chip decodes):
#   * browser  — Plex Web transcodes essentially all audio to AAC stereo. Corroborated by
#                this household's own numbers: Chrome is 3 direct / 11 transcode (79%),
#                by far the worst client it owns.
#   * mobile   — AAC/MP3/AC3/E-AC3/FLAC; no DTS, no TrueHD. Channels are NOT capped at 2:
#                Plex mobile passes multichannel through on cast/AirPlay, and this
#                household's Android is 131 direct / 5 transcode (3.7%) — a 2-channel cap
#                would have predicted a transcode on the 71% of the library that is 5.1.
#   * tv       — smart TVs, streaming sticks, consoles, Apple TV. Dolby Digital / DD+
#                passthrough, but NOT DTS (Samsung dropped DTS after the 2018 models, and
#                Plex on tvOS transcodes it) and never the lossless formats.
#   * full     — desktop/HTPC-grade decoders (Plex Desktop, Plex HTPC/PMP, Kodi, Shield):
#                the only class that handles DTS-HD MA / TrueHD / PCM.
_AUDIO_CLASS_KEYS: "tuple[tuple[frozenset, int, tuple[str, ...]], ...]" = (
    (_AUDIO_BROWSER, 2, ("web", "chrome", "safari", "firefox", "edge", "opera")),
    (_AUDIO_MOBILE, 8, ("ipad", "iphone", "ios", "ipados", "android")),
    (_AUDIO_FULL, 8, ("windows", "mac", "macos", "linux", "kodi",
                      "plex htpc", "plex media player",
                      "nvidia shield", "shield android tv")),
    # everything else in the table is a TV / streamer / console.
)


def _apply_audio_classes() -> None:
    """Fill ``direct_play_audio`` / ``max_audio_channels`` on every shipped entry.

    Runs once at import. Anything not named in :data:`_AUDIO_CLASS_KEYS` falls into the
    TV/streamer class — the majority of the table, and the conservative middle of the
    three real classes (it has DD/DD+ but not DTS or the lossless formats)."""
    assigned: dict[str, tuple] = {}
    for codecs, channels, keys in _AUDIO_CLASS_KEYS:
        for k in keys:
            assigned[k] = (codecs, channels)
    for key, cap in list(_DEVICE_CAPABILITIES.items()):
        codecs, channels = assigned.get(key, (_AUDIO_TV, 8))
        _DEVICE_CAPABILITIES[key] = cap._replace(
            direct_play_audio=codecs, max_audio_channels=channels)


_apply_audio_classes()

# Audio codec aliases → the canonical token the audio half of the matrix speaks. The *arr
# mediaInfo ``audio_codec`` column is a DISPLAY name with optional object-audio suffixes
# ("EAC3 Atmos", "TrueHD Atmos", "DTS-HD MA"), so a bare dict lookup misses most of it —
# :func:`normalize_audio_codec` strips the suffix before looking here.
_AUDIO_CODEC_ALIASES: dict[str, str] = {
    "aac": "aac", "aac lc": "aac", "he-aac": "aac", "mp4a": "aac",
    "mp3": "mp3", "mp2": "mp2", "mpeg audio": "mp2",
    "ac3": "ac3", "ac-3": "ac3", "dolby digital": "ac3", "dd": "ac3",
    "eac3": "eac3", "e-ac3": "eac3", "ec-3": "eac3", "dolby digital plus": "eac3",
    "ddp": "eac3", "dd+": "eac3",
    "dts": "dts", "dts-es": "dts", "dca": "dts",
    "dts-hd": "dtshd", "dts-hd ma": "dtshd", "dts-hd hra": "dtshd", "dts-x": "dtshd",
    "dtshd": "dtshd", "dts:x": "dtshd",
    "truehd": "truehd", "true-hd": "truehd", "mlp": "truehd",
    "flac": "flac", "opus": "opus", "vorbis": "vorbis", "wma": "wma",
    "pcm": "pcm", "lpcm": "pcm", "pcm_s16le": "pcm", "pcm_s24le": "pcm",
}

#: Suffixes an *arr display name appends to the underlying codec. They describe the OBJECT
#: layer (Atmos rides on E-AC-3 / TrueHD; DTS:X on DTS-HD), not a different codec, so they
#: are stripped before the alias lookup rather than enumerated as separate keys.
_AUDIO_CODEC_SUFFIXES: tuple[str, ...] = (" atmos", " audio", " (atmos)")

# BACK-COMPAT: device platform → max supported resolution. DERIVED from
# :data:`_DEVICE_CAPABILITIES` (same keys, same order) so the old readers — the
# ``trakt.movies.scorer`` re-export shim and anything that walks the dict looking for the
# first fuzzy hit — keep working unchanged. New code should call
# :func:`device_resolution_ceiling` / :func:`resolve_device_capability` instead, which
# match the most SPECIFIC key rather than the first one in insertion order.
_DEVICE_RESOLUTION_CEILING: dict[str, int] = {
    k: cap.max_resolution for k, cap in _DEVICE_CAPABILITIES.items()
}

# Codec aliases → the canonical token used by the capability matrix. The *arr mediaInfo
# columns are encoder-named, not codec-named ("x264" is 1,584 of this library's 2,003
# movie files), so an un-normalised lookup would miss nearly everything.
_CODEC_ALIASES: dict[str, str] = {
    "h264": "h264", "x264": "h264", "avc": "h264", "avc1": "h264", "h.264": "h264",
    "hevc": "hevc", "h265": "hevc", "x265": "hevc", "h.265": "hevc",
    "hev1": "hevc", "hvc1": "hevc",
    "av1": "av1", "av01": "av1",
    "vp9": "vp9", "vp09": "vp9", "vp8": "vp8",
    "mpeg2": "mpeg2", "mpeg2video": "mpeg2", "mpeg-2": "mpeg2", "mp2v": "mpeg2",
    "mpeg4": "mpeg4", "xvid": "mpeg4", "divx": "mpeg4",
    "msmpeg4": "mpeg4", "msmpeg4v3": "mpeg4",
    "vc1": "vc1", "vc-1": "vc1", "wmv3": "vc1",
}

# Codecs that commonly require transcoding on typical consumer devices.
# If the household has never transcoded this codec, score it well.
#
# UNCHANGED SET / UNCHANGED MEANING: this is only ever consulted on the branch where a
# transcode HAS been observed for the codec, where it selects the PARTIAL (+2) rung over
# no credit at all. It can never award the direct-play (+5) rung, so ``av1``'s presence
# here does not contradict the AV1 rule — that rule lives in :func:`codec_transcode_prior`,
# which owns the no-observation branch. (Known wart, deliberately left alone so households
# WITH transcode data see no semantic change: the tokens here are raw, so an "x264" file
# that has been observed transcoding scores 0.0 rather than 2.0.)
_TRANSCODE_FRIENDLY_CODECS: set[str] = {
    "h264", "avc", "hevc", "h265", "av1",
    "aac", "ac3", "eac3",
}

# Kids certifications
_KIDS_CERTS: frozenset[str] = frozenset(
    {"g", "pg", "tv-g", "tv-y", "tv-y7", "all", "e", "u"}
)


# Language NAME / ISO 639-2 (3-letter) → ISO 639-1 (2-letter). The *arr APIs hand
# us the display NAME ("English") under originalLanguage.name, and mediaInfo audio
# tracks use 3-letter codes ("eng"); the G1 penalty and the preferred_languages
# config both speak ISO 639-1 ("en"). Without this map, "english" != "en" so EVERY
# English title wrongly earned the −8 non-preferred-language penalty. Covers the
# languages that actually appear in a typical *arr library; unknown values pass
# through unchanged (an unrecognised non-preferred language still gets penalised).
_LANGUAGE_ALIAS_TO_ISO1: dict[str, str] = {
    # display names
    "english": "en", "japanese": "ja", "french": "fr", "korean": "ko",
    "hindi": "hi", "chinese": "zh", "mandarin": "zh", "cantonese": "zh",
    "italian": "it", "german": "de", "spanish": "es", "castilian": "es",
    "portuguese": "pt", "russian": "ru", "dutch": "nl", "flemish": "nl",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi",
    "polish": "pl", "turkish": "tr", "arabic": "ar", "hebrew": "he",
    "thai": "th", "vietnamese": "vi", "indonesian": "id", "malay": "ms",
    "tagalog": "tl", "filipino": "tl", "greek": "el", "czech": "cs",
    "hungarian": "hu", "romanian": "ro", "ukrainian": "uk", "tamil": "ta",
    "telugu": "te", "malayalam": "ml", "kannada": "kn", "bengali": "bn",
    "marathi": "mr", "punjabi": "pa", "urdu": "ur", "persian": "fa", "farsi": "fa",
    "catalan": "ca", "icelandic": "is", "croatian": "hr", "serbian": "sr",
    "slovak": "sk", "slovenian": "sl", "bulgarian": "bg", "lithuanian": "lt",
    "latvian": "lv", "estonian": "et", "latin": "la",
    # ISO 639-2/B (and a couple /T) 3-letter codes seen in mediaInfo audio tracks
    "eng": "en", "jpn": "ja", "fra": "fr", "fre": "fr", "kor": "ko", "hin": "hi",
    "zho": "zh", "chi": "zh", "ita": "it", "deu": "de", "ger": "de", "spa": "es",
    "por": "pt", "rus": "ru", "nld": "nl", "dut": "nl", "swe": "sv", "nor": "no",
    "dan": "da", "fin": "fi", "pol": "pl", "tur": "tr", "ara": "ar", "heb": "he",
    "tha": "th", "vie": "vi", "ind": "id", "ell": "el", "gre": "el", "ces": "cs",
    "cze": "cs", "hun": "hu", "ron": "ro", "rum": "ro", "ukr": "uk", "fas": "fa",
    "per": "fa",
}


def preferred_language_available(audio_languages, subtitles, preferred_languages) -> bool:
    """True if a title is WATCHABLE in a preferred language — a preferred-language
    AUDIO track (dub) OR a preferred-language SUBTITLE track (sub) is present on the
    actual file.

    ``audio_languages`` / ``subtitles`` are the parquet's slash/comma-joined ISO-code
    strings (e.g. ``"jpn/eng"`` for an anime with an English dub, ``"eng/eng"`` for
    English subs); ``preferred_languages`` is the config list (e.g. ``["en"]``). Empty
    preference → True (no language gate at all).

    This makes the Group-G1 penalty FILE-AWARE: an anime whose ORIGINAL language is
    Japanese but which ships an English dub (audio) OR English subtitles is consumable
    and must not be penalised for its origin. Only a file with NEITHER a preferred
    audio NOR a preferred subtitle track is genuinely un-watchable as-is (→ G1 penalty
    + a candidate for language re-acquisition).
    """
    pref = {normalize_lang(p) for p in (preferred_languages or [])}
    pref.discard(None)
    if not pref:
        return True
    for blob in (audio_languages, subtitles):
        if not blob:
            continue
        for code in str(blob).replace(",", "/").split("/"):
            code = code.strip()
            if code and normalize_lang(code) in pref:
                return True
    return False


def normalize_lang(value) -> str | None:
    """Language display NAME or ISO 639-2 code → ISO 639-1 code (lowercased).

    ``"English"`` / ``"english"`` / ``"eng"`` → ``"en"``; an already-2-letter code
    (``"en"``) passes through; an unknown value passes through lowercased (so an
    unrecognised non-preferred language is still treated as non-preferred). Empty /
    None → None. Pure — used by both scorers' G1 so the penalty compares like-for-like.
    """
    if not value:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    return _LANGUAGE_ALIAS_TO_ISO1.get(s, s)


# ── Device capability resolution (Group-D commons) ───────────────────────────

def normalize_codec(value) -> "str | None":
    """A codec token from *arr mediaInfo / Tautulli → the canonical name the capability
    matrix speaks (``"x264"``/``"AVC"``/``"h.264"`` → ``"h264"``, ``"x265"`` → ``"hevc"``,
    ``"XviD"``/``"DivX"`` → ``"mpeg4"``).

    Returns None for an empty value OR for a token the matrix has no opinion about — the
    caller MUST treat None as "no prior", not as "unsupported", so an unrecognised codec
    keeps today's neutral behaviour instead of being punished for being unknown. Pure."""
    if not value:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    return _CODEC_ALIASES.get(s)


def normalize_audio_codec(value) -> "str | None":
    """An *arr ``audio_codec`` display name → the canonical token the audio half of the
    capability matrix speaks (``"EAC3 Atmos"`` → ``"eac3"``, ``"DTS-HD MA"`` → ``"dtshd"``,
    ``"TrueHD Atmos"`` → ``"truehd"``).

    Returns None for an empty value OR for a name the matrix has no opinion about — the
    caller MUST read None as "no prior", never as "unsupported", so an unrecognised format
    contributes NO risk rather than being punished for being unknown. Pure."""
    if not value:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    for suffix in _AUDIO_CODEC_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    hit = _AUDIO_CODEC_ALIASES.get(s)
    if hit is not None:
        return hit
    # "dts-hd master audio" and friends: fall back to the longest alias PREFIX.
    prefixes = [k for k in _AUDIO_CODEC_ALIASES if s.startswith(k)]
    return _AUDIO_CODEC_ALIASES[max(prefixes, key=len)] if prefixes else None


def audio_transcode_share(codec, channels, platform_usage, capabilities=None) -> "float | None":
    """Share of the household's PLAYS that happen on devices which must TRANSCODE this
    audio track — the audio twin of :func:`codec_direct_play_share`, expressed as risk
    (incapable / known) rather than as capability.

    A device transcodes the track when it cannot direct-play the CODEC **or** when the
    track carries more channels than it can render (a downmix is an audio transcode). Both
    halves matter: DTS on a Samsung TV fails the codec test, 5.1 in Plex Web fails the
    channel test.

    Play-weighted for the same reason the video share is: the household is described by
    where it actually watches, not by what it owns. Platforms the matrix does not
    recognise enter NEITHER numerator nor denominator. Returns None ("no prior", not "no
    risk") with no platform data, an unrecognised codec, or no resolved platform. Pure."""
    a = normalize_audio_codec(codec)
    if a is None:
        return None
    if not platform_usage:
        return None
    try:
        ch = int(float(channels)) if channels is not None else 0
    except (TypeError, ValueError):
        ch = 0
    known = incapable = 0.0
    for platform, plays in (platform_usage or {}).items():
        cap = resolve_device_capability(platform, capabilities)
        if cap is None:
            continue
        try:
            n = float(plays or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        known += n
        if a not in cap.direct_play_audio or (ch > 0 and ch > cap.max_audio_channels):
            incapable += n
    if known <= 0:
        return None
    return incapable / known


def resolve_device_capability(platform, capabilities=None) -> "DeviceCapability | None":
    """A Tautulli/Plex platform string → its :class:`DeviceCapability`, or None when the
    matrix has no entry (the caller then degrades to neutral — an unknown device must
    never be guessed at).

    MATCH ORDER, most specific first, so a table entry can never be shadowed by a shorter
    one and the answer does not depend on dict insertion order:

      1. **exact** — ``"Android"`` → ``android`` (1080), NOT ``android tv`` (2160).
      2. **longest key contained IN the platform** — ``"Roku Ultra"`` → ``roku ultra``
         (2160) rather than ``roku`` (1080); ``"Samsung Smart TV"`` → ``samsung`` rather
         than the catch-all ``tv``.
      3. **shortest key that CONTAINS the platform** — the reverse direction the original
         matcher also allowed (``"Apple"`` → ``apple tv``). Shortest = most conservative,
         because a partial name cannot tell you which SKU it is.

    ``capabilities`` overrides the shipped table (see :func:`resolve_device_capabilities`).
    Pure."""
    if not platform:
        return None
    caps = capabilities if capabilities is not None else _DEVICE_CAPABILITIES
    key = str(platform).strip().lower()
    if not key:
        return None
    hit = caps.get(key)
    if hit is not None:
        return hit
    forward = [k for k in caps if k and k in key]
    if forward:
        return caps[max(forward, key=len)]
    reverse = [k for k in caps if k and key in k]
    if reverse:
        return caps[min(reverse, key=len)]
    return None


def device_resolution_ceiling(platform, capabilities=None) -> "int | None":
    """The max resolution a platform string can play, or None when unknown.
    :func:`resolve_device_capability` with the codec half discarded — what D1/D3 read."""
    cap = resolve_device_capability(platform, capabilities)
    return cap.max_resolution if cap is not None else None


def codec_direct_play_share(codec, platform_usage, capabilities=None) -> "float | None":
    """Share of the household's PLAYS that happen on devices which direct-play *codec*.

    Weighted by play count, so a household is described by where it actually watches
    rather than by which devices it owns. Only platforms the matrix RECOGNISES enter the
    calculation — an unknown device contributes to neither numerator nor denominator, so
    it dilutes nothing and is never guessed at.

    Returns None (meaning "no prior available", NOT "unsupported") when there is no
    platform data, when the codec is unrecognised, or when not one platform resolved.
    ``av1`` short-circuits to 0.0: Plex transcodes it on effectively every client
    regardless of hardware decode (see :data:`_PLEX_NEVER_DIRECT_PLAY`). Pure."""
    c = normalize_codec(codec)
    if c is None:
        return None
    if c in _PLEX_NEVER_DIRECT_PLAY:
        return 0.0
    if not platform_usage:
        return None
    known = capable = 0.0
    for platform, plays in (platform_usage or {}).items():
        cap = resolve_device_capability(platform, capabilities)
        if cap is None:
            continue
        try:
            n = float(plays or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        known += n
        if c in cap.direct_play:
            capable += n
    if known <= 0:
        return None
    return capable / known


def codec_transcode_prior(codec, platform_usage, capabilities=None, *,
                          full: float = 5.0) -> float:
    """D2's credit for a codec with **no observed transcode history** — the cold-start
    half of transcode avoidance.

    D2 used to hand out the full direct-play credit to any codec absent from
    ``transcode_stats``, which reads "we have never seen this transcode" as "it never
    will". On a household whose Tautulli history has no per-codec detail (this one's
    ``transcode_stats`` is a single ``{"unknown/unknown": 132}`` bucket) that meant an
    AV1 file and an XviD file both scored a clean +5. This replaces the unconditional
    +5 with the capability matrix's opinion, scaled by how much of the household's
    viewing happens on devices that can direct-play the codec:

        share >= 0.75  → ``full``  (the old value — a broadly supported codec is unchanged)
        share >= 0.50  → 3.0
        share >  0.0   → 2.0
        share == 0.0   → 1.0       (nothing in the house direct-plays it, but it has not
                                    actually been CAUGHT transcoding, so not 0.0)
        no prior       → ``full``  (unknown codec, or no recognised device → today's
                                    behaviour, unchanged)

    AV1 always lands on the 0.0-share rung, on any hardware, because the constraint is
    Plex's rather than the device's — see the codec convention on the matrix. Pure."""
    share = codec_direct_play_share(codec, platform_usage, capabilities)
    if share is None:
        return full
    if share >= 0.75:
        return full
    if share >= 0.50:
        return 3.0
    if share > 0.0:
        return 2.0
    return 1.0


def resolve_device_capabilities(config=None) -> "dict[str, DeviceCapability]":
    """The device capability matrix to use: ``config.scoring.device_capabilities`` merged
    OVER the shipped :data:`_DEVICE_CAPABILITIES`.

    The escape hatch for a device the shipped table has never heard of — or one it gets
    wrong for a particular household. The config form is keyed by a lowercase substring of
    the platform string Tautulli reports::

        "scoring": {"device_capabilities": {
            "shield android tv": {"max_resolution": 2160, "codecs": ["h264", "hevc", "vp9"]},
            "my projector":      {"max_resolution": 1080},
            "old plasma":        720
        }}

    A bare int is shorthand for ``{"max_resolution": <int>}``. ``codecs`` defaults to the
    universal H.264 baseline when omitted, and is normalised through
    :func:`normalize_codec`, so ``"x265"`` and ``"HEVC"`` both land on ``hevc``. An
    operator entry REPLACES a shipped entry with the same key.

    Codecs in :data:`_PLEX_NEVER_DIRECT_PLAY` (AV1) are stripped from operator entries
    too: that is a statement about what Plex does with the file, not about the device, so
    it is not an operator-tunable knob.

    Malformed entries are SKIPPED individually (a typo in one device must not discard the
    rest of the table), and any failure to read the config at all falls back to the
    shipped matrix — a bad config must never take a run down. Pure."""
    try:
        raw = ((config or {}).get("scoring", {}) or {}).get("device_capabilities")
    except Exception:
        return dict(_DEVICE_CAPABILITIES)
    if not raw or not isinstance(raw, dict):
        return dict(_DEVICE_CAPABILITIES)

    out = dict(_DEVICE_CAPABILITIES)
    for name, spec in raw.items():
        try:
            key = str(name).strip().lower()
            if not key:
                continue
            audio_raw = None
            channels = None
            if isinstance(spec, (int, float)) and not isinstance(spec, bool):
                res, codecs_raw = int(spec), None
            elif isinstance(spec, dict):
                res = int(spec.get("max_resolution", spec.get("resolution")))
                codecs_raw = spec.get("codecs", spec.get("direct_play"))
                audio_raw = spec.get("audio_codecs", spec.get("direct_play_audio"))
                channels = spec.get("max_audio_channels")
            else:
                continue
            if res <= 0:
                continue
            codecs = set(_AVC)
            if codecs_raw:
                normalised = {normalize_codec(c) for c in codecs_raw}
                normalised.discard(None)
                if normalised:
                    codecs = normalised
            # Audio half (v2 device-fit only). Omitted → the universal AAC/MP3 baseline,
            # which is the CONSERVATIVE answer: an operator who names a device but says
            # nothing about its audio gets "assume it downmixes anything exotic", not a
            # free pass. ``max_audio_channels`` likewise defaults to the table's 8 rather
            # than to 2, so an unspecified device is not accused of downmixing 5.1.
            audio = set(_AUDIO_BASELINE)
            if audio_raw:
                a_norm = {normalize_audio_codec(c) for c in audio_raw}
                a_norm.discard(None)
                if a_norm:
                    audio = a_norm
            try:
                max_ch = int(channels) if channels is not None else 8
            except (TypeError, ValueError):
                max_ch = 8
            out[key] = DeviceCapability(res, frozenset(codecs) - _PLEX_NEVER_DIRECT_PLAY,
                                        frozenset(audio), max(1, max_ch))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


# ── Shared pure helpers ──────────────────────────────────────────────────────

def affinity_topk(names: list[str], aff_map: dict, cap: float) -> float:
    """Top-3 mean affinity of *names* against *aff_map*, scaled to *cap*.

    The single source of truth for the Group-B / Group-E affinity bump used by
    BOTH ``score_movie`` (where it was a nested closure) and ``score_show`` (where
    it was a module-level duplicate). Returns 0.0 when there is nothing to match.

    *aff_map* is a ``{name: weight}`` map (e.g. actor/director/genre/studio
    affinity). Each present name contributes ``weight / max_weight``; the top-3
    such ratios are averaged and scaled to *cap*. Names are matched case-folded.
    """
    if not names or not aff_map:
        return 0.0
    top = max(aff_map.values(), default=1) or 1
    matched = [
        aff_map.get(n, aff_map.get(n.lower(), 0)) / top
        for n in names
        if aff_map.get(n, aff_map.get(n.lower(), 0)) > 0
    ]
    if not matched:
        return 0.0
    # Use top-3 average to reduce noise from long cast lists
    top3 = sorted(matched, reverse=True)[:3]
    return round(min(cap, (sum(top3) / len(top3)) * cap), 3)


def person_affinity_score(
    media_people_ids: dict,
    person_weights: dict,
    cap: float,
    *,
    role_weights: dict | None = None,
) -> float:
    """Group-C4 person-affinity: a title's people-overlap with the household's taste.

    The id-keyed parallel to Group-B's name-based ``affinity_topk`` — it is immune to
    "Scarlett Johansson" vs alias name-drift because it intersects stable
    ``tmdb_person_id`` ints. It MUST stay a separate function: ``affinity_topk`` does
    ``aff_map.get(n.lower())`` which raises ``AttributeError`` on an int key.

    ``media_people_ids`` — the title's people by role ``{role: [tmdb_person_id]}`` (from
                           the people_matrix forward map OR the live credits dict the
                           scorer already receives).
    ``person_weights``   — ``{tmdb_person_id: weight}`` household affinity (watched-set
                           derived; see ``aggregate_person_affinity``).
    ``cap``              — max contribution. With ``cap <= 0`` (the scorer default) this
                           returns 0.0 → the term is byte-identical until a caller opts in.

    Per matched person: ``(weight / max_weight) * role_weight``; the top-3 such products
    are averaged and scaled to ``cap`` (mirrors ``affinity_topk``'s top-3-mean shape so
    C4 behaves like the other affinity bumps, only keyed on ids). 0.0 when either map is
    empty or nothing matches. ⚠️ ``role_weight`` here is the SAME table
    ``aggregate_person_affinity`` already applied building ``person_weights`` — the
    weight acts at BOTH stages, so cross-role ratios are effectively squared
    end-to-end; the 2026-08-07 table is tuned under that regime (GLD-PPL-13, open —
    change either application site and the table's numbers change meaning).
    """
    if not media_people_ids or not person_weights or cap <= 0:
        return 0.0
    from scripts.managers.machine_learning.people_matrix.build import PERSON_ROLE_WEIGHTS
    role_weights = role_weights or PERSON_ROLE_WEIGHTS
    top = max(person_weights.values(), default=1) or 1

    contributions: list[float] = []
    for role, pids in media_people_ids.items():
        rw = role_weights.get(role, 0.0)
        if rw <= 0:
            continue
        for pid in pids:
            w = person_weights.get(pid, 0)
            if w > 0:
                contributions.append((w / top) * rw)
    if not contributions:
        return 0.0
    top3 = sorted(contributions, reverse=True)[:3]
    return round(min(cap, (sum(top3) / len(top3)) * cap), 3)


def resolve_person_affinity_inputs(config, affinity_raw) -> "tuple[dict, float]":
    """Owned-scorer Group-C4 inputs ``(person_weights, cap)`` from config + the cached
    household person-affinity (``people_matrix/affinity``, ``{str(person_id): weight}``).

    The single gate for BOTH the movie (space_pressure) and show (episode_files) upgrade
    paths so they can't drift. ``cap`` is forced to 0.0 — making C4 byte-identical — when
    the term is config-disabled (``scoring.person_affinity.enabled``) OR the people-matrix
    affinity is empty, so a library that never built the matrix is wholly unaffected.
    Default cap 8.0 (mirrors the Group-C2 ratios + the C4 integration test) when enabled
    and weights exist."""
    weights: dict[int, float] = {}
    for k, v in (affinity_raw or {}).items():
        try:
            weights[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    pa = ((config or {}).get("scoring", {}) or {}).get("person_affinity", {}) or {}
    enabled = bool(pa.get("enabled", True)) if isinstance(pa, dict) else bool(pa)
    try:
        cap = float(pa.get("cap", 8.0)) if isinstance(pa, dict) else 8.0
    except (TypeError, ValueError):
        cap = 8.0
    if not enabled or not weights or cap <= 0:
        return weights, 0.0
    return weights, cap


# ── Group-A5 — explicit watchlist intent ─────────────────────────────────────

#: Decay knobs for DATED intent. A title watchlisted six years ago and never watched is
#: weaker intent than one added last week — but it is not NO intent, so the exponential
#: decay is floored rather than allowed to reach zero. Half-life 365d: a year-old listing
#: is worth half a fresh one; ``STALE_FLOOR`` 0.25 is where a listing older than two
#: half-lives lands and stays. On this household every Trakt ``listed_at`` is May 2020
#: (~6.2 years → 2**-6.2 ≈ 0.014), so all 40 Trakt shows sit ON the floor: a Trakt-only
#: title is worth a quarter of a fresh listing, which is the intended verdict.
INTENT_HALF_LIFE_DAYS = 365.0
INTENT_STALE_FLOOR = 0.25

#: Shield expiry, in days of watchlister DORMANCY. Deliberately the SAME 90 days as
#: ``saga_retention.dormancy_window_days`` (factories/onboarding/schema): both rules answer
#: "is this person still an active viewer whose stated intent should hold disk?", and two
#: different answers to that question in one codebase is how a hold becomes unexplainable.
INTENT_DORMANCY_DAYS = 90


def _intent_age_days(listed_at, now) -> "float | None":
    """Whole days between an ISO-8601 listing timestamp and *now*; None if unparseable.

    Never NEGATIVE: a clock skew that puts a listing in the future must read as FRESH
    (0 days), not as a bonus multiplier above 1.0."""
    if not listed_at or now is None:
        return None
    s = str(listed_at).strip()
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now if isinstance(now, datetime) else None
    if ref is None:
        try:
            ref = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return max(0.0, (ref - dt).total_seconds() / 86400.0)


def intent_recency_factor(listed_at, now, *, half_life_days: float = INTENT_HALF_LIFE_DAYS,
                          stale_floor: float = INTENT_STALE_FLOOR) -> float:
    """Staleness multiplier in ``[stale_floor, 1.0]`` for one DATED listing.

    ``1.0`` when the feed is UNDATED (``listed_at`` None or unparseable) — the honest
    answer for Plex's union, which carries no per-item timestamp; inventing one from the
    rolling snapshot files would date every title to yesterday. Exponential half-life
    otherwise, floored so ancient-but-real intent still outranks none at all."""
    age = _intent_age_days(listed_at, now)
    if age is None:
        return 1.0
    hl = float(half_life_days or 0.0)
    if hl <= 0:
        return 1.0
    return max(float(stale_floor), 2.0 ** (-age / hl))


def watchlist_intent_score(entry, cap: float, *, now=None,
                           half_life_days: float = INTENT_HALF_LIFE_DAYS,
                           stale_floor: float = INTENT_STALE_FLOOR) -> float:
    """Group-A5 explicit-intent bump: "somebody in this house said they want to watch this".

    ``entry`` is one value from ``next_watch.build_intent_index`` —
    ``{"sources": (feed, …), "members": (name, …), "dated": {feed: iso}, "anchor": iso}``.
    ``cap`` is the max contribution; with ``cap <= 0`` (the scorer default) this returns
    0.0 → the term is byte-identical until a caller opts in, exactly like
    ``person_affinity_score``.

    Three graded terms, multiplied, then capped:

      * SOURCE strength — ``next_watch.INTENT_SOURCE_STRENGTH``, which is
        ``services/acquisition/scorer._SOURCE_SCORE`` over 100. The BEST evidence wins
        (max over sources), so a title on the live Plex watchlist is not dragged down by
        also sitting on a stale Trakt one.
      * RECENCY — per-source decay against that source's own ``listed_at``; undated feeds
        do not decay. Applied per source BEFORE the max, so "fresh on Plex, stale on
        Trakt" correctly resolves to fresh.
      * MEMBERS — ``next_watch.member_fraction``: 0.60 solo, +0.12 per extra member,
        1.0 at four. Solo is deliberately NOT full credit; see that function.

    A movie on one member's Plex watchlist with cap 8.0 scores ``8 * 1.00 * 1.0 * 0.60``
    = 4.8; two members 5.76; a 2020-only Trakt show ``8 * 1.00 * 0.25 * 0.60`` = 1.2."""
    if not entry or cap is None or cap <= 0:
        return 0.0
    from scripts.managers.machine_learning.next_watch import (
        INTENT_SOURCE_STRENGTH,
        member_fraction,
    )
    sources = entry.get("sources") or ()
    if not sources:
        return 0.0
    dated = entry.get("dated") or {}
    best = 0.0
    for src in sources:
        weight = INTENT_SOURCE_STRENGTH.get(str(src), 0.0)
        if weight <= 0:
            continue                      # unknown feed → no credit, never a guessed tier
        best = max(best, weight * intent_recency_factor(
            dated.get(src), now, half_life_days=half_life_days, stale_floor=stale_floor))
    if best <= 0:
        return 0.0
    return round(min(float(cap), float(cap) * best * member_fraction(len(entry.get("members") or ()) or 1)), 3)


def intent_hold_active(entry, now, *, dormancy_days: float = INTENT_DORMANCY_DAYS) -> bool:
    """Is this title's watchlist DELETE SHIELD still live?

    The shield exists because points alone cannot save a title: a watchlisted film with a
    weak taste profile can still fall under the delete ceiling, and deleting something the
    household explicitly asked for is the one deletion that is never defensible.

    It MUST expire, or one forgotten watchlist entry holds disk forever. It expires the
    way ``lifecycle/saga_retention`` already expires watchlist intent — on the
    WATCHLISTER'S OWN DORMANCY, not on the age of the listing. That is the right anchor:
    a title added in 2020 by somebody who watched something last night is live intent from
    an active viewer; a title added last week by an account that has not played anything in
    six months is not. ``entry["anchor"]`` is the most recent play by any member who asked
    for the title (stamped by ``next_watch.build_intent_index`` from the same per-user
    last-activity map ``saga_retention_producer._attach_watchlist`` uses).

    Fail-CLOSED on a missing anchor: an entry we cannot date to an active viewer does NOT
    hold. Holding on unknown provenance is exactly the "held forever" failure this guard
    exists to prevent, and the title still keeps its A5 POINTS either way."""
    if not entry:
        return False
    age = _intent_age_days(entry.get("anchor"), now)
    if age is None:
        return False
    return age <= max(0.0, float(dormancy_days or 0.0))


def intent_decay_kwargs(half_life_days=None, stale_floor=None) -> dict:
    """``{}`` when both knobs are None, else the ``score_movie``/``score_show`` kwargs.

    The feature adapters (``features/movie_features``, ``features/show_features``) carry
    the knobs as OPTIONAL and default them to None, because "the caller said nothing" and
    "the caller said 365.0" are different statements and only the first may be answered by
    the module constants. Forwarding None directly would not do that — the scorers would
    then hand None to ``float()`` inside the decay and raise — so an unset knob is dropped
    from the call entirely and the scorer's own default applies. That is what keeps every
    existing caller (and every test that calls the scorers positionally) byte-identical."""
    out: dict = {}
    if half_life_days is not None:
        out["intent_half_life_days"] = half_life_days
    if stale_floor is not None:
        out["intent_stale_floor"] = stale_floor
    return out


def resolve_intent_inputs(config, intent_index) -> "tuple[dict, float, float, float]":
    """Owned-scorer Group-A5 inputs ``(index, cap, half_life_days, stale_floor)`` from
    config + the built intent index.

    The single gate for BOTH the movie (space_pressure) and show (episode_files) paths so
    they cannot drift — the twin of :func:`resolve_person_affinity_inputs`. ``cap`` is
    forced to 0.0, making A5 byte-identical, when the term is config-disabled
    (``scoring.watchlist_intent.enabled``) OR the index is empty, so an install with no
    watchlist at all is wholly unaffected. Default cap 8.0 — Robert's decision, and the
    same weight C1/C4 carry: "we said we want this" is as strong a keep signal as "you are
    most of the way through this collection".

    THE DECAY KNOBS ARE RETURNED HERE FOR ONE REASON: they were DOCUMENTATION-ONLY. Both
    ``half_life_days`` and ``stale_floor`` have been pinned in ``config.json`` and written
    up in the onboarding schema since A5 shipped, but nothing read them — this resolver
    returned only ``(index, cap)`` and the decay ran off the module constants
    :data:`INTENT_HALF_LIFE_DAYS` / :data:`INTENT_STALE_FLOOR`, which happen to hold the
    same 365.0 / 0.25. An operator editing either value got silence. They are resolved
    here rather than inside :func:`watchlist_intent_score` so the config read happens ONCE
    per pass (not once per title) and so the values can be folded into the score memos'
    CONTEXT hash — without that, changing a half-life would move every dated title's score
    while every memo happily served the old one.

    Those two constants remain the DEFAULTS, so an absent/blank/garbage config value
    reproduces today's numbers exactly; a non-numeric entry falls back rather than raising,
    because a typo in a decay knob must not be able to fail a scoring pass."""
    index = {k: v for k, v in (intent_index or {}).items() if v}
    wl = ((config or {}).get("scoring", {}) or {}).get("watchlist_intent", {}) or {}
    enabled = bool(wl.get("enabled", True)) if isinstance(wl, dict) else bool(wl)
    try:
        cap = float(wl.get("cap", 8.0)) if isinstance(wl, dict) else 8.0
    except (TypeError, ValueError):
        cap = 8.0
    try:
        half_life = float(wl.get("half_life_days", INTENT_HALF_LIFE_DAYS)) \
            if isinstance(wl, dict) else INTENT_HALF_LIFE_DAYS
    except (TypeError, ValueError):
        half_life = INTENT_HALF_LIFE_DAYS
    try:
        floor = float(wl.get("stale_floor", INTENT_STALE_FLOOR)) \
            if isinstance(wl, dict) else INTENT_STALE_FLOOR
    except (TypeError, ValueError):
        floor = INTENT_STALE_FLOOR
    # A floor outside [0, 1] is not a floor, it is a multiplier — clamp rather than trust
    # it, or ``stale_floor: 5`` would silently turn the staleness term into a 5x BONUS on
    # every ancient listing (max(5.0, 2**-age) == 5.0).
    floor = min(1.0, max(0.0, floor))
    if not enabled or not index or cap <= 0:
        return index, 0.0, half_life, floor
    return index, cap, half_life, floor


def user_rating_score(
    user_rating: float | None,
    *,
    slope: float = 2.0,
    pos_cap: float = 10.0,
    neg_cap: float = -5.0,
    confidence: float = 1.0,
) -> float:
    """Group-A4 declared-rating bump — ONE formula, parameterised per medium.

    Linear about 5/10: ``(rating - 5) * slope``, clamped to ``[neg_cap, pos_cap]``,
    then scaled by ``confidence`` in ``[0, 1]``. Returns 0.0 when unrated or
    non-positive.

    The DEFAULTS reproduce the original symmetric movie term (slope 2, +10/-5,
    full confidence) — so ``score_movie`` is byte-for-byte unchanged. ``score_show``
    passes a gentler shape because a declared series rating is a stickier, weaker
    signal than revealed episode engagement (A2): lower slope/cap, a softened
    penalty, and a ``confidence`` derived from how much of the series has actually
    been watched (a 10/10 after two episodes is trusted less than after four
    seasons). Kept in the shared space so movie/show A4 can DIFFER without the math
    drifting — only the knobs differ.
    """
    if user_rating is None or user_rating <= 0:
        return 0.0
    raw = min(pos_cap, max(neg_cap, (user_rating - 5.0) * slope))
    return round(raw * max(0.0, min(1.0, confidence)), 2)


def related_graph_affinity(
    related_ids,
    watched_ids,
    *,
    cap: float = 4.0,
) -> float:
    """Collaborative 'related-graph' affinity, shared by both scorers (Group-C3).

    How many of a title's Trakt-RELATED neighbours the household has watched. This
    generalises the Group-C collection/universe terms (C1 collection-completeness,
    C2 universe-siblings) from FORMAL franchises to Trakt's similarity graph — the
    "people like me who watch the neighbours of this title enjoy it" signal that
    works for owned content (unlike Trakt's personalised recommendations, which only
    surface titles you do NOT own).

    ``related_ids``  — the title's related-neighbour ids (TMDb for movies, TVDb for
                       shows), e.g. extracted from the daemon-cached related bucket.
    ``watched_ids``  — the household watched-set in the SAME id space.
    ``cap``          — max contribution (default +4, mirroring C2; configurable).

    Count-based tiers (a related list runs ~20-100 long, so the absolute number of
    watched neighbours is the meaningful axis — mirrors C2's sibling-count style),
    scaled to ``cap``:
        >= 10 watched neighbours -> cap        (household is deep in this cluster)
        >=  5                    -> cap * 0.75
        >=  2                    -> cap * 0.375
        ==  1                    -> cap * 0.25
        0  (or either set empty) -> 0.0
    """
    if not related_ids or not watched_ids:
        return 0.0
    n = len(set(related_ids) & set(watched_ids))
    if n <= 0:
        return 0.0
    if n >= 10:
        frac = 1.0
    elif n >= 5:
        frac = 0.75
    elif n >= 2:
        frac = 0.375
    else:
        frac = 0.25
    return round(min(cap, cap * frac), 2)


def resolve_quality_ladder(config=None) -> "list[tuple[int, str]]":
    """The score→profile ladder to use, from ``config.scoring.quality_ladder`` or the
    calibrated default :data:`QUALITY_PROFILE_THRESHOLDS`.

    The config form is a list of ``[threshold, profile_name_pattern]`` pairs, e.g.::

        "scoring": {"quality_ladder": [[50, "Remux 2160p"], [38, "Remux 2160p"],
                                       [33, "Remux 1080p"], [29, "Bluray 1080p"],
                                       [25, "WEBDL 1080p"], [0, "HD 720p"]]}

    Validated and normalised: pairs are coerced to ``(int, str)``, sorted DESCENDING by
    threshold (the lookup walks top-down and returns the first match, so an out-of-order
    list would silently mis-tier), and a ladder with no rung at or below 0 gets no
    special treatment — ``score_to_profile`` still falls through to ``"SD"``. ANY
    malformed value falls back to the default rather than raising: a typo in config must
    not take a run down. Pure."""
    try:
        raw = ((config or {}).get("scoring", {}) or {}).get("quality_ladder")
    except Exception:
        return list(QUALITY_PROFILE_THRESHOLDS)
    if not raw:
        return list(QUALITY_PROFILE_THRESHOLDS)
    out: list[tuple[int, str]] = []
    try:
        for pair in raw:
            threshold, label = pair[0], pair[1]
            out.append((int(threshold), str(label)))
    except (TypeError, ValueError, IndexError, KeyError):
        return list(QUALITY_PROFILE_THRESHOLDS)
    if not out:
        return list(QUALITY_PROFILE_THRESHOLDS)
    return sorted(out, key=lambda p: p[0], reverse=True)


def ladder_rung_for_resolution(token: str, ladder=None, default: int | None = None):
    """The LOWEST ladder threshold whose profile label mentions ``token`` (e.g. ``"2160"``
    → 38 on the calibrated ladder), or ``default`` when no rung matches.

    The single place any other module may ask "what score does the ladder require for
    this resolution tier?" — so a parallel ladder (``sizing.size_model``) can track the
    calibrated rungs instead of hard-coding a second copy that silently drifts."""
    rungs = [t for t, label in (ladder or QUALITY_PROFILE_THRESHOLDS) if token in str(label)]
    return min(rungs) if rungs else default


def score_to_profile(score: int, ladder=None) -> str:
    """
    Map a 0-100 watchability score to a target quality profile name pattern.

    The returned string is a *pattern* — callers should fuzzy-match it against
    their actual Radarr/Sonarr quality profile names.

    Minimum floor is HD-720p — SD content is absorbed into 720p since older
    movies without HD masters still benefit from the 720p container and metadata.

    ``ladder`` overrides :data:`QUALITY_PROFILE_THRESHOLDS` (see
    :func:`resolve_quality_ladder` for the config form). The default ladder is
    CALIBRATED to this household's real score distribution — see the percentile
    table on the constant.

    Returns
    -------
    str  one of the ladder's labels.
    """
    for threshold, profile in (ladder or QUALITY_PROFILE_THRESHOLDS):
        if score >= threshold:
            return profile
    return "SD"


def select_profile_id(
    score: int,
    ranked_profiles: list[dict],
    target_resolution: int | None = None,
    ladder=None,
) -> int | None:
    """
    Select a quality-profile id for *score* from *ranked_profiles*.

    Shared by ``score_to_radarr_profile_id`` and ``score_to_sonarr_profile_id`` —
    Radarr and Sonarr quality profiles share the same items/quality/resolution
    shape, so the selection is identical: map the score to a profile-name pattern,
    never exceed *target_resolution*, and prefer the highest-resolution match.

    ``ranked_profiles`` should be the list from ``_fetch_ranked_profiles`` (sorted
    ascending by max resolution). Returns None if no suitable profile is found.

    NOTE: this deliberately does NOT consult the codec-aware brain
    (``quality_analytics.profile_selector.choose_codec_profile``). That selector is
    wired only into the read-only ``report_codec_routing`` preview: on this library's
    real Tautulli data it flags 0 titles that would change codec to cut transcoding,
    the only available swap (HEVC->H.264) costs disk, and video codec is at most a ~10%
    slice of transcodes. Keep it preview-only until there are >=2 codec-variant profiles
    per resolution tier to route between — wiring it here today changes nothing but cost.
    """
    if not ranked_profiles:
        return None

    profile_label = score_to_profile(score, ladder)

    def _max_res(p: dict) -> int:
        best = 0
        for item in (p.get("items") or []):
            if not item.get("allowed"):
                continue
            res = (item.get("quality") or {}).get("resolution", 0)
            if isinstance(res, (int, float)):
                best = max(best, int(res))
            for sub in (item.get("items") or []):
                if sub.get("allowed"):
                    sr = (sub.get("quality") or {}).get("resolution", 0)
                    if isinstance(sr, (int, float)):
                        best = max(best, int(sr))
        return best

    # Filter by resolution ceiling if provided
    eligible = [
        p for p in ranked_profiles
        if target_resolution is None or _max_res(p) <= target_resolution
    ]
    if not eligible:
        eligible = ranked_profiles  # fallback: ignore ceiling

    # Match by name pattern (case-insensitive substring)
    label_lower = profile_label.lower()
    matched = [p for p in eligible if label_lower in (p.get("name") or "").lower()]
    if matched:
        # Return highest-ranked matching profile
        return sorted(matched, key=_max_res)[-1]["id"]

    # Fallback: return the highest eligible profile below the score tier
    return sorted(eligible, key=_max_res)[-1]["id"]
