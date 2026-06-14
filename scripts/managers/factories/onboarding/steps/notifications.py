"""
steps/notifications.py — Discord notifications (optional).
================================================================================
``notifications.discord.webhook_url`` is a secret (matched by the "webhook"
substring in is_secret_key), so it is stored in the keyring like any other.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding import schema
from scripts.managers.factories.onboarding.steps.base import Step, StepResult, should_configure


class NotificationsStep(Step):
    name = "notifications"
    title = "Discord notifications"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("Discord notifications")
        notif = cfg.setdefault("notifications", {}).setdefault("discord", dict(schema._DISCORD_DEFAULTS))
        if not should_configure(prompter, "notifications.discord", "Discord notifications",
                                default_on=bool(notif.get("webhook_url") or notif.get("enabled")),
                                probe_path="notifications.discord.webhook_url"):
            return [StepResult("discord", ok=None, detail="skipped", skipped=True)]

        webhook = prompter.secret("notifications.discord.webhook_url", "Discord webhook URL",
                                  default=notif.get("webhook_url", ""), required=True)
        notif["webhook_url"] = webhook
        notif["enabled"] = prompter.confirm("notifications.discord.enabled",
                                            "Enable Discord notifications?",
                                            default=bool(notif.get("enabled")) or bool(webhook))
        notif["username"] = prompter.text("notifications.discord.username", "Discord bot username",
                                          default=notif.get("username", "Glidearr"), required=False)
        detail = "enabled" if notif["enabled"] else "configured (disabled)"
        return [StepResult("discord", ok=bool(webhook), detail=detail)]
