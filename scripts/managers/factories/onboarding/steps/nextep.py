"""
steps/nextep.py — next-episode (stay-ahead) prefetch tuning (recommended ON).
================================================================================
The Sonarr stay-ahead prefetch downloads upcoming episodes so the next one is
ready before you press play. Three tunables shape HOW FAR it walks per series —
all recommended ON and live under ``acquisition.next_episode``:

  * graduated_cap — smooth per-series episode cap that scales with episode length
    (≈6 episodes for a 45-min show, more for short ones, hard-capped at 24), instead
    of the old all-or-nothing cliff where a short-episode show grabbed the whole library.
  * recency_gate  — walk the most-recently-watched series first, and skip series with
    no watch in N days UNLESS an episode is airing soon (so a mid-season break is safe).
  * budget_ramp   — give higher-watchability series a bigger prefetch buffer and lower
    ones a smaller one (0.5×–1.5× of the base hours, neutral at the median).

None of these are secrets, so they persist to config.json in plaintext. They are
ALSO active by default in code, so this step only needs to record the user's choice;
"off" persists ``{"enabled": False}`` per feature — the canonical disable, which (unlike
a bare ``{}``) survives the onboarding schema re-merge and is offered back as the
default on a later reconfigure.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.base import Step, StepResult
from scripts.managers.machine_learning.acquisition.next_episode_planner import (
    DEFAULT_BUDGET_RAMP,
    DEFAULT_GRADUATED_CAP,
    DEFAULT_RECENCY_GATE,
)

# ── Detailed, charted guidance (ASCII-only so it renders on any console). Shown once
# before the recommended/customize/off choice, in every mode (incl. headless setup
# logs), so the operator sees exactly what each setting does to SEARCHES.
_ADVICE_INTRO = (
    "\n   -- What each setting does, and how it changes what gets searched --\n"
    "   Every episode the prefetch flags is monitored and gets an EpisodeSearch (an\n"
    "   indexer query, then a download). So these three settings trade how far you\n"
    "   buffer ahead against how many searches/downloads run each cycle and how much\n"
    "   disk fills before you watch. Defaults are the recommended starting point."
)

_ADVICE_GRADUATED = (
    "\n   [1/3] GRADUATED CAP  --  max episodes to prefetch per series, by episode length\n"
    "         (short shows are small files, so they buffer deeper; long shows stay low):\n"
    "\n"
    "           episode length | max episodes prefetched\n"
    "             >= 45 min     | ######                    6   <- base_cap\n"
    "                30 min     | #########                 9\n"
    "                22 min     | ############             12\n"
    "                15 min     | ##################       18\n"
    "             <= 11 min     | ######################## 24   <- hard_cap\n"
    "\n"
    "         The per-series RUNTIME budget (setting 3) also caps total hours grabbed --\n"
    "         whichever limit is SMALLER wins. Turn this OFF for the old behaviour: a\n"
    "         flat 6 for normal shows but UNLIMITED for short ones (can grab a whole\n"
    "         season at once).\n"
    "         Expect: higher base_cap / hard_cap = more EpisodeSearches per short series."
)

_ADVICE_RECENCY = (
    "\n   [2/3] RECENCY GATE  --  which series are worth searching (most-recent first):\n"
    "\n"
    "           each watched series\n"
    "                 |\n"
    "           watched <= cold_days ago? --yes--> PREFETCH (search the next episodes)\n"
    "                 | no\n"
    "                 v\n"
    "           an episode airing soon?    --yes--> PREFETCH (paused for a mid-season break)\n"
    "                 | no\n"
    "                 v\n"
    "           COLD  ->  skip, no searches this run\n"
    "\n"
    "         Abandoned shows stop consuming search + disk budget, and the series you\n"
    "         actually watch are served first when a run is time-limited. Turn this OFF\n"
    "         to walk EVERY watched series in library order (more searches, slower).\n"
    "         Expect: a larger cold_days keeps more shows warm = more searches."
)

_ADVICE_BUDGET = (
    "\n   [3/3] BUDGET RAMP  --  prefetch HOURS per series, by watchability (base ~3h):\n"
    "\n"
    "           watchability   | prefetch budget\n"
    "             100 (top)    | ############### 4.50h   <- high_mult (1.5x)\n"
    "              75          | ############    3.75h\n"
    "              50 (median) | ##########      3.00h   <- neutral (1.0x)\n"
    "              25          | #######         2.25h\n"
    "               0 (low)    | #####           1.50h   <- low_mult (0.5x)\n"
    "\n"
    "         Your most-watched shows buffer further ahead; rarely-watched ones less.\n"
    "         At ~45 min/episode, 3h is about 4 episodes (more for shorter shows). Turn\n"
    "         this OFF to give every series the same flat budget.\n"
    "         Expect: a wider spread (lower low_mult / higher high_mult) concentrates\n"
    "         searches on your favourites."
)


class NextEpisodeStep(Step):
    name = "next_episode"
    title = "Next-episode prefetch tuning"
    optional = True

    @staticmethod
    def _feature_off(d) -> bool:
        """A feature's persisted block reads as OFF unless ``enabled`` is truthy —
        mirroring the runtime guard (`if not (cfg and cfg.get("enabled"))`) so a block
        without a truthy enabled (absent/{}/{enabled:False}/enabled-less) classifies OFF."""
        return not (d and d.get("enabled"))

    def run(self, prompter, cfg, ctx):
        prompter.section("Next-episode prefetch tuning")
        block = cfg.setdefault("acquisition", {}).setdefault("next_episode", {})

        # Detailed, charted guidance — shown ONCE before the choice, in EVERY mode
        # (and in headless/Docker setup logs), so nobody misses what's active. ASCII-only
        # so it renders on any console. "recommended" keeps these defaults; "off" disables.
        prompter.notice(_ADVICE_INTRO)
        prompter.notice(_ADVICE_GRADUATED)
        prompter.notice(_ADVICE_RECENCY)
        prompter.notice(_ADVICE_BUDGET)
        prompter.notice(
            "\n   recommended = the three defaults above. customize = tune each. off = legacy.\n"
            "   Any value is editable later in config.json under acquisition.next_episode."
        )

        # Default to the current persisted choice so a reconfigure (interactive Enter
        # or headless with no override) preserves a prior "off" instead of silently
        # flipping it back on. Only an EXPLICIT all-disabled config defaults to "off";
        # an absent/partial block defaults to "recommended" (the active-by-default
        # contract — absent ≠ disabled). ``mode`` is never written to cfg.
        cur_off = all(k in block and self._feature_off(block[k])
                      for k in ("graduated_cap", "recency_gate", "budget_ramp"))
        mode = prompter.choice(
            "acquisition.next_episode.mode",
            "How should next-episode prefetch be tuned?",
            options=["recommended", "customize", "off"],
            default="off" if cur_off else "recommended",
        )

        if mode == "off":
            # Canonical disable: {"enabled": False} short-circuits each feature's runtime
            # path (graduated → cliff, recency → never skip, ramp → 1.0×) AND survives the
            # onboarding schema re-merge (a bare {} would be clobbered back to ON).
            block["graduated_cap"] = {"enabled": False}
            block["recency_gate"] = {"enabled": False}
            block["budget_ramp"] = {"enabled": False}
            return [StepResult("next_episode", ok=None, detail="off (legacy behaviour)", skipped=True)]

        grad = dict(DEFAULT_GRADUATED_CAP)
        rec  = dict(DEFAULT_RECENCY_GATE)
        ramp = dict(DEFAULT_BUDGET_RAMP)

        if mode == "customize":
            _cur_grad = block.get("graduated_cap") or grad
            if prompter.confirm(
                "acquisition.next_episode.graduated_cap.enabled",
                "Enable graduated episode cap (smooth cap by episode length)?",
                default=bool(_cur_grad.get("enabled", True)),
            ):
                grad["base_cap"] = prompter.integer(
                    "acquisition.next_episode.graduated_cap.base_cap",
                    "Episodes to prefetch for a normal-length (45-min) series",
                    default=int(_cur_grad.get("base_cap", grad["base_cap"])),
                )
                grad["hard_cap"] = prompter.integer(
                    "acquisition.next_episode.graduated_cap.hard_cap",
                    "Hard ceiling on episodes for very short series",
                    default=int(_cur_grad.get("hard_cap", grad["hard_cap"])),
                )
            else:
                grad = {"enabled": False}

            _cur_rec = block.get("recency_gate") or rec
            if prompter.confirm(
                "acquisition.next_episode.recency_gate.enabled",
                "Enable recency gate (walk hottest first, skip cold series)?",
                default=bool(_cur_rec.get("enabled", True)),
            ):
                rec["cold_days"] = prompter.integer(
                    "acquisition.next_episode.recency_gate.cold_days",
                    "Days without a watch before a series is considered cold",
                    default=int(_cur_rec.get("cold_days", rec["cold_days"])),
                )
            else:
                rec = {"enabled": False}

            _cur_ramp = block.get("budget_ramp") or ramp
            if not prompter.confirm(
                "acquisition.next_episode.budget_ramp.enabled",
                "Enable budget ramp (bigger buffer for most-watched series)?",
                default=bool(_cur_ramp.get("enabled", True)),
            ):
                ramp = {"enabled": False}

        block["graduated_cap"] = grad
        block["recency_gate"] = rec
        block["budget_ramp"] = ramp

        detail = "recommended ON" if mode == "recommended" else "customized"
        prompter.success(f"   Next-episode prefetch: {detail}")
        return [StepResult("next_episode", ok=True, detail=detail)]
