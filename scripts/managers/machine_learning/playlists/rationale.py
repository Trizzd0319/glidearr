"""
playlists/rationale.py — the concise "why is this in your playlist?" explainer.
================================================================================
Turns the signals that drove an item's rank into a short, human reason for the preview
grid. Priority order (strongest personal signal first): active-watching (JIT) → the item's
genres that sit in the USER's top affinity → matching cast/crew (when the library carries
them) → franchise/universe membership → a plain "household pick" fallback for a profile
with no personal signal yet. Pure + deterministic; the SERVICE supplies the item's
attributes + the user's affinity, the brain only phrases it.

NOTE: cast/crew explanation lights up automatically once the enrichment daemon populates
``cast_names``/``director_names`` (sparse today); genres + JIT + franchise carry it now.
"""
from __future__ import annotations


def _top_matches(names, weights, n: int) -> list[str]:
    """The up-to-``n`` of ``names`` that appear in the user's ``weights`` affinity, strongest
    first, returned in their ORIGINAL display form (matching is case-insensitive)."""
    if not names or not weights:
        return []
    w = {str(k).strip().lower(): float(v) for k, v in weights.items()}
    cand, seen = [], set()
    for x in names:
        key = str(x).strip().lower()
        if key and key not in seen and w.get(key, 0.0) > 0.0:
            seen.add(key)
            cand.append((w[key], str(x).strip()))
    cand.sort(key=lambda t: -t[0])
    return [name for _, name in cand[:n]]


def explain_reason(genres, genre_aff, *, cast=(), crew=(), people_aff=None,
                   is_jit: bool = False, franchise_name: str | None = None,
                   universe_name: str | None = None, max_genres: int = 2) -> str:
    """Concise 'why this is here' for ONE playlist item.

    ``genre_aff`` is the user's ``{genre: weight}`` affinity (exactly what the builder
    passes to ``genre_match`` — NOT the full affinity dict). ``people_aff`` is an optional
    merged ``{name: weight}`` actor+director affinity; cast/crew explanation stays dormant
    (no ``people_aff``) until the library populates ``cast_names``/``director_names``.
    Returns a compact ``·``-joined reason like ``"watching now · Drama·Action"`` or
    ``"Creed Collection · Comedy"`` or ``"household pick"``. ASCII + cp1252-safe only
    (``·`` = 0xB7) so it never crashes the Windows console log handler."""
    parts: list[str] = []
    if is_jit:
        parts.append("watching now")                     # active binge — ASCII only
    gm = _top_matches(genres, genre_aff, max_genres)
    if gm:
        parts.append("·".join(gm))                       # e.g. "Drama·Action"
    people = _top_matches(list(cast or ()) + list(crew or ()), people_aff, 2)
    if people:
        parts.append("w/ " + ", ".join(people))
    if universe_name:
        parts.append(f"{universe_name} universe")
    elif franchise_name:
        parts.append(str(franchise_name))
    if not parts:
        parts.append("household pick")
    return " · ".join(parts)
