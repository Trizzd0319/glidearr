"""lifecycle/restore_policy.py — restore recovered deletions (pure).
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: Decide which previously-deleted items to re-acquire on score recovery.

PULLS FROM (decision cores to migrate here):
  - radarr/repair/anomaly::restore_recovered_deletions (decision core)
  - sonarr/cache/episode_files::restore_recovered_episode_deletions (decision core)

PUBLIC API (to implement):
  - plan_restores(restore_set, current_scores, config) -> list[AcquirePlan]

DEPENDS ON: contracts
SERVICE REMAINDER (stays in the service as the thin adapter): Service keeps the restore-set read + re-monitor/search APPLY.

────────────────────────────────────────────────────────────────────────────────
LANDED: THE DELETED-EPISODE LEDGER'S RELEASE IDENTITY
────────────────────────────────────────────────────────────────────────────────
``sonarr/{inst}/deleted_episodes`` used to record only ``{series_id: {episodes:
[[s,e]…], ts}}`` — WHAT was deleted, with nothing about WHICH release it was. On
restore that left only a blind ``EpisodeSearch``: whatever the indexers happen to
serve today, at whatever quality, from whatever group. Since the per-viewer
retention rule (``lifecycle.viewer_retention``) deliberately bets that
re-acquisition is cheap, the bet is only honest if what comes back is what left.

RECORDED (all already on the parquet row being deleted — NO live indexer lookup;
a guid is worth seconds, not months, and a stored one is a liability):
``scene_name``, ``release_group``, ``quality_name``, ``resolution``, ``size_bytes``.

SCHEMA + MIGRATION: v1 entries (no ``releases`` map) stay valid forever. Readers
go through :func:`ledger_releases`, which returns ``{}`` for a v1 entry, and every
consumer treats "no recorded release" exactly like "the recorded release did not
match" → the existing blind search. The fallback is TOTAL by construction: a
missing, stale or unmatchable scene_name can never block a restore.

Public API (landed):
  * LEDGER_SCHEMA_VERSION / RELEASE_FIELDS
  * episode_key(season, episode)          -> "S02E15"
  * release_record(row)                   -> the recorded identity, or None
  * merge_ledger_entry(existing, incoming) -> one entry, v1-safe
  * ledger_releases(entry)                -> {"S02E15": {...}}  ({} for v1)
  * match_release(releases, recorded)     -> the chosen release dict, or None
  * redact_grab_url(url)                  -> url with secrets named-placeholdered (GLD-RST-07)
  * refill_grab_url(url, key)             -> url made whole again, or None if it cannot be
  * looks_like_secret(value)              -> the credential-shape test both of the above use
"""
from __future__ import annotations

import re

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

LEDGER_SCHEMA_VERSION = 2

# The identity fields lifted off the parquet row at delete time. Deliberately NOT
# a guid/indexerId: those expire (indexer caches roll over in hours-to-days) while
# a restore may fire months later, and a dead guid is worse than no guid because
# it looks actionable.
RELEASE_FIELDS = ("scene_name", "release_group", "quality_name", "resolution", "size_bytes",
                  "video_codec")
# ``video_codec`` added GLD-RST-01. It was the one identity field the parquet already
# carried that the record ignored, and it is the field that most changes what a restore
# FEELS like: an x265/AV1 encode and an x264 encode at the same quality string and the
# same resolution are the same row here but different playback (direct play vs transcode,
# see quality_analytics.transcode_causes). Additive and v2-safe: release_record skips
# absent fields, so pre-existing entries simply score without it.

# Scene names are compared on a normalised form: case-folded, punctuation → space,
# runs of whitespace collapsed. That absorbs the .-vs-_ vs-space churn between what
# Sonarr stored as sceneName and what an indexer serves as the release title.
_NORM_RE = re.compile(r"[^0-9a-z]+")


def _norm(value) -> str:
    return _NORM_RE.sub(" ", str(value or "").lower()).strip()


# Codec spellings that mean the SAME encode. Sonarr's mediaInfo.videoCodec and a scene
# release title rarely agree on wording ("x265" / "h265" / "hevc" are one thing), so a
# recorded codec is expanded to its family before being looked for in a release title.
# Unknown codecs fall through to themselves — an unrecognised value still matches an
# exact spelling and never matches a different family.
_CODEC_ALIASES = (
    ("x265", "h265", "hevc"),
    ("x264", "h264", "avc"),
    ("av1", "aom", "svtav1"),
    ("vp9",),
    ("mpeg2", "mpeg 2"),
    ("xvid", "divx"),
)


