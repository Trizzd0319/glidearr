"""
prompts.py — input abstraction for interactive (TTY) and headless onboarding.
================================================================================
One ``Prompter`` interface, two implementations:

  * InteractivePrompter — terminal wizard. ``input()`` for normal fields,
    ``getpass`` for secrets (never echoed). Shows the current default in
    brackets; pressing Enter keeps it. Colour-coded, NO_COLOR-aware (same scheme
    as scripts/support/setup/setup_secrets.py).

  * HeadlessPrompter — no TTY (CI / Docker / unraid). Every field resolves from
    its ``RECOMMENDARR_*`` env var (via env_map). A required-but-missing value is
    recorded and logged (with the exact env var name to set) but NEVER blocks —
    onboarding is warn-and-continue.

Both expose ``notice/success/warn`` so transient messages (most importantly the
Trakt device-code URL) surface in interactive output AND in the logs that a
container/unraid operator reads.
"""
from __future__ import annotations

import getpass
import os
import sys

from scripts.managers.factories.onboarding import env_map

# ── Colour (honours NO_COLOR + non-TTY) ───────────────────────────────────────
_COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def red(s):    return _c("31", s)
def yellow(s): return _c("33", s)
def green(s):  return _c("32", s)
def cyan(s):   return _c("36", s)
def bold(s):   return _c("1", s)


class Prompter:
    """Abstract prompt interface. Subclasses implement the I/O."""

    is_interactive = False

    def __init__(self, logger=None, secret_store=None):
        self.logger = logger
        self.secret_store = secret_store
        # env var names of required fields that resolved empty (headless).
        self.missing_required: list[str] = []

    # ── value prompts (override) ──────────────────────────────────────────────
    def text(self, path, label, default="", required=False, secret=False) -> str:
        raise NotImplementedError

    def secret(self, path, label, default="", required=False) -> str:
        return self.text(path, label, default=default, required=required, secret=True)

    def integer(self, path, label, default=0, required=False) -> int:
        raise NotImplementedError

    def confirm(self, path, label, default=False) -> bool:
        raise NotImplementedError

    def choice(self, path, label, options, default=None, required=False) -> str:
        raise NotImplementedError

    # ── messaging (override for routing) ──────────────────────────────────────
    def notice(self, msg):  self._emit(msg, "log_info")
    def success(self, msg): self._emit(green(msg), "log_success")
    def warn(self, msg):    self._emit(yellow(msg), "log_warning")
    def section(self, title): self._emit("\n" + bold(cyan(f"── {title} ──")), "log_info")

    def _emit(self, msg, level):
        raise NotImplementedError


class InteractivePrompter(Prompter):
    is_interactive = True

    def _emit(self, msg, level):
        print(msg)

    def _label(self, label, default, secret):
        if secret:
            hint = "[set — Enter keeps]" if default else "[unset]"
        else:
            hint = f"[{default}]" if default not in (None, "") else ""
        return f"{label} {yellow(hint)}: " if hint else f"{label}: "

    def text(self, path, label, default="", required=False, secret=False) -> str:
        default = "" if default is None else str(default)
        # For a blank secret, fall back to any value already in the SecretStore
        # (env/keyring) so re-creating a session lets the user press Enter to keep
        # the existing credential — important after a config.json was lost.
        if secret and not default and self.secret_store is not None:
            try:
                existing = self.secret_store.get(path)
            except Exception:
                existing = None
            if existing:
                default = existing
        prompt = self._label(label, default, secret)
        while True:
            try:
                raw = (getpass.getpass(prompt) if secret else input(prompt)).strip()
            except EOFError:
                # No usable interactive stdin. Use the default / skip optionals;
                # for a required field, abort rather than spin forever.
                if default:
                    return default
                if not required:
                    return ""
                raise
            if raw:
                return raw
            if default:
                return default
            if not required:
                return ""
            self.warn("   This value is required.")

    def integer(self, path, label, default=0, required=False) -> int:
        while True:
            raw = self.text(path, label, default=str(default), required=required)
            if raw == "" and not required:
                return int(default or 0)
            try:
                return int(str(raw).strip())
            except ValueError:
                self.warn(f"   '{raw}' is not a whole number.")

    def confirm(self, path, label, default=False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            raw = ""
            try:
                raw = input(f"{label} {yellow(suffix)}: ").strip().lower()
            except EOFError:
                pass
            if not raw:
                return bool(default)
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            self.warn("   Please answer y or n.")

    def choice(self, path, label, options, default=None, required=False) -> str:
        options = list(options)
        if not options:
            return self.text(path, label, default=default or "", required=required)
        print(f"{label}:")
        for i, opt in enumerate(options, 1):
            mark = " (default)" if opt == default else ""
            print(f"   {cyan(str(i))}) {opt}{mark}")
        while True:
            raw = ""
            try:
                raw = input(f"   choose 1-{len(options)} or type a value [{default or ''}]: ").strip()
            except EOFError:
                if default is not None:
                    return default
                if not required:
                    return ""
                raise
            if not raw:
                if default is not None:
                    return default
                if not required:
                    return ""
                self.warn("   A choice is required.")
                continue
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1]
            return raw  # free-text value accepted


class HeadlessPrompter(Prompter):
    is_interactive = False

    def _emit(self, msg, level):
        fn = getattr(self.logger, level, None) if self.logger else None
        if callable(fn):
            try:
                fn(msg)
                return
            except Exception:
                pass
        print(msg)

    def _resolve(self, path, default, required):
        val = env_map.get_env(path)
        if val is not None:
            return val
        if required and (default in (None, "")):
            name = env_map.env_for(path)
            self.missing_required.append(name)
            self.warn(f"[Onboarding] missing required value — set env var {name}")
        return "" if default is None else str(default)

    def text(self, path, label, default="", required=False, secret=False) -> str:
        return self._resolve(path, default, required)

    def integer(self, path, label, default=0, required=False) -> int:
        raw = self._resolve(path, default, required)
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            return int(default or 0)

    def confirm(self, path, label, default=False) -> bool:
        val = env_map.get_env(path)
        return env_map.is_truthy(val) if val is not None else bool(default)

    def choice(self, path, label, options, default=None, required=False) -> str:
        val = env_map.get_env(path)
        if val is not None:
            return val
        return default if default is not None else ("" if not required else "")


def make_prompter(mode: str | None, logger=None, secret_store=None) -> Prompter:
    """Pick a prompter: explicit ``mode`` ("interactive"/"headless") or autodetect
    from whether stdin is a TTY."""
    if mode == "interactive":
        return InteractivePrompter(logger=logger, secret_store=secret_store)
    if mode == "headless":
        return HeadlessPrompter(logger=logger, secret_store=secret_store)
    is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    cls = InteractivePrompter if is_tty else HeadlessPrompter
    return cls(logger=logger, secret_store=secret_store)
