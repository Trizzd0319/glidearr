# hooks — Design

> Breadcrumb: [glidearr](../..) › [scripts](../README.md) › **hooks**

**Package** — `scripts.hooks`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [`scripts/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Two of Glidearr's invariants share an unpleasant property: **violating them is
cheap and silent, while detecting them later is expensive or impossible.**

1. **Secrets in git.** The repo talks to six authenticated APIs. A committed key
   is not "fix it in the next commit" — git history is permanent, so the only
   real remedy is rotating the credential everywhere it is used. The cost is
   asymmetric enough that prevention must be mechanical.

2. **Brain-layer purity.** [`DESIGN.md`](../DESIGN.md) §3.1 states the
   load-bearing rule: services do I/O, the brain decides. If a brain module
   imports `requests` or reaches into `scripts.managers.services`, nothing breaks
   at commit time — or at run time. What breaks is the
   [`eval/`](../managers/machine_learning/eval/README.md) replay harness, weeks
   later, when someone tries to forward-validate a scoring change and discovers
   the "pure function" needs a live Radarr. By then the import is load-bearing
   and removing it is a refactor.

Code review does not reliably catch either. Both are enforced at commit.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | **Fail closed.** Ambiguity blocks the commit; the author can override deliberately. |
| G2 | **Fast.** Staged-diff only. A hook that costs seconds gets disabled by its owner. |
| G3 | **No false-positive tax.** A guard that cries wolf gets `--no-verify`'d permanently, which is worse than no guard. |
| G4 | **Never print a secret.** The failure message must not itself leak. |
| G5 | **Degrade gracefully.** Missing Python or missing `gitleaks` must not hard-error. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Scanning git history | That is a remediation problem, not a prevention one. Use `gitleaks detect` manually. |
| N2 | Server-side enforcement | Single-operator repo; no forge-side hook infrastructure. |
| N3 | Full static analysis of the brain | Only the import boundary is guarded. Behavioural purity (hidden global state) is not checked. |
| N4 | Guarding the service layer | Services are *supposed* to do I/O. |

---

## 3. Architecture

### 3.1 Component map

```
git commit
   │
   ▼
pre-commit  (POSIX sh)
   │  resolves python | python3, else exits 0 with a warning  ← G5
   │
   ├─► secret_scan.py ──── exit 1 ──► BLOCKED
   │        │
   │        │ staged filenames ─► BLOCK_FILES regex
   │        │ staged +lines    ─► ALLOW regex ─► PATTERNS[]
   │        └─ gitleaks protect --staged   (if on PATH)
   │
   └─► brain_purity.py (exec) ── exit 1 ──► BLOCKED
            │
            └─ walk machine_learning/<17 guarded subpackages>
               └─ ast.parse ─► Import / ImportFrom nodes
                  └─ forbidden top-level module? service layer? *_api?
```

`pre-commit` uses `exec` for the second hook, so `brain_purity.py`'s exit code
becomes the hook's exit code directly.

### 3.2 Control flow

Sequential and short-circuiting: a secret finding blocks before the purity check
runs. This ordering is deliberate — the secret scan is the higher-severity guard,
and reporting both classes of failure at once produces a wall of output that
encourages blanket `--no-verify`.

### 3.3 Data contracts

Neither hook has a data contract with the application. They consume `git` output
and the filesystem, and communicate solely via exit code and stderr. They import
nothing from `scripts/`, which keeps them runnable even when the package is in a
broken intermediate state mid-refactor.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | AST parsing for purity, not grep | Brain docstrings are full of the phrase "NO HTTP"; grep would false-positive on the documentation describing the rule (G3) | Regex over source |
| D2 | Skip relative imports in the purity walk | `from .foo import bar` stays inside the brain by construction; scanning them adds noise, not signal | Scan all imports |
| D3 | Allow-list before pattern match in the secret scan | Placeholders (`<YOUR_KEY>`, `CHANGEME`, `""`) are the dominant false-positive source (G3) | Pattern-only |
| D4 | Mask all reported values | The block message goes to a terminal, gets pasted into chat logs and issue trackers (G4) | Print the match |
| D5 | Staged-diff only | Full-tree scan is seconds; staged diff is milliseconds (G2) | Full working-tree scan |
| D6 | `default_config.json` exempt from the filename block, not the content scan | It is the blank template and must be committable, but must stay blank | Blanket exemption |
| D7 | Suffix match on `*_api` rather than an explicit module list | New services get guarded automatically without editing the hook | Hardcoded `{radarr_api, sonarr_api, …}` |
| D8 | `gitleaks` optional, not required | Hard dependency would break the hook on a fresh machine (G5) | Require gitleaks |
| D9 | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` | Windows `cp1252` consoles otherwise crash the hook on non-ASCII paths, turning a guard into an outage | Assume UTF-8 |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No file matching `BLOCK_FILES` is ever committed (except `default_config.json`). |
| I2 | No guarded brain subpackage imports an HTTP client, the service layer, or a `*_api` module. |
| I3 | A hook failure message never contains an unmasked credential. |
| I4 | A missing Python interpreter results in exit 0 with a warning, never a crash. |
| I5 | Hooks import nothing from `scripts/` — they run against a broken tree. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| No `python` on PATH | `command -v` in [`pre-commit`](./pre-commit) | Warn to stderr, **exit 0** — commit proceeds unguarded | Guard silently off (accepted, G5) |
| `gitleaks` absent | `shutil.which` returns `None` | Skip; built-in patterns still run | Reduced ruleset |
| Brain file has a syntax error | `ast.parse` raises | Reported as its own violation line, blocks commit | Correct — a syntax error should block |
| Non-UTF-8 console | `reconfigure(errors="replace")` | Mangled glyphs, hook still completes | Cosmetic |
| False positive | Author judgement | `git commit --no-verify` | Author-controlled |
| Legacy flat brain module violates purity | **Not detected** — outside guarded subpackages | Silently passes | Known gap, §9 P1 |

**The §6 row that matters:** the "no Python ⇒ exit 0" path means the guard is
*advisory*, not a hard gate. It is the right call for a local hook (a hook that
blocks all work when a tool is missing gets uninstalled) but it means neither
invariant is guaranteed by this mechanism alone. CI enforcement is §9 P2.

---

## 7. Configuration surface

No config keys. Behaviour is controlled by:

| Surface | Where | Effect |
|---|---|---|
| `core.hooksPath` | git config | Activates the hooks for the clone |
| `BLOCK_FILES` | [`secret_scan.py`](./secret_scan.py) | Filenames that may never be committed |
| `PATTERNS` | [`secret_scan.py`](./secret_scan.py) | Secret-shaped content rules |
| `ALLOW` | [`secret_scan.py`](./secret_scan.py) | False-positive suppression |
| `_GUARDED_SUBPACKAGES` | [`brain_purity.py`](./brain_purity.py) | Which brain subpackages are enforced |
| `_FORBIDDEN_TOP` | [`brain_purity.py`](./brain_purity.py) | Banned top-level modules |
| [`.gitleaks.toml`](../../.gitleaks.toml) | repo root | gitleaks ruleset |

---

## 8. Implemented capabilities

- ✅ Filename blocklist with `default_config.json` carve-out
- ✅ JWT / credential-literal / URL-credential content patterns
- ✅ Placeholder allow-list to suppress the common false positives
- ✅ Masked reporting (never prints a full secret)
- ✅ Optional `gitleaks protect --staged` integration
- ✅ AST-based brain-purity guard across 17 subpackages
- ✅ Relative-import and `test_*` exclusion
- ✅ Windows console-encoding hardening
- ✅ Graceful skip when Python is unavailable
- ✅ Unit test for the purity guard ([`test_brain_purity.py`](./test_brain_purity.py))

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Migrate the two legacy flat brain modules** into guarded subpackages, closing the §6 scope gap | Completes I2 coverage | S | Step-9 ML cleanup |
| P2 | **Run both guards in CI**, not only locally | Removes the "guard silently off" failure mode | S | Any CI runner |
| P3 | **Docs-parity guard** — assert every `.py` has a doc row and every folder has README+DESIGN | Prevents doc rot at this repo's scale | S | [`mirror_docs.py`](../support/tools/mirror_docs.py) |
| P4 | **Purity rule: no filesystem writes in the brain** — extend the AST walk to `open(..., 'w')`, `Path.write_*`, `to_parquet` | I10 currently covers imports only; a brain module could still write to disk | M | — |
| P5 | **No-`global_cache` guard** — brain modules must not accept or reference a cache handle | Same class of erosion as HTTP | M | P4 |
| P6 | **Test-file purity exemption review** — `test_*.py` is skipped entirely; a test fixture importing the service layer can normalise the violation | Low, but cheap to close | S | — |
| P7 | **`unittest`-runnable secret-scan tests** mirroring `test_brain_purity.py` | The secret scan is currently untested | S | — |
| P8 | **Commit-msg hook** enforcing conventional-commit prefixes | Enables changelog generation | S | — |
| P9 | **Report machine-readable output** (`--json`) so CI can annotate the diff | Better CI UX | S | P2 |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should the `--no-verify` escape hatch be logged somewhere auditable? | — |
| Q2 | Is a 16-char minimum the right threshold for credential literals, given some API keys are shorter? | — |
| Q3 | Should purity guard `machine_learning/updates/` and `foundation/` and `thresholds/`, which are currently unguarded subpackages? | P1 |

**On Q3:** `updates/`, `foundation/`, `thresholds/`, `discovery/`, `labels/`,
`challenger/`, `features/` — of these only `features` appears in
`_GUARDED_SUBPACKAGES`. Several genuinely-pure subpackages are therefore
unguarded. Worth an explicit audit rather than assuming the list is complete.

## 11. Related designs

- [`scripts/DESIGN.md`](../DESIGN.md) §5 (invariants), §3.1 (layer rule)
- [`managers/machine_learning/ARCHITECTURE.md`](../managers/machine_learning/ARCHITECTURE.md)
- [`managers/factories/config/DESIGN_secrets_backend.md`](../managers/factories/config/DESIGN_secrets_backend.md)