def _codec_aliases(codec: str) -> tuple:
    """Every spelling of *codec*'s family, normalised. Returns ``(codec,)`` for anything
    not in the table so an unknown value is still matched literally rather than dropped."""
    c = _norm(codec).replace(" ", "")
    if not c:
        return ()
    for family in _CODEC_ALIASES:
        if c in family:
            return family
    return (c,)


def episode_key(season, episode) -> "str | None":
    """``(2, 15)`` → ``"S02E15"`` — the per-episode key inside a ledger entry's
    ``releases`` map. None when either index is missing/unparseable."""
    try:
        return f"S{int(season):02d}E{int(episode):02d}"
    except (TypeError, ValueError):
        return None


def release_record(row) -> "dict | None":
    """Project a parquet row (any mapping) down to :data:`RELEASE_FIELDS`.

    Returns None when the row carries NO usable identity at all — recording an
    all-null stub would just make a v2 entry that behaves exactly like v1 while
    pretending otherwise. ``scene_name`` is populated on roughly a fifth of rows
    and ``release_group`` on roughly a third, so partial records are the norm and
    are kept: a group + quality still narrows a targeted search considerably.

    Accepts ANY mapping. The emptiness check is ``row is None`` rather than
    ``not row``, because a pandas Series raises on truthiness ("truth value of a
    Series is ambiguous") and the delete paths hand this a parquet ROW directly.
    ``GLD-RST-08`` hit exactly that: every one of 266 records was lost to the
    caller's swallowed exception, with only a warning per row to show for it. An
    empty dict still returns None via ``out or None`` below, so the contract is
    unchanged for every existing caller."""
    if row is None:
        return None
    out = {}
    for field in RELEASE_FIELDS:
        val = row.get(field) if hasattr(row, "get") else None
        if val is None or val != val:            # None or NaN
            continue
        if field in ("resolution", "size_bytes"):
            try:
                out[field] = int(val)
            except (TypeError, ValueError):
                continue
        else:
            s = str(val).strip()
            if s:
                out[field] = s
    return out or None


def ledger_releases(entry) -> dict:
    """The ``{episode_key: release_record}`` map of a ledger entry.

    ``{}`` for a v1 entry (or any malformed one) — the whole migration story in
    one accessor: old records simply have no recorded release, which every caller
    already handles as "fall back to the blind search"."""
    if not isinstance(entry, dict):
        return {}
    rel = entry.get("releases")
    return rel if isinstance(rel, dict) else {}


# ── TIER 1: SONARR'S OWN DOWNLOAD HISTORY (GLD-RST-02) ──────────────────────────
# WHY THIS IS ENRICHMENT AND NOT A GRAB. A ``grabbed`` history row carries the guid of
# the release that was actually taken — and that is exactly the thing this module already
# refuses to store, for the reason stated at RELEASE_FIELDS: indexer caches roll over in
# hours-to-days, a restore fires months later, and a dead guid is worse than no guid
# because it looks actionable. Re-POSTing a historical guid would mostly fail, and fail
# QUIETLY (``_make_request`` returns None on a soft reject).
#
# What history has that the ledger does NOT is ``sourceTitle`` — the full release title.
# That is the highest-value key in match_release (+3 exact / +2 substring, against +1 each
# for group / quality / resolution / codec). It matters because ``scene_name`` is populated
# on roughly a fifth of parquet rows: on this operator's live ledger it is absent from
# EVERY entry, so every targeted restore runs at confidence 3 (group + quality +
# resolution) when it could run at 6.
#
# So tier 1 does not replace the search — it makes the search's KEY strong enough to find
# the right release. Order: history-enriched identity -> interactive search -> blind search.
HISTORY_GRAB_EVENTS = ("grabbed",)


