"""
RadarrSyncProfileScoresManager — align per-profile custom-format SCORES across Radarr instances.
================================================================================
Custom-format *definitions* are synced by RadarrSyncCustomFormatsManager; their SCORES live inside
each quality profile (``formatItems[*].score``) and were never synced. This leaf reads a canonical
``{cf_name: score}`` map from the source instance (default ``standard``) and aligns every target
instance's profiles to it, so the 4K instance scores releases the same way standard does.

Everything is keyed by NAME (cf ids + profile ids are per-instance) and defaults to a complete no-op:

  • ``scoring.cf_sync.enabled`` is False by default → the whole feature is inert.
  • ``self.dry_run`` → log the full per-CF diff grid, PUT nothing.
  • FILL-ONLY by default → only set a target score that is currently UNSET (0); a target score that
    already differs is a CONFLICT, logged and SKIPPED, never clobbered. Overwriting an existing
    score requires BOTH ``overwrite_existing`` AND the ``cf_sync_overwrite_consent`` opt-in.

Apply is a round-trip: GET the live profile, mutate ONLY ``formatItems[*].score`` for the affected
CFs, PUT the whole object back (cutoff / items / minFormatScore / cutoffFormatScore / language
preserved untouched). A pre-write snapshot of the target's profiles + CFs is stashed in global_cache.
"""
from __future__ import annotations

import os

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.size_model import profile_max_quality

_CONSENT_ENV_VARS = ("RECOMMENDARR_CF_SYNC_OVERWRITE_CONSENT", "GLIDEARR_CF_SYNC_OVERWRITE_CONSENT")
_CONSENT_TRUTHY = {"1", "true", "yes", "on", "y"}
# the dedicated 4K instance's categorized labels (mirror uhd_reconcile._UHD_LABELS)
_UHD_LABELS = ("4K", "4k", "uhd", "UHD", "2160p", "2160")
_UHD_RES = 2160


def cf_sync_overwrite_consented(config) -> bool:
    """Explicit opt-in to OVERWRITE existing (non-zero) target CF scores — the destructive mode that
    can erase per-instance tuning. A non-empty env var overrides ``cf_sync_overwrite_consent`` in
    config. Default False; the fill-only path needs no consent."""
    for var in _CONSENT_ENV_VARS:
        raw = os.environ.get(var)
        if raw is not None and raw.strip() != "":
            return raw.strip().lower() in _CONSENT_TRUTHY
    try:
        return bool(config.get("cf_sync_overwrite_consent", False)) if config else False
    except Exception:
        return False


class RadarrSyncProfileScoresManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrSyncManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrSyncManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self._parent = kwargs.get("manager")
        self.radarr_api = kwargs.get("radarr_api") or getattr(self._parent, "radarr_api", None)
        self.instance_manager = (kwargs.get("instance_manager")
                                 or getattr(self._parent, "instance_manager", None))
        self.dry_run = kwargs.get("dry_run", getattr(self._parent, "dry_run", False) if self._parent else False)
        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ── helpers ────────────────────────────────────────────────────────────────
    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _cfg(self) -> dict:
        return ((self.config.get("scoring") or {}).get("cf_sync") or {}) if self.config else {}

    def _instance_count(self) -> int:
        return len([k for k in (self.config.get("radarr_instances") or {}) if k != "default_instance"]) \
            if self.config else 0

    def enabled(self) -> bool:
        """AUTOMATIC: runs whenever the service has 2+ instances (there is something to keep in sync),
        unless the operator explicitly opts out with ``scoring.cf_sync.enabled: false``. A single-
        instance install is a natural no-op (no targets). Writes still honour dry_run + the fill-only /
        consent gates, so 'automatic' never means 'unsupervised clobber'."""
        if self._cfg().get("enabled") is False:
            return False
        return self._instance_count() >= 2

    def _overwrite_armed(self) -> bool:
        return bool(self._cfg().get("overwrite_existing", False)) and cf_sync_overwrite_consented(self.config)

    def _cf(self):
        """The sibling custom-formats manager (carries the name-keyed readers)."""
        return getattr(self._parent, "custom_formats", None)

    def _target_instances(self, source) -> list:
        insts = [k for k in (self.config.get("radarr_instances") or {}) if k != "default_instance"]
        src = self._resolve_instance(source)
        include_test = self._cfg().get("include_test", True)
        out = []
        for inst in insts:
            if self._resolve_instance(inst) == src:
                continue
            if not include_test and str(inst).lower() == "test":
                continue
            out.append(inst)
        return out

    def _canonical_map(self, source) -> dict:
        """``{cf_name: score}`` of the NON-ZERO scores to propagate. From the configured
        ``reference_profile`` on the source, else the richest source profile (most non-zero scores).
        Zero scores are excluded so the sync never zeroes a target's tuning."""
        cf = self._cf()
        if cf is None:
            return {}
        by_profile = cf.read_profile_scores_by_name(source) or {}
        ref = self._cfg().get("reference_profile")
        if ref and ref in by_profile:
            chosen = by_profile[ref]
        else:
            chosen = max(by_profile.values(),
                         key=lambda s: sum(1 for v in s.values() if v), default={})
        return {name: int(score) for name, score in chosen.items() if int(score) != 0}

    # ── definition sync (directional, additive POST-only) ───────────────────────
    @LoggerManager().log_function_entry
    @timeit("sync_definitions")
    def sync_definitions(self) -> dict:
        """Ensure every custom-format DEFINITION on the source exists on each target, by NAME
        (directional, additive — POST a missing def, never edit or delete an existing one). Scores
        are meaningless without the definition, so this runs before the score sync. Honours the
        custom-formats manager's dry_run; on a live create it invalidates the target CF read cache so
        the score plan sees the new definitions."""
        stats = {"created": 0, "present": 0}
        if not self.enabled():
            return stats
        cf = self._cf()
        if cf is None:
            return stats
        source = self._cfg().get("source_instance", "standard")
        source_cfs = cf.get_custom_formats(source) or []
        for inst in self._target_instances(source):
            have = {str(c.get("name", "")).strip().lower() for c in (cf.get_custom_formats(inst) or [])}
            made = 0
            for c in source_cfs:
                nm = str(c.get("name", "")).strip().lower()
                if not nm:
                    continue
                if nm in have:
                    stats["present"] += 1
                    continue
                cf.add_custom_format(inst, {k: v for k, v in c.items() if k != "id"})  # strip source id
                stats["created"] += 1
                made += 1
            if made and not self.dry_run:
                self.global_cache.set(f"radarr.custom_formats.{self._resolve_instance(inst)}", None)
        if stats["created"]:
            self.logger.log_info(f"[CFSync] custom-format definitions: {stats['created']} "
                                 f"{'would be ' if self.dry_run else ''}created on target(s), "
                                 f"{stats['present']} already present.")
        return stats

    # ── 2160p/UHD quality-profile routing to the 4K instance (additive) ──────────
    def _uhd_instance(self):
        """The dedicated 4K instance (categorized 4K/uhd/2160p label that resolves to a DISTINCT
        instance), or None. Mirrors uhd_reconcile._uhd_instance."""
        cat = (self.config.get("radarr_instances_categorized") or {}) if self.config else {}
        insts = (self.config.get("radarr_instances") or {}) if self.config else {}
        di = insts.get("default_instance")
        default = di.get("name") if isinstance(di, dict) else di
        for label in _UHD_LABELS:
            inst = cat.get(label)
            if inst and str(inst) in insts and str(inst) != str(default):
                return str(inst)
        return None

    @staticmethod
    def _is_uhd_profile(profile) -> bool:
        """A 2160p/UHD-tier profile to route to the 4K instance. A profile EXPLICITLY named for a
        lower tier (1080p/720p/…) is NOT a 4K profile even though it may ALLOW 2160p as a fallback
        (a common TRaSH pattern) — so name-exclude those first, then require a genuine 2160p reach."""
        name = str(profile.get("name") or "").lower()
        if any(h in name for h in ("1080", "720", "576", "480", "360")):
            return False
        try:
            res = profile_max_quality(profile)[0]
        except Exception:
            return False
        return res is not None and int(res) >= _UHD_RES

    @LoggerManager().log_function_entry
    @timeit("sync_uhd_profiles")
    def sync_uhd_profiles(self) -> dict:
        """Copy the source instance's 2160p/UHD quality PROFILES onto the dedicated 4K instance,
        by NAME (create if missing — ADDITIVE; never overwrites or deletes an existing profile). The
        profile's per-CF scores are carried by name (CF ids resolved on the 4K instance, so run the
        definition sync first). No-op when there is no distinct 4K instance. Honours dry_run."""
        stats = {"created": 0, "present": 0}
        if not self.enabled():
            return stats
        cf = self._cf()
        fourk = self._uhd_instance()
        source = self._cfg().get("source_instance", "standard")
        if cf is None or not fourk or self._resolve_instance(fourk) == self._resolve_instance(source):
            return stats
        src = self._resolve_instance(source)
        dst = self._resolve_instance(fourk)
        src_profiles = self.radarr_api._make_request(src, "qualityprofile", fallback=[]) or []
        uhd_src = [p for p in src_profiles if self._is_uhd_profile(p)]
        if not uhd_src:
            return stats
        have = {str(p.get("name", "")).strip().lower()
                for p in (self.radarr_api._make_request(dst, "qualityprofile", fallback=[]) or [])}
        dst_cf_ids = cf.cf_name_to_id(fourk) or {}
        dst_names = {str(c.get("name", "")).strip().lower(): c.get("name")
                     for c in (cf.get_custom_formats(fourk) or [])}
        for p in uhd_src:
            nm = str(p.get("name", "")).strip().lower()
            if nm in have:
                stats["present"] += 1
                continue
            payload = self._uhd_profile_payload(p, dst_cf_ids, dst_names)
            if self.dry_run:
                self.logger.log_info(f"[CFSync] [dry_run] would create 2160p profile "
                                     f"'{p.get('name')}' on the 4K instance '{fourk}'.")
            else:
                self.radarr_api._make_request(dst, "qualityprofile", method="POST", payload=payload)
            stats["created"] += 1
        if stats["created"]:
            self.logger.log_info(f"[CFSync] {stats['created']} UHD profile(s) "
                                 f"{'would be ' if self.dry_run else ''}created on the 4K instance "
                                 f"'{fourk}' ({stats['present']} already present).")
        return stats

    @staticmethod
    def _uhd_profile_payload(src_profile, dst_cf_ids, dst_names) -> dict:
        """Build the POST payload to create a copy of a source profile on the 4K instance: clone the
        source (strip its id), and rebuild formatItems against the TARGET's CF ids (by name) carrying
        the source's scores — a target CF the source doesn't score gets 0 (its default)."""
        src_scores = {}
        for fi in (src_profile.get("formatItems") or []):
            if fi.get("name"):
                try:
                    src_scores[str(fi["name"]).strip().lower()] = int(fi.get("score", 0) or 0)
                except (TypeError, ValueError):
                    pass
        format_items = [{"format": cid, "name": dst_names.get(lower, lower),
                         "score": src_scores.get(lower, 0)} for lower, cid in dst_cf_ids.items()]
        payload = {k: v for k, v in src_profile.items() if k != "id"}
        payload["formatItems"] = format_items
        return payload

    # ── plan (read-only diff grid) ───────────────────────────────────────────────
    @LoggerManager().log_function_entry
    @timeit("plan_score_sync")
    def plan_score_sync(self) -> list:
        """Per (instance, profile, cf) diff rows. action ∈ noop / fill / skip-conflict / overwrite /
        definition-missing. No writes."""
        cf = self._cf()
        if cf is None or self.radarr_api is None:
            return []
        source = self._cfg().get("source_instance", "standard")
        canonical = self._canonical_map(source)
        if not canonical:
            return []
        overwrite = self._overwrite_armed()
        rows: list = []
        for inst in self._target_instances(source):
            target_by_profile = cf.read_profile_scores_by_name(inst) or {}
            target_ids = cf.cf_name_to_id(inst) or {}
            for pname, pscores in target_by_profile.items():
                pscores_lower = {str(k).strip().lower(): v for k, v in pscores.items()}
                for cf_name, new_score in canonical.items():
                    key = str(cf_name).strip().lower()
                    if key not in target_ids:
                        rows.append({"instance": inst, "profile": pname, "cf": cf_name,
                                     "old": None, "new": new_score, "action": "definition-missing"})
                        continue
                    old = int(pscores_lower.get(key, 0))
                    if old == new_score:
                        action = "noop"
                    elif old == 0:
                        action = "fill"
                    elif overwrite:
                        action = "overwrite"
                    else:
                        action = "skip-conflict"
                    rows.append({"instance": inst, "profile": pname, "cf": cf_name,
                                 "old": old, "new": new_score, "action": action})
        return rows

    # ── apply (gated, dry-run-safe, round-trip PUT) ──────────────────────────────
    @LoggerManager().log_function_entry
    @timeit("apply_score_sync")
    def apply_score_sync(self) -> dict:
        stats = {"filled": 0, "overwritten": 0, "conflicts": 0, "missing": 0,
                 "profiles_put": 0, "failed": 0, "not_found": 0}
        if not self.enabled():
            return stats
        rows = self.plan_score_sync()
        if not rows:
            if not self._canonical_map(self._cfg().get("source_instance", "standard")):
                self.logger.log_warning("[CFSync] canonical score map is EMPTY — nothing to sync. Check "
                                        "source_instance / reference_profile, and that the source profile "
                                        "actually has non-zero custom-format scores.")
            else:
                self.logger.log_info("[CFSync] no custom-format score differences to apply.")
            return stats
        self._log_grid(rows)
        stats["conflicts"] = sum(1 for r in rows if r["action"] == "skip-conflict")
        stats["missing"] = sum(1 for r in rows if r["action"] == "definition-missing")
        actionable_actions = {"fill"} | ({"overwrite"} if self._overwrite_armed() else set())
        actionable = [r for r in rows if r["action"] in actionable_actions]
        if not actionable:
            self.logger.log_info(f"[CFSync] custom-format scores already in sync across "
                                 f"{len({(r['instance'], r['profile']) for r in rows})} profile(s) — "
                                 f"nothing to change ({stats['conflicts']} conflict(s), "
                                 f"{stats['missing']} definition-missing).")
            return stats
        if self.dry_run:
            self.logger.log_info(f"[CFSync] [dry_run] would update {len(actionable)} score(s) across "
                                 f"{len({(r['instance'], r['profile']) for r in actionable})} profile(s); PUT nothing.")
            return stats

        cf = self._cf()
        # group actionable rows by (instance, profile)
        by_target: dict = {}
        for r in actionable:
            by_target.setdefault((r["instance"], r["profile"]), []).append(r)
        snapped: set = set()
        for (inst, pname), prows in by_target.items():
            resolved = self._resolve_instance(inst)
            if resolved not in snapped:
                self._snapshot(resolved)
                snapped.add(resolved)
            try:
                profiles = self.radarr_api._make_request(resolved, "qualityprofile", fallback=[]) or []
                prof = next((p for p in profiles if p.get("name") == pname), None)
                if prof is None or prof.get("id") is None:
                    stats["not_found"] += 1
                    self.logger.log_warning(f"[CFSync] profile '{pname}' not found on {resolved} at apply "
                                            f"time (renamed/deleted since plan?) — skipped.")
                    continue
                want = {str(r["cf"]).strip().lower(): int(r["new"]) for r in prows}
                id_to_name = {v: k for k, v in (cf.cf_name_to_id(inst).items() if cf else [])}
                applied: set = set()                   # apply each CF name at most once per profile
                changed = 0
                for fi in (prof.get("formatItems") or []):
                    nm = str(fi.get("name") or "").strip().lower()
                    if not nm:                         # formatItem without a name → resolve via cf id
                        fid = fi.get("format")
                        if isinstance(fid, dict):
                            fid = fid.get("id")
                        nm = id_to_name.get(fid, "")
                    if nm in want and nm not in applied:
                        applied.add(nm)
                        if int(fi.get("score", 0) or 0) != want[nm]:
                            fi["score"] = want[nm]
                            changed += 1
                if not changed:
                    continue
                self.radarr_api._make_request(resolved, f"qualityprofile/{prof['id']}",
                                              method="PUT", payload=prof)
                stats["profiles_put"] += 1
                for r in prows:
                    if r["action"] == "fill":
                        stats["filled"] += 1
                    elif r["action"] == "overwrite":
                        stats["overwritten"] += 1
                if self.global_cache:
                    self.global_cache.set(f"radarr.custom_formats.{resolved}", None)  # invalidate read cache
            except Exception as e:
                stats["failed"] += 1
                self.logger.log_warning(f"[CFSync] profile '{pname}' on {resolved} failed: {e}")
        self.logger.log_info(f"[CFSync] applied: {stats['filled']} filled, {stats['overwritten']} "
                             f"overwritten, {stats['conflicts']} conflict(s) skipped, {stats['missing']} "
                             f"definition-missing, across {stats['profiles_put']} profile PUT(s).")
        return stats

    def _snapshot(self, resolved):
        """Stash the target's current profiles + CFs so a bad apply can be reverted."""
        try:
            profiles = self.radarr_api._make_request(resolved, "qualityprofile", fallback=[]) or []
            cfs = self.radarr_api._make_request(resolved, "customformat", fallback=[]) or []
            self.global_cache.set(f"radarr.cf_sync.snapshot.{resolved}",
                                  {"qualityprofiles": profiles, "customformats": cfs})
        except Exception as e:
            if self.logger:
                self.logger.log_warning(f"[CFSync] pre-write snapshot for {resolved} failed ({e}) — "
                                        f"a bad apply could not be auto-reverted from cache.")

    def _log_grid(self, rows):
        if not (self.logger and hasattr(self.logger, "log_grid")):
            return
        shown = [r for r in rows if r["action"] != "noop"][:60]
        if not shown:
            return
        grid = [[str(r["instance"]), str(r["profile"])[:24], str(r["cf"])[:28],
                 "-" if r["old"] is None else str(r["old"]), str(r["new"]), r["action"]] for r in shown]
        self.logger.log_grid(["Instance", "Profile", "Custom Format", "Old", "New", "Action"], grid,
                             title="Custom-format score sync plan")
