#!/usr/bin/env python3
"""
discord_purge.py — one-off: clear past Recommendarr posts from the webhook's
channel so it can start fresh.

Why a bot token?
----------------
A webhook can *post* and can *delete a message by id*, but it has no way to
*list* a channel's history. To find the old posts we need read access, which
only a bot (or user) token provides. So this tool needs a BOT token for a bot
that is a member of the server with **Read Message History** + **Manage
Messages** on the target channel. The webhook URL itself (read from the keyring)
supplies the channel id and the webhook id we filter on — you don't have to look
either up by hand.

The bot token is read ONLY from the DISCORD_BOT_TOKEN env var and is never
printed, logged, or stored.

Usage (PowerShell)
------------------
    $env:DISCORD_BOT_TOKEN = "<bot token>"

    # Preview only — list what would be deleted, delete nothing:
    python scripts/support/tools/discord_purge.py --dry-run

    # Delete just the messages this webhook posted (default):
    python scripts/support/tools/discord_purge.py

    # Delete EVERY message in the channel (only for a dedicated channel):
    python scripts/support/tools/discord_purge.py --all

Optional overrides (normally auto-derived from the webhook):
    --channel-id <id>     target channel id
    --webhook-url <url>   use this webhook URL instead of the keyring one
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests

# Windows consoles default to cp1252; make our output encoding-proof regardless.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API = "https://discord.com/api/v10"

# Matches how DiscordNotifier / onboarding store the secret.
_KEYRING_SERVICE = "recommendarr"
_KEYRING_KEY = "notifications.discord.webhook_url"


# ── webhook / token resolution ────────────────────────────────────────────────

def _webhook_url_from_keyring() -> str | None:
    try:
        import keyring
        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    except Exception:
        return None


def _parse_webhook_id(url: str) -> str | None:
    # https://discord.com/api/webhooks/{webhook_id}/{token}
    parts = url.split("?", 1)[0].rstrip("/").split("/")
    try:
        i = parts.index("webhooks")
    except ValueError:
        return None
    return parts[i + 1] if i + 1 < len(parts) else None


def _fetch_channel_id(webhook_url: str) -> tuple[str | None, str | None]:
    """GET the webhook (no auth needed) → (channel_id, webhook_name)."""
    base = webhook_url.split("?", 1)[0].rstrip("/")
    try:
        r = requests.get(base, timeout=10)
        if not r.ok:
            return None, None
        obj = r.json()
        return str(obj.get("channel_id") or "") or None, obj.get("name")
    except Exception:
        return None, None


# ── rate-limit aware requests ──────────────────────────────────────────────────

def _request(method: str, url: str, headers: dict, *, json_body=None):
    """One request with up to a few 429 retries honouring Retry-After."""
    for _ in range(6):
        r = requests.request(method, url, headers=headers, json=json_body, timeout=15)
        if r.status_code != 429:
            return r
        # Discord sends retry_after (seconds) in the JSON body and/or header.
        wait = 1.0
        try:
            wait = float(r.json().get("retry_after", wait))
        except Exception:
            try:
                wait = float(r.headers.get("Retry-After", wait))
            except Exception:
                pass
        print(f"  rate-limited — waiting {wait:.1f}s")
        time.sleep(wait + 0.25)
    return r  # last response (still 429) — caller will report it


def _list_messages(channel_id: str, headers: dict):
    """Yield every message in the channel, newest→oldest, paginating by `before`."""
    before = None
    while True:
        url = f"{API}/channels/{channel_id}/messages?limit=100"
        if before:
            url += f"&before={before}"
        r = _request("GET", url, headers)
        if r.status_code == 403:
            sys.exit("ERROR: 403 Forbidden listing messages — the bot needs "
                     "'View Channel' + 'Read Message History' on this channel.")
        if r.status_code == 401:
            sys.exit("ERROR: 401 Unauthorized — DISCORD_BOT_TOKEN is invalid.")
        if not r.ok:
            sys.exit(f"ERROR: Failed to list messages: {r.status_code} {r.text[:200]}")
        batch = r.json()
        if not batch:
            return
        for m in batch:
            yield m
        before = batch[-1]["id"]


# ── main ────────────────────────────────────────────────────────────────────--

def main() -> int:
    ap = argparse.ArgumentParser(description="Purge Recommendarr posts from the Discord channel.")
    ap.add_argument("--all", action="store_true",
                    help="Delete EVERY message in the channel, not just this webhook's.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be deleted; delete nothing.")
    ap.add_argument("--channel-id", default=None, help="Override the target channel id.")
    ap.add_argument("--webhook-url", default=None, help="Override the webhook URL (else keyring).")
    args = ap.parse_args()

    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not bot_token:
        print("ERROR: Set DISCORD_BOT_TOKEN first. In PowerShell:")
        print('    $env:DISCORD_BOT_TOKEN = "<bot token>"')
        print("  The bot must be in the server with Read Message History + Manage Messages.")
        return 2

    webhook_url = (args.webhook_url or _webhook_url_from_keyring() or
                   os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()
    if not webhook_url:
        return _fail("ERROR: No webhook URL found (keyring/--webhook-url/DISCORD_WEBHOOK_URL).")

    webhook_id = _parse_webhook_id(webhook_url)
    channel_id = args.channel_id
    wh_name = None
    if not channel_id:
        channel_id, wh_name = _fetch_channel_id(webhook_url)
    if not channel_id:
        return _fail("ERROR: Could not determine channel id from the webhook. Pass --channel-id.")

    scope = "ALL messages" if args.all else f"webhook posts (id {webhook_id})"
    label = f' "{wh_name}"' if wh_name else ""
    print(f"Channel {channel_id}{label}: targeting {scope}"
          + (" — DRY RUN" if args.dry_run else ""))

    headers = {"Authorization": f"Bot {bot_token}",
               "User-Agent": "Recommendarr-purge/1.0"}

    # Collect targets first (can't delete while paginating the same listing).
    targets = []
    for m in _list_messages(channel_id, headers):
        if args.all or (webhook_id and str(m.get("webhook_id") or "") == str(webhook_id)):
            targets.append(m["id"])

    if not targets:
        print("Nothing to delete — channel already clean for that scope.")
        return 0

    print(f"Found {len(targets)} message(s) to delete.")
    if args.dry_run:
        for mid in targets:
            print(f"  would delete {mid}")
        print("Dry run — nothing deleted.")
        return 0

    deleted = failed = 0
    for mid in targets:
        r = _request("DELETE", f"{API}/channels/{channel_id}/messages/{mid}", headers)
        if r.status_code in (200, 204):
            deleted += 1
        elif r.status_code == 404:
            deleted += 1  # already gone
        else:
            failed += 1
            print(f"  ERROR: {mid}: {r.status_code} {r.text[:120]}")
        time.sleep(0.35)  # stay under the per-channel delete rate limit

    print(f"Done. Deleted {deleted}, failed {failed}.")
    return 0 if failed == 0 else 1


def _fail(msg: str) -> int:
    print(msg)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
