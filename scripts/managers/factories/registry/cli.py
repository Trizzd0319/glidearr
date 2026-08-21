import os
import re
from prettytable import PrettyTable

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit

# The root of the checkout THIS FILE belongs to, derived from __file__ rather than
# configured -- the whole point of the anomaly column is to catch a module that was
# imported from somewhere else, so the reference cannot come from the same config
# the suspect import would have read.
#   <root>/scripts/managers/factories/registry/cli.py  ->  five dirnames up = <root>
_CHECKOUT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

# Origin strings are built by RegistryCore.register as "<file>:<line> in <func>()".
# Splitting on ":" is wrong on Windows (drive letter), so anchor on the extension.
_ORIGIN_FILE_RE = re.compile(r"^(?P<file>.+?\.pyw?):\d+\b")


class RegistryCLI:
    def __init__(self, registry):
        self.registry = registry

    @staticmethod
    def _split_camel_case(name):
        """Split CamelCase string into components."""
        return re.findall(r'[A-Z][a-z]*|[a-z]+|\d+', name)

    def _expected_subpaths(self, klass):
        """Every module subpath a class name could legitimately imply, or ().

        An EMPTY tuple means NO RULE APPLIES -- only the four service prefixes
        below yield one. Everything else (brain managers, factories, plex,
        routing, support) has no naming convention this can check, and the
        distinction matters: 'this class is not where its name says' and 'this
        class name says nothing about where it should be' are different facts,
        and collapsing them into one bool is how a path check becomes a false
        positive on every manager it was never written to cover (P-C).

        MULTIPLE candidates because CamelCase cannot tell a directory boundary
        from an underscore: SonarrSeriesSpacePressureManager lives in
        sonarr/series/space_pressure.py, not sonarr/series/space/pressure.py.
        Both joins are legitimate, so both are accepted; a warning that fires on
        correctly-placed modules is worth less than no warning at all. The first
        candidate is the all-slashes form, used for the message.
        """
        try:
            base = (klass or "").strip()
            if base.endswith("Manager"):
                base = base[:-len("Manager")]

            for service in ["sonarr", "radarr", "trakt", "tautulli"]:
                if not base.lower().startswith(service):
                    continue
                rel = base[len(service):]
                parts = [s.lower() for s in self._split_camel_case(rel)]
                if not parts:
                    return (service,)

                # Each boundary is independently "/" or "_" -> 2^(n-1) forms.
                # n is 1-4 in practice, so this is at most eight strings.
                forms = [parts[0]]
                for part in parts[1:]:
                    forms = [f + sep + part for f in forms for sep in ("/", "_")]
                return tuple(f"{service}/{f}" for f in forms)
        except Exception as e:
            LoggerManager().log_debug(f"[RegistryCLI] _expected_subpaths error: {e}")

        return ()

    def _is_expected_path(self, name, klass, origin_path):
        """True if the origin path matches any subpath derived from the class name.

        ⚠️ Takes a bare FILE PATH, not a registry origin string -- origins are
        '<file>:<line> in <func>()' and every endswith() below would miss on one.
        Use _source_path() to extract the file half first. That signature
        mismatch is part of why this sat written-but-uncalled: there was no
        caller holding the shape it wanted.

        A False here means 'a rule applied and the path did not satisfy it'
        ONLY when _expected_subpaths() is non-empty; otherwise it means 'no
        rule'. Callers that treat False as a finding must check both.
        """
        if not origin_path or origin_path == "unknown":
            return False

        normalized = origin_path.replace("\\", "/").lower()

        for subpath in self._expected_subpaths(klass):
            if normalized.endswith(f"{subpath}.py"):
                return True
            if normalized.endswith("/__init__.py") and f"/{subpath}/" in normalized:
                return True

        return False

    @staticmethod
    def _source_path(source):
        """The FILE half of a registry origin string, or None if it carries none."""
        if not isinstance(source, str) or not source or source == "unknown":
            return None
        match = _ORIGIN_FILE_RE.match(source.strip())
        return match.group("file") if match else None

    def _path_anomaly(self, klass, source):
        """The Anomaly cell for one registry row. Empty string means 'nothing to say'.

        This replaces a test for the substring 'pycharmprojects', which was
        exactly INVERTED for this install: the canonical checkout lives under
        C:/Users/rober/PycharmProjects/glidearr, so every healthy row was
        flagged, while an import from a stale mirror elsewhere on disk -- the
        one thing the column exists to catch -- was reported clean. A detector
        that fires on everything and stays silent on the real case is worse than
        no detector, because the noise trains you to skip the column.

        Three outcomes, deliberately distinct:
          * outside this checkout  -> ❌, the stale-mirror case
          * inside, but not where the class name says -> ⚠️, weaker and advisory
          * no origin, or no naming rule for this class -> "", say nothing
        """
        path = self._source_path(source)
        if not path:
            # Absent origin is not evidence of a wrong origin.
            return ""

        try:
            resolved = os.path.normcase(os.path.abspath(path))
            root = os.path.normcase(os.path.abspath(_CHECKOUT_ROOT))
            if resolved != root and not resolved.startswith(root + os.sep):
                return f"❌ OUTSIDE this checkout ({_CHECKOUT_ROOT})"
        except Exception as e:
            LoggerManager().log_debug(f"[RegistryCLI] _path_anomaly root check failed: {e}")
            return ""

        subpaths = self._expected_subpaths(klass)
        if subpaths and not self._is_expected_path(None, klass, path):
            return f"⚠️ not under /{subpaths[0]}"

        return ""

    @LoggerManager().log_function_entry
    @timeit("print_detailed_registry")
    def print_detailed_registry(self, category="manager"):
        logger = LoggerManager()
        # get_all_verbose, not get_all: the wrapper carries `origin` and
        # `parent_name`, and the Source column is the whole point of the dump.
        entries = self.get_all_verbose(category)
        table = PrettyTable()
        table.title = f"📋 Registry Dump — Category: {category}"
        table.field_names = ["Manager", "Class", "Parent", "Source", "Anomaly"]
        table.align["Manager"] = "l"
        table.align["Class"] = "l"
        table.align["Parent"] = "l"
        table.align["Source"] = "l"
        table.align["Anomaly"] = "l"

        if not entries:
            table.add_row(["—"] * 5)
            logger.log_info(str(table))
            return

        for name, entry in entries.items():
            # Both entry shapes, read through ONE unwrap. The dict branch used to
            # read "class" and "source" -- keys RegistryCore.register has never
            # written (it writes instance/origin/parent_name), so had that branch
            # ever been taken the dump would have shown Unknown/n-a for every row.
            obj = self._unwrap(entry)
            cls = getattr(obj, "__class__", type(obj)).__name__
            if isinstance(entry, dict):
                parent = entry.get("parent_name") or getattr(obj, "parent_name", None) or "—"
                source = entry.get("origin") or getattr(obj, "_registered_from", "n/a")
            else:
                parent = getattr(obj, "parent_name", None) or "—"
                source = getattr(obj, "_registered_from", "n/a")

            table.add_row([name, cls, parent, source, self._path_anomaly(cls, source)])

        logger.log_info(str(table))

    @LoggerManager().log_function_entry
    @timeit("print_tree_view")
    def print_tree_view(self, category="manager"):
        """Print the registry as a parent -> child tree, keyed on `parent_name`.

        base_manager has called this behind the `print_registry_tree` kwarg since
        before it existed (GLD-REG-03): an AttributeError sat inside the same
        try/except as registration AND parent linking, so switching the debug
        flag on would have silently cost every manager its inherited logger,
        config, global_cache, validator and dry_run -- reported only as a
        'failed to register' warning. The call site is now outside that block;
        this is the other half.

        Self-parenting is normal here, not a bug to route around: several
        managers overwrite parent_name with their own class name in __init__
        (see GLD-SPLIT-02), so a node naming itself is treated as a root.
        """
        logger = LoggerManager()
        entries = self.get_all(category)
        if not entries:
            logger.log_info(f"🌳 Registry tree — {category}: (empty)")
            return

        parent_of = {
            name: (getattr(obj, "parent_name", None) or None)
            for name, obj in entries.items()
        }
        children = {}
        roots = []
        for name, parent in parent_of.items():
            # A parent that is absent from the registry is not a parent we can
            # draw from; the node becomes a root rather than disappearing.
            if parent and parent != name and parent in entries:
                children.setdefault(parent, []).append(name)
            else:
                roots.append(name)

        lines = [f"🌳 Registry tree — {category} ({len(entries)} registered)"]
        drawn = set()

        def _walk(node, prefix, is_last):
            connector = "└── " if is_last else "├── "
            cls = getattr(entries[node], "__class__", type(entries[node])).__name__
            label = node if node == cls else f"{node} ({cls})"
            if node in drawn:
                # Cycle guard. Mutual parenting is reachable via the same
                # overwrite-in-__init__ inconsistency, and an unguarded walk
                # would recurse until the stack gives out.
                lines.append(f"{prefix}{connector}{label} ↩ (already shown)")
                return
            drawn.add(node)
            lines.append(f"{prefix}{connector}{label}")
            kids = sorted(children.get(node, []))
            child_prefix = prefix + ("    " if is_last else "│   ")
            for i, kid in enumerate(kids):
                _walk(kid, child_prefix, i == len(kids) - 1)

        for i, root in enumerate(sorted(roots)):
            _walk(root, "", i == len(roots) - 1)

        # Anything left is inside a cycle with no root -- surfaced rather than dropped.
        orphaned = sorted(set(entries) - drawn)
        if orphaned:
            lines.append(f"⚠️ unreachable from any root (parent cycle): {', '.join(orphaned)}")

        logger.log_info("\n".join(lines))
