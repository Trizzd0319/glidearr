"""
decision_log.py — one line per acquisition candidate, and why it got that answer.
================================================================================
THE GAP THIS FILLS (`GLD-ACQS-22`). Routing, playlists and relocation each have a
dedicated log because their per-title plans are long. Acquisition has the LARGEST
candidate pool in the system -- 1,017 on 2026-08-22 -- and writes a COUNT:

    [Acquisition] 10 funded (~44 GB), 0 refused, 107 capped (hard_max_adds)

Which of the 1,007 were capped, and in what order they ranked, is nowhere. That is
the same shape as `GLD-ACQS-20` (an outcome counted but not surfaced) one level up:
that fix corrected the NUMBER, the identities are still unrecorded.

IT MATTERS MORE NOW THAN IT DID. With 5.68 TB free the space budget refuses almost
nothing, so `hard_max_adds` is the SOLE limiter -- the ranking alone decides which
ten of a thousand titles the household gets. A ranking nobody can see is a ranking
nobody can check.

WHY A SEPARATE FILE RATHER THAN MORE LINES IN default.log
---------------------------------------------------------
A thousand per-title lines would bury the run narrative an operator actually reads.
`routing.log` already established the split: the main log gets the count, the
dedicated file gets the plan. This follows it exactly, including regeneration per
run -- a stale decision log read against a fresh summary is worse than none.

Pure module: records in, rendered lines out. No I/O, no logger, stdlib only, so the
whole thing is testable without a manager.
"""
from __future__ import annotations

#: Dispositions, in the order they are reported. Ordering is presentation only, but
#: it is deliberate: what the household GOT first, what it nearly got second, what
#: was refused last.
DISPOSITIONS: tuple[str, ...] = ("funded", "would-fund", "deferred", "capped", "refused", "skipped")

#: Dispositions that mean "the budget said no", as opposed to "the run was a preview".
_DENIED = frozenset({"capped", "refused", "skipped"})


def new_log() -> dict:
    """A fresh per-run accumulator. Explicitly constructed rather than module-level
    state, so two managers in one process cannot silently share one."""
    return {"rows": [], "seq": 0}


def record(log, *, title, disposition, score=None, tier=None, gb=None,
           instance=None, reason=None, media=None) -> dict:
    """Append one candidate's outcome. Returns the log for chaining.

    EVERY field except title and disposition is optional, and that is deliberate:
    the call sites know different things. The pause check knows the instance and
    nothing about size; the budget knows GB and tier; the hard-max branch knows only
    that it ran out of room. A recorder that demanded a full row would push its call
    sites into inventing values, and an invented `score` in a ranking audit is worse
    than a blank one.
    """
    if not isinstance(log, dict) or not isinstance(log.get("rows"), list):
        return log                      # never let a diagnostic break the run
    # `seq` is cosmetic (a stable row number), so a corrupt one must degrade rather
    # than raise -- fall back to the row count, which is always available and always
    # sane. Three separate hostile-input shapes broke earlier versions of this guard
    # (`rows` missing, `rows` not a list, `seq` non-numeric); a diagnostic that can
    # crash the pass it is documenting is worse than no diagnostic.
    try:
        _seq = int(log.get("seq") or 0) + 1
    except (TypeError, ValueError):
        _seq = len(log["rows"]) + 1
    log["seq"] = _seq
    log["rows"].append({
        "n": _seq, "title": str(title or "?"), "disposition": str(disposition or "?"),
        "score": score, "tier": tier, "gb": gb, "instance": instance,
        "reason": reason, "media": media,
    })
    return log


def _fmt(v, spec="") -> str:
    if v is None:
        return "-"
    try:
        return format(v, spec) if spec else str(v)
    except (TypeError, ValueError):
        return str(v)


def summarise(log) -> dict:
    """``{disposition: count}`` plus totals. Used to prove the file and the main
    log's summary line agree -- if they ever disagree, one of them is lying, and
    historically it has been the summary."""
    rows = (log or {}).get("rows") if isinstance(log, dict) else None
    rows = rows if isinstance(rows, list) else []
    out = {d: 0 for d in DISPOSITIONS}
    for r in rows:
        if not isinstance(r, dict):
            continue
        out[r.get("disposition")] = out.get(r.get("disposition"), 0) + 1
    out["total"] = sum(1 for r in rows if isinstance(r, dict))
    out["denied"] = sum(n for d, n in out.items() if d in _DENIED)
    _gb = [r.get("gb") for r in rows if isinstance(r, dict)
           and r.get("disposition") == "funded" and isinstance(r.get("gb"), (int, float))]
    out["funded_gb"] = round(sum(_gb), 1) if _gb else 0.0
    return out


def render(log, *, instance_label=None, hard_max=None, free_gb=None) -> list:
    """The whole file, as a list of lines.

    Rows are grouped by disposition and ordered WITHIN each group by score,
    descending -- because the question this file exists to answer is "why these ten
    and not those ten", and that is a question about rank. Unscored rows sort last
    rather than being dropped: a candidate with no score still consumed a decision.
    """
    rows = (log or {}).get("rows") if isinstance(log, dict) else None
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    s = summarise(log)
    head = [
        "==== acquisition decisions ====",
        f"candidates {s['total']}  |  funded {s['funded']} (~{s['funded_gb']} GB)  "
        f"|  capped {s['capped']}  |  refused {s['refused']}  |  deferred {s['deferred']}",
    ]
    ctx = []
    if instance_label: ctx.append(f"instance {instance_label}")
    if hard_max is not None: ctx.append(f"hard_max_adds {hard_max}")
    if free_gb is not None: ctx.append(f"free {free_gb:.0f} GB")
    if ctx:
        head.append("  ".join(ctx))
    # When the cap is the sole limiter, SAY SO. That is the condition under which
    # the ranking below is the only thing deciding what the household gets, and it
    # is not obvious from the counts alone.
    if hard_max is not None and s["capped"] and not s["refused"]:
        head.append(f"NOTE: nothing was refused on budget -- hard_max_adds ({hard_max}) is the "
                    f"sole limiter, so the ranking below decided the outcome.")
    head.append("")

    body = []
    for disp in DISPOSITIONS:
        group = [r for r in rows if r.get("disposition") == disp]
        if not group:
            continue
        group.sort(key=lambda r: (r.get("score") is None,
                                  -(r.get("score") if isinstance(r.get("score"), (int, float)) else 0)))
        body.append(f"-- {disp}: {len(group)} --")
        for r in group:
            # EVERY field via .get(). A row reaching here may be missing anything --
            # four separate hostile shapes broke earlier versions of this function one
            # at a time, which is what happens when a formatter assumes the dict it is
            # handed. Assume nothing; the whole point of this file is to survive the
            # run it is documenting.
            bits = [f"{_fmt(r.get('score'), '.0f'):>4}", f"{str(r.get('title') or '?')[:58]:58s}"]
            if r.get("media"):    bits.append(f"[{r['media']}]")
            if r.get("tier"):     bits.append(f"tier {r['tier']}")
            if r.get("gb") is not None: bits.append(f"{_fmt(r.get('gb'), '.1f')} GB")
            if r.get("instance"): bits.append(f"-> {r['instance']}")
            if r.get("reason"):   bits.append(f"({r['reason']})")
            body.append("  " + "  ".join(bits))
        body.append("")
    return head + body
