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
_WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikipedia's hand-curated franchise tree: each subcategory of this root is one franchise, its member
# pages the franchise's shows. Catches cross-named families the Wikidata P2512/P179 edges miss (the
# member pages are still filtered to actual TVDB-id series before they count — same guard as the others).
_FRANCHISE_ROOT_CAT = "Category:Television series by franchise"
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


def _wiki_api(params: dict, *, timeout: int = 30, retries: int = 6) -> dict:
    """One MediaWiki action-API GET → parsed JSON, honouring 429/503 ``Retry-After`` rate-limiting."""
    url = _WIKI_API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries - 1:
                wait = min(int(e.headers.get("Retry-After", "10") or "10"), 60) * (attempt + 1)
                print(f"  wiki rate-limited ({e.code}); waiting {wait}s…", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def _wiki_members(category: str, cmtype: str, *, with_qid: bool = False):
    """Yield the members of a category (following ``continue`` paging). ``cmtype='subcat'`` →
    ``{title}``; ``cmtype='page'`` with ``with_qid`` → article pages as ``{title, qid}`` (the Wikidata
    item id from pageprops, namespace-0 articles only)."""
    if with_qid:
        params = {"action": "query", "generator": "categorymembers", "gcmtitle": category,
                  "gcmtype": cmtype, "gcmnamespace": 0, "gcmlimit": "500",
                  "prop": "pageprops", "ppprop": "wikibase_item"}
        cont: dict = {}
        while True:
            d = _wiki_api({**params, **cont})
            for p in ((d.get("query") or {}).get("pages") or {}).values():
                yield {"title": p.get("title"), "qid": (p.get("pageprops") or {}).get("wikibase_item")}
            if d.get("continue"):
                cont = d["continue"]
            else:
                return
    else:
        params = {"action": "query", "list": "categorymembers", "cmtitle": category,
                  "cmtype": cmtype, "cmlimit": "500"}
        cont = {}
        while True:
            d = _wiki_api({**params, **cont})
            for m in (d.get("query") or {}).get("categorymembers", []):
                yield {"title": m.get("title")}
            if d.get("continue"):
                cont = d["continue"]
            else:
                return


def _resolve_qids_to_tvdb(qids, *, batch: int = 180) -> dict:
    """``[QID…]`` → ``{QID: (tvdb, label, date)}`` via WDQS, KEEPING only items that carry a TVDB id
    (``P4835``) — the same actual-TV-series filter the P2512/P179 edges use. Batched VALUES so a few
    queries resolve hundreds of pages."""
    out: dict = {}
    qids = [q for q in qids if q and str(q).startswith("Q")]
    for i in range(0, len(qids), batch):
        values = " ".join(f"wd:{q}" for q in qids[i:i + batch])
        q = ("SELECT ?item ?tvdb ?label ?date WHERE { VALUES ?item { %s } "
             "?item wdt:P4835 ?tvdb . ?item rdfs:label ?label . FILTER(LANG(?label) = 'en') "
             "OPTIONAL { ?item wdt:P571 ?date } }" % values)
        for b in _sparql(q):
            qid = ((b.get("item") or {}).get("value") or "").rsplit("/", 1)[-1]
            tv = _int((b.get("tvdb") or {}).get("value"))
            if tv is not None and qid not in out:
                out[qid] = (tv, (b.get("label") or {}).get("value"), (b.get("date") or {}).get("value"))
    return out


def fetch_wikipedia_categories():
    """Wikipedia ``Category:Television series by franchise`` → ``(edges, nodes)``. Each subcategory is a
    franchise; its member articles are resolved to TVDB-id series and star-connected (so they union
    with the Wikidata graph and catch cross-named families P2512/P179 miss)."""
    subcats = [m["title"] for m in _wiki_members(_FRANCHISE_ROOT_CAT, "subcat")]
    cat_qids: dict = {}
    all_qids: set = set()
    skipped = 0
    for cat in subcats:
        try:
            qids = [m["qid"] for m in _wiki_members(cat, "page", with_qid=True) if m.get("qid")]
        except urllib.error.URLError as e:                      # a persistent rate-limit/parse on one
            skipped += 1                                        # category never aborts the whole sweep
            print(f"  wiki category skipped ({cat}): {e}", file=sys.stderr)
            continue
        if len(qids) >= 2:                                       # a 1-member category can't be a franchise
            cat_qids[cat] = qids
            all_qids.update(qids)
        time.sleep(0.3)                                          # be polite to the API
    if skipped:
        print(f"  ({skipped} of {len(subcats)} categories skipped on fetch error)", file=sys.stderr)
    resolved = _resolve_qids_to_tvdb(sorted(all_qids))
    groups: dict = {}
    for cat, qids in cat_qids.items():
        members = [resolved[q] for q in qids if q in resolved]   # TVDB-bearing series only
        if len(members) >= 2:
            groups[cat] = {"members": members, "types": set()}
    return franchise_star_edges(groups, min_members=2)


def _merge_nodes(dst: dict, src: dict) -> None:
    """Fold ``src`` node metadata into ``dst`` in place: add missing tvdbs, fill empty title/date gaps
    (existing values win, so the FIRST source's metadata is authoritative)."""
    for tv, meta in src.items():
        cur = dst.get(tv)
        if cur is None:
            dst[tv] = dict(meta)
        else:
            if not cur.get("title") and meta.get("title"):
                cur["title"] = meta["title"]
            if cur.get("date") is None and meta.get("date"):
                cur["date"] = meta["date"]


def generate(*, min_members: int = 2, deny=None, wiki: bool = True):
    """Fetch the franchise graph — Wikidata spin-off (P2512) ∪ part-of-the-series (P179) ∪ (when
    ``wiki``) the Wikipedia ``Television series by franchise`` category tree — and build the catalog.
    Returns ``(catalog, edges, nodes)``."""
    e_spin, n_spin = fetch_p2512()
    e_part, n_part = fetch_p179()
    edges = e_spin + e_part
    nodes = dict(n_part)
    _merge_nodes(nodes, n_spin)
    if wiki:
        e_wiki, n_wiki = fetch_wikipedia_categories()
        edges += e_wiki
        _merge_nodes(nodes, n_wiki)
    return build_franchises(edges, nodes, min_members=min_members, deny=deny), edges, nodes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate the TV-franchise catalog from Wikidata (P2512 + P179) + Wikipedia categories.")
    ap.add_argument("--out", default=None, help="output JSON path (default: the package catalog file)")
    ap.add_argument("--min-members", type=int, default=2, help="minimum series per franchise")
    ap.add_argument("--no-wiki", action="store_true", help="skip the Wikipedia franchise-category edge")
    ap.add_argument("--dry-run", action="store_true", help="print stats only, do not write")
    args = ap.parse_args(argv)

    try:
        catalog, edges, nodes = generate(min_members=args.min_members, wiki=not args.no_wiki)
    except Exception as e:                                       # network / parse — fail loud, write nothing
        print(f"generation failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    members = sum(len(v["shows"]) for v in catalog.values())
    src = "P2512 + P179" + ("" if args.no_wiki else " + Wikipedia categories")
    print(f"{len(edges)} edges ({src}), {len(nodes)} tvdb series "
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
