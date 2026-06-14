"""One-shot metadata extractor for the human-read audit checklist.

Walks every .py and .md under scripts/ (plus repo-root docs/config), and for each
file records: its own module docstring (the "what it claims to do" an auditor
checks the code against), top-level class/function names, line count, whether it
is a runnable entrypoint, and heuristic security touchpoints (network / exec /
delete / secret / filesystem). Each file is assigned an ORGANIC execution stage +
sub-order derived from scripts/main.py's run order and each service manager's
component_dependencies insertion order — NOT alphabetical tree order.

Output: audit_metadata.json at repo root. Pure read-only; safe to re-run.
"""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]   # repo root (…/Glidearr)
SCRIPTS = ROOT / "scripts"

# ── Security touchpoint heuristics (hints for the auditor, not verdicts) ──────
PATTERNS = {
    "NET":  re.compile(r"\b(requests|httpx|aiohttp|urllib|arrapi)\b|https?://|webhook|api_url|base_url", re.I),
    "EXEC": re.compile(r"\b(subprocess|Popen|os\.system|os\.exec|\beval\(|\bexec\(|pickle\.load|joblib\.load|yaml\.load)\b"),
    "DEL":  re.compile(r"\b(unlink|rmtree|os\.remove|shutil\.rm|deleteMovie|deleteEpisode|delete_movie|delete_episode|deleteFile|monitored\s*=\s*False)\b|\bDELETE\b"),
    "SEC":  re.compile(r"\b(api_key|apikey|api_token|access_token|refresh_token|password|secret|keyring|credential|client_secret|bearer)\b", re.I),
    "FS":   re.compile(r"\b(write_text|to_parquet|to_json|to_csv|open\(|mkdir|os\.rename|shutil\.move|moveFiles)\b"),
}


def first_doc(text):
    """Module docstring → single trimmed line."""
    try:
        tree = ast.parse(text)
        doc = ast.get_docstring(tree)
    except Exception:
        doc = None
    if not doc:
        return ""
    doc = " ".join(doc.strip().split())
    return doc[:400]


def top_defs(text):
    try:
        tree = ast.parse(text)
    except Exception:
        return [], []
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    funcs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return classes, funcs


def md_summary(text):
    """First heading + first prose paragraph of a markdown file."""
    head, para = "", ""
    lines = text.splitlines()
    for ln in lines:
        s = ln.strip()
        if s.startswith("#"):
            head = s.lstrip("# ").strip()
            break
    buf = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("```") or s.startswith("|") or s.startswith("---"):
            if buf:
                break
            continue
        buf.append(s)
        if len(" ".join(buf)) > 200:
            break
    para = " ".join(buf)[:400]
    return head, para


def sec_flags(text):
    return " ".join(k for k, rx in PATTERNS.items() if rx.search(text))


# ── Organic stage assignment ─────────────────────────────────────────────────
# (stage_code, stage_label, phase_when) chosen in priority order.
def rel(p):
    return p.relative_to(ROOT).as_posix()


