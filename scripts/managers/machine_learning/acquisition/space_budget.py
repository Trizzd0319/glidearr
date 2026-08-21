"""
acquisition/space_budget.py — bytes, not counts, as the acquisition constraint.
================================================================================
``max_adds_per_run`` capped acquisition by COUNT. The operator's actual policy is
capacity: add as many titles as fit while free space stays above the pressure
band; the only cost of a big wave is how long the pipeline takes to drain it.
A count is the wrong currency for that — 10 remux movies and 10 anime pilots
differ by two orders of magnitude in bytes.

This module prices every candidate in GB and funds them, in priority order,
out of a per-run headroom budget::

    budget = max(0, free - U) - in_flight

``U`` is the same band top every other space gate uses (``space_targets``:
``free_space_limit`` + headroom, or 25% of the drive). ``in_flight`` is the sum
of bytes committed by RECENT runs that have not yet landed — without it, run N
reads free=3800, commits 400 GB of grabs, and run N+1 two hours later reads the
same 3800 (downloads still queued) and commits the same 400 again. That is the
GLD-RST-05 phantom-headroom failure with the sign flipped, and the committed
ledger below is what prevents it.

Fail direction — READ BEFORE EDITING
------------------------------------
Every other space gate here fails OPEN (unreadable disk → free=inf → the add is
never blocked by a transient error). That is correct for a COUNT-capped system:
"open" still means at most N adds. Under an UNCAPPED byte budget the same
convention would mean "unlimited adds, no budget" — the failure mode the budget
exists to prevent. So the budget inverts it: **any gap in the information the
budget needs (unreadable free space, unreadable/corrupt ledger) collapses the
run back to the bounded legacy count cap**, never to unlimited. Uncertainty may
only make the system LESS aggressive (``bin_forecast``'s rule).

P-C notes (absent vs empty vs unknown)
--------------------------------------
* An ABSENT ledger key is genuinely "nothing ever committed" (the feature's
  first run) and is safely read as empty — stated here because that conflation
  is usually the bug, not the fix. A ledger key that EXISTS but is the wrong
  type, or a cache read that RAISES, is *unknown*, not empty: unknown in-flight
  would be under-counted as 0 (MORE aggressive), so both trigger the count-cap
  fallback instead.
* A candidate with no usable ``expected_size_gb`` is priced at a conservative
  DEFAULT, never 0 — charging 0 would exempt from the budget exactly the titles
  we know least about.
* A ledger entry whose ``gb`` will not parse is priced at the default on the
  in-flight side too (overstating in-flight shrinks the budget: safe).

Pure module: no I/O, no manager access, stdlib only. The service owns every
cache read/write and passes plain values in.
"""
from __future__ import annotations

# Cache key for the committed-bytes ledger (one list, entries carry their pool).
LEDGER_KEY = "acquisition/space_budget/committed"

# One pool key for shared_pool mode. Radarr standard/ultra and Sonarr standard
# on this deployment are one Unraid array behind TRaSH hardlinks — charging them
# independently would double-spend the same free space.
SHARED_POOL = ("*", "*")

DEFAULTS = {
    # Off by default at the MODULE level: a tester who copies the code without
    # config keeps the legacy count-cap behaviour byte-identically. The
    # operator's config enables it explicitly.
    "enabled": False,
    # One budget across all instances (min headroom), because on a single-array
    # deployment every instance reports the same underlying free space. Set
    # false only when instances genuinely sit on separate volumes; the shared
    # default under-grabs in that case (safe), never over-grabs.
    "shared_pool": True,
    # How long a committed grab counts against the budget before it is presumed
    # dead (grab failed / release never found). Too SHORT releases bytes that
    # are still downloading (over-commit — the dangerous direction); too LONG
    # merely under-grabs for a while. Shows add unsearched and wait for the
    # pilot pass next run, so the TTL must cover add→pilot-search→download.
    "committed_ttl_hours": 72,
    # Price for a candidate with no usable expected_size_gb. Deliberately HIGH
    # (a 1080p remux movie runs 15-35 GB): overcharging unknowns under-grabs.
    "default_movie_gb": 15.0,
    # Per-EPISODE default. Show adds are charged ONE episode at the pilot floor
    # (search_on_add=false + pilot path grabs the pilot only; quality climbs by
    # the watch-based path, which is watch-gated and out of budget scope).
    "default_episode_gb": 2.0,
    # Optional absolute count ceiling ON TOP of the byte budget. 0 = none
    # (operator ruling 2026-08-20: bytes are the constraint, not counts).
    # Tester templates may set this as a seatbelt.
    "hard_max_adds": 0,
}


