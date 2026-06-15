"""
steps/deletions.py — informed consent for media deletion (DESTRUCTIVE; opt-in).
================================================================================
Deletion is the one irreversible thing glidearr does. It stays OFF unless the
operator (1) explicitly consents here (or via RECOMMENDARR_DELETIONS_CONSENT /
GLIDEARR_DELETIONS_CONSENT for headless / Docker) AND (2) sets a free_space_limit
floor. Without consent everything else still runs — scoring, quality-profile
changes, playlist planning/showing — and only NEW acquisition pauses once free
space reaches the floor (since space cannot be reclaimed by deleting).

``deletions_consent`` / ``free_space_limit`` are NOT secrets → plaintext config.json.
The runtime gate is machine_learning/space/space_targets.deletions_enabled (which
requires BOTH consent AND the floor).
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.base import Step, StepResult


class DeletionsStep(Step):
    name = "deletions"
    title = "Media deletion (consent)"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("Media deletion — DESTRUCTIVE, explicit opt-in")

        # Shown in every mode (incl. the headless setup log) — this is the
        # informed-consent explanation.
        prompter.notice(
            "   Glidearr curates your *arr libraries by watchability score. When free disk\n"
            "   space stays below your floor, it FIRST downgrades low-value titles to smaller\n"
            "   files; only if that is not enough can it DELETE the lowest-scoring, already-\n"
            "   watched (or grace-expired) files to reclaim space down to the floor. Deletion\n"
            "   is the one irreversible action and is OFF by default.\n"
            "   Guards: downgrades run first; franchise / universe / keep-tagged titles are\n"
            "   protected; recently-watched titles are spared; dry_run previews the full plan\n"
            "   without touching a single file.\n"
            "   If you DECLINE: glidearr still scores, changes quality profiles, and builds /\n"
            "   shows playlists — it simply never deletes, and instead PAUSES new acquisitions\n"
            "   once free space reaches your floor (it cannot make room any other way).\n"
            "   Change this anytime: deletions_consent in config.json, or the\n"
            "   RECOMMENDARR_DELETIONS_CONSENT environment variable."
        )

        # Headless (Docker/CI): informed consent needs a TTY, so never default it on
        # here — consent comes from the env var at runtime.
        if not getattr(prompter, "is_interactive", False):
            return [StepResult(
                "deletions", ok=None, skipped=True,
                detail="headless — set RECOMMENDARR_DELETIONS_CONSENT=true (+ free_space_limit) to allow deletion",
            )]

        consent = bool(prompter.confirm(
            "deletions_consent",
            "Allow glidearr to DELETE media files to keep free space above your floor?",
            default=bool(cfg.get("deletions_consent", False)),
        ))
        cfg["deletions_consent"] = consent

        # The floor governs WHEN to reclaim (with consent) and WHEN to pause
        # acquisition (without consent), so capture it either way.
        try:
            cur_floor = int(cfg.get("free_space_limit", 0) or 0)
        except (TypeError, ValueError):
            cur_floor = 0
        floor = prompter.integer(
            "free_space_limit",
            "Free-space floor in GB to keep available (0 = unset; arms deletion / acquisition pause)",
            default=cur_floor,
        )
        try:
            floor = int(floor or 0)
        except (TypeError, ValueError):
            floor = 0
        cfg["free_space_limit"] = floor

        if consent and floor > 0:
            return [StepResult("deletions", ok=True, detail=f"deletion ARMED (floor {floor} GB)")]
        if consent and floor <= 0:
            return [StepResult("deletions", ok=False,
                               detail="consented, but free_space_limit unset — deletion stays OFF until floor > 0")]
        tail = f"; acquisition pauses below {floor} GB" if floor > 0 else ""
        return [StepResult("deletions", ok=None, skipped=True,
                           detail=f"declined — plan / profiles / playlists only{tail}")]
