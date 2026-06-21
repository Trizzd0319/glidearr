"""
franchise_graph.py — pure graph core for the TV-franchise catalog generator.
================================================================================
Turns spin-off **edges** ``(tvdb_a, tvdb_b)`` + per-series **node** metadata
``{tvdb: {title, date}}`` into connected-component franchises:
``{franchise_key: {"titles": [...], "shows": [tvdb...]}}`` with members in debut order.

The cross-named families (Grey's↔Station 19↔Private Practice, Buffy↔Angel, Breaking Bad↔
Better Call Saul) that the runtime same-stem clusterer CAN'T catch live here: a franchise is a
connected component over the spin-off graph, and every node is already filtered to a real TV
series (it carries a TheTVDB id). This is Layer 2 of the design — see
``coordinator/tv_franchise_discovery.md``.

PURE — stdlib only, no network, no I/O. The standalone generator
(``support/tools/generate_tv_franchises.py``) does the Wikidata fetch and feeds these; the
seam (``universe_order.tv_franchise_universes``) consumes the catalog the generator writes.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict


def normalize_key(title) -> str:
    """A franchise key stem from a title: accent-folded, lowercased, stripped to ``[a-z0-9]``
    — "Grey's Anatomy"→'greysanatomy', "Star Trek: TOS"→'startrektos'. Mirrors the runtime
    clusterer's normalisation so a baked franchise and a same-name cluster collide on key."""
    s = unicodedata.normalize("NFKD", str(title or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def connected_components(edges) -> list[set]:
    """Undirected connected components over ``(a, b)`` edges → list of node-id sets (union-find).
    Direction is ignored (a spin-off and its parent are one franchise either way). Nodes never
    seen in an edge don't appear. PURE."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:        # path-compress
            parent[x], x = root, parent[x]
        return root

    for a, b in edges or []:
        if a is None or b is None or a == b:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    comps: dict = defaultdict(set)
    for node in list(parent):
        comps[find(node)].add(node)
    return list(comps.values())


def franchise_star_edges(groups, *, min_members: int = 2, max_members=None, deny_types=None):
    """Membership ``groups`` → connector ("star") edges + the member nodes that survive the filters.

    ``groups`` — ``{group_id: {"members": [(tvdb, title, date)…], "types": set[str]}}`` — the
    Wikidata 'part of the series' (``P179``) shape, where the members of one group share a franchise
    node. A group is DROPPED when it has fewer than ``min_members`` distinct tvdb members, MORE than
    ``max_members`` (the programming-SLOT over-capture — a Christmas-calendar / telenovela-time-slot
    node bundles dozens of UNRELATED shows), or its ``types`` intersect ``deny_types`` (lower-cased
    non-franchise type labels, e.g. 'program block', 'wikimedia list article'). Survivors are
    star-connected — the first member ↔ each other member — so a P179 franchise becomes ONE connected
    component once these edges union with the spin-off (``P2512``) edges. Returns ``(edges, nodes)``
    with ``nodes`` = ``{tvdb: {"title", "date"}}`` (first non-null date wins). PURE."""
    deny = {str(d).lower() for d in (deny_types or ())}
    edges: list = []
    nodes: dict = {}
    for slot in (groups or {}).values():
        if not isinstance(slot, dict):
            continue
        seen: set = set()
        uniq: list = []
        for m in (slot.get("members") or []):
            tv = m[0] if m else None
            if isinstance(tv, int) and not isinstance(tv, bool) and tv not in seen:
                seen.add(tv)
                uniq.append(m)
        if len(uniq) < min_members:
            continue
        if max_members is not None and len(uniq) > max_members:   # programming-slot over-capture
            continue
        if deny and {str(t).lower() for t in (slot.get("types") or ())} & deny:
            continue
        for tv, title, date in uniq:
            cur = nodes.get(tv)
            if cur is None:
                nodes[tv] = {"title": title, "date": date}
            elif cur.get("date") is None and date:
                cur["date"] = date
        anchor = uniq[0][0]
        for m in uniq[1:]:
            edges.append((anchor, m[0]))
    return edges, nodes


def build_franchises(edges, nodes, *, min_members: int = 2, deny=None) -> dict:
    """Spin-off ``edges`` + ``nodes`` metadata → ``{franchise_key: {"titles": [...], "shows":
    [tvdb...]}}``.

    ``edges`` — iterable of ``(tvdb_a, tvdb_b)``. ``nodes`` — ``{tvdb: {"title": str, "date":
    str|None}}`` (``date`` an ISO inception string, used only to ORDER members). A connected
    component with ``>= min_members`` *known* nodes becomes a franchise; members are debut-ordered
    (date asc, undated last, then title). The key is ``normalize_key(earliest title)``, made
    unique on collision (``…2``, ``…3``). ``deny`` (normalized keys) are dropped — the escape hatch
    for a bad Wikidata merge a reviewer wants to suppress. Deterministic (stable component +
    member order) so the generated JSON diffs cleanly. PURE."""
    deny = {str(d) for d in (deny or ())}
    out: dict = {}
    used: set = set()
    # stable component order: largest first, tie-broken by smallest member id
    for comp in sorted(connected_components(edges), key=lambda c: (-len(c), min(c))):
        members = [tv for tv in comp if tv in nodes]
        if len(members) < min_members:
            continue
        members.sort(key=lambda tv: (nodes[tv].get("date") is None,
                                     nodes[tv].get("date") or "",
                                     str(nodes[tv].get("title") or "")))
        titles = [nodes[tv].get("title") for tv in members]
        base = normalize_key(titles[0]) or f"f{members[0]}"
        if base in deny:
            continue
        key, n = base, 2
        while key in used:
            key, n = f"{base}{n}", n + 1
        used.add(key)
        out[key] = {"titles": titles, "shows": members}
    return out
