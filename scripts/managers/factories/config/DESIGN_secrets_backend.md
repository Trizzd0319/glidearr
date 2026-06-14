# DESIGN — Secrets Backend (config-file-first, container-aware, optional OS keystore)

> **Status:** Planned for Glidearr v1 release. Supersedes the keyring-default model.
> **Thesis:** Docker is the primary delivery matrix, and an OS keyring does **not** survive a container's lifecycle. So **secrets live in the config file by default** (the "SOP config file", on a private mounted volume, overridable by env vars). The **OS keystore stays available as an opt-in** for bare-metal users who want encrypted-at-rest secrets — offered during onboarding **only when we are not running inside a container**.

This refines the existing [`secret_store.py`](secret_store.py) + [`config_loader.py`](config_loader.py) + [`secret_bootstrap.py`](secret_bootstrap.py) machinery; it does not replace it.

---

## 0. TL;DR

| | Today (Recommendarr) | Glidearr |
|---|---|---|
| Default secret store | OS keyring | **Config file** (inline, gitignored, `0600`) |
| Keyring | required-ish (default target) | **opt-in**, non-container only, optional dependency |
| Env vars | override (always) | override (always) — the recommended Docker injection path |
| Onboarding | stores to keyring | **asks** config-vs-keystore *only when not containerized* |
| Resolution order | env → keyring → inline | env → **selected backend** |

One new setting (`secrets.backend`), one new tiny module (`container_detect.py`), and backend-aware branches in the load/save overlay. Everything else (`is_secret_key`, `iter_secret_paths`, env-var contract, log scrubbing, atomic `0600` write) is reused unchanged.

---

## 1. The `secrets.backend` setting

```jsonc
"secrets": {
  "backend": "config"      // "config" (default) | "keyring"
}
```

- **`config`** (default) — secret values live **inline in `config.json`**. The file is already git-ignored (`**/config/config.json`) and written atomically with `0600` perms. In Docker it sits on a mounted `/config` volume; env vars still override.
- **`keyring`** — secret values live in the OS keystore (Windows Credential Manager / macOS Keychain / Linux Secret Service); the on-disk fields are blanked. Selectable only on a non-container host with a usable backend.

Resolution (per secret, first hit wins), unchanged at the call site (`config["trakt"]["client_secret"]`):

```
1. env var  GLIDEARR_<PATH>          # 12-factor: Docker secrets / k8s / CI — always wins
2. selected backend:
     backend == "config"  →  the inline value already in config.json
     backend == "keyring" →  keyring.get_password("glidearr", <path>)
3. None  →  missing
```

---

## 2. Container detection — `container_detect.is_in_container()`

New module `scripts/managers/factories/config/container_detect.py`. Layered, cross-platform-safe (returns `False` on a normal Windows/macOS/Linux desktop):

```python
from __future__ import annotations
import os
from pathlib import Path


def is_in_container() -> bool:
    """Best-effort: are we running inside a container (Docker/Podman/k8s/LXC)?

    Order: explicit marker our image sets → runtime marker files →
    `container` env → cgroup/mountinfo signatures. False on bare-metal desktops.
    """
    # 1. Explicit marker baked into the official Glidearr image — most reliable.
    if os.environ.get("GLIDEARR_DOCKER", "").strip().lower() in ("1", "true", "yes"):
        return True
    # 2. Runtime marker files (Docker / Podman).
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    # 3. Some runtimes export `container=` (podman, systemd-nspawn).
    if os.environ.get("container"):
        return True
    # 4. cgroup / mountinfo signatures (Linux only; absent on Win/macOS → no false positive).
    for proc in ("/proc/1/cgroup", "/proc/self/mountinfo"):
        try:
            text = Path(proc).read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        if any(tok in text for tok in ("docker", "containerd", "kubepods", "/lxc/", "libpod")):
            return True
    return False
```

The official `Dockerfile` sets `ENV GLIDEARR_DOCKER=1`, making detection deterministic for our own image; the heuristics (2–4) cover hand-rolled images and other runtimes.

---

## 3. Onboarding flow

During first-run setup, after the config skeleton exists, decide the backend:

```
in_container  = is_in_container()
keyring_ready = SecretStore().available()      # usable OS keystore backend present

if in_container:
    backend = "config"
    log: "📦 Container detected → secrets will be stored in your mounted config file.
          Keep /config on a private volume, or inject GLIDEARR_* env vars for stricter setups."
    # no prompt

elif keyring_ready:
    # the only case where we ASK
    choice = prompt(
        "Where should Glidearr keep your API keys & tokens?",
        options = {
            "1": "OS keystore — encrypted at rest, per-user (recommended for desktop)",
            "2": "Config file — simpler, plaintext in your gitignored config.json",
        },
        default = "1",
    )
    backend = "keyring" if choice == "1" else "config"

else:                                   # bare-metal but no usable keystore (headless, missing backend)
    backend = "config"
    log: "ℹ️ No usable OS keystore here → secrets stored in config.json (0600, gitignored)."

config["secrets"]["backend"] = backend
```

This is the **whole** feature the request asks for: container ⇒ config file, no question; non-container ⇒ offer the keystore.

---

## 4. Backend-aware load/save (the only behavioral edits)

`SecretStore.__init__(..., backend: str = "config")` carries the choice. `ConfigLoader` reads `config["secrets"]["backend"]` and constructs the store accordingly.