def stage_of(r):
    """Return dict(stage_code, stage, phase, when_hint)."""
    s = r
    def res(code, label, phase, when):
        return {"stage_code": code, "stage": label, "phase": phase, "when": when}

    # ── 00 ENTRY / cold-start bootstrap ──
    if s == "scripts/main.py":
        return res("00", "Entry · main.py", "Cold start — process entry", "ALWAYS ▶")
    if s.startswith("scripts/managers/factories/onboarding/"):
        return res("00", "Entry · onboarding", "Cold start — first-run setup", "ALWAYS")
    if s.startswith("scripts/managers/factories/config/"):
        return res("00", "Entry · config + secrets", "Cold start — config load", "ALWAYS")
    if s.startswith("scripts/managers/factories/daemons/"):
        return res("01", "Daemon · supervisor/paths", "Cold start — daemon (re)spawn", "OPT-IN")
    if s.startswith("scripts/support/setup/"):
        return res("00", "Entry · setup scripts", "Cold start — provisioning helpers", "ALWAYS ▶")
    if s in ("scripts/support/utilities/auth_validator.py", "scripts/support/utilities/bootstrap.py"):
        return res("00", "Entry · auth validation", "Cold start — parallel auth check", "ALWAYS")
    if s.startswith("scripts/support/utilities/logger/"):
        return res("00", "Entry · logging", "Cold start — logger", "ALWAYS")
    if s.startswith("scripts/hooks/"):
        return res("00", "Entry · git/CI hooks", "Dev-time — pre-commit guards", "DEV")

    # ── 01 DAEMON (the detached enrich process itself) ──
    if s.startswith("scripts/support/daemons/"):
        return res("01", "Daemon · enrich loop", "Background — out-of-band Trakt enrich", "OPT-IN ▶")

    # ── 02 FACTORIES (shared plumbing built in Main.__init__) ──
    if s.startswith("scripts/managers/factories/"):
        return res("02", "Factories · core plumbing", "Cold start — factory construction", "ALWAYS")

    # ── Services ──
    if s.startswith("scripts/managers/services/tautulli/"):
        return res("03", "Tautulli", "Phase 1/2 — runs 1st", "ALWAYS")
    if s.startswith("scripts/managers/services/trakt/"):
        return res("04", "Trakt", "Phase 1/2 — runs 2nd", "ALWAYS")
    if s.startswith("scripts/managers/services/mal/"):
        return res("05", "MAL (anime)", "Phase 2 — runs 3rd", "OPT-IN (self-disables)")
    if s.startswith("scripts/managers/services/sonarr/"):
        return res("06", "Sonarr", "Phase 1/2 — runs 4th", "ALWAYS")
    if s.startswith("scripts/managers/services/radarr/"):
        return res("07", "Radarr", "Phase 1/2 — runs 5th (last core)", "ALWAYS")

    # ── 08 ML BRAIN (pure plan() libs, called from within services) ──
    if s.startswith("scripts/managers/machine_learning/"):
        return res("08", "ML brain", "Within Sonarr/Radarr/coordinator runs", "IMPORT (brain)")

    # ── 09 COORDINATOR (Phase 2.5) ──
    if s.startswith("scripts/managers/services/coordinator/"):
        return res("09", "Space coordinator", "Phase 2.5 — unified delete pool", "OPT-IN")

    # ── 10 PHASE 3 (opt-in capabilities) ──
    if s.startswith("scripts/managers/services/calendar/"):
        return res("10", "Calendar", "Phase 3 — runs 1st", "OPT-IN")
    if s.startswith("scripts/managers/services/acquisition/"):
        return res("10", "Acquisition", "Phase 3 — runs 2nd", "OPT-IN")
    if s.startswith("scripts/managers/services/writeback/"):
        return res("10", "Writeback", "Phase 3 — runs 3rd", "OPT-IN")
    if s == "scripts/managers/services/renamer.py":
        return res("10", "Renamer", "Helper — find caller / standalone", "?")
    if s.startswith("scripts/managers/services/") and s.count("/") == 3:
        return res("10", "Services · package init", "Service package glue", "ALWAYS")

    # ── 11 NOTIFY + end-of-run ──
    if s.startswith("scripts/support/notifications/"):
        return res("11", "Notifications", "End of run — Discord summary", "OPT-IN")
    if s == "scripts/managers/orchestration/dry_run.py":
        return res("11", "Dry-run ledger", "End of run — plan rollup support", "ALWAYS")

    # ── 13 STANDALONE TOOLS / DEBUG / TESTS (never on the main path) ──
    if s.startswith("scripts/support/tools/"):
        return res("13", "Standalone tools", "Manual — operator CLI", "STANDALONE ▶")
    if s.startswith("scripts/debug/"):
        return res("13", "Debug scripts", "Manual — diagnostics", "DEBUG ▶")
    if "/test/" in s or s.endswith("test.py") or s.endswith("_test.py") or "tvdb_test" in s or s.endswith("api test.py"):
        return res("13", "Test / probe scripts", "Manual — API probes", "TEST ▶")

    # ── 12 SHARED UTILITIES (import-only, used throughout) ──
    if s.startswith("scripts/support/utilities/") or s.startswith("scripts/support/config/"):
        return res("12", "Shared utilities", "Cross-cutting — imported throughout", "IMPORT (shared)")
    if s.startswith("scripts/support/"):
        return res("12", "Shared support", "Cross-cutting", "IMPORT (shared)")

    # ── 14 ROOT docs/config ──
    if "/" not in s or s.count("/") == 0:
        return res("14", "Repo root", "Docs / packaging", "DOC/CONFIG")
    if s.startswith("scripts/") and s.count("/") == 1:
        return res("02", "Scripts · package init", "Package glue", "ALWAYS")
    if s.startswith("scripts/managers/") and s.endswith("__init__.py"):
        return res("02", "Scripts · package init", "Package glue", "ALWAYS")
    return res("15", "Other", "Unclassified", "?")


