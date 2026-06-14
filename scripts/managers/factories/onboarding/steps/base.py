"""
steps/base.py — Step contract + small shared helpers.
================================================================================
Each step takes the live prompter, the working config dict, and a transient
``ctx`` (used to pass e.g. root folders discovered from Sonarr/Radarr to the
Library step). A step returns a list of ``StepResult`` for the final summary.
``ctx`` is NEVER persisted — only ``cfg`` is saved.
"""
from __future__ import annotations

import time

from scripts.managers.factories.onboarding import env_map


class StepResult:
    """One row in the onboarding summary table.

    ok: True = validated OK · False = saved but unreachable · None = skipped/N-A.
    """

    def __init__(self, service: str, ok=True, detail: str = "", skipped: bool = False):
        self.service = service
        self.ok = ok
        self.detail = detail
        self.skipped = skipped

    @property
    def icon(self) -> str:
        if self.skipped or self.ok is None:
            return "—"
        return "✅" if self.ok else "⚠️"


class Step:
    name = "step"
    title = "Step"
    optional = False

    def __init__(self, logger=None):
        self.logger = logger

    def run(self, prompter, cfg, ctx) -> list:
        raise NotImplementedError


# ── shared helpers ────────────────────────────────────────────────────────────

def should_configure(prompter, key: str, label: str, default_on: bool, probe_path: str | None = None) -> bool:
    """Decide whether to configure an optional service.

    Interactive: a yes/no prompt (defaulting to ``default_on``).
    Headless: configure when something already exists (``default_on``) or the
    operator supplied a probe env var — otherwise skip silently.
    """
    if prompter.is_interactive:
        return prompter.confirm(f"{key}.enable", f"Configure {label}?", default=default_on)
    if default_on:
        return True
    return bool(probe_path and env_map.get_env(probe_path))


def csv_field(prompter, path: str, label: str, current, suggestions=None, fallback=None, require=None) -> list:
    """Prompt for a comma-separated list, returning a clean list of tokens.

    ``suggestions`` are shown (interactive only) as typical valid values; when the
    field has no current value, ``fallback`` seeds the editable default so the user
    can accept a sensible set with Enter. ``require`` is a mandatory token that is
    force-included (case-insensitive) because routing depends on it — e.g.
    'documentary' must be present for documentary content to reach the documentary
    root folder.
    """
    if suggestions and getattr(prompter, "is_interactive", False):
        prompter.notice("   Typical values: " + ", ".join(suggestions))
    base = current if (isinstance(current, list) and current) else (fallback if fallback is not None else current)
    default = ",".join(base) if isinstance(base, list) else str(base or "")
    raw = prompter.text(path, label, default=default, required=False)
    values = env_map.split_list(raw)
    if require and not any(str(v).lower() == require.lower() for v in values):
        values.insert(0, require)
        prompter.notice(f"   (kept required '{require}' — content is only routed to the "
                        f"{require} root folder when its genres include '{require}')")
    return values


def host_field(prompter, ctx, path: str, label: str, default: str = "") -> str:
    """Prompt for a host/IP, offering to reuse one host across all instances/services.

    On the first host entered (across the whole wizard), asks whether the same
    host/IP applies everywhere; if yes, it becomes the editable default for every
    later host prompt (press Enter to keep). An instance's own existing value
    always takes precedence as the default. Ports and API keys stay per-instance.
    Headless is unaffected (no prompt; values come from env per path).
    """
    shared = ctx.get("shared_host") if isinstance(ctx, dict) else None
    eff_default = default or (shared or "")
    host = prompter.text(path, label, default=eff_default, required=True)
    if host and isinstance(ctx, dict) and not ctx.get("shared_host_asked"):
        ctx["shared_host_asked"] = True
        if getattr(prompter, "is_interactive", False) and prompter.confirm(
            f"{path}.__shared",
            f"   Use {host} as the host/IP for all other instances and services?",
            default=True,
        ):
            ctx["shared_host"] = host
    return host


def token_expired(auth: dict) -> bool:
    """True if a Trakt authorization dict is missing/expired."""
    try:
        created = int(auth.get("created_at", 0))
        life = int(auth.get("expires_in", 0))
    except (TypeError, ValueError):
        return True
    if not created or not life:
        return True
    return (int(time.time()) - created) > life
