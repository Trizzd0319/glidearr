"""
steps/english_dub.py -- English-audio (dub) prioritization for Radarr (opt-in).
================================================================================
For viewers who want foreign-language films in an English DUB. Radarr ranks QUALITY
before language, so by default a high-quality original-language release always beats a
lower-quality English one -- a foreign film silently lands in its original language. This
feature layers an English preference on top WITHOUT ever leaving you empty-handed.

Five pieces, each independently toggleable (all recommended ON), recorded under
``english_dub`` in config.json (plaintext -- none are secrets). The persisted block is the
spec the Radarr English-dub setup reads; the per-piece live changes (custom formats, the
English profile ladder, film enrollment) are applied by the matching setup tool under
``scripts/support/tools/`` (commands shown below), since those touch your specific live
instances.

"off" persists ``{"enabled": False}`` per piece -- the canonical disable that survives the
onboarding schema re-merge (unlike a bare ``{}``) and is offered back as the default on a
later reconfigure.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.base import Step, StepResult

# The five pieces (key, default-on). Order = the order they should be applied.
_PIECES = [
    ("cf_scoring",      True),
    ("theatrical_seek", True),
    ("english_ladder",  True),
    ("lock_owned_dubs", True),
    ("auto_enroll",     True),
]

# ── Detailed, ASCII-only guidance (renders on any console / headless setup logs) ──
_ADVICE_INTRO = (
    "\n   -- English-audio (dub) prioritization: what it does --\n"
    "   Radarr compares QUALITY tier first, custom-format score only as a tiebreaker\n"
    "   WITHIN a tier. So a foreign film's high-quality original release out-ranks any\n"
    "   lower-quality English one, and you end up with the original language. These five\n"
    "   pieces make English win WHERE IT CAN without ever blocking a grab. For English-\n"
    "   origin films nothing changes (English == original, the bonus is uniform)."
)

_ADVICE_CF = (
    "\n   [1/5] CUSTOM-FORMAT SCORING  --  prefer an English track on every profile:\n"
    "         + adds 'English Audio' (+1500) and 'Dual Audio' (+500, stacks -> 2000) CFs\n"
    "           to every quality profile, so within a tier:\n"
    "             dual-audio (original + English)  >  English dub  >  original-only\n"
    "         + relaxes hard language gates (profile language -> Any) so foreign titles\n"
    "           are never silently rejected.\n"
    "         + zeroes the -10000 hard blockers (Language: Not Original / Dubs Only / VOSTFR)\n"
    "           that otherwise reject English dubs of foreign content.\n"
    "         Net: 'dub if it exists, else original' -- never empty-handed.\n"
    "         Apply: python scripts/support/tools/_audio_lang_apply.py --apply"
)

_ADVICE_THEATRICAL = (
    "\n   [2/5] THEATRICAL SEEK PROFILE  --  English even when only a cam exists:\n"
    "         + creates 'English - Theatrical OK': English REQUIRED, with the TELESYNC tier\n"
    "           elevated above low-res, cams allowed. For theatrical-only titles whose only\n"
    "           English release is a cam (no home release yet).\n"
    "         + upgrades ON, so it auto-replaces the cam with a real English Blu-ray/WEB\n"
    "           the moment one exists. You assign it per-title (e.g. a just-released movie).\n"
    "         Apply: python scripts/support/tools/_english_theatrical_profile.py --apply"
)

_ADVICE_LADDER = (
    "\n   [3/5] ENGLISH PROFILE LADDER  --  keep climbing quality WITHOUT losing English:\n"
    "         + clones every watchability quality tier into an English-REQUIRED twin and\n"
    "           records the parallel ladder in config 'radarr_quality_ladder_english'.\n"
    "         + the upgrader then promotes an English-locked film up the ENGLISH ladder\n"
    "           (720p -> 1080p -> Remux/4K) instead of dropping it onto a mixed-language\n"
    "           tier -- so a higher-quality original release can NEVER overwrite the dub.\n"
    "         This is the one piece that is also live in code (watch_likelihood reads the\n"
    "         english ladder; no twin ids configured == off == normal ladder, byte-identical).\n"
    "         Apply: python scripts/support/tools/_english_tier_ladder.py --apply"
)

_ADVICE_LOCK = (
    "\n   [4/5] LOCK OWNED DUBS  --  protect English files you already have:\n"
    "         + moves every owned foreign film whose file Radarr tags English onto the\n"
    "           English twin of its current tier, so a later higher-quality original-\n"
    "           language release can't replace your dub. Pure protection -- no downloads\n"
    "           (the language gate is already satisfied, so nothing is re-grabbed).\n"
    "         Apply: python scripts/support/tools/_english_lock_owned_dubs.py --apply"
)

_ADVICE_ENROLL = (
    "\n   [5/5] AUTO-ENROLL ALL FOREIGN FILMS  --  one-shot, whole library:\n"
    "         + assigns EVERY non-English-origin film to an English profile (quality-profile\n"
    "           change only; monitored status, tags and files are untouched).\n"
    "             - monitored films  -> a CAM-FREE English profile (seek a real English\n"
    "               release; never a cam; existing file kept until a real one imports).\n"
    "             - unmonitored films -> English policy only, ZERO downloads (Radarr never\n"
    "               searches unmonitored movies).\n"
    "         Reversible: the tool snapshots every prior profile (run it with --revert).\n"
    "         Apply: python scripts/support/tools/_english_autoenroll.py --apply"
)


class EnglishDubStep(Step):
    name = "english_dub"
    title = "English-audio (dub) prioritization"
    optional = True

    @staticmethod
    def _feature_off(d) -> bool:
        """A piece's persisted block reads OFF unless ``enabled`` is truthy (mirrors the
        active-by-default contract: absent/{}/{enabled:False} all classify OFF)."""
        return not (d and d.get("enabled"))

    def run(self, prompter, cfg, ctx):
        prompter.section("English-audio (dub) prioritization")
        block = cfg.setdefault("english_dub", {})

        for advice in (_ADVICE_INTRO, _ADVICE_CF, _ADVICE_THEATRICAL,
                       _ADVICE_LADDER, _ADVICE_LOCK, _ADVICE_ENROLL):
            prompter.notice(advice)
        prompter.notice(
            "\n   recommended = all five ON. customize = pick per piece. off = none.\n"
            "   Editable later in config.json under english_dub; live setup is applied by the\n"
            "   tools above (each has a --apply dry-run/apply, and is safe to re-run)."
        )

        # Default to the current persisted choice so a reconfigure preserves a prior
        # "off"; an absent/partial block defaults to "recommended" (absent != disabled).
        cur_off = all(k in block and self._feature_off(block[k]) for k, _ in _PIECES)
        mode = prompter.choice(
            "english_dub.mode",
            "How should English-dub prioritization be set?",
            options=["recommended", "customize", "off"],
            default="off" if cur_off else "recommended",
        )

        if mode == "off":
            for key, _ in _PIECES:
                block[key] = {"enabled": False}
            return [StepResult("english_dub", ok=None, detail="off", skipped=True)]

        enabled_count = 0
        for key, default_on in _PIECES:
            if mode == "customize":
                cur = block.get(key) or {}
                on = prompter.confirm(
                    f"english_dub.{key}.enabled",
                    f"Enable '{key.replace('_', ' ')}'?",
                    default=bool(cur.get("enabled", default_on)),
                )
            else:
                on = default_on
            block[key] = {"enabled": bool(on)}
            enabled_count += int(bool(on))

        detail = "recommended (5/5 ON)" if mode == "recommended" else f"customized ({enabled_count}/5 ON)"
        prompter.success(f"   English-dub prioritization: {detail}")
        if enabled_count:
            prompter.notice(
                "   To apply the enabled pieces to your live Radarr now, run the matching\n"
                "   --apply command(s) listed above (start with _audio_lang_apply.py)."
            )
        return [StepResult("english_dub", ok=True, detail=detail)]
