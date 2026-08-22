"""
tier_pairing.py — pair Sonarr and Radarr instances by RESOLUTION TIER.
================================================================================
OPT-IN. A tiered layout (``tv/720/…``, ``tv/1080/…``, ``tv/2160/…`` with one arr
instance per tier) is one deployment shape, not the shipped default. A standard
install has ONE Sonarr and ONE Radarr with untiered roots, and everything here
returns "nothing to pair" for it.

WHAT THIS IS FOR
----------------
When a deployment DOES run several instances, onboarding has to know which
Sonarr goes with which Radarr — a 720 Sonarr paired to a 4K Radarr would route
every upgrade to the wrong library. That pairing is usually obvious from the
install itself, and asking an operator to restate what the config already says
is how onboarding questionnaires become long enough to click through blindly.

So: infer where the evidence is unambiguous, ASK where it is not, and never
guess. :func:`pair_instances` returns both halves separately — the pairs it is
confident about and the ones it needs answered — so the caller can prompt for
exactly the gaps.

WHY ROOT PATHS AND NOT NAMES
----------------------------
Instance KEYS are the weakest evidence available. A real install has Sonarr
``standard`` (whose roots are ``/data/media/tv/720/…``) beside Radarr ``ultra``
(whose roots are ``/data/media/movies/4k/…``): neither key names its tier, and
"standard" means 720 on one deployment and 1080 on the next. The ROOT FOLDERS
say what the instance actually holds, and they cannot drift from the truth
because the arr itself reports them.

Every signal is therefore weighed together — key, base URL, and root paths — and
a tier is only assigned when they do not CONTRADICT each other. Two different
tiers in the evidence is an ambiguity to raise, not a majority vote to take.

WHAT IS DELIBERATELY NOT INFERRED
---------------------------------
``4k`` and ``uhd`` map to 2160 because those are industry-standard synonyms with
one meaning. ``ultra``, ``standard``, ``main``, ``hd`` and friends do NOT map to
anything: they are house style, and one deployment's "ultra" is another's 1080.
Guessing there would silently pair the wrong libraries, and the symptom — media
arriving in the wrong tier — would look like a routing bug for weeks.

Pure module: dicts in, pairing out. No I/O, stdlib only.
"""
from __future__ import annotations

import re

#: Canonical tiers, low to high. Ordering matters for display only.
TIERS: tuple[str, ...] = ("480", "720", "1080", "2160")

#: Unambiguous synonyms. Kept SHORT on purpose -- see the module docstring. An
#: entry here is a claim that the token has exactly one meaning across every
#: deployment, and very few do.
_TIER_ALIASES: dict[str, str] = {
    "4k": "2160", "uhd": "2160", "2160p": "2160",
    "1080p": "1080", "fhd": "1080",
    "720p": "720", "hd720": "720",
    "480p": "480", "sd": "480",
}

#: A tier token must stand alone -- bounded by a non-alphanumeric or a string
#: edge. Without this, "1080" matches inside "10802" and "sd" inside "vsdx";
#: substring matching on short tokens is the trap that keeps a `pop` out of the
#: kids-network list for the same reason.
_BOUND = r"(?<![a-z0-9])(%s)(?![a-z0-9])"


def _tokens(text) -> set[str]:
    """Every tier token in one evidence string, canonicalised."""
    s = str(text or "").lower()
    found: set[str] = set()
    for tok in list(_TIER_ALIASES) + list(TIERS):
        if re.search(_BOUND % re.escape(tok), s):
            found.add(_TIER_ALIASES.get(tok, tok))
    return found


def detect_tier(*evidence) -> "str | None":
    """The tier every piece of evidence agrees on, or ``None``.

    ``None`` means "cannot tell" and is returned for BOTH no-evidence and
    contradictory-evidence. Those are different situations for a human but the
    same instruction for the caller: ask. :func:`tier_conflict` distinguishes
    them when the prompt wants to explain itself.
    """
    seen: set[str] = set()
    for e in evidence:
        if isinstance(e, (list, tuple, set)):
            for x in e:
                seen |= _tokens(x)
        else:
            seen |= _tokens(e)
    return seen.pop() if len(seen) == 1 else None


def tier_conflict(*evidence) -> "set[str]":
    """The competing tiers when evidence disagrees; empty when it does not.

    A non-empty result is worth SAYING in the prompt -- "this instance looks like
    both 720 and 1080" tells the operator their config is inconsistent, which is
    more useful than asking them to pick blind.
    """
    seen: set[str] = set()
    for e in evidence:
        if isinstance(e, (list, tuple, set)):
            for x in e:
                seen |= _tokens(x)
        else:
            seen |= _tokens(e)
    return seen if len(seen) > 1 else set()


