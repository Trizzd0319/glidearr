"""
steps/media.py — Tautulli & Plex (optional, live-validated).
================================================================================
Tautulli is written in the LIVE nested shape ``{"default": {url, port, api,
base_url}}`` (tautulli/__init__.py collapses ``.default``). Plex is a flat block.
Both are optional: skipped via a yes/no prompt interactively, or skipped headless
unless a value/probe env var is present.
"""
from __future__ import annotations

import re
import uuid

from scripts.managers.factories.onboarding import schema, validators
from scripts.managers.factories.onboarding.steps.base import Step, StepResult, host_field, should_configure

# Parental-controls age tiers offered for plex.playlists.profile_ages (matches
# machine_learning/playlists/cert_gate tier names). 'adult' = no restriction.
_AGE_TIERS = ["adult", "little_kid", "older_kid", "teen"]


def _select_indices(raw: str, count: int) -> list[int]:
    """Parse a 1-based, comma/space-separated selection (``"1,3 5"`` or a ``"2-4"``
    range) into sorted, de-duplicated 0-based indices within ``[0, count)``. Junk and
    out-of-range tokens are ignored so a fat-fingered entry can't crash setup."""
    picked: set[int] = set()
    for tok in re.split(r"[,\s]+", (raw or "").strip()):
        if not tok:
            continue
        lo, sep, hi = tok.partition("-")
        if sep and lo.isdigit() and hi.isdigit():
            for n in range(int(lo), int(hi) + 1):
                if 1 <= n <= count:
                    picked.add(n - 1)
        elif tok.isdigit() and 1 <= int(tok) <= count:
            picked.add(int(tok) - 1)
    return sorted(picked)


def _verify_and_store_pin(prompter, pins, title, user_uuid, token, client_identifier, attempts=2):
    """Prompt for a profile's PIN and VERIFY it by minting the per-user token the
    runtime will use (the /switch endpoint). On success, store ``pins[title]`` and
    confirm. On a definite rejection (wrong PIN) re-prompt up to ``attempts`` times,
    then store the last entry with a clear warning rather than blocking setup. A
    non-rejection (network/unknown) is stored unverified with a soft note so a flaky
    check never traps the user. No verify when the profile carries no stable uuid."""
    for attempt in range(max(1, attempts)):
        pin = prompter.secret(f"plex.pins.{title}.pin", f"PIN for '{title}'", required=False)
        if not pin:
            return                                   # left blank → skip this profile
        if not (user_uuid and token):
            pins[title] = {"pin": pin}               # nothing to verify against — trust it
            return
        v = validators.plex_switch_user(token, client_identifier, user_uuid, pin)
        if v.get("ok"):
            prompter.success(f"   PIN verified for '{title}'.")
            pins[title] = {"pin": pin}
            return
        if v.get("rejected") and attempt < max(1, attempts) - 1:
            prompter.warn(f"   PIN for '{title}' was rejected by Plex — re-enter it.")
            continue                                 # give the operator another try now
        if v.get("rejected"):
            prompter.warn(f"   PIN for '{title}' still rejected — saved anyway; this profile's "
                          f"per-user features will be skipped at run-time until the PIN is fixed.")
        else:
            prompter.warn(f"   Couldn't verify '{title}' PIN ({v.get('error')}) — saved unverified.")
        pins[title] = {"pin": pin}
        return


def _tautulli_current(tcfg: dict) -> dict:
    """Extract the current Tautulli instance dict from any supported shape."""
    if isinstance(tcfg.get("default"), dict):
        return tcfg["default"]
    if tcfg and all(isinstance(v, str) for v in tcfg.values()):
        return tcfg  # legacy flat shape
    for v in tcfg.values():
        if isinstance(v, dict):
            return v
    return {}


class TautulliStep(Step):
    name = "tautulli"
    title = "Tautulli"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("Tautulli")
        cur = _tautulli_current(cfg.get("tautulli", {}) or {})
        if not should_configure(prompter, "tautulli", "Tautulli",
                                default_on=bool(cur.get("url")),
                                probe_path="tautulli.default.url"):
            return [StepResult("tautulli", ok=None, detail="skipped", skipped=True)]

        host = host_field(prompter, ctx, "tautulli.default.url", "Tautulli host/IP (or full URL)",
                          default=cur.get("url", ""))
        port = prompter.text("tautulli.default.port", "Tautulli port",
                             default=str(cur.get("port", "") or "8181"), required=False)
        api = prompter.secret("tautulli.default.api", "Tautulli API key",
                              default=cur.get("api", ""), required=True)
        block = {
            "url": host.strip(),
            "port": str(port).strip(),
            "api": api,
            "base_url": schema.build_base_url(host, port),
        }
        cfg["tautulli"] = {"default": block}

        res = validators.tautulli_ping(host, port, api, base_url=block["base_url"])
        if res["ok"]:
            prompter.success(f"   Tautulli OK ({res['version']})")
            return [StepResult("tautulli", ok=True, detail=res["version"])]
        prompter.warn(f"   Tautulli not reachable: {res['error']} — saved, fix later")
        return [StepResult("tautulli", ok=False, detail=res["error"] or "unreachable")]


