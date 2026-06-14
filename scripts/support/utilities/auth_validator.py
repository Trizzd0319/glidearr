"""
ServiceAuthValidator
====================
Validates Radarr, Sonarr, Trakt (+ optional Plex / MDBList) credentials
concurrently and emits a single summary line:

    [Auth] ✅ Radarr:standard v6.1  ✅ Sonarr:720 v4.0  ✅ Trakt:{username}  (0.84s)

Individual service log lines are suppressed during the auth check.
Called from Main.__init__ before any manager is constructed.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.managers.factories.config.__Init__ import ConfigManager
    from scripts.support.utilities.logger.logger import LoggerManager


def validate_all(config: "ConfigManager", logger: "LoggerManager") -> dict[str, dict]:
    """
    Run Radarr, Sonarr, and Trakt auth checks in parallel.

    Returns a dict keyed by service name:
        {
          "radarr":  {"ok": True,  "label": "standard", "version": "6.1.1"},
          "sonarr":  {"ok": True,  "label": "720",       "version": "4.0.14"},
          "trakt":   {"ok": True,  "label": "{username}", "version": None},
        }
    """
    tasks = {
        "radarr": _check_radarr,
        "sonarr": _check_sonarr,
        "trakt":  _check_trakt,
    }
    # Plex is optional + NON-critical: only probe its account scope when a token is
    # configured, so a Plex-less install adds no plex.tv call and no summary noise.
    plex_configured = bool((config.get("plex", {}) or {}).get("plex_token"))
    if plex_configured:
        tasks["plex"] = _check_plex
    # MDBList is optional + OPT-IN: only validate when an apikey is configured, so an
    # MDBList-less install makes no api.mdblist.com call and adds no summary noise.
    mdblist_configured = bool((config.get("mdblist", {}) or {}).get("apikey"))
    if mdblist_configured:
        tasks["mdblist"] = _check_mdblist

    results: dict[str, dict] = {}
    t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="auth") as pool:
        futures = {pool.submit(fn, config): svc for svc, fn in tasks.items()}
        for future in as_completed(futures):
            svc = futures[future]
            try:
                results[svc] = future.result()
            except Exception as e:
                results[svc] = {"ok": False, "label": svc, "version": None, "error": str(e)}

    elapsed = time.monotonic() - t0

    # Build the single summary line
    summary_order = (["radarr", "sonarr", "trakt"]
                     + (["plex"] if plex_configured else [])
                     + (["mdblist"] if mdblist_configured else []))
    parts = []
    for svc in summary_order:
        r = results.get(svc, {"ok": False, "label": svc})
        icon    = "✅" if r.get("ok") else "❌"
        label   = r.get("label", svc)
        version = r.get("version")
        ver_str = f" v{version}" if version else ""
        error   = r.get("error", "")
        err_str = f" ({error[:40]})" if error and not r.get("ok") else ""
        parts.append(f"{icon} {svc.capitalize()}:{label}{ver_str}{err_str}")

    summary = "  ".join(parts)
    logger.log_info(f"[Auth] {summary}  ({elapsed:.2f}s)")

    return results


# ── Per-service checks ────────────────────────────────────────────────────────

def _check_radarr(config: "ConfigManager") -> dict:
    instances = config.get("radarr_instances", {})
    results = []
    for name, cfg in instances.items():
        if name == "default_instance" or not isinstance(cfg, dict):
            continue
        if cfg.get("failed"):
            results.append({"ok": False, "label": name, "version": None, "error": "marked failed"})
            continue
        try:
            from arrapi import RadarrAPI as _RadarrAPI
            base_url = cfg.get("base_url") or cfg.get("url", "")
            api_key  = cfg.get("api", "")
            api      = _RadarrAPI(base_url, api_key)
            version  = getattr(api.system_status(), "version", "unknown")
            results.append({"ok": True, "label": name, "version": version})
        except Exception as e:
            results.append({"ok": False, "label": name, "version": None, "error": str(e)[:60]})

    if not results:
        return {"ok": False, "label": "?", "version": None, "error": "no instances configured"}

    # Aggregate: ok only if all pass; label = first instance name for brevity
    all_ok   = all(r["ok"] for r in results)
    label    = results[0]["label"] if len(results) == 1 else f"{len(results)} instances"
    version  = results[0].get("version") if all_ok else None
    error    = next((r["error"] for r in results if not r["ok"]), None)
    return {"ok": all_ok, "label": label, "version": version, "error": error}


def _check_sonarr(config: "ConfigManager") -> dict:
    instances = config.get("sonarr_instances", {})
    results = []
    for name, cfg in instances.items():
        if name == "default_instance" or not isinstance(cfg, dict):
            continue
        if cfg.get("failed"):
            results.append({"ok": False, "label": name, "version": None, "error": "marked failed"})
            continue
        try:
            from arrapi import SonarrAPI as _SonarrAPI
            base_url = cfg.get("base_url") or cfg.get("url", "")
            api_key  = cfg.get("api", "")
            api      = _SonarrAPI(base_url, api_key)
            version  = getattr(api.system_status(), "version", "unknown")
            results.append({"ok": True, "label": name, "version": version})
        except Exception as e:
            results.append({"ok": False, "label": name, "version": None, "error": str(e)[:60]})

    if not results:
        return {"ok": False, "label": "?", "version": None, "error": "no instances configured"}

    all_ok  = all(r["ok"] for r in results)
    label   = results[0]["label"] if len(results) == 1 else f"{len(results)} instances"
    version = results[0].get("version") if all_ok else None
    error   = next((r["error"] for r in results if not r["ok"]), None)
    return {"ok": all_ok, "label": label, "version": version, "error": error}


def _check_trakt(config: "ConfigManager") -> dict:
    import requests as _req, time as _time

    trakt_cfg = config.get("trakt", {})
    client_id = trakt_cfg.get("client_id", "")
    auth      = trakt_cfg.get("authorization", {})
    token     = auth.get("access_token", "")
    username  = trakt_cfg.get("username", "")

    if not client_id or not token:
        return {"ok": False, "label": "?", "version": None, "error": "no credentials"}

    # Check token expiry
    try:
        created  = int(auth.get("created_at", 0))
        lifespan = int(auth.get("expires_in", 0))
        if lifespan and (int(_time.time()) - created) > lifespan:
            return {"ok": False, "label": username or "?", "version": None, "error": "token expired"}
    except (TypeError, ValueError):
        pass

    # Quick connectivity ping — /users/me
    try:
        resp = _req.get(
            "https://api.trakt.tv/users/me",
            headers={
                "Content-Type":      "application/json",
                "trakt-api-version": "2",
                "trakt-api-key":     client_id,
                "Authorization":     f"Bearer {token}",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            label = resp.json().get("username") or username or "?"
            return {"ok": True, "label": label, "version": None}
        return {
            "ok": False, "label": username or "?", "version": None,
            "error": f"HTTP {resp.status_code}",
        }
    except Exception as e:
        return {"ok": False, "label": username or "?", "version": None, "error": str(e)[:60]}


def _check_plex(config: "ConfigManager") -> dict:
    """Probe Plex account-owner scope (plex.tv/api/v2/user). NON-critical: a scope
    failure means per-user features degrade to owner-only, not that the run aborts."""
    import requests as _req

    plex_cfg = config.get("plex", {}) or {}
    token = plex_cfg.get("plex_token", "")
    client_id = plex_cfg.get("client_identifier", "")
    if not token:
        return {"ok": False, "label": "?", "version": None, "error": "no token"}
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    if client_id:
        headers["X-Plex-Client-Identifier"] = client_id
    try:
        resp = _req.get("https://plex.tv/api/v2/user", headers=headers, timeout=10)
        if resp.status_code == 200:
            try:
                label = resp.json().get("username") or "owner"
            except Exception:
                label = "owner"
            return {"ok": True, "label": label, "version": None}
        if resp.status_code in (401, 403):
            return {"ok": False, "label": "owner-only", "version": None, "error": "not account-scoped"}
        return {"ok": False, "label": "?", "version": None, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "label": "?", "version": None, "error": str(e)[:60]}


def _check_mdblist(config: "ConfigManager") -> dict:
    """Validate the MDBList API key and read the account TIER + request budget. The label
    carries the tier + budget (e.g. 'trizzd (supporter, 12/1000/day)') so the [Auth] line
    surfaces it. Opt-in: only called when mdblist.apikey is configured (see validate_all)."""
    from scripts.managers.services.mdblist.client import validate_key

    apikey = (config.get("mdblist", {}) or {}).get("apikey", "")
    if not apikey:
        return {"ok": False, "label": "?", "version": None, "error": "no apikey"}
    r = validate_key(apikey)
    if not r.get("ok"):
        return {"ok": False, "label": "?", "version": None, "error": r.get("error")}
    tier = r.get("tier") or "unknown"
    budget = ""
    if r.get("limit") is not None:
        used = r.get("used")
        budget = f", {used if used is not None else '?'}/{r['limit']}/day"
    label = f"{r.get('username') or 'ok'} ({tier}{budget})"
    return {"ok": True, "label": label, "version": None,
            "tier": tier, "limit": r.get("limit"), "used": r.get("used")}