def instance_evidence(key: str, block) -> tuple:
    """Everything that could name an instance's tier, weakest signal last.

    Root folders first because the arr reports them and they describe what the
    instance actually holds; the key and URL are house style and may say nothing.
    """
    block = block if isinstance(block, dict) else {}
    roots = block.get("root_folders") or block.get("rootFolders") or []
    if isinstance(roots, dict):
        roots = list(roots.values())
    return (list(roots), str(key or ""), str(block.get("base_url") or ""))


def _real_instances(instances) -> dict:
    """Drop the bookkeeping keys an instances block carries (``default_instance``)
    and anything that is not a mapping."""
    out = {}
    for k, v in (instances or {}).items():
        if k == "default_instance" or not isinstance(v, dict):
            continue
        out[k] = v
    return out


def tiers_for(instances) -> dict:
    """``{instance_key: tier_or_None}`` for one service."""
    return {k: detect_tier(*instance_evidence(k, v))
            for k, v in _real_instances(instances).items()}


def pair_instances(sonarr_instances, radarr_instances) -> dict:
    """Pair Sonarr to Radarr by tier.

    Returns::

        {"pairs":    {tier: {"sonarr": key, "radarr": key}},   # confident
         "ask":      [ {...}, ... ],                           # needs a human
         "single":   bool}                                     # nothing to pair

    ``single`` is True for the standard one-of-each install, and the caller
    should skip the whole prompt in that case rather than asking a question with
    one possible answer.

    An instance whose tier could not be determined lands in ``ask`` WITH its
    evidence, so the prompt can show what was looked at. A tier present on only
    one side also lands in ``ask`` -- a 1080 Sonarr with no 1080 Radarr is a real
    and legitimate state (this deployment has exactly that), and inventing a
    partner for it would be worse than reporting it.
    """
    son = _real_instances(sonarr_instances)
    rad = _real_instances(radarr_instances)
    if len(son) <= 1 and len(rad) <= 1:
        return {"pairs": {}, "ask": [], "single": True}

    s_t, r_t = tiers_for(son), tiers_for(rad)
    pairs, ask = {}, []

    for tier in TIERS:
        s_keys = [k for k, t in s_t.items() if t == tier]
        r_keys = [k for k, t in r_t.items() if t == tier]
        if len(s_keys) == 1 and len(r_keys) == 1:
            pairs[tier] = {"sonarr": s_keys[0], "radarr": r_keys[0]}
        elif s_keys or r_keys:
            # One side only, or two candidates on a side. Both are answerable
            # only by the operator.
            ask.append({"tier": tier, "sonarr": s_keys, "radarr": r_keys,
                        "why": ("no matching instance on the other side"
                                if bool(s_keys) != bool(r_keys)
                                else "more than one instance claims this tier")})

    for svc, table, blocks in (("sonarr", s_t, son), ("radarr", r_t, rad)):
        for key, tier in table.items():
            if tier is not None:
                continue
            ev = instance_evidence(key, blocks[key])
            conflict = sorted(tier_conflict(*ev))
            ask.append({"tier": None, "service": svc, "instance": key,
                        "evidence": ev, "conflict": conflict,
                        "why": (f"evidence names more than one tier: {', '.join(conflict)}"
                                if conflict else "no tier found in the name, URL or root folders")})
    return {"pairs": pairs, "ask": ask, "single": False}


def describe(result) -> list:
    """Human lines for the onboarding prompt -- what was inferred and what is
    still needed. Returns ``[]`` for a single-instance install, so a standard
    deployment sees nothing about tiers at all."""
    if not isinstance(result, dict) or result.get("single"):
        return []
    lines = []
    for tier, p in sorted((result.get("pairs") or {}).items(),
                          key=lambda kv: TIERS.index(kv[0]) if kv[0] in TIERS else 99):
        lines.append(f"{tier}p: sonarr '{p['sonarr']}' + radarr '{p['radarr']}' (detected)")
    for a in (result.get("ask") or []):
        if a.get("tier"):
            lines.append(f"{a['tier']}p: sonarr={a['sonarr'] or '-'} radarr={a['radarr'] or '-'} "
                         f"— {a['why']}")
        else:
            lines.append(f"{a['service']} '{a['instance']}': {a['why']}")
    return lines