**`SecretStore.get(path)`**
```
env = os.environ.get(env_name(path))
if env: return env
if self.backend == "keyring" and self.keyring_ok:
    return keyring.get_password(self.service, path) or None
return None        # "config" backend → the inline value is already in config; loader leaves it
```

**`_overlay_secrets()` (load)** — branch on backend:
- `keyring`: today's behavior (overlay env/keyring over blank fields).
- `config`: env-var override only; **inline values are kept as-is and are NOT warned about** (inline is the intended state now, not a migration smell).

**`_strip_secrets_to_store()` (save)** — branch on backend:
- `keyring`: today's behavior (persist to keyring, blank on disk).
- `config`: **no strip** — secrets stay inline. (Fields whose value is supplied purely via `GLIDEARR_*` env may still be written blank, so the env stays the source of truth.)

`secret_bootstrap.audit()` keeps reporting `env | keyring | inline | missing`, but under the `config` backend `inline` is logged as **normal**, not a warning.

---

## 5. Security model (config backend)

Inline secrets are safe *because of the surrounding guarantees*, which already exist:
- **Never committed** — `config.json` is git-ignored (`**/config/config.json`).
- **Not world-readable** — saved atomically with `chmod 0600` (no-op on Windows; document NTFS ACLs / keep the volume private there).
- **Override for the strict** — any secret can be supplied via `GLIDEARR_<PATH>` env (Docker secrets, k8s, CI) and kept blank on disk.
- **Log-scrubbed** — resolved secret values are registered with the logger's scrubber on load (unchanged).
- **Private volume guidance** — docs tell Docker users to mount `/config` on a non-shared volume and prefer `GLIDEARR_*` env for multi-tenant hosts.

The keystore backend remains the strongest option (encrypted at rest) and is one onboarding choice away on desktops.

---

## 6. Dependency change — keyring becomes optional

`keyring` moves from a base requirement to an **optional extra** so the Docker image is lean and never imports it:
- `requirements.txt` → base set **without** `keyring`.
- `pip install glidearr[keyring]` (or `requirements-keystore.txt`) for desktop users who pick the keystore.
- `secret_store.py` already imports keyring inside a `try/except` and degrades to env-only — so a missing keyring is already a no-op. If a non-container user picks "keystore" but the package is absent, onboarding tells them to `pip install glidearr[keyring]` and falls back to `config`.

---

## 7. Migration & compatibility

- **Glidearr fresh install:** default `secrets.backend = "config"`. Enter secrets in onboarding (or env), they land in `config.json`.
- **Desktop user who wants the keystore:** picks it in onboarding → `backend = "keyring"`, existing strip-to-keyring path applies.
- **Existing Recommendarr → Glidearr (this repo):** since credentials are being **rotated** for the public launch anyway, no keyring migration script is needed — choose `config`, paste the rotated secrets. (The old keyring service `recommendarr` is simply abandoned; see the rename scope.)
- The reverse migrator (`keyring → config`) is a nice-to-have, not a blocker.

---

## 8. Phased plan

| Phase | Scope | Risk |
|-------|-------|------|
| **P0** | `container_detect.is_in_container()` + unit tests (mock `/.dockerenv`, env, cgroup) | none — pure, standalone |
| **P1** | `secrets.backend` setting + schema default `"config"`; `SecretStore(backend=…)`; backend-aware `_overlay_secrets` / `_strip_secrets_to_store` | medium — security path; gate behind tests, keep keyring path byte-identical when `backend="keyring"` |
| **P2** | Onboarding choice (container-gated) + messaging; `Dockerfile` sets `GLIDEARR_DOCKER=1` | low |
| **P3** | Make `keyring` an optional extra; trim base `requirements.txt`; docs (`GLIDEARR_*` table, private-volume guidance) | low |

Folds into the `Recommendarr → Glidearr` rename: the keyring `SERVICE`/`ENV_PREFIX` rename (`glidearr` / `GLIDEARR_`) happens here, and the keyring stops being critical-path because it's now opt-in.

---

## 9. Open questions

1. **One config file or two?** Keep secrets in `config.json` (simplest, current shape) vs a sibling `secrets.json`. Recommendation: **one file** — `config.json` is already gitignored and the existing overlay/strip walks it; a second file adds surface for little gain. Revisit only if non-secret config wants to be committable.
2. **Windows `0600` equivalent.** `os.fchmod` no-ops on Windows; document NTFS ACL guidance or apply `icacls` on save for desktop Windows users who choose `config`.
3. **Default for non-container, keyring-available, non-interactive** (e.g. a bare-metal cron with no TTY). Recommendation: default `config` (can't prompt); a flag/env (`GLIDEARR_SECRETS_BACKEND`) can force `keyring` headlessly.
4. **`GLIDEARR_SECRETS_BACKEND` env override** — let containers/automation pin the backend without onboarding. Cheap; include in P1.

---

## 10. Key files

**New:** `scripts/managers/factories/config/container_detect.py`
**Edited:** `secret_store.py` (backend param; `SERVICE`/`ENV_PREFIX` → `glidearr`/`GLIDEARR_`), `config_loader.py` (backend-aware overlay/strip), `secret_bootstrap.py` (inline-is-normal under `config`), `onboarding/__init__.py` + secret step (the choice), `onboarding/schema.py` + default config (`secrets.backend`), `requirements.txt` (+ optional `[keyring]` extra), `Dockerfile` (`ENV GLIDEARR_DOCKER=1`).
