"""
playlists/grouping.py — bind items that share a series / franchise / universe.
================================================================================
The operator's rule: items sharing a series, franchise, OR universe must stay
CONTIGUOUS in the playlist. The naive "group by the first universe label" is a trap
— Radarr stores multi-universe membership as ``'|'.join(sorted(labels))`` (alpha
order), so a film in {mcu, spiderman} and a film in {spiderman} would pick
DIFFERENT first labels (mcu vs spiderman) and scatter apart despite sharing the
spiderman universe (red-team CRITICAL).

Fix: treat "shares any affinity" as an equivalence relation and take its CONNECTED
COMPONENTS (union-find). Any two items linked by a shared universe/franchise/series
— directly or transitively — land in one group, so contiguity is guaranteed. The
group is NAMED by the broadest affinity present (universe ▷ franchise ▷ series ▷
standalone), purely for the preview/rationale; the label never affects contiguity.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from scripts.managers.machine_learning.playlists.models import (
    FRANCHISE,
    SERIES,
    STANDALONE,
    UNIVERSE,
    PlaylistInput,
)


@dataclass(frozen=True)
class Group:
    """A connected component of items that share a series/franchise/universe."""
    kind: str                       # universe | franchise | series | standalone
    key: str                        # deterministic label (preview/debug only)
    members: tuple[PlaylistInput, ...]


class _DSU:
    """Minimal union-find over item indices."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:        # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)   # deterministic: keep lower root


def _affinity_keys(it: PlaylistInput):
    """Every equivalence token this item carries. Sharing ANY one binds two items."""
    for u in it.universes:
        u = (u or "").strip().lower()
        if u:
            yield f"u:{u}"
    if it.franchise:
        yield f"f:{str(it.franchise).strip().lower()}"
    if it.series_id is not None:
        yield f"s:{it.series_id}"


def _label(members: list[PlaylistInput]) -> tuple[str, str]:
    """Name a component by the BROADEST affinity present (deterministic tie-break:
    most-common label, then lexicographically smallest)."""
    univ = Counter(u.strip().lower() for m in members for u in m.universes if (u or "").strip())
    if univ:
        return UNIVERSE, _top(univ)
    fran = Counter(str(m.franchise).strip().lower() for m in members if m.franchise)
    if fran:
        return FRANCHISE, _top(fran)
    series = {m.series_id for m in members if m.series_id is not None}
    if series:
        return SERIES, str(min(series))
    return STANDALONE, members[0].rating_key      # singleton (no shared affinity)


def _top(counter: Counter) -> str:
    """Most frequent label; ties broken by smallest string (fully deterministic)."""
    return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def group_items(items: list[PlaylistInput]) -> list[Group]:
    """Partition ``items`` into connected components over shared affinities.

    Items with NO shared affinity (no universe/franchise/series, or a unique one)
    fall out as singleton ``standalone`` groups. Group order here is NOT meaningful —
    ``ordering`` ranks the groups; this only guarantees membership/contiguity.
    """
    items = list(items)
    if not items:
        return []
    dsu = _DSU(len(items))
    first_seen: dict[str, int] = {}
    for i, it in enumerate(items):
        for key in _affinity_keys(it):
            if key in first_seen:
                dsu.union(first_seen[key], i)
            else:
                first_seen[key] = i

    comps: dict[int, list[PlaylistInput]] = {}
    for i, it in enumerate(items):
        comps.setdefault(dsu.find(i), []).append(it)

    groups = []
    for members in comps.values():
        kind, key = _label(members)
        groups.append(Group(kind=kind, key=key, members=tuple(members)))
    return groups


def coverage_stats(groups: list[Group]) -> dict:
    """Per-ITEM grouping breakdown — the graceful-degradation signal. If everything
    is ``series``/``standalone``, the franchise/universe tags aren't present yet
    (e.g. Kometa hasn't tagged Sonarr), so the operator knows the saga grouping is
    running in fallback mode rather than silently under-grouping."""
    by_kind = Counter()
    for g in groups:
        by_kind[g.kind] += len(g.members)
    total = sum(by_kind.values())
    return {
        "items": total,
        "groups": len(groups),
        UNIVERSE: by_kind[UNIVERSE],
        FRANCHISE: by_kind[FRANCHISE],
        SERIES: by_kind[SERIES],
        STANDALONE: by_kind[STANDALONE],
        # share of items grouped by a *real* multi-item affinity (not singleton fallback)
        "grouped_pct": round(100.0 * (total - by_kind[STANDALONE]) / total, 1) if total else 0.0,
    }
