"""
mal_list.py — reflect household watch progress back to the user's MAL list.
================================================================================
Builds per-show watched-episode counts from Tautulli history (matched to the MAL
list by normalized title — best-effort) and PATCHes ``my_list_status`` where the
watched count exceeds what MAL records: status → completed when all episodes are
watched, else watching. Logs matched/unmatched. dry_run-gated.
"""
from __future__ import annotations

from scripts.managers.services.writeback._util import fetch_history, norm_title


class MalListSync:
    def __init__(self, mal, tautulli, global_cache, config, logger, dry_run: bool):
        self.mal = mal
        self.tautulli = tautulli
        self.gc = global_cache
        self.config = config
        self.logger = logger
        self.dry_run = dry_run

    def run(self) -> dict:
        if not self.mal or not getattr(self.mal, "enabled", False):
            self.logger.log_debug("[writeback] MAL not enabled — skipping list sync.")
            return {"ok": False}
        tau = getattr(self.tautulli, "api", None)
        if not tau:
            return {"ok": False}

        watched = self._watched_counts(tau)        # norm_title -> distinct episode count
        user = (self.config.get("mal", {}) or {}).get("username", "default")
        animelist = (self.gc.get(f"mal/{user}/animelist") if self.gc else None) \
            or self.mal.mal_api.get_anime_list() or []

        updated = would = matched = 0
        for item in animelist:
            node = item.get("node", {}) or {}
            ls = item.get("list_status", {}) or {}
            wc = watched.get(norm_title(node.get("title")))
            if not wc:
                continue
            matched += 1
            current = ls.get("num_episodes_watched") or 0
            if wc <= current:
                continue
            total = node.get("num_episodes") or 0
            new_status = "completed" if (total and wc >= total) else "watching"
            capped = min(wc, total) if total else wc
            if self.dry_run:
                self.logger.log_info(
                    f"[writeback] dry_run — would set MAL '{node.get('title')}' → "
                    f"{new_status} ({capped} eps).")
                would += 1
                continue
            self.mal.mal_api.update_list_status(
                node.get("id"), status=new_status, num_watched_episodes=capped)
            updated += 1

        self.logger.log_info(
            f"[writeback] MAL list: {matched} matched, "
            f"{would if self.dry_run else updated} {'would-update' if self.dry_run else 'updated'}.")
        return {"ok": True, "matched": matched, "updated": updated, "would": would}

    def _watched_counts(self, tau) -> dict:
        counts: dict = {}
        seen: dict = {}  # title -> set of (season, ep)
        wb = (self.config.get("trakt_writeback", {}) if self.config else {}) or {}
        for e in fetch_history(tau, self.logger, max_pages=int(wb.get("history_max_pages", 20))):
            if e.get("media_type") != "episode":
                continue
            pct = e.get("percent_complete") or 0
            if e.get("watched_status") != 1 and pct < 85:
                continue
            title = norm_title(e.get("grandparent_title"))
            s, ep = e.get("parent_media_index"), e.get("media_index")
            if not title or not str(ep).isdigit():
                continue
            seen.setdefault(title, set()).add((str(s), str(ep)))
        for title, eps in seen.items():
            counts[title] = len(eps)
        return counts
