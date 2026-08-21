"""
affinity/account_links.py — two logins, one viewer (the PURE half).
================================================================================
``account_links`` declares groups of accounts that are the SAME PERSON — a managed
Plex profile plus that person's own login, say. ``GLD-TAUT-15`` merged their
*gradings*: plays are rewritten onto the group PRIMARY before the brain groups
them, and the merged affinity matrix is handed back to every member.

That is only half of "both profiles show the same thing". The 2026-08-20 live run
proved it: ``Mom`` and ``mirandan75`` graded IDENTICALLY (14 genres / 816 actors /
16 directors each) and their playlists still diverged —

    The Long Glide   Mom 24 items   mirandan75 16 items
    TV Up Next       completely different series

— while their *movie* Up Next matched for 25 straight rows. That pattern is the
diagnosis: identical affinity, different WATCHED-SETS. The movie shelves agreed
because neither profile had watched those films; the TV shelves disagreed because
each profile's watch history is its own.

This module is the shared identity half, extracted so the two consumers cannot
drift. ``TautulliUsersManager`` already had this parsing inline; a second copy in
the playlist builders would be a textbook P-E — and the failure mode is ugly, since
a builder that disagreed with the grader about who is linked would merge one and
not the other, producing exactly the half-joined state this exists to fix.

Pure: config in, sets out. No I/O, stdlib only.
"""
from __future__ import annotations

LINK_CFG_KEY = "account_links"

# Every field a roster entry might carry the account's name/id under. Rosters differ
# by caller (Tautulli user lists, Plex tracked users, the identity map), and a link
# that resolves for one consumer and not another is worse than one that never
# resolves at all -- it produces a half-merged viewer.
_NAME_FIELDS = ("username", "tautulli_username", "friendly_name", "title",
                "safe_user", "user_id", "tautulli_user_id")


def parse_links(config) -> tuple:
    """``(alias, groups)`` from ``account_links``.

    ``alias`` maps every member key (lowercased) -> its group's PRIMARY key;
    ``groups`` is the normalised list of member-key lists, first member primary.

    A GROUP must be a list. A bare string (``["a", "b"]`` instead of
    ``[["a", "b"]]`` -- the shape a comma-separated env overlay would produce)
    would otherwise iterate its CHARACTERS and build aliases out of single letters,
    so it is rejected rather than silently linking 'm' to 'o'. A one-member group
    is a no-op, not a link.
    """
    raw = None
    try:
        raw = (config or {}).get(LINK_CFG_KEY)
    except Exception:
        raw = None
    alias, groups = {}, []
    if not isinstance(raw, list):
        return alias, groups
    for grp in raw:
        if isinstance(grp, str) or not isinstance(grp, (list, tuple)):
            continue
        members = [str(x).strip().lower() for x in (grp or []) if str(x).strip()]
        if len(members) < 2:
            continue
        groups.append(members)
        for mkey in members:
            alias[mkey] = members[0]
    return alias, groups


def malformed_groups(config) -> list:
    """Entries in ``account_links`` that are not usable groups, as short descriptions
    the CALLER can log.

    The parser is pure and cannot log, but silence here would be its own defect: a
    misshapen config would no-op invisibly, and the operator would see two accounts
    stubbornly not merging with nothing to explain it. Separating detection from
    reporting lets the parser stay pure AND the failure stay loud — the same split
    ``_family_ids`` uses (return a reason, let the caller render it).
    """
    raw = None
    try:
        raw = (config or {}).get(LINK_CFG_KEY)
    except Exception:
        return []
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [f"account_links must be a LIST of groups, got {type(raw).__name__}"]
    out = []
    for grp in raw:
        if isinstance(grp, str) or not isinstance(grp, (list, tuple)):
            out.append(
                f"expected a LIST of accounts, got {type(grp).__name__} ({grp!r}) - "
                f'that entry is IGNORED. Shape is [["primary", "other"]], a list of groups.')
            continue
        members = [str(x).strip() for x in (grp or []) if str(x).strip()]
        if len(members) == 1:
            out.append(f"group {members!r} has ONE member - a link needs at least two; ignored.")
    return out


def account_keys(entry) -> set:
    """Every key an ``account_links`` member could name this roster entry by."""
    keys = set()
    if not isinstance(entry, dict):
        return keys
    for f in _NAME_FIELDS:
        v = entry.get(f)
        if v is not None and str(v).strip():
            keys.add(str(v).strip().lower())
    return keys


def linked_id_map(config, roster, *, id_field: str = "tautulli_user_id") -> dict:
    """``{id: {ids of everyone in that account's link group}}``.

    The value ALWAYS contains the account's own id, so a caller can use the map
    unconditionally: ``ids = m.get(uid) or {uid}`` needs no branch for the
    unlinked case, and an unlinked account is a group of one rather than a
    special case someone has to remember.

    Ids are returned in the roster's own type (not stringified) because callers
    feed them straight back to APIs keyed on the original type -- a str `"555"`
    where an int `555` was expected silently returns nothing rather than raising.
    """
    alias, _ = parse_links(config)
    if not alias:
        return {}
    # primary key -> ids of every roster entry in that group
    by_primary: dict = {}
    owner: dict = {}
    for entry in (roster or []):
        uid = (entry or {}).get(id_field)
        if uid is None:
            continue
        primary = None
        for k in account_keys(entry):
            if k in alias:
                primary = alias[k]
                break
        if primary is None:
            continue
        by_primary.setdefault(primary, set()).add(uid)
        owner[uid] = primary
    return {uid: set(by_primary.get(primary, {uid})) for uid, primary in owner.items()}


def expand(linked_map, user_id) -> set:
    """The id-set to query for ``user_id`` -- its whole group, or just itself.

    ``None`` in, empty out: a caller with no id has nothing to query, and
    returning ``{None}`` would send a null id to an API that answers it with the
    HOUSEHOLD history, silently giving one profile everyone else's watches.
    """
    if user_id is None:
        return set()
    return set((linked_map or {}).get(user_id) or {user_id})
