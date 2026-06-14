"""
steps/library.py — root folders, genres, free-space limit, dry-run.
================================================================================
Runs after Sonarr/Radarr so it can offer the root folders discovered from those
instances (stashed in ``ctx['root_folders']``) as choices.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.base import Step, StepResult, csv_field

# Genre tags commonly applied (TVDB/Trakt/*arr) that pair with each category.
# Suggestions are shown to the user; the *_DEFAULT seeds the editable default so
# Enter accepts a sensible set. These match against a title's genre tags, so only
# values your library actually uses will ever map.
_DOC_SUGGESTIONS = ["documentary", "biography", "history", "nature", "reality",
                    "crime", "war", "science", "music", "sport", "news", "travel"]
_DOC_DEFAULT = ["documentary", "reality", "nature", "history", "biography"]
_ANIME_SUGGESTIONS = ["anime", "animation"]
_ANIME_DEFAULT = ["anime"]


class LibraryStep(Step):
    name = "library"
    title = "Library & general"

    def run(self, prompter, cfg, ctx):
        prompter.section("Library & general")

        cfg["dry_run"] = prompter.confirm(
            "dry_run", "Dry run (plan only — make no changes to your services)?",
            default=bool(cfg.get("dry_run", True)))

        cfg["free_space_limit"] = prompter.integer(
            "free_space_limit", "Minimum free space to keep per disk (GB)",
            default=int(cfg.get("free_space_limit") or 0))

        discovered = sorted({p for p in ctx.get("root_folders", []) if p})
        rf = cfg.setdefault("rootFolders", {})
        for key, label in (("series", "Series"), ("anime", "Anime"), ("documentary", "Documentary")):
            cur = rf.get(key, "")
            if prompter.is_interactive and discovered:
                rf[key] = prompter.choice(
                    f"rootFolders.{key}", f"{label} root folder",
                    options=discovered, default=cur or discovered[0])
            else:
                rf[key] = prompter.text(
                    f"rootFolders.{key}", f"{label} root folder path",
                    default=cur, required=False)

        cfg["animeGenres"] = csv_field(
            prompter, "animeGenres", "Anime genres (comma-separated, must include 'anime')",
            cfg.get("animeGenres") or [], suggestions=_ANIME_SUGGESTIONS, fallback=_ANIME_DEFAULT,
            require="anime")
        cfg["documentaryGenres"] = csv_field(
            prompter, "documentaryGenres", "Documentary genres (comma-separated, must include 'documentary')",
            cfg.get("documentaryGenres") or [], suggestions=_DOC_SUGGESTIONS, fallback=_DOC_DEFAULT,
            require="documentary")

        set_roots = sum(1 for v in rf.values() if v)
        return [StepResult("library", ok=True, detail=f"root folders {set_roots}/3, dry_run={cfg['dry_run']}")]
