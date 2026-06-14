#!/usr/bin/env python3
"""
Pre-commit secret guard for Glidearr.

Blocks a commit when the staged changes either:
  - add a file that must never be committed (config.json, .env, *.pem/.key, ...), or
  - introduce a secret-looking value (JWT, quoted api_key/token/secret literal,
    or an ?apikey=/token= URL query).

Also runs `gitleaks protect --staged` when gitleaks is on PATH (full ruleset).

Bypass (use sparingly, only for a confirmed false positive):
    git commit --no-verify
"""
import re
import shutil
import subprocess
import sys

# Never let console encoding (e.g. Windows cp1252) crash the hook.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Files whose presence in a commit is itself the problem (secrets live here).
# default_config.json is the blank template -> allowed (content scan still applies).
BLOCK_FILES = re.compile(
    r"(?i)(^|/)(config\.json|\.env(\..+)?|secrets?\.(json|ya?ml|txt)|id_rsa|"
    r".*\.pem|.*\.key|.*\.pfx|.*\.p12)$"
)

# Lines that are clearly NOT secrets (placeholders / scrub markers / blanks).
ALLOW = re.compile(
    r"(\*\*\*REMOVED\*\*\*|<[^>]*>|REPLACE|PLACEHOLDER|EXAMPLE|CHANGEME|"
    r"your[_-]?\w+|:\s*\"\"\s*,?\s*$|=\s*\"\"\s*$|=\s*$|:\s*null)",
    re.I,
)

PATTERNS = [
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}")),
    # Quoted credential literal: "api_key": "abcd..."  /  client_secret = 'abcd...'
    ("credential literal", re.compile(
        r"(?i)(api_?key|access_token|refresh_token|client_secret|client_id|"
        r"plex_?token|password|secret|\btoken\b|\bapi\b)\s*[:=]\s*"
        r"[\"']([A-Za-z0-9_\-./+=]{16,})[\"']")),
    # Credential in a URL query string: ?apikey=...  &token=...
    ("url credential", re.compile(
        r"(?i)[?&](api_?key|access_token|token)=([A-Za-z0-9_\-./+=]{12,})")),
]


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, errors="replace").stdout


def _staged_files():
    return [l for l in _git("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines() if l.strip()]


def _staged_added_lines(path):
    out = _git("diff", "--cached", "-U0", "--", path)
    return [l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++")]


def _mask(v):
    return (v[:4] + "..." + v[-2:]) if len(v) > 8 else "***"


def main() -> int:
    findings = []
    for f in _staged_files():
        nf = f.replace("\\", "/")
        if BLOCK_FILES.search(nf) and not nf.endswith("default_config.json"):
            findings.append(("blocked file", f, f"'{f}' must never be committed - secrets/keys belong in the keyring/env."))
            continue
        for line in _staged_added_lines(f):
            if ALLOW.search(line):
                continue
            for label, pat in PATTERNS:
                m = pat.search(line)
                if m:
                    val = m.group(m.lastindex) if m.lastindex else m.group(0)
                    findings.append((label, f, _mask(val)))
                    break

    gitleaks_failed = False
    gl = shutil.which("gitleaks")
    if gl:
        r = subprocess.run([gl, "protect", "--staged", "--no-banner", "--redact"],
                           capture_output=True, text=True)
        gitleaks_failed = r.returncode != 0

    if findings or gitleaks_failed:
        print("\n[BLOCKED] pre-commit: potential secret(s) in staged changes.\n")
        for label, f, detail in findings[:40]:
            print(f"  - [{label}] {f}: {detail}")
        if gitleaks_failed:
            print("  - gitleaks flagged staged content - run `gitleaks protect --staged -v` for detail.")
        print("\n  Fix: keep secrets out of the repo - store them via `python scripts/support/setup/setup_secrets.py`")
        print("       (OS keyring) or RECOMMENDARR_* env vars; config.json must stay blank.")
        print("  False positive? Bypass with:  git commit --no-verify  (use sparingly).\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
