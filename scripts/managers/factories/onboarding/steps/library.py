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
#
# Documentary is kept DELIBERATELY TIGHT: a title only routes to the Documentaries
# library if it carries the 'documentary' genre. Broad story genres (crime, war,
# history, sport, …) are NOT documentary tags — including them sweeps scripted crime
# / war / period dramas (FBI, The Sopranos, Masters of the Air) into Documentaries.
# 'reality' is its OWN bucket below — never fold it into documentary.
_DOC_SUGGESTIONS = ["documentary", "biography", "nature"]
_DOC_DEFAULT = ["documentary"]
_REALITY_SUGGESTIONS = ["reality", "reality-tv", "game show", "talk show"]
_REALITY_DEFAULT = ["reality"]
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
        for key, label in (("series", "Series"), ("anime", "Anime"),
                           ("documentary", "Documentary"), ("reality", "Reality")):
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
        cfg["realityGenres"] = csv_field(
            prompter, "realityGenres", "Reality genres (comma-separated, must include 'reality')",
            cfg.get("realityGenres") or [], suggestions=_REALITY_SUGGESTIONS, fallback=_REALITY_DEFAULT,
            require="reality")

        set_roots = sum(1 for v in rf.values() if v)
        return [StepResult("library", ok=True, detail=f"root folders {set_roots}/4, dry_run={cfg['dry_run']}")]
