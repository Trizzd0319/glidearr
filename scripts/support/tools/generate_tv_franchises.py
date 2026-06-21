"""
generate_tv_franchises.py — standalone generator for the TV-franchise catalog.
================================================================================
Builds the Layer-2 cross-named franchise catalog from **Wikidata**, unioning two edges between
TheTVDB-id-bearing (``P4835``) television series: spin-off (``P2512``) and 'part of the series'
(``P179`` — so a franchise modelled as shared membership, not a direct spin-off, is still caught).
Connected components = franchises; members are debut-ordered by inception date (``P571``). Output
is JSON (PR-reviewable — binary would hide a bad edit) at the catalog path the playlist builder
loads. The ``P179`` edge has a programming-SLOT over-capture (Christmas-calendar / telenovela
time-slot nodes); it is guarded by a size cap + a bad-type exclude (see ``franchise_star_edges``).

  python -m scripts.support.tools.generate_tv_franchises [--out PATH] [--min-members N] [--dry-run]

DIFF-REVIEW the output before committing. Wikidata is openly editable, so a vandalised or
spurious spin-off edge would inject a bogus franchise; the JSON diff is the guard — this tool
NEVER auto-commits. The pure graph logic lives in
``services/plex/playlists/franchise_graph``; this module only does the network fetch + write.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from scripts.managers.services.plex.playlists.franchise_graph import (
    build_franchises, franchise_star_edges,
)

_ENDPOINT = "https://query.wikidata.org/sparql"
_UA = "glidearr-tv-franchise-gen/1.0 (offline catalog build; contact via project repo)"

# P179 ('part of the series') over-capture guards. The P4835 filter already drops episode/season
# rows; these drop the programming-SLOT rows — a Christmas-calendar / telenovela-time-slot node
# bundles dozens of UNRELATED shows. Real P179 franchises run 2-6 members; slots run 9-53.
_MAX_P179_GROUP = 8
_BAD_FRANCHISE_TYPES = {"program block", "wikimedia list article", "christmas tradition", "term",
                        "television genre", "trademark", "brand"}

# ONE query → the whole franchise graph: every spin-off (P2512) edge whose BOTH endpoints are
# TheTVDB-id-bearing (P4835) series, carrying each endpoint's English label + inception date
# (P571, for member ordering). One request keeps us under WDQS rate limits; edges AND node
# metadata are derived from the same rows.
_P2512_Q = """
SELECT ?atvdb ?aLabel ?adate ?btvdb ?bLabel ?bdate WHERE {
  ?a wdt:P2512 ?b .
  ?a wdt:P4835 ?atvdb .
  ?b wdt:P4835 ?btvdb .
  ?a rdfs:label ?aLabel . FILTER(LANG(?aLabel) = "en")
  ?b rdfs:label ?bLabel . FILTER(LANG(?bLabel) = "en")
  OPTIONAL { ?a wdt:P571 ?adate }
  OPTIONAL { ?b wdt:P571 ?bdate }
}
"""

# Second edge: 'part of the series' (P179). Each TVDB-id series + its franchise node, with the
# franchise's P31 types concatenated (so a programming-slot node can be excluded by type). Members
# sharing a franchise node are co-members — see franchise_star_edges.
_P179_Q = """
SELECT ?tvdb ?label ?date ?series (GROUP_CONCAT(DISTINCT ?typeLabel; SEPARATOR="|") AS ?types) WHERE {
  ?s wdt:P179 ?series .
  ?s wdt:P4835 ?tvdb .
  ?s rdfs:label ?label . FILTER(LANG(?label) = "en")
  OPTIONAL { ?s wdt:P571 ?date }
  OPTIONAL { ?series wdt:P31 ?stype . ?stype rdfs:label ?typeLabel . FILTER(LANG(?typeLabel) = "en") }
}
GROUP BY ?tvdb ?label ?date ?series
"""


def _catalog_path() -> str:
    """``…/plex/playlists/tv_franchises.generated.json`` — co-located with the loader."""
    import scripts.managers.services.plex.playlists.franchise_graph as fg
    return os.path.join(os.path.dirname(fg.__file__), "tv_franchises.generated.json")


def _sparql(query: str, *, timeout: int = 90, retries: int = 5) -> list[dict]:
    """Run a SPARQL query, honouring WDQS 429 rate-limiting (Retry-After, default 65s)."""
    url = _ENDPOINT + "?" + urllib.parse.urlencode({"format": "json", "query": query})
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/sparql-results+json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)["results"]["bindings"]
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = min(int(e.headers.get("Retry-After", "65") or "65"), 120)
                print(f"  rate-limited (429); waiting {wait}s…", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def fetch_p2512():
    """Spin-off (P2512) edges → ``(edges, nodes)``: edges ``[(tvdb_a, tvdb_b)…]`` between TVDB-id
    series and node metadata ``{tvdb: {"title", "date"}}`` (first non-null date wins)."""
    edges, nodes = [], {}

    def _note(tv, label, date):
        cur = nodes.get(tv)
        if cur is None:
            nodes[tv] = {"title": label, "date": date}
        elif cur.get("date") is None and date:
            cur["date"] = date

    for b in _sparql(_P2512_Q):
        a = _int((b.get("atvdb") or {}).get("value"))
        c = _int((b.get("btvdb") or {}).get("value"))
        if a is None or c is None or a == c:
            continue
        edges.append((a, c))
        _note(a, (b.get("aLabel") or {}).get("value"), (b.get("adate") or {}).get("value"))
        _note(c, (b.get("bLabel") or {}).get("value"), (b.get("bdate") or {}).get("value"))
    return edges, nodes


def fetch_p179():
    """'Part of the series' (P179) franchises → ``(edges, nodes)``. Groups TVDB-id series by their
    shared franchise node, then :func:`franchise_star_edges` drops the programming-slot over-capture
    (size cap + bad-type exclude) and star-connects the survivors so they union with the spin-off
    graph."""
    groups: dict = {}
    for b in _sparql(_P179_Q):
        tv = _int((b.get("tvdb") or {}).get("value"))
        qid = (b.get("series") or {}).get("value")
        if tv is None or not qid:
            continue
        slot = groups.setdefault(qid, {"members": [], "types": set()})
        slot["members"].append((tv, (b.get("label") or {}).get("value"), (b.get("date") or {}).get("value")))
        slot["types"].update(t.strip().lower()
                             for t in ((b.get("types") or {}).get("value") or "").split("|") if t.strip())
    return franchise_star_edges(groups, min_members=2, max_members=_MAX_P179_GROUP,
                                deny_types=_BAD_FRANCHISE_TYPES)


def generate(*, min_members: int = 2, deny=None):
    """Fetch the Wikidata graph (spin-off P2512 ∪ part-of-the-series P179) and build the franchise
    catalog. Returns ``(catalog, edges, nodes)``."""
    e_spin, n_spin = fetch_p2512()
    e_part, n_part = fetch_p179()
    edges = e_spin + e_part
    nodes = dict(n_part)
    for tv, meta in n_spin.items():                              # P2512 metadata wins, but fill gaps
        cur = nodes.get(tv)
        if cur is None:
            nodes[tv] = meta
        else:
            if not cur.get("title") and meta.get("title"):
                cur["title"] = meta["title"]
            if cur.get("date") is None and meta.get("date"):
                cur["date"] = meta["date"]
    return build_franchises(edges, nodes, min_members=min_members, deny=deny), edges, nodes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the TV-franchise catalog from Wikidata (P2512 + P179).")
    ap.add_argument("--out", default=None, help="output JSON path (default: the package catalog file)")
    ap.add_argument("--min-members", type=int, default=2, help="minimum series per franchise")
    ap.add_argument("--dry-run", action="store_true", help="print stats only, do not write")
    args = ap.parse_args(argv)

    try:
        catalog, edges, nodes = generate(min_members=args.min_members)
    except Exception as e:                                       # network / parse — fail loud, write nothing
        print(f"generation failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    members = sum(len(v["shows"]) for v in catalog.values())
    print(f"{len(edges)} edges (P2512 + P179), {len(nodes)} tvdb series "
          f"-> {len(catalog)} franchises ({members} member series)")
    biggest = sorted(catalog.items(), key=lambda kv: -len(kv[1]["shows"]))[:8]
    for k, v in biggest:
        print(f"  {k:24s} {len(v['shows']):2d}  {', '.join(v['titles'][:4])}"
              + (" …" if len(v["titles"]) > 4 else ""))
    if args.dry_run:
        print("(dry-run — nothing written)")
        return 0

    out = args.out or _catalog_path()
    with open(out, "w", encoding="utf-8") as f:
        json.dump({k: catalog[k] for k in sorted(catalog)}, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    print(f"-> wrote {out}  (DIFF-REVIEW before committing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
