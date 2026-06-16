"""
classification/keep_policy.py — resolve keep_* policy from tags (pure).
================================================================================
Relocated from ``radarr/cache/movie_files._build_keep_policy_map`` (ML Step 5c).
Maps each movie's tag labels to a keep policy + universe name. PURE — no HTTP, no
global_cache; the service keeps the tag FETCH and applies the returned maps to
Parquet columns.

Public API:
  * build_keep_policy_map(movies, tag_label_map) -> (policy_map, universe_name_map)

(``radarr/repair/anomaly._resolve_keep_policy`` and
``sonarr/cache/episode_files._resolve_keep_policy_map`` are separate single-row /
episode resolvers — candidates for a later micro-slice.)
"""
from __future__ import annotations

# Known franchise hint tags used to label a bare-"universe" movie when no explicit
# keep-universe-<name> suffix is present. A movie tagged bare "universe" + one of these short
# tags (e.g. "universe" + "conjuring") groups under that hint — the LESS AWKWARD alternative to
# a per-franchise "keep-universe-<name>" tag. Hints only activate when the operator applies the
# tag; an unused hint changes nothing. Use "startrek"/"starwars" rather than the ambiguous
# legacy "star". Add the canonical short token for any franchise you tag.
FRANCHISE_HINTS = {
    "mcu", "xmen", "dc", "star", "startrek", "starwars", "transformers", "fast",
    "godzilla", "jurassic", "matrix", "alien", "predator",
    "terminator", "indiana", "rocky", "rambo", "mission",
    # extended set — franchises common in larger libraries
    "conjuring", "demonslayer", "dragonball", "potter", "mummy", "scorpion",
    "creed", "blade", "ghostrider", "fantasticfour", "taylorswift", "viewaskew",
    "spiderman", "venom", "hellraiser", "saw", "purge", "johnwick",
}

# Movie keep-tag label sets (Radarr uses hyphens). Shared by build_keep_policy_map
# (list form) and resolve_keep_policy (single-movie form).
KEEP_FOREVER_LABELS = frozenset({"keep", "keep-forever"})
KEEP_MOVIE_LABELS = frozenset({"keep-movie"})


def build_keep_policy_map(
    movies: list[dict], tag_label_map: dict
) -> "tuple[dict[int, str | None], dict[int, str | None]]":
    """Build keep-policy and universe-name maps from tag assignments.

    Priority (highest first):
      "keep" | "keep-forever"                    -> "keep_forever"
      "keep-movie"                               -> "keep_movie"
      "keep-universe" | "keep-universe-<name>"   -> "keep_universe" (never deleted;
                                                    quality-change only)
      bare "universe" (without keep- prefix)     -> "universe" (deletable as an
                                                    absolute last resort)

    Universe label resolution (in order): the suffix of "keep-universe-<name>";
    else known FRANCHISE_HINTS on the same movie; else "universe".

    Returns ``(policy_map, universe_name_map)``.
    """
    policy_map:        dict[int, "str | None"] = {}
    universe_name_map: dict[int, "str | None"] = {}

    keep_forever_labels = {"keep", "keep-forever"}
    keep_movie_labels   = {"keep-movie"}

    for movie in movies:
        mid     = movie.get("id")
        tag_ids = movie.get("tags") or []
        labels  = {tag_label_map.get(tid, "").lower() for tid in tag_ids}

        prefixed = [
            lbl for lbl in labels
            if lbl == "keep-universe" or lbl.startswith("keep-universe-")
        ]
        has_bare_universe = "universe" in labels
        is_keep_universe  = bool(prefixed)
        is_bare_universe  = has_bare_universe and not is_keep_universe
        is_any_universe   = is_keep_universe or is_bare_universe

        if is_any_universe:
            named = [
                lbl[len("keep-universe-"):]
                for lbl in prefixed
                if lbl.startswith("keep-universe-")
            ]
            if not named:
                named = sorted(labels & FRANCHISE_HINTS)
            if not named:
                named = ["universe"]
            universe_name_map[mid] = "|".join(sorted(set(named)))
        else:
            universe_name_map[mid] = None

        if labels & keep_forever_labels:
            policy_map[mid] = "keep_forever"
        elif labels & keep_movie_labels:
            policy_map[mid] = "keep_movie"
        elif is_keep_universe:
            policy_map[mid] = "keep_universe"   # never deleted
        elif is_bare_universe:
            policy_map[mid] = "universe"         # deletable as last resort
        else:
            policy_map[mid] = None

    return policy_map, universe_name_map


def resolve_keep_policy(movie: dict, tag_label_map: dict) -> "str | None":
    """Resolve a SINGLE movie's keep_policy from its Radarr tag labels — the
    per-movie twin of build_keep_policy_map's policy output (without the universe
    name). Priority: keep_forever > keep_movie > keep_universe > universe > None.
    A non-None result is an explicit user override (never unmonitor/delete)."""
    labels = {(tag_label_map.get(t) or "").lower() for t in (movie.get("tags") or [])}
    if labels & KEEP_FOREVER_LABELS:
        return "keep_forever"
    if labels & KEEP_MOVIE_LABELS:
        return "keep_movie"
    if any(lbl == "keep-universe" or lbl.startswith("keep-universe-") for lbl in labels):
        return "keep_universe"
    if "universe" in labels:
        return "universe"
    return None


def series_keep_policy(tag_ids, keep_series_id, keep_season_id) -> "str | None":
    """Resolve a SERIES' keep_policy from its Sonarr tag-id list. ``keep_series``
    wins over ``keep_season``; ``None`` when neither tag is present. The service
    FETCHes the Sonarr tag catalogue + per-series tag ids and calls this per series."""
    if keep_series_id is not None and keep_series_id in tag_ids:
        return "keep_series"
    if keep_season_id is not None and keep_season_id in tag_ids:
        return "keep_season"
    return None