# Per-service component run order (from each manager's component_dependencies
# insertion order + __init__ construction). Lower = earlier.
SONARR_SUB = {"api": 0, "": 1, "validator": 11, "instance": 2, "cache": 3, "storage": 4,
              "series": 5, "episodes": 6, "quality": 7, "monitoring": 8, "sync": 9,
              "repair": 10, "orchestration": 12}
RADARR_SUB = {"api": 0, "": 1, "validator": 10, "instance": 2, "cache": 3, "storage": 4,
              "movies": 5, "quality": 6, "monitoring": 7, "sync": 8, "repair": 9,
              "orchestration": 11}
TAUTULLI_SUB = {"": 0, "api": 1, "validator": 2, "instances": 3}
GENERIC_SUB = {"api": 0, "": 1, "auth": 1, "validator": 2, "instance": 2, "instances": 2,
               "cache": 3, "movies": 4, "series": 4, "history": 4, "episodes": 5,
               "quality": 6, "storage": 7, "sync": 8, "monitoring": 9, "repair": 10,
               "orchestration": 11}
FACTORY_SUB = {"": 0, "registry": 3, "cache": 4, "mixins": 6, "singleton": 7, "config": 1, "daemons": 8}
ML_SUB = {"": 0, "scoring": 1, "contracts": 2, "lifecycle": 3, "acquisition": 4,
          "space": 5, "routing": 6, "quality_analytics": 7, "updates": 8}


def substage(r, stage_code):
    """Sub-order within a stage + the leaf component label."""
    parts = r.split("/")
    # the path segment immediately under the stage root
    def seg_after(prefix_depth):
        return parts[prefix_depth] if len(parts) > prefix_depth + 1 else ""

    if stage_code == "00":  # cold-start: enforce true entry order
        if r == "scripts/main.py":
            return -1, "main.py"
        if "/onboarding/" in r:
            depth = 2 if r.endswith("onboarding/steps") else 1
            return 1, parts[-2] if len(parts) > 1 else "onboarding"
        if "/config/" in r:
            return 2, "config"
        if "auth_validator" in r or "bootstrap" in r:
            return 3, "auth"
        if "/logger/" in r:
            return 4, "logger"
        if "/setup/" in r:
            return 5, "setup"
        if "/hooks/" in r:
            return 6, "hooks"
        return 1, parts[-2] if len(parts) > 1 else ""

    if stage_code == "06":  # sonarr: scripts/managers/services/sonarr/<seg>/...
        seg = seg_after(4)
        comp = seg or parts[-1]
        return SONARR_SUB.get(seg, 6), comp
    if stage_code == "07":  # radarr
        seg = seg_after(4)
        comp = seg or parts[-1]
        return RADARR_SUB.get(seg, 6), comp
    if stage_code == "03":  # tautulli
        seg = seg_after(4)
        return TAUTULLI_SUB.get(seg, 5), seg or parts[-1]
    if stage_code == "04":  # trakt
        seg = seg_after(4)
        return GENERIC_SUB.get(seg, 5), seg or parts[-1]
    if stage_code == "08":  # ml
        seg = seg_after(3)
        return ML_SUB.get(seg, 5), seg or parts[-1]
    if stage_code == "02":  # factories
        seg = seg_after(3) if r.startswith("scripts/managers/factories/") else ""
        return FACTORY_SUB.get(seg, 2), seg or parts[-1]
    # default: group by the dir right above the file
    return 0, (parts[-2] if len(parts) > 1 else "")


