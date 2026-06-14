"""
onboarding.py - guided first-run / full setup for Glidearr.

    python scripts/support/setup/onboarding.py                # interactive wizard (auto-detects TTY)
    python scripts/support/setup/onboarding.py --headless     # env-driven (CI / Docker / unraid)
    python scripts/support/setup/onboarding.py --reconfigure  # re-run every step, pre-filled
    python scripts/support/setup/onboarding.py --service trakt   # re-run just one step
    python scripts/support/setup/onboarding.py --config /tmp/x.json   # target a different config
    python scripts/support/setup/onboarding.py --print-env-template > .env.example

Collects root folders, the number of Sonarr/Radarr sessions + API keys, Trakt
OAuth (incl. token generation), Tautulli, Plex, TVDB, MAL, genres and Discord
notifications. Each service is validated live (warn-and-continue). Secrets are
stored in the OS keyring; config.json secret fields are left blank.
"""
import argparse
import os
import sys
from pathlib import Path

# UTF-8 safe console (never crash on cp1252), honours NO_COLOR / non-TTY.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root (file: scripts/support/*/<name>.py)
sys.path.insert(0, str(REPO_ROOT))

from scripts.managers.factories.onboarding import OnboardingManager      # noqa: E402
from scripts.managers.factories.onboarding import env_map                # noqa: E402
from scripts.support.utilities.logger.logger import LoggerManager        # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Glidearr onboarding / setup wizard.")
    parser.add_argument("--headless", action="store_true",
                        help="Env-driven, no prompts (CI / Docker / unraid).")
    parser.add_argument("--interactive", action="store_true",
                        help="Force the interactive wizard even without a TTY.")
    parser.add_argument("--reconfigure", action="store_true",
                        help="Re-run every step with current values pre-filled.")
    parser.add_argument("--service", metavar="NAME",
                        help="Run a single step only (sonarr, radarr, library, trakt, "
                             "tautulli, plex, tvdb, mal, mdblist, notifications).")
    parser.add_argument("--config", metavar="PATH",
                        help="Path to the config.json to write (defaults to the app config).")
    parser.add_argument("--print-env-template", action="store_true",
                        help="Print a .env.example for headless/Docker and exit.")
    parser.add_argument("--print-env-markdown", action="store_true",
                        help="Print the env-var contract as a markdown table and exit.")
    args = parser.parse_args()

    if args.print_env_template:
        print(env_map.generate_env_example())
        return 0
    if args.print_env_markdown:
        print(env_map.generate_markdown_table())
        return 0

    if args.headless and args.interactive:
        print("Choose either --headless or --interactive, not both.", file=sys.stderr)
        return 2

    mode = "headless" if args.headless else "interactive" if args.interactive else None
    logger = LoggerManager()

    try:
        mgr = OnboardingManager(
            logger=logger,
            config_path=args.config,
            mode=mode,
            reconfigure=args.reconfigure,
            only_service=args.service,
        )
        ok = mgr.run()
        return 0 if ok else 1
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