def codec_from_title(title) -> "str | None":
    """The video codec named in a release title, normalised to its family's first spelling
    (``"hevc"`` and ``"h265"`` both return ``"x265"``), or None.

    Release titles are the only codec source available on this path — neither a
    ``/release`` row nor a history row carries mediaInfo. Longest alias first so a short
    alias cannot shadow a longer one from another family."""
    hay = _norm(title)
    if not hay:
        return None
    packed = hay.replace(" ", "")
    for family in _CODEC_ALIASES:
        for alias in sorted(family, key=len, reverse=True):
            if alias in packed:
                return family[0]
    return None


def history_release_record(rows, *, events=HISTORY_GRAB_EVENTS) -> "dict | None":
    """Project Sonarr history rows for ONE episode down to a :data:`RELEASE_FIELDS`
    record built from the MOST RECENT grab, or None.

    ``rows`` is the raw ``GET /history?episodeId=`` record list. Rows whose ``eventType``
    is not in *events* are ignored, so an import / delete / rename row can never be
    mistaken for the release that was taken. Newest first by ``date``; a row with an
    unparseable date sorts last rather than being dropped — a grab with a garbled
    timestamp is still better evidence than no grab at all.

    Deliberately does NOT carry the guid — see the block comment above."""
    if not isinstance(rows, list):
        return None
    want = {_norm(e) for e in events}
    grabs = [r for r in rows if isinstance(r, dict) and _norm(r.get("eventType")) in want]
    if not grabs:
        return None
    grabs.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    row = grabs[0]
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    qual = ((row.get("quality") or {}).get("quality") or {})
    title = row.get("sourceTitle") or ""

    out: dict = {}
    if str(title).strip():
        out["scene_name"] = str(title).strip()
    for src, field in ((data.get("releaseGroup"), "release_group"),
                       (qual.get("name"), "quality_name")):
        if src is not None and str(src).strip():
            out[field] = str(src).strip()
    for src, field in ((qual.get("resolution"), "resolution"),
                       (data.get("size"), "size_bytes")):
        try:
            if src is not None:
                out[field] = int(src)
        except (TypeError, ValueError):
            pass
    codec = codec_from_title(title)
    if codec:
        out["video_codec"] = codec
    return out or None


def merge_release_records(primary, secondary) -> "dict | None":
    """One record from two, *primary* winning field by field.

    The ledger is authoritative: it was lifted off the actual FILE at delete time, while
    history describes what was GRABBED, which is not always what was imported (a repack, a
    manual replacement, an edit made outside Glidearr). History therefore only ever FILLS
    fields the ledger left empty — in practice ``scene_name``, and ``video_codec`` on any
    entry written before GLD-RST-01.

    Either side may be None/empty; returns None only when both are."""
    a = primary if isinstance(primary, dict) else {}
    b = secondary if isinstance(secondary, dict) else {}
    out = dict(b)
    for k, v in a.items():
        if v is not None and v != "":
            out[k] = v
    return out or None


def merge_ledger_entry(existing, incoming) -> dict:
    """Merge a freshly-deleted batch into whatever is already on disk for one
    series, tolerating BOTH schema versions on either side.

    * ``episodes`` — deduped union of the ``[season, episode]`` pairs (unchanged
      v1 shape, so an older build reading this ledger still works).
    * ``releases`` — per-episode identity, later write wins (a re-delete after a
      restore recorded the release that is actually gone NOW).
    * ``ts``       — the incoming timestamp; the restore cooldown measures from the
      most recent deletion, not the first.
    """
    cur = dict(existing) if isinstance(existing, dict) else {}
    inc = incoming if isinstance(incoming, dict) else {}

    episodes = [list(x) for x in (cur.get("episodes") or [])
                if isinstance(x, (list, tuple)) and len(x) == 2]
    have = {tuple(x) for x in episodes}
    for se in (inc.get("episodes") or []):
        if isinstance(se, (list, tuple)) and len(se) == 2 and tuple(se) not in have:
            episodes.append(list(se))
            have.add(tuple(se))

    releases = dict(ledger_releases(cur))
    releases.update(ledger_releases(inc))

    out = {"episodes": episodes, "ts": inc.get("ts") or cur.get("ts")}
    if releases:
        out["releases"] = releases
        out["v"] = LEDGER_SCHEMA_VERSION
    elif cur.get("v"):
        out["v"] = cur["v"]
    return out


