"""
generate_tv_franchises.py — standalone generator for the TV-franchise catalog.
================================================================================
Builds the Layer-2 cross-named franchise catalog from **Wikidata**: spin-off (``P2512``) edges
between TheTVDB-id-bearing (``P4835``) television series. Connected components = franchises;
members are debut-ordered by inception date (``P571``). Output is JSON (PR-reviewable — binary
would hide a bad edit) at the catalog path the playlist builder loads.

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

from scripts.managers.services.plex.playlists.franchise_graph import build_franchises

_ENDPOINT = "https://query.wikidata.org/sparql"
_UA = "glidearr-tv-franchise-gen/1.0 (offline catalog build; contact via project repo)"

# ONE query → the whole franchise graph: every spin-off (P2512) edge whose BOTH endpoints are
# TheTVDB-id-bearing (P4835) series, carrying each endpoint's English label + inception date
# (P571, for member ordering). One request keeps us under WDQS rate limits; edges AND node
# metadata are derived from the same rows.
_GRAPH_Q = """
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


def fetch_graph():
    """One SPARQL round-trip → ``(edges, nodes)``: edges ``[(tvdb_a, tvdb_b)…]`` and node metadata
    ``{tvdb: {"title", "date"}}`` (first non-null date wins across the rows a node appears in)."""
    edges, nodes = [], {}

    def _note(tv, label, date):
        cur = nodes.get(tv)
        if cur is None:
            nodes[tv] = {"title": label, "date": date}
        elif cur.get("date") is None and date:
            cur["date"] = date

    for b in _sparql(_GRAPH_Q):
        a = _int((b.get("atvdb") or {}).get("value"))
        c = _int((b.get("btvdb") or {}).get("value"))
        if a is None or c is None or a == c:
            continue
        edges.append((a, c))
        _note(a, (b.get("aLabel") or {}).get("value"), (b.get("adate") or {}).get("value"))
        _note(c, (b.get("bLabel") or {}).get("value"), (b.get("bdate") or {}).get("value"))
    return edges, nodes


def generate(*, min_members: int = 2, deny=None):
    """Fetch the Wikidata graph and build the franchise catalog. Returns ``(catalog, edges, nodes)``."""
    edges, nodes = fetch_graph()
    return build_franchises(edges, nodes, min_members=min_members, deny=deny), edges, nodes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the TV-franchise catalog from Wikidata spin-off edges.")
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
    print(f"{len(edges)} spin-off edges, {len(nodes)} tvdb series "
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