class PlexStep(Step):
    name = "plex"
    title = "Plex"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("Plex")
        cur = cfg.get("plex", {}) or {}
        if not should_configure(prompter, "plex", "Plex",
                                default_on=bool(cur.get("url")),
                                probe_path="plex.plex_token"):
            return [StepResult("plex", ok=None, detail="skipped", skipped=True)]

        host = host_field(prompter, ctx, "plex.url", "Plex host/IP (or full URL)",
                          default=cur.get("url", ""))
        port = prompter.integer("plex.port", "Plex port",
                                default=int(cur.get("port") or 32400))
        # The account OWNER's token is what unlocks Home-profile enumeratoin (per-user
        # playlists + age-gating); a managed/Home-user token only ever sees itself.
        # Tell the operator how to grab the right one before prompting for it.
        prompter.notice(
            "   Tip: use the Plex ACCOUNT OWNER's token (not a managed/Home user) — only the\n"
            "   owner token can list Home profiles for per-user playlists and age-gating.\n"
            "   To find it: sign in to Plex as the owner, open any library item, then\n"
            "   ⋯ (More) → Get Info → View XML, and copy the X-Plex-Token=... value from the\n"
            "   URL that opens. (Or copy PlexOnlineToken=\"...\" from the server's Preferences.xml.)")
        token = prompter.secret("plex.plex_token", "Plex token (X-Plex-Token)",
                                default=cur.get("plex_token", ""), required=True)
        media = prompter.text("plex.plex_media_path", "Plex media path",
                              default=cur.get("plex_media_path", ""), required=False)
        # Stable X-Plex-Client-Identifier: persist ONCE so v2 endpoints don't 401 and
        # a per-run uuid4 doesn't spawn device churn / 2FA (DESIGN Q2). Preserve any
        # existing one.
        client_identifier = cur.get("client_identifier") or str(uuid.uuid4())
        # Start from the EXISTING block so capability sub-keys this step doesn't manage
        # (episodes / reconcile / on_deck / collections / playlists, plus the schema
        # skeleton's defaults) survive a Plex reconfigure — only the connection fields
        # below are overwritten. (Previously this rebuilt cfg["plex"] from scratch and
        # silently dropped every other plex.* sub-config on a re-run.) The nested PIN
        # map rides along via this copy.
        block = dict(cur)
        block.update({
            "url": host.strip(),
            "port": port,
            "plex_token": token,
            "plex_media_path": media.strip(),
            "client_identifier": client_identifier,
        })
        cfg["plex"] = block

        # Local-PMS reachability (/identity).
        res = validators.plex_ping(host, port, token)
        if res["ok"]:
            prompter.success(f"   Plex OK ({res['version']})")
        else:
            prompter.warn(f"   Plex not reachable: {res['error']} — saved, fix later")

        # Account-OWNER scope probe (plex.tv/api/v2/user). Today onboarding only did
        # /identity — misleadingly green — so a server/managed-scoped token looked fine
        # but every per-user feature would silently degrade. Surface it now.
        scope = validators.plex_account_scope(token, client_identifier)
        if scope["ok"]:
            prompter.success(f"   Plex account scope OK ({scope['version']}) — per-user features enabled")
        else:
            prompter.warn(
                f"   Plex account scope: {scope['error']} — watchlist/per-user features will "
                f"run owner-only until an account-owner token is provided")

        # Fetch the Home roster ONCE (interactive + account-owner scope) and reuse it
        # for both PIN capture and age-tier detection.
        roster = self._fetch_home_roster(prompter, token, client_identifier, scope_ok=scope["ok"])

        # Optional: capture PINs for PIN-protected Home profiles (credentials → stored
        # as secrets, never plaintext). PIN-less profiles are fully automatic.
        self._configure_pins(prompter, cfg, roster, token, client_identifier)

        # Optional: per-profile age ratings (parental controls) → playlist age-gating.
        self._configure_profile_ages(prompter, cfg, roster)

        # Optional: personal "Up Next" playlist behaviour (build tuning + opt-in write-back).
        self._configure_playlists(prompter, cfg)

        ok = bool(res["ok"])
        detail = res["version"] if res["ok"] else (res["error"] or "unreachable")
        if res["ok"] and not scope["ok"]:
            detail = f"{res['version']} (owner-only scope)"
        return [StepResult("plex", ok=ok, detail=detail)]

    # ── Home roster (shared by PINs + age tiers) ───────────────────────────────
    @staticmethod
    def _fetch_home_roster(prompter, token, client_identifier, scope_ok):
        """Fetch the Plex Home roster ONCE (interactive + account-owner scope) so PIN
        capture and age-tier detection share it. Returns ``[]`` when unavailable
        (headless / no scope / fetch failure) — callers degrade gracefully."""
        if not (getattr(prompter, "is_interactive", False) and scope_ok and token):
            return []
        r = validators.plex_home_users(token, client_identifier)
        if r.get("ok"):
            users = r.get("users") or []
            if not users:
                prompter.notice("   No Home profiles found (single-user account).")
            return users
        if r.get("error"):
            prompter.warn(f"   Couldn't list Home profiles ({r['error']}) — manual entry where needed.")
        return []

    # ── PIN capture ───────────────────────────────────────────────────────────
    def _configure_pins(self, prompter, cfg, roster, token, client_identifier):
        """Collect PINs for PIN-protected Home profiles. With the fetched ``roster`` it
        presents a NUMBERED pick-list (PIN-protected profiles flagged); when the roster
        is unavailable (headless / no scope) it degrades to env-driven free-text title
        entry. PINs land in ``plex.pins = {title: {"pin": ...}}`` (the ``pin`` leaf is
        keyring'd)."""
        existing = cfg["plex"].get("pins") or {}
        if not prompter.confirm("plex.has_pins",
                                "Configure PINs for PIN-protected Home profiles?",
                                default=bool(existing)):
            return

        pins: dict = dict(existing)
        if roster:
            self._pins_from_roster(prompter, pins, roster, token, client_identifier)
        else:
            self._pins_from_titles(prompter, pins)

        if pins:
            cfg["plex"]["pins"] = pins

    # ── age tiers (parental controls → playlist gating) ────────────────────────
    @staticmethod
    def _detect_tier(restriction_profile) -> str:
        """Map a Plex restriction profile to an age-tier name; 'adult' when absent."""
        key = str(restriction_profile or "").strip().lower().replace("-", "_").replace(" ", "_")
        return key if key in ("little_kid", "older_kid", "teen") else "adult"

    def _configure_profile_ages(self, prompter, cfg, roster):
        """Per-profile age tier → ``plex.playlists.profile_ages`` so playlists are gated
        to age-appropriate content. Auto-DETECTS each managed profile's tier from Plex's
        restriction profile and lets the operator confirm/adjust (the auto-detect isn't
        guaranteed on every PMS, so the confirm is the reliable path). Only non-adult
        tiers are stored (adult is the default). No-op headless / when no managed
        profiles were listed."""
        managed = [u for u in (roster or []) if isinstance(u, dict)
                   and not u.get("is_admin") and (u.get("title") or "").strip()]
        if not managed:
            return
        any_kid = any(self._detect_tier(u.get("restriction_profile")) != "adult" for u in managed)
        if not prompter.confirm("plex.has_profile_ages",
                                "Set per-profile age ratings (parental controls for playlists)?",
                                default=any_kid):
            return
        prompter.notice("   Tiers: little_kid (G/TV-Y/TV-G)  older_kid (+PG/TV-Y7/TV-PG)  "
                        "teen (+PG-13/TV-14)  adult (no limit)")
        pl = dict(cfg["plex"].get("playlists") or {})
        ages = dict(pl.get("profile_ages") or {})
        for u in managed:
            title = u["title"].strip()
            detected = self._detect_tier(u.get("restriction_profile"))
            tier = str(prompter.choice(
                f"plex.profile_ages.{title}", f"   Age rating for '{title}'",
                _AGE_TIERS, default=ages.get(title) or detected) or "adult").strip().lower()
            if tier in _AGE_TIERS and tier != "adult":
                ages[title] = tier
            else:
                ages.pop(title, None)        # adult = default → don't persist
        if ages:
            pl["profile_ages"] = ages
        else:
            pl.pop("profile_ages", None)
        if pl:
            cfg["plex"]["playlists"] = pl

    # ── personal playlist behaviour (build tuning + opt-in write-back) ─────────
    def _configure_playlists(self, prompter, cfg):
        """Opt-in per-user "Up Next" playlist behaviour → ``plex.playlists.*``. One entry
        prompt gates three toggles, ALL default-OFF so the default path is unchanged:
          • writeback.enabled        — actually WRITE the playlists into Plex (also needs
                                       ``dry_run=false`` to actuate);
          • recency_boost.enabled    — lift a show you're caught up on when a new
                                       season/episode lands;
          • cold_start_kids_prior    — seed a no-history kid profile from the household's
                                       age-appropriate viewing.
        Playlists build on the owned-media scans (``plex.episodes.enabled`` /
        ``plex.movies.enabled``); a notice points there when they're off. No-op when the
        operator declines the entry prompt."""
        plex = cfg.get("plex", {}) or {}
        pl = dict(plex.get("playlists") or {})
        wb = dict(pl.get("writeback") or {})
        rb = dict(pl.get("recency_boost") or {})
        already = bool(wb.get("enabled") or rb.get("enabled") or pl.get("cold_start_kids_prior"))

        if not prompter.confirm("plex.has_playlist_options",
                                "Set up personal 'Up Next' playlists for each profile?",
                                default=already):
            return
        scans_on = (bool((plex.get("episodes", {}) or {}).get("enabled"))
                    or bool((plex.get("movies", {}) or {}).get("enabled")))
        if not scans_on:
            prompter.notice("   Note: playlists need the owned-media scans — set "
                            "plex.episodes.enabled / plex.movies.enabled to build them.")

        wb["enabled"] = bool(prompter.confirm(
            "plex.playlists.writeback.enabled",
            "   Write the playlists INTO Plex (create real playlists)? Also needs dry_run=false.",
            default=bool(wb.get("enabled", False))))
        rb["enabled"] = bool(prompter.confirm(
            "plex.playlists.recency_boost.enabled",
            "   Surface a show you're caught up on when a new season/episode arrives?",
            default=bool(rb.get("enabled", False))))
        cold = bool(prompter.confirm(
            "plex.playlists.cold_start_kids_prior",
            "   Seed a no-history kid profile from the household's kid-show viewing?",
            default=bool(pl.get("cold_start_kids_prior", False))))

        pl["writeback"] = wb
        pl["recency_boost"] = rb
        pl["cold_start_kids_prior"] = cold
        cfg["plex"]["playlists"] = pl

    @staticmethod
    def _pins_from_roster(prompter, pins, roster, token, client_identifier):
        """Render the numbered Home roster (flagging owner / managed / PIN-protected
        and any PIN already saved), collect PINs for the comma-separated picks, and
        verify each one against the /switch endpoint so a wrong PIN is caught now."""
        prompter.notice("   Home profiles:")
        for i, u in enumerate(roster, 1):
            flags = []
            if u.get("is_admin"):
                flags.append("owner")
            if u.get("protected"):
                flags.append("PIN-protected")
            elif u.get("is_managed"):
                flags.append("managed")
            tag = f"  ({', '.join(flags)})" if flags else ""
            saved = " *" if (pins.get(u["title"]) or {}).get("pin") else ""
            prompter.notice(f"     {i}) {u['title']}{tag}{saved}")
        prompter.notice("   (* = PIN already saved)")
        raw = prompter.text("plex.pin_select",
                            "Which profiles need a PIN? (comma-separated numbers, blank to skip)",
                            default="", required=False)
        for idx in _select_indices(raw, len(roster)):
            u = roster[idx]
            _verify_and_store_pin(prompter, pins, u["title"], u.get("uuid", ""),
                                  token, client_identifier)

    @staticmethod
    def _pins_from_titles(prompter, pins):
        """Free-text fallback: enter PIN-protected profile titles directly. Used
        headlessly (env-driven via ``plex.pin_titles``) and whenever the Home roster
        can't be fetched, preserving the original behaviour as a safety net."""
        titles = prompter.text("plex.pin_titles",
                               "PIN-protected profile titles (comma-separated)",
                               default="", required=False)
        for title in [t.strip() for t in (titles or "").split(",") if t.strip()]:
            pin = prompter.secret(f"plex.pins.{title}.pin", f"PIN for '{title}'", required=False)
            if pin:
                pins[title] = {"pin": pin}