def match_release(releases, recorded, *, min_confidence=2) -> "dict | None":
    """Pick the release from a Sonarr interactive-search result that IS the one we
    recorded at delete time, or None to fall back to the blind ``EpisodeSearch``.

    ``releases`` is the raw ``GET /release?episodeId=`` list; ``recorded`` is a
    :func:`release_record`. Each candidate scores:

      +3  its normalised title EQUALS the recorded scene_name (a certain match)
      +2  its normalised title CONTAINS the recorded scene_name (or vice versa —
          Sonarr stores sceneName without the extension, indexers vary)
      +1  same release group (case-insensitive)
      +1  same quality name
      +1  same resolution
      +1  same video codec (GLD-RST-01) — matched against the candidate's
          ``customFormats`` / title, since a /release row carries no mediaInfo

    ``min_confidence`` (2) is the bar: a group match alone (+1) is NOT enough to
    call a release "the same one", but group + quality is, and any title match is.
    Ties break on the highest score, then on the closest ``size_bytes`` to the
    recorded size — two encodes from the same group at the same quality differ
    mostly by bitrate, and the one nearest the original is the honest restore.

    Returns None on an empty/garbled search result, an empty ``recorded``, or when
    nothing clears the bar. The caller MUST treat None as "blind search" — that
    total fallback is why a stale scene_name can never block a restore."""
    if not recorded or not isinstance(releases, list):
        return None
    want_title = _norm(recorded.get("scene_name"))
    want_group = _norm(recorded.get("release_group"))
    want_quality = _norm(recorded.get("quality_name"))
    want_codec = _norm(recorded.get("video_codec"))
    try:
        want_res = int(recorded["resolution"]) if recorded.get("resolution") is not None else None
    except (TypeError, ValueError):
        want_res = None
    try:
        want_size = int(recorded["size_bytes"]) if recorded.get("size_bytes") is not None else None
    except (TypeError, ValueError):
        want_size = None

    best = None
    best_key = None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        score = 0
        title = _norm(rel.get("title"))
        if want_title and title:
            if title == want_title:
                score += 3
            elif want_title in title or title in want_title:
                score += 2
        if want_group and _norm(rel.get("releaseGroup")) == want_group:
            score += 1
        qual = ((rel.get("quality") or {}).get("quality") or {})
        if want_quality and _norm(qual.get("name")) == want_quality:
            score += 1
        if want_res is not None:
            try:
                if int(qual.get("resolution")) == want_res:
                    score += 1
            except (TypeError, ValueError):
                pass
        # CODEC (GLD-RST-01). A ``/release`` row has no mediaInfo, so the codec can only
        # be read off the release TITLE (and any custom-format labels). Aliases are folded
        # because the same encode is spelled several ways in the wild. A codec we cannot
        # find in the title scores 0 rather than penalising — absent is not "different".
        if want_codec:
            hay = " ".join([
                title,
                _norm(" ".join(str(cf.get("name") or "") for cf in (rel.get("customFormats") or [])
                               if isinstance(cf, dict))),
            ])
            if hay and any(a in hay for a in _codec_aliases(want_codec)):
                score += 1
        if score < min_confidence:
            continue
        try:
            size_gap = abs(int(rel.get("size") or 0) - want_size) if want_size else 0
        except (TypeError, ValueError):
            size_gap = 0
        key = (score, -size_gap)
        if best_key is None or key > best_key:
            best, best_key = rel, key
    return best


# ══════════════════════════════════════════════════════════════════════════════
# GRAB-URL REDACTION (GLD-RST-07) — the inverse of push_payload's refill.
# ══════════════════════════════════════════════════════════════════════════════
# WHY THIS MOVED HERE. The scrub used to be a staticmethod on the Sonarr space
# pressure manager while its inverse (push_payload) lived here, so the two halves
# of one contract sat in different layers. Radarr's delete path needs the same
# scrub, and a service-to-service import is exactly the pressure that produces a
# second, subtly different copy (P-E). They live together now.
#
# WHY THE POLICY INVERTED. The old rule named six secret query params and passed
# everything else through. On a cache that safe_cache_clear rotates, an unknown
# param leaking is a bounded mistake. The deletion archive is APPEND-ONLY and
# never rotates, so the same leak becomes a credential on disk permanently — and
# the fail direction has to flip with the damage bound, exactly as GLD-ACQS-13 did
# for the space budget. Unknown now means REDACT. Over-redaction costs a
# release/push that falls back to a magnet or a search, which is what happens
# today anyway; under-redaction cannot be undone.
#
# MEASURED. Against a corpus of real grab shapes (Prowlarr link=, Jackett path=,
# Torznab direct, path-embedded passkey, gazelle authkey/torrent_pass, netloc
# userinfo) the previous implementation leaked 6 of 9 secret-bearing URLs; two of
# those read CLEAN to a naive check because the passkey was base64 inside link=.

