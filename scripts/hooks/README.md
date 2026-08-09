# hooks

> Breadcrumb: [glidearr](../..) › [scripts](../README.md) › **hooks**

**Package** — `scripts.hooks` (not importable as a package — these are executables)
**Run position** — Git pre-commit. Never part of the `main.py` run path.
**One-liner** — Two commit-time guards that mechanically enforce the repo's most expensive-to-violate invariants: no secrets in git, and no I/O in the brain layer.

---

## Purpose

Some invariants are too costly to enforce by review alone:

- **A leaked API key** is unrecoverable once pushed — rotation is the only fix.
- **Brain-layer purity** ([`DESIGN.md`](../DESIGN.md) §5, I10) erodes silently. One
  `import requests` inside `machine_learning/` and the offline
  [`eval/`](../managers/machine_learning/eval/README.md) harness stops being able
  to replay decisions — but nothing fails loudly at the time of the commit.

Both are therefore machine-checked on every commit, chained so that the secret
scan runs first and the purity check runs second.

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`pre-commit`](./pre-commit) | this file | POSIX shell entry point. Resolves `python`/`python3`, runs [`secret_scan.py`](./secret_scan.py), then `exec`s [`brain_purity.py`](./brain_purity.py) | ✅ Implemented |
| [`secret_scan.py`](./secret_scan.py) | this file | Blocks commits that add secret-bearing files or introduce secret-looking literals | ✅ Implemented |
| [`brain_purity.py`](./brain_purity.py) | this file | AST-based guard: no HTTP client, no service layer, no `*_api` imports inside guarded brain subpackages | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_brain_purity.py`](./test_brain_purity.py) | Violation detection in [`brain_purity.py`](./brain_purity.py) |

---

## Installation

Hooks are **not** active on a fresh clone. Activate per-clone with either:

```bash
python scripts/support/setup/install_hooks.py
# or
git config core.hooksPath scripts/hooks
```

See [`support/setup/install_hooks.py`](../support/setup/install_hooks.py).

Bypass for a confirmed false positive — **use sparingly**:

```bash
git commit --no-verify
```

---

## `secret_scan.py` — detection rules

Operates only on **staged** changes (`git diff --cached`), so it is fast and
never flags pre-existing history.

**Blocked filenames** (presence is itself the failure):
`config.json`, `.env*`, `secrets.{json,yaml,yml,txt}`, `id_rsa`, `*.pem`,
`*.key`, `*.pfx`, `*.p12`.

`default_config.json` is explicitly exempted — it is the blank template — though
its *content* is still scanned.

**Content patterns** (applied to added lines only):

| Label | Matches |
|---|---|
| JWT | `eyJ…` three-segment tokens |
| credential literal | quoted `api_key` / `access_token` / `refresh_token` / `client_secret` / `client_id` / `plex_token` / `password` / `secret` / `token` values ≥16 chars |
| url credential | `?apikey=` / `&token=` / `&access_token=` query values ≥12 chars |

**Allow-list** (suppresses false positives): `***REMOVED***`, `<angle
placeholders>`, `REPLACE`, `PLACEHOLDER`, `EXAMPLE`, `CHANGEME`, `your_*`,
empty strings, and `: null`.

**gitleaks** — if `gitleaks` is on `PATH`, `gitleaks protect --staged` also runs
with the repo ruleset in [`.gitleaks.toml`](../../.gitleaks.toml). Its failure
alone blocks the commit.

Findings are printed **masked** (`abcd...yz`), never in full.

---

## `brain_purity.py` — the guarded boundary

AST-based, not grep-based. This matters: the brain modules are full of docstrings
saying "NO global_cache / NO HTTP", and a grep implementation would false-positive
on its own documentation.

**Guarded subpackages** under `managers/machine_learning/`:

`contracts` · `affinity` · `features` · `scoring` · `likelihood` · `sizing` ·
`classification` · `space` · `lifecycle` · `acquisition` · `ledger` · `routing` ·
`quality_analytics` · `eval` · `next_watch` · `playlists` · `people_matrix`

**Forbidden imports:**

| Category | Detail |
|---|---|
| HTTP clients | `requests`, `httpx`, `urllib3`, `aiohttp` |
| Service layer | anything under `scripts.managers.services.*` |
| API modules | any module whose final segment ends in `_api` |

Relative imports (`level > 0`) are skipped — they stay inside the brain by
construction. `test_*.py` files are skipped.

**Known scope gap (intentional):** two legacy *flat* top-level brain modules,
[`profile_selector.py`](../managers/machine_learning/quality_analytics/profile_selector.py)
and
[`watchhistoryaggregator.py`](../managers/machine_learning/watchhistoryaggregator.py),
predate the migration. They live at the package root rather than in a guarded
subpackage, so the walk skips them. They are Step-9 cleanup targets — see
[`DESIGN.md`](./DESIGN.md) §9.

---

## Data in / data out

| Direction | Source/Sink | Payload |
|---|---|---|
| IN | `git diff --cached` | Staged filenames and added lines |
| IN | Filesystem walk of `machine_learning/<guarded>/` | Python source, parsed to AST |
| OUT | stderr / stdout | Masked findings, violation list |
| OUT | Exit code | `0` allow, `1` block |

Neither hook reads config, touches `global_cache`, or calls any external API.

---

## Navigation

- **Up:** [`scripts/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Related:** [`support/setup/install_hooks.py`](../support/setup/install_hooks.py) · [`.gitleaks.toml`](../../.gitleaks.toml) · [`support/setup/setup_secrets.py`](../support/setup/setup_secrets.py)