def collect():
    rows = []
    py = sorted(SCRIPTS.rglob("*.py"))
    md = sorted(SCRIPTS.rglob("*.md"))
    extra = [ROOT / "README.md", ROOT / "requirements.txt",
             ROOT / ".gitleaks.toml", ROOT / "LICENSE"]
    md_by_dirstem = {}

    all_files = [p for p in (py + md) if "__pycache__" not in p.as_posix()
                 and ".pytest_cache" not in p.as_posix()]
    all_files += [p for p in extra if p.exists()]

    for p in all_files:
        r = rel(p)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        ext = p.suffix.lower()
        st = stage_of(r)
        sub, comp = substage(r, st["stage_code"])
        loc = text.count("\n") + 1 if text else 0
        row = {
            "path": r,
            "name": p.name,
            "ext": ext,
            "stage_code": st["stage_code"],
            "stage": st["stage"],
            "phase": st["phase"],
            "when": st["when"],
            "component": comp,
            "sub": sub,
            "loc": loc,
        }
        if ext == ".py":
            row["doc"] = first_doc(text)
            classes, funcs = top_defs(text)
            row["defs"] = (", ".join(classes[:3]) + (" · " if classes and funcs else "")
                           + ", ".join(funcs[:4]))[:160]
            row["runnable"] = ('if __name__ == "__main__"' in text
                               or "if __name__ == '__main__'" in text)
            row["sec"] = sec_flags(text)
            row["md"] = None
        else:
            head, para = md_summary(text)
            row["doc"] = (head + (" — " if head and para else "") + para)[:400]
            row["defs"] = ""
            row["runnable"] = False
            row["sec"] = ""
            # mark as a paired doc if a .py with same stem sits beside it
            stem = p.with_suffix(".py")
            row["pairs_py"] = stem.exists()
            md_by_dirstem[(str(p.parent), p.stem)] = r
        rows.append(row)

    # Attach paired_md back onto .py rows
    for row in rows:
        if row["ext"] == ".py":
            p = ROOT / row["path"]
            key = (str(p.parent), p.stem)
            row["paired_md"] = md_by_dirstem.get(key)
    return rows


def order_key(row):
    return (row["stage_code"], row["sub"], row["component"],
            0 if row["ext"] == ".py" else 1, row["name"].lower())


if __name__ == "__main__":
    rows = collect()
    rows.sort(key=order_key)
    for i, row in enumerate(rows, 1):
        row["order"] = i
    out = ROOT / "audit_metadata.json"
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")

    # summary
    from collections import Counter
    by_stage = Counter((r["stage_code"], r["stage"]) for r in rows)
    print(f"TOTAL rows: {len(rows)}  (py={sum(1 for r in rows if r['ext']=='.py')}, "
          f"md={sum(1 for r in rows if r['ext']=='.md')}, "
          f"other={sum(1 for r in rows if r['ext'] not in ('.py','.md'))})")
    print("-" * 60)
    for (code, label), n in sorted(by_stage.items()):
        print(f"  {code}  {label:<34} {n:>4}")
    print("-" * 60)
    sec = Counter(f for r in rows for f in r["sec"].split() if f)
    print("security touchpoints:", dict(sec))
    runnable = [r["path"] for r in rows if r["runnable"]]
    print(f"runnable entrypoints (__main__): {len(runnable)}")