#: v1 placeholder. Entries already on disk carry it and must keep refilling forever.
REDACTION_PLACEHOLDER = "<redacted>"

#: Query params that are known-inert Torznab/Newznab/*arr search terms. Anything
#: NOT here is redacted — see the fail-direction note above.
_SAFE_QS_KEYS = frozenset({
    "t", "id", "cat", "q", "o", "season", "ep", "imdbid", "tvdbid", "tmdbid",
    "tvmazeid", "rid", "limit", "offset", "extended", "raw", "attrs", "group",
    "maxsize", "minsize", "action", "type", "format", "file", "title", "dn",
})

#: Params carrying a NESTED download URL rather than a scalar. Prowlarr's ``link``
#: is base64 of the upstream indexer URL, which on a private tracker embeds the
#: passkey — so blanking the outer apikey alone leaves the real secret on disk,
#: merely base64-encoded and invisible to any grep for it.
_BLOB_QS_KEYS = frozenset({"link", "path", "url", "href"})

#: Route words that are never credentials, whatever their length.
_SAFE_PATH_WORDS = frozenset({
    "download", "downloads", "torrent", "torrents", "getnzb", "nzb", "api",
    "indexer", "release", "releases", "rss", "feed", "dl", "file", "files",
})

#: Placeholder names refillable from config. The *arr-side indexer key is the ONE
#: secret this system holds; a tracker passkey, a path token and netloc userinfo
#: were never stored and never can be put back.
_REFILLABLE_NAMES = ("apikey", "api_key", "jackett_apikey", "rss_key", "rsskey")

_TOKENISH = re.compile(r"^[A-Za-z0-9_-]+$")

# A release name and a passkey are both long alphanumeric runs; only one carries
# MEDIA structure. Requiring a POSITIVE signal (rather than loosening the length
# test) keeps the fail direction intact — a flat random token cannot match, so the
# exemption can never be used to smuggle a credential through.
_MEDIA_TOKEN = re.compile(
    r"(?i)(?:^|[._\-])(?:s\d{1,2}e\d{1,3}|\d{3,4}p|(?:19|20)\d{2}"
    r"|web[._\-]?dl|webrip|bluray|bdrip|hdtv|remux|dvdrip|hdr|x26[45]|h\.?26[45]|hevc)"
    r"(?:[._\-]|$)")


def _placeholder(kind) -> str:
    """``"apikey"`` -> ``"<redacted:apikey>"``.

    Placeholders are NAMED because more than one component of a single URL can now
    be blanked, and the refill knows exactly one secret. An unnamed placeholder
    would let it stuff the indexer api key into a passkey slot and emit a URL that
    fails at the tracker instead of failing here, where it is diagnosable."""
    return f"<redacted:{kind}>"


def looks_like_secret(value, *, min_len: int = 16) -> bool:
    """Whether *value* has the SHAPE of an opaque credential.

    Deliberately crude and deliberately biased toward yes. A passkey is a long
    charset-restricted token; a release name carries dots or a media token, a
    numeric id is all digits, a route word is short and in the vocabulary. Each is
    excluded; anything else long enough is treated as a secret.

    The asymmetry is the whole point. A false positive costs a push that would have
    fallen back to a magnet or a search. A false negative writes a credential into a
    file that is never rotated."""
    v = str(value or "")
    if len(v) < min_len or not _TOKENISH.fullmatch(v):
        return False
    if v.isdigit() or v.lower() in _SAFE_PATH_WORDS:
        return False
    if _MEDIA_TOKEN.search(v):
        return False                      # a named release, not a credential
    if any(c.isdigit() for c in v) and any(c.isalpha() for c in v):
        return True
    # Pure-alpha keys are rarer but real (some trackers issue alpha-only rss keys).
    # A legitimate path segment that long with no digit, dot or media token is not a
    # thing, so length alone carries it past the bar.
    return len(v) >= 24


