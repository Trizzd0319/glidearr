from tabulate import tabulate

from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class ComponentManagerMixin:

    # ── load_components ──────────────────────────────────────────────────────

    def load_components(self, component_map, registry_prefix: str,
                        api_kwarg_name: str = "api", **kwargs):
        """
        Dynamically load components and attach them as attributes.
        Emits ONE summary line per manager:

            [ManagerName] components (N/N): name1✅  name2✅  name3❌
        """
        self.load_summary = {}
        service = self.__class__.__name__

        excluded_keys = {
            api_kwarg_name, "api", "manager", "instance_manager",
            "logger", "config", "global_cache",
            "validator", "registry", "metrics",
        }
        cleaned_kwargs = {k: v for k, v in kwargs.items() if k not in excluded_keys}
        api_value      = getattr(self, api_kwarg_name, None) or getattr(self, "api", None)

        injected = {
            api_kwarg_name:     api_value,
            "manager":          self,
            "instance_manager": getattr(self, "instance_manager", None),
            "logger":           getattr(self, "logger",           None),
            "config":           getattr(self, "config",           None),
            "global_cache":     getattr(self, "global_cache",     None),
            "validator":        getattr(self, "validator",        None),
            "registry":         getattr(self, "registry",         None),
            "metrics":          getattr(self, "metrics",          None),
        }

        rows: list[tuple[str, str]] = []
        for name, cls in component_map.items():
            try:
                instance = cls(**injected, **cleaned_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"{registry_prefix}.{name}_initialized", True)
                self.load_summary[name] = "✅"
                rows.append((name, "✅"))
            except Exception as e:
                self.registry.set_flag(f"{registry_prefix}.{name}_initialized", False)
                self.load_summary[name] = f"❌"
                rows.append((name, "❌"))
                self.logger.log_error(f"[{service}] ❌ {name} ({cls.__name__}): {e}")

        n_ok    = sum(1 for _, s in rows if s == "✅")
        n_total = len(rows)
        parts   = "  ".join(f"{n}{s}" for n, s in rows)
        self.logger.log_debug(f"[{service}] {n_ok}/{n_total}: {parts}")

        return {name: getattr(self, name) for name in component_map if hasattr(self, name)}

    # ── log_filtered_component_summary ───────────────────────────────────────

    def log_filtered_component_summary(
        self,
        service_name: str,
        component_label: str,
        critical_components,
        noncritical_components,
        all_critical_loaded: bool,
    ):
        """
        Silently accumulate status for the end-of-run table.
        No inline log line — the parent manager's prepare() summary covers it.
        """
        if not hasattr(self.logger, "_component_summary_rows"):
            self.logger._component_summary_rows = []
        failed_list = (
            [
                name for name in sorted(
                    set(critical_components) | set(noncritical_components)
                )
                if not str(self.load_summary.get(name, "")).startswith("✅")
            ]
            if not all_critical_loaded else []
        )
        self.logger._component_summary_rows.append(
            [service_name, component_label,
             "✅" if all_critical_loaded else "❌",
             ", ".join(failed_list)]
        )

    # ── log_final_run_summary ────────────────────────────────────────────────

    def log_final_run_summary(self):
        """Print a compact table of all manager statuses at end of run."""
        if not hasattr(self.logger, "_component_summary_rows"):
            return
        rows = self.logger._component_summary_rows
        if rows:
            table = tabulate(
                rows,
                headers=["Service", "Manager", "Status", "Failures"],
                tablefmt="simple",
            )
            self.logger.log_info(f"\n📋 Component summary:\n{table}")
        del self.logger._component_summary_rows

    # ── register ─────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("register")
    def register(self, parent_name=None, **kwargs):
        """Register this component in the manager registry."""
        resolved = parent_name
        if resolved is None:
            if hasattr(self, "manager") and self.manager:
                resolved = self.manager.__class__.__name__
            elif hasattr(self, "parent_name"):
                resolved = self.parent_name
        try:
            self.registry.register("manager", self.name, self, parent_name=resolved)
        except Exception as e:
            self.logger.log_warning(f"[{self.name}] ⚠️ registry register failed: {e}")
