"""
DiscordNotifier
===============
Sends a structured run-summary embed to a Discord webhook after each
Glidearr run.

Config (config.json → notifications.discord):
    webhook_url      — Discord webhook URL (required to send)
    enabled          — true/false toggle (default false)
    username         — Bot display name   (default "Glidearr")
    avatar_url       — Optional bot avatar URL
    color_success    — Embed colour when run is clean  (decimal RGB, default green)
    color_warning    — Embed colour for dry-run / warnings
    color_error      — Embed colour when errors occurred
    replace_previous — Delete the previous run-summary message on the next run so
                       only the latest summary remains in the channel (default true)

Replace-previous behaviour
--------------------------
When ``replace_previous`` is on, ``send_run_summary`` posts the new embed with
``?wait=true`` (so Discord returns the created message id), records that id under
a hash of the webhook URL in ``support/cache/notifications/discord_state.json``,
then deletes the message id stored from the previous run. One-off ``send_alert``
messages are never tracked or deleted.

Obtaining a webhook URL
-----------------------
1. Open Discord → Server Settings → Integrations → Webhooks → New Webhook
2. Choose a channel, copy the URL, paste into config.json.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Where the last-posted run-summary message id is remembered between runs, so the
# next run can delete it. Sibling of the Trakt cache buckets under support/cache/.
# discord.py → parents[1] == scripts/support/
_STATE_DIR  = Path(__file__).resolve().parents[1] / "cache" / "notifications"
_STATE_PATH = _STATE_DIR / "discord_state.json"

# Defense-in-depth: strip internal network topology (full URLs, bare host:port
# / IP[:port]) from any error text before it is embedded and POSTed to Discord.
_URL_RE       = re.compile(r'https?://\S+')
_HOST_PORT_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?')


class DiscordNotifier:

    def __init__(self, config: dict | None = None, logger=None):
        self.logger = logger
        raw = config or {}
        # Accept both a raw config dict and a ConfigManager wrapper
        if hasattr(raw, "get"):
            discord_cfg = (raw.get("notifications") or {}).get("discord") or {}
        else:
            discord_cfg = {}

        self.enabled     = bool(discord_cfg.get("enabled",       False))
        self.webhook_url = str( discord_cfg.get("webhook_url",   "") or "").strip()
        self.username    = str( discord_cfg.get("username",       "Glidearr"))
        self.avatar_url  = str( discord_cfg.get("avatar_url",    "") or "")
        self.c_success   = int( discord_cfg.get("color_success",  3066993))   # green
        self.c_warning   = int( discord_cfg.get("color_warning", 16776960))   # yellow
        self.c_error     = int( discord_cfg.get("color_error",   15158332))   # red
        # When on, each run deletes the previous run-summary message so only the
        # latest one remains in the channel.
        self.replace_previous = bool(discord_cfg.get("replace_previous", True))

    # ── Public ──────────────────────────────────────────────────────────────

    def send_run_summary(self, summary: dict) -> bool:
        """Post a full run-summary embed.  Returns True on success.

        With ``replace_previous`` on, posts with ``?wait=true`` to learn the new
        message id, deletes the message recorded by the previous run, then
        records the new id for the next run to delete.
        """
        if not self._active():
            return False
        try:
            embed = self._build_run_embed(summary)
            if not self.replace_previous:
                return self._post({"embeds": [embed]}) is not None

            resp = self._post({"embeds": [embed]}, wait=True)
            if resp is None:
                return False
            new_id = self._extract_message_id(resp)
            old_id = self._load_message_id()
            if old_id and old_id != new_id:
                self._delete_message(old_id)
            self._save_message_id(new_id)
            return True
        except Exception as e:
            self._log(f"[Discord] send_run_summary failed: {e}")
            return False

    def send_alert(self, title: str, message: str,
                   level: str = "warning") -> bool:
        """Post a one-off alert embed."""
        if not self._active():
            return False
        colour = {"success": self.c_success,
                  "warning": self.c_warning,
                  "error":   self.c_error}.get(level, self.c_warning)
        embed = {
            "title":       title,
            "description": _scrub_egress(message)[:4096],
            "color":       colour,
            "timestamp":   _now_iso(),
        }
        try:
            return self._post({"embeds": [embed]}) is not None
        except Exception as e:
            self._log(f"[Discord] send_alert failed: {e}")
            return False

    # ── Embed construction ───────────────────────────────────────────────────

    def _build_run_embed(self, s: dict) -> dict:
        """
        Build a rich Discord embed from a RunSummaryCollector dict.

        Expected top-level keys (all optional):
            run_duration_s, dry_run, errors,
            radarr, sonarr, tautulli, trakt
        """
        dry_run  = bool(s.get("dry_run"))
        duration = float(s.get("run_duration_s") or 0)
        errors   = list(s.get("errors") or [])

        colour = self.c_error   if errors  else (
                 self.c_warning if dry_run  else self.c_success)

        title = ("🧪 DRY RUN — " if dry_run else "") + "Glidearr Run Complete"
        desc  = (f"⏱️ `{_fmt_dur(duration)}`"
                 + ("  •  ❌ errors" if errors else "  •  ✅ clean"))

        fields: list[dict] = []

        # ── Radarr ──────────────────────────────────────────────────────────
        rad = s.get("radarr") or {}
        rf  = _section([
            _row("📈 Upgraded",       rad.get("movies_upgraded")),
            _row("📉 Downgraded",     rad.get("movies_downgraded")),
            _row("🔍 Searched",       rad.get("movies_searched")),
            _row("🔕 Unmonitored",    rad.get("movies_unmonitored")),
            _row("🗑️ Queue cleared", rad.get("queue_cancelled")),
            _row("💾 Space freed",    _gb(rad.get("space_freed_gb"))),
            _row("⚠️ Errors",        rad.get("errors")),
        ])
        if rf:
            fields.append({"name": "🎬 Radarr", "value": rf, "inline": True})

        # ── Sonarr ──────────────────────────────────────────────────────────
        son = s.get("sonarr") or {}
        sf  = _section([
            _row("📥 Episodes acquired",  son.get("episodes_acquired")),
            _row("⬆️ JIT upgraded",      son.get("episodes_jit_upgraded")),
            _row("⬇️ JIT restored",      son.get("episodes_jit_restored")),
            _row("📈 Series upgraded",    son.get("series_upgraded")),
            _row("🗑️ Queue cleared",     son.get("queue_cancelled")),
            _row("⚠️ Errors",           son.get("errors")),
        ])
        if sf:
            fields.append({"name": "📺 Sonarr", "value": sf, "inline": True})

        # ── Data sources ────────────────────────────────────────────────────
        tau = s.get("tautulli") or {}
        trk = s.get("trakt")    or {}
        df  = _section([
            _row("📊 History entries",   tau.get("history_entries")),
            _row("⭐ Trakt ratings",      trk.get("ratings_added")),
            _row("🎭 Metadata indexed",   tau.get("metadata_indexed")),
            _row("👤 Users tracked",      tau.get("users_tracked")),
        ])
        if df:
            fields.append({"name": "📡 Data", "value": df, "inline": True})

        # ── Errors ──────────────────────────────────────────────────────────
        if errors:
            err_lines = "\n".join(f"• {_scrub_egress(e)}" for e in errors[:8])
            if len(errors) > 8:
                err_lines += f"\n…and {len(errors) - 8} more"
            fields.append({
                "name":   "❌ Errors",
                "value":  f"```\n{err_lines[:1000]}\n```",
                "inline": False,
            })

        return {
            "title":       title,
            "description": desc,
            "color":       colour,
            "fields":      fields,
            "footer":      {"text": "Glidearr"},
            "timestamp":   _now_iso(),
        }

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _post(self, payload: dict, *, wait: bool = False):
        """POST an embed payload to the webhook.

        Returns the ``requests.Response`` on success (so the caller can read the
        created-message id when *wait* is set), or ``None`` on failure. ``wait``
        adds ``?wait=true`` so Discord returns the created message body instead of
        a bare ``204``.
        """
        body: dict[str, Any] = {"embeds": payload.get("embeds", [])}
        if self.username:
            body["username"] = self.username
        if self.avatar_url:
            body["avatar_url"] = self.avatar_url

        params = {"wait": "true"} if wait else None
        resp = self._send("POST", self.webhook_url, params=params, json_body=body)
        if resp is None or not resp.ok:
            if resp is not None:
                self._log(f"[Discord] POST {resp.status_code}: {resp.text[:300]}")
            return None
        return resp

    def _delete_message(self, message_id: str) -> None:
        """Delete a previously-posted run-summary message (best effort)."""
        base = self.webhook_url.split("?", 1)[0].rstrip("/")
        resp = self._send("DELETE", f"{base}/messages/{message_id}")
        # 404 = already gone (deleted manually / expired) — nothing to do.
        if resp is not None and not resp.ok and resp.status_code != 404:
            self._log(f"[Discord] delete previous message {resp.status_code}: "
                      f"{resp.text[:200]}")

    def _send(self, method: str, url: str, *,
              params: dict | None = None, json_body: dict | None = None):
        """Issue one request with a single 429 retry. Returns Response or None."""
        kwargs: dict[str, Any] = {"timeout": 10}
        if params:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["data"] = json.dumps(json_body)
            kwargs["headers"] = {"Content-Type": "application/json"}
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 5))
                self._log(f"[Discord] Rate-limited — retrying in {wait}s")
                time.sleep(wait)
                resp = requests.request(method, url, **kwargs)
            return resp
        except Exception as e:
            self._log(f"[Discord] {method} failed: {_scrub_egress(e)}")
            return None

    # ── Last-message state (for replace_previous) ─────────────────────────────

    @staticmethod
    def _extract_message_id(resp) -> str | None:
        try:
            return str((resp.json() or {}).get("id") or "") or None
        except Exception:
            return None

    def _webhook_hash(self) -> str:
        return hashlib.sha256(self.webhook_url.encode("utf-8")).hexdigest()[:16]

    def _load_message_id(self) -> str | None:
        """Return the message id recorded last run, iff it belongs to THIS
        webhook (guards against deleting a message in a since-changed channel)."""
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if data.get("webhook_hash") == self._webhook_hash():
                return str(data.get("message_id") or "") or None
        except Exception:
            pass
        return None

    def _save_message_id(self, message_id: str | None) -> None:
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _STATE_PATH.write_text(
                json.dumps({"webhook_hash": self._webhook_hash(),
                            "message_id": message_id or ""}),
                encoding="utf-8",
            )
        except Exception as e:
            self._log(f"[Discord] could not persist last message id: {e}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _active(self) -> bool:
        if not self.enabled:
            return False
        if not self.webhook_url:
            self._log("[Discord] webhook_url not configured — notifications disabled")
            return False
        return True

    def _log(self, msg: str):
        if self.logger:
            self.logger.log_warning(msg)
        else:
            print(msg)


# ── Module-level helpers ─────────────────────────────────────────────────────

def _scrub_egress(text) -> str:
    """Strip URLs and bare host:port/IP topology from a string before egress."""
    s = _URL_RE.sub("<redacted-url>", str(text))
    return _HOST_PORT_RE.sub("<redacted-host>", s)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _gb(val) -> str | None:
    if val is None:
        return None
    try:
        return f"{float(val):.1f} GB"
    except (TypeError, ValueError):
        return None


def _row(label: str, value) -> str | None:
    """Return a formatted field line, or None if value is falsy."""
    if not value and value != 0:
        return None
    return f"{label}: **{value}**"


def _section(rows: list[str | None]) -> str:
    """Join non-None rows into a field value string."""
    return "\n".join(r for r in rows if r) or ""
