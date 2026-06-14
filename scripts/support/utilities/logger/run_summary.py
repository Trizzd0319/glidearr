"""RunSummaryManager — the end-of-run consolidated decision/movement report.

Managers that would otherwise dump a per-title detail grid into the LIVE log (universe
quality actions, monitored-missing triage, stale-prune candidates, deletions, Trakt
enrichment ETA, …) instead record their rows here via ``add_rows``. At the very end of the
run ``Main.run`` calls ``render`` once, emitting the whole lot grouped **by service, then by
concern**: a per-service header followed by that service's concern tables (each with a
leading ``Instance`` column), so the detail lives in one organised place rather than
scrolling past inline. Per-pass one-line count summaries stay in the live log; only the
per-row detail moves here.

Run-scoped: a fresh instance is created per ``GlobalCacheManager`` (one per run/process), so
nothing leaks between runs. All managers reach it via the shared ``global_cache.run_summary``.

Section contract: one (service, concern) pair == one column schema. Callers adding rows to
the same (service, concern) must pass the same ``headers`` (rows are normalised to that width
defensively). ``order`` pins the concern's position within its service block (shared scheme
so the same concern lines up across services). Cells must be plain ASCII — ``log_grid`` pads
with NBSP and the cp1252 console/log can't encode decorative glyphs.
"""
from __future__ import annotations


class RunSummaryManager:
    # Services render in this order; any others follow alphabetically.
    _SERVICE_ORDER = ["sonarr", "radarr", "plex", "trakt", "tautulli", "mal"]

    def __init__(self, logger=None):
        self.logger = logger
        # (service, concern) -> {"order": int, "headers": [...], "rows": [[...], ...]}
        self._sections: dict[tuple, dict] = {}
        self._next_order = 0

    def add_rows(self, service: str, concern: str, instance: str,
                 headers, rows, *, order: int | None = None) -> None:
        """Record detail ``rows`` under (``service``, ``concern``) with an ``Instance``
        column prepended.

        ``rows`` is a sequence of cell-sequences. No-op on empty/falsy ``rows``. Repeated
        calls for the same (service, concern) accumulate (e.g. one call per instance). The
        first call fixes the table's headers and display order; later rows are normalised to
        that width.
        """
        if not rows:
            return
        key = (str(service), str(concern))
        sec = self._sections.get(key)
        if sec is None:
            if order is None:
                self._next_order += 1
                order = self._next_order
            sec = self._sections[key] = {
                "order": order,
                "headers": ["Instance", *[str(h) for h in headers]],
                "rows": [],
            }
        width = len(sec["headers"]) - 1          # cells expected after Instance
        inst = str(instance)
        for r in rows:
            cells = [("" if c is None else str(c)) for c in list(r)[:width]]
            cells += [""] * (width - len(cells))  # pad short rows to keep columns aligned
            sec["rows"].append([inst, *cells])

    def has_data(self) -> bool:
        return any(s["rows"] for s in self._sections.values())

    def _service_sort_key(self, service: str):
        try:
            return (0, self._SERVICE_ORDER.index(service))
        except ValueError:
            return (1, service)

    def render(self, logger=None, *, cap: int = 40) -> None:
        """Emit the report grouped by service then concern. Per-service header, then that
        service's concern tables (in ``order``). No-op when empty or no logger available."""
        log = logger or self.logger
        if log is None or not self.has_data():
            return

        by_service: dict[str, list] = {}
        for (service, concern), sec in self._sections.items():
            if sec["rows"]:
                by_service.setdefault(service, []).append((concern, sec))

        log.log_info("===== END-OF-RUN SUMMARY =====")
        for service in sorted(by_service, key=self._service_sort_key):
            log.log_info(f"--- {service.upper()} ---")
            for concern, sec in sorted(by_service[service], key=lambda cs: cs[1]["order"]):
                log.log_grid(sec["headers"], sec["rows"], title=concern, cap=cap)