def _redact_blob(value) -> "str | None":
    """A nested-URL param, scrubbed in place, or None when it must be blanked whole.

    Returns None in two cases, both meaning "the caller must not keep this":
    the blob cannot be decoded to a URL at all (we cannot prove it is clean), or the
    inner URL DID carry a secret. In the second case the scrubbed inner is
    deliberately NOT re-encoded: a base64 blob hides its own placeholders, so
    push_payload would see only a refillable outer apikey and push a URL that is
    dead on the inside. Blanking the param makes the descriptor read as what it
    is — un-pushable. The indexer name is carried separately, so nothing
    identifying is lost."""
    from urllib.parse import unquote, urlparse
    import base64
    raw = unquote(str(value or ""))
    if not raw:
        return None
    decoders = [("plain", lambda s: s)]
    if _TOKENISH.fullmatch(raw.replace("=", "")) or "=" in raw:
        decoders += [
            ("b64", lambda s: base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")),
            ("b64url", lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")),
        ]
    for kind, fn in decoders:
        try:
            inner = fn(raw)
        except Exception:
            continue
        p = urlparse(inner)
        if not p.scheme or not p.netloc:
            continue
        cleaned = redact_grab_url(inner, _depth=1)
        if cleaned is None or REDACTION_PLACEHOLDER[:-1] in cleaned:
            return None
        if kind == "plain":
            return cleaned
        enc = (base64.b64encode if kind == "b64" else base64.urlsafe_b64encode)(cleaned.encode())
        return enc.decode()
    return None


def redact_grab_url(url, *, _depth: int = 0) -> "str | None":
    """*url* with every credential-shaped component replaced by a named placeholder.

    Four channels carry secrets; the previous implementation scrubbed one:

      0. malformed query — ``&k=v`` appended with no ``?`` (urlparse calls it PATH)
      1. query params      — allowlisted, not denylisted (unknown key -> redact)
      2. nested URL params — ``link=`` / ``path=`` decoded, scrubbed, re-encoded
      3. path segments     — token-shaped segments blanked (``/rss/<key>/``)
      4. netloc userinfo   — ``user:pass@host`` stripped

    Returns None for anything unparseable. A URL we cannot confidently redact is one
    we must not persist — that contract is unchanged from the original."""
    if not url:
        return None
    try:
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
        p = urlparse(str(url))
        if not p.scheme or not p.netloc:
            return None

        netloc = p.netloc
        if "@" in netloc:                                       # (4)
            netloc = f"{_placeholder('userinfo')}@{netloc.rsplit('@', 1)[1]}"

        # (0) MALFORMED QUERY STRINGS. Real indexers emit URLs whose parameters are
        # appended with `&` and NO `?` — e.g.
        #   https://host/getnzb/<id>.nzb&i=139402&r=<apikey>
        # urlparse puts that entire tail in PATH and leaves `query` empty, so neither
        # the query allowlist NOR the path-segment test can see it: the segment holds
        # `.` and `&`, fails the token charset check, and passes through VERBATIM.
        # This shipped a live Newznab apikey (`r=`) into 266 archive rows before it was
        # caught by reading real output rather than the corpus, which contained only
        # well-formed URLs. Split the tail back into a query so the allowlist applies.
        path, query = p.path, p.query
        if not query and "&" in path:
            path, _, query = path.partition("&")

        segs = []                                               # (3)
        for seg in path.split("/"):
            segs.append(_placeholder("pathtoken") if looks_like_secret(seg) else seg)
        path = "/".join(segs)

        qs = []
        for k, v in parse_qsl(query, keep_blank_values=True):    # (1) + (2)
            kl = k.lower()
            if kl in _BLOB_QS_KEYS and _depth == 0:
                inner = _redact_blob(v)
                qs.append((k, inner if inner is not None else _placeholder(kl)))
            elif kl in _SAFE_QS_KEYS:
                qs.append((k, _placeholder(kl) if looks_like_secret(v) else v))
            else:
                qs.append((k, _placeholder(kl)))
        return urlunparse(p._replace(netloc=netloc, path=path, query=urlencode(qs)))
    except Exception:
        return None


def refill_grab_url(url, indexer_api_key=None) -> "str | None":
    """*url* with refillable placeholders restored, or None when it cannot be made whole.

    v1 entries hold a bare ``<redacted>`` and keep working unchanged. v2 entries hold
    NAMED placeholders, and the name decides: an indexer-key placeholder is refilled
    from config, and ANY other surviving placeholder means the URL carries a secret
    this system deliberately never wrote down — so the descriptor is dropped rather
    than pushed. Sonarr would accept a half-dead URL, fail the fetch, and surface it
    as a bad release, which reads as a broken indexer instead of the missing
    credential it actually is.

    Both encodings are checked: a placeholder in the PATH survives literally while
    one in the QUERY is percent-encoded by urlencode."""
    if not url:
        return None
    from urllib.parse import quote
    out = str(url)
    if indexer_api_key:
        names = [_placeholder(n) for n in _REFILLABLE_NAMES] + [REDACTION_PLACEHOLDER]
        for token in names:
            for form in (token, quote(token, safe=""),
                         quote(token, safe="").replace("%3A", "%3a")):
                out = out.replace(form, indexer_api_key)
    for probe in ("<redacted", "%3Credacted", "%3credacted"):
        if probe in out:
            return None
    return out


def push_descriptor(raw, *, events=HISTORY_GRAB_EVENTS) -> dict:
    """A redacted ``POST /release/push`` descriptor built from *arr grab history — GLD-RST-08.

    WHY THIS IS PURE AND SHARED. ``GLD-RST-02`` built this as a method on the Sonarr
    step-down manager, wired to that ONE path. Every DELETE path needs it and Radarr
    needs it too, and a service-to-service import is exactly the pressure that
    produces a second, subtly different copy (**P-E**). The HTTP call stays in the
    service adapter; only the projection lives here.

    *raw* is the unmodified response body. Sonarr answers ``history?episodeId=`` with
    a paged ``{records: [...]}`` envelope on some routes and a bare list on others;
    Radarr's ``history/movie?movieId=`` returns a bare list. All three shapes are
    tolerated rather than betting on one.

    ``eventType`` is matched against BOTH the string form (``"grabbed"``) and the
    integer form (``1``): the query string filters server-side with the int, but the
    JSON body carries the string, and a caller that omits the query filter must still
    get grabs only. An import / delete / rename row must never be mistaken for the
    release that was taken.

    Every URL is passed through :func:`redact_grab_url`, so nothing credential-shaped
    reaches the archive. Returns ``{}`` — never raises, never partially — on any
    unusable input: a missing descriptor degrades a future restore to an indexer
    search, which is exactly what happens today, and it must never cost a deletion.
    """
    rows = raw.get("records") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        return {}
    want = {_norm(e) for e in events} | {"1"}
    grabs = [r for r in rows if isinstance(r, dict)
             and (_norm(r.get("eventType")) in want or r.get("eventType") == 1)]
    if not grabs:
        return {}
    grabs.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    row = grabs[0]
    data = row.get("data") if isinstance(row.get("data"), dict) else {}

    out = {
        "title":        row.get("sourceTitle"),
        "protocol":     data.get("protocol") or row.get("protocol"),
        "publish_date": data.get("publishedDate"),
        "indexer":      data.get("indexer"),
        "info_url":     redact_grab_url(data.get("nzbInfoUrl")),
        "download_url": redact_grab_url(data.get("downloadUrl")),
        # Torrents only, and the ONE durable identifier in the whole record: an
        # infohash is content-addressed, so it stays valid for as long as a swarm
        # exists. A usenet grab has no equivalent.
        "info_hash":    data.get("torrentInfoHash") or data.get("infoHash"),
        "grabbed_at":   row.get("date"),
    }
    return {k: v for k, v in out.items() if v}


def push_descriptors_by_episode(raw, *, events=HISTORY_GRAB_EVENTS) -> dict:
    """``{"S02E15": descriptor}`` from ONE series-wide history fetch — GLD-RST-08.

    WHY SERIES-WIDE AND NOT PER-EPISODE. The parquet delete path carries
    ``episode_file_id`` but NOT ``episodeId``, so ``history?episodeId=`` — the filter
    the step-down path uses — is unavailable to it without a second lookup per row.
    Fetching ``history?seriesId=&includeEpisode=true`` once per SERIES answers every
    episode of that series in one call: on a 266-row prune spanning ~50 series that
    is ~50 requests rather than 266.

    Rows are keyed off the embedded ``episode`` object. A row without one is keyed by
    ``episodeId`` instead so it is still retrievable, rather than being dropped — a
    grab we cannot key is still evidence the grab happened.

    Returns ``{}`` on any unusable input. Never raises.
    """
    rows = raw.get("records") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        return {}
    buckets: dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        ep = r.get("episode") if isinstance(r.get("episode"), dict) else {}
        key = episode_key(ep.get("seasonNumber"), ep.get("episodeNumber"))
        if not key:
            eid = r.get("episodeId")
            key = f"id:{eid}" if eid is not None else None
        if not key:
            continue
        buckets.setdefault(key, []).append(r)
    out = {}
    for key, group in buckets.items():
        d = push_descriptor(group, events=events)
        if d:
            out[key] = d
    return out


def magnet_from_hash(info_hash, title=None, trackers=None) -> "str | None":
    """A ``magnet:`` URI rebuilt from a bare infohash — GLD-RST-04.

    WHY THIS IS THE ONLY PERMANENT RESTORE HANDLE WE GET. Every other identifier in
    a grab record decays: a ``/release`` guid dies when the indexer's search cache
    rolls over (hours), a usenet ``downloadUrl`` dies when the indexer drops the
    release, and a SAB ``nzo_id`` dies with the queue entry. An infohash is
    CONTENT-ADDRESSED — a digest of the torrent's own metadata — so it stays valid
    for exactly as long as a swarm exists, and needs no indexer, no account and no
    secret to use. Forty hex characters buy a restore path that outlives the
    indexer the file came from.

    Accepts 40-char hex (v1) or 32-char base32, which clients and trackers report
    interchangeably. Anything else returns None rather than emitting a magnet that
    would silently never resolve.

    PRIVATE TRACKERS: a bare magnet resolves via DHT/PEX and private trackers
    disable both, so those still need the passkey-bearing download_url. This is an
    ADDITION to the push descriptor, never a replacement for it.
    """
    h = str(info_hash or "").strip().lower()
    if not h:
        return None
    is_hex = len(h) == 40 and all(c in "0123456789abcdef" for c in h)
    is_b32 = len(h) == 32 and all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in h)
    if not (is_hex or is_b32):
        return None
    from urllib.parse import quote
    uri = f"magnet:?xt=urn:btih:{h.upper()}"
    if title:
        uri += f"&dn={quote(str(title))}"
    for tr in (trackers or []):
        if tr:
            uri += f"&tr={quote(str(tr), safe='')}"
    return uri


def push_payload(descriptor, *, indexer_api_key=None) -> "dict | None":
    """A ``POST /api/v3/release/push`` body built from an archived push descriptor.

    Reverses the redaction applied at archive time: ``<redacted>`` placeholders are
    refilled from *indexer_api_key*, so the secret lives in config and never on disk
    in the ledger. A descriptor still holding a placeholder with no key supplied is
    DROPPED rather than pushed — Sonarr would accept the URL, fail the fetch, and
    surface it as a dead release, which reads as a bad indexer rather than the
    misconfiguration it actually is.

    Prefers ``magnetUrl`` when an infohash is present: that path needs no credential
    at all and cannot expire. Falls back to the refilled ``downloadUrl``.
    """
    d = descriptor or {}
    if not d.get("title"):
        return None

    def _refill(url):
        # GLD-RST-07: delegated to refill_grab_url, which understands the NAMED
        # placeholders the v2 scrub emits. v1's bare <redacted> still refills, so
        # every entry already on disk keeps working.
        return refill_grab_url(url, indexer_api_key)

    magnet = magnet_from_hash(d.get("info_hash"), title=d.get("title"))
    dl = _refill(d.get("download_url"))
    if not magnet and not dl:
        return None
    out = {
        "title":       d.get("title"),
        "protocol":    str(d.get("protocol") or "").lower() or ("torrent" if magnet else "usenet"),
        "publishDate": d.get("publish_date"),
        "indexer":     d.get("indexer"),
        "infoUrl":     _refill(d.get("info_url")),
        "size":        d.get("size_bytes"),
    }
    if magnet:
        out["magnetUrl"] = magnet
    if dl:
        out["downloadUrl"] = dl
    return {k: v for k, v in out.items() if v is not None}