def config_for(acq_cfg) -> dict:
    """Merge ``acquisition.space_budget`` over the defaults. Unknown keys and
    ``None`` values are ignored, so a partial block keeps every other default."""
    raw = {}
    try:
        raw = (acq_cfg.get("space_budget", {}) or {}) if hasattr(acq_cfg, "get") else {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        if k in out and v is not None:
            out[k] = v
    return out


def pool_key(candidate, shared: bool) -> tuple:
    """The budget pool a candidate draws from: the shared pool, or its own
    (service, instance). Service is derived the same way the add loop derives
    it — shows route to sonarr, everything else to radarr."""
    if shared:
        return SHARED_POOL
    svc = "sonarr" if (candidate or {}).get("type") == "show" else "radarr"
    return (svc, str((candidate or {}).get("instance")))


def _as_pos_float(v):
    """float(v) when it parses to a positive number, else None. Unknown is
    UNKNOWN — never 0 (see the module P-C notes)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def charge_gb(candidate, cfg) -> tuple:
    """``(gb, defaulted)`` — the byte price of one candidate.

    Movies: the resolver's ``expected_size_gb`` for the RESOLVED profile (the
    dual-version HD baseline is already the capped <=1080p copy; the 4K
    companion is a separate candidate priced separately at add time).
    Shows: ``expected_size_gb`` is per-episode; the charge is ONE episode —
    the pilot-floor policy means an add pulls the pilot, nothing more, until
    watches earn upgrades. A deployment that turns full-season search-on-add
    back on understates here; that trade is documented, not hidden.
    Unknown/garbage size: the conservative default for the medium.
    """
    gb = _as_pos_float((candidate or {}).get("expected_size_gb"))
    if gb is not None:
        return gb, False
    is_show = (candidate or {}).get("type") == "show"
    return float(cfg["default_episode_gb" if is_show else "default_movie_gb"]), True


def reconcile(entries, now_ts, ttl_seconds) -> tuple:
    """``(kept, expired)`` — split the ledger by TTL. An entry with an absent or
    unparseable ``committed_at`` is EXPIRED (it cannot be aged, and keeping it
    forever would permanently shrink the budget with no path to release).

    TTL-only reconciliation is deliberately v1: an entry that imported early
    stays until TTL (budget under-counts: safe), a failed grab stays until TTL
    (same). The one wrong-direction case — a download still in flight PAST the
    TTL — is what the generous default TTL exists to make rare. A hasFile-based
    reconcile is a designed-in upgrade, not a prerequisite."""
    kept, expired = [], []
    for en in (entries or []):
        if not isinstance(en, dict):
            expired.append(en)
            continue
        try:
            t0 = float(en.get("committed_at"))
        except (TypeError, ValueError):
            t0 = None
        if t0 is None or (float(now_ts) - t0) >= float(ttl_seconds):
            expired.append(en)
        else:
            kept.append(en)
    return kept, expired


def inflight_by_pool(entries, shared: bool, fallback_gb: float) -> dict:
    """Sum committed GB per pool. An entry whose ``gb`` will not parse counts as
    ``fallback_gb`` — overstating in-flight shrinks the budget (safe direction);
    dropping it would silently under-count."""
    out: dict = {}
    for en in (entries or []):
        if not isinstance(en, dict):
            continue
        if shared:
            k = SHARED_POOL
        else:
            k = (str(en.get("service") or "?"), str(en.get("instance")))
        gb = _as_pos_float(en.get("gb"))
        out[k] = out.get(k, 0.0) + (gb if gb is not None else float(fallback_gb))
    return out


def make_entry(candidate, gb, now_ts) -> dict:
    """One committed-ledger entry. ``gb`` is the charge actually levied (the
    resolved or defaulted price), not re-derived, so the release on expiry
    exactly mirrors the debit at commit."""
    return {
        "service": "sonarr" if (candidate or {}).get("type") == "show" else "radarr",
        "instance": str((candidate or {}).get("instance")),
        "type": (candidate or {}).get("type"),
        "ext_id": (candidate or {}).get("ext_id"),
        "title": str((candidate or {}).get("title") or "")[:80],
        "gb": round(float(gb), 2),
        "committed_at": float(now_ts),
    }


class BudgetContext:
    """Mutable per-run budget state: remaining GB per pool, charges levied, and
    the commits to persist. Built once by the service from a validated snapshot
    (the service falls back to the count cap rather than constructing this on
    incomplete information), then consulted by selection AND by the in-loop 4K
    companion path — companions are planned after selection, so the budget must
    stay live through the add loop or the largest files in the system would be
    the only ones it never sees."""

    def __init__(self, budgets: dict, cfg: dict, now_ts: float):
        self.budgets = {k: max(0.0, float(v)) for k, v in (budgets or {}).items()}
        self.cfg = dict(cfg)
        self.now_ts = float(now_ts)
        self.commits: list = []
        self.stats = {"funded": 0, "skipped_space": 0, "skipped_gb": 0.0,
                      "skipped_hard_max": 0,
                      "defaulted_charges": 0, "uncharged_no_pool": 0,
                      "charged_gb": 0.0}

    def pool_of(self, candidate) -> tuple:
        return pool_key(candidate, bool(self.cfg.get("shared_pool", True)))

    def try_charge(self, candidate) -> tuple:
        """``(ok, gb, defaulted)``. Debits the candidate's pool when it fits.
        A candidate whose pool was never priced (its gateway was unavailable at
        snapshot time) passes UNCHARGED — the add will fail at the gateway
        anyway, and refusing it here would turn an outage into a silent policy
        change. Counted in ``uncharged_no_pool`` so it is never invisible."""
        gb, defaulted = charge_gb(candidate, self.cfg)
        pool = self.pool_of(candidate)
        if pool not in self.budgets:
            self.stats["uncharged_no_pool"] += 1
            return True, None, defaulted
        if gb <= self.budgets[pool]:
            self.budgets[pool] -= gb
            self.stats["funded"] += 1
            self.stats["charged_gb"] += gb
            if defaulted:
                self.stats["defaulted_charges"] += 1
            return True, gb, defaulted
        self.stats["skipped_space"] += 1
        self.stats["skipped_gb"] += gb
        return False, gb, defaulted

    def commit(self, candidate, gb) -> None:
        """Record an ACTUALLY-ADDED candidate's bytes for the cross-run ledger.
        Called only on action=='added' — a would-add (dry) or add-failed leaves
        its in-run charge spent (mildly conservative) but writes nothing."""
        price = _as_pos_float(gb)
        if price is None:
            price, _ = charge_gb(candidate, self.cfg)
        self.commits.append(make_entry(candidate, price, self.now_ts))


def select(eligible, ctx: BudgetContext) -> tuple:
    """``(selected, skipped)`` — fund candidates in the given priority order,
    skip-and-continue: an item that does not fit is skipped and the walk goes
    on, so a 40 GB movie near the budget edge does not strand the cheap shows
    behind it. Starvation of big titles is self-limiting — they stay top of
    the order and get first claim on the refreshed budget next run; a big title
    that NEVER fits is one the free-space policy is correctly refusing.

    Funded candidates are stamped in place with ``space_charge_gb`` and
    ``space_pool`` (the breakdown frame and the decisions table read both);
    skipped ones get ``skip_reason='space_budget'`` plus the price that did not
    fit, so the log can say exactly what was refused and how big it was."""
    hard_max = int(ctx.cfg.get("hard_max_adds", 0) or 0)
    selected, skipped = [], []
    for e in (eligible or []):
        if hard_max and len(selected) >= hard_max:
            e["skip_reason"] = "space_budget_hard_max"
            # Counted in ctx.stats, not just the caller's skip tally. The hard-max
            # branch returns BEFORE try_charge, so before this it touched no stat at
            # all -- and the summary line reads its counters, so a run that refused
            # 122 titles on the cap reported "0 refused". A headline saying nothing
            # was turned away, while 122 things were, is worse than no headline.
            ctx.stats["skipped_hard_max"] = ctx.stats.get("skipped_hard_max", 0) + 1
            skipped.append(e)
            continue
        ok, gb, _defaulted = ctx.try_charge(e)
        if ok:
            e["space_charge_gb"] = gb
            e["space_pool"] = "/".join(ctx.pool_of(e)) if gb is not None else None
            selected.append(e)
        else:
            e["skip_reason"] = "space_budget"
            e["space_charge_gb"] = gb
            skipped.append(e)
    return selected, skipped
