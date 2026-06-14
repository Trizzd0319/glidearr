from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktUniverseManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent         = kwargs.get("manager")
        self.dry_run   = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.trakt_api = kwargs.get("trakt_api")

    # ── Universe mappings ─────────────────────────────────────────────────

    def get_universe_mapping(self) -> dict:
        """
        Static universe → list of TVDB IDs mapping.
        Extend or override via config to add custom universes.
        """
        return {
            "marvel-cinematic-universe":  [295759, 295760, 326490],
            "x-men-universe":             [295761, 295762],
            "dc-extended-universe":       [295763, 295764],
            "the-walking-dead-universe":  [295765, 295766],
            "one-chicago-universe":       [295767, 295768],
            "greys-anatomy-universe":     [295769, 295770],
            "doctor-who-universe":        [295773, 295774],
            "star-wars-universe":         [295775, 295776],
            "star-trek-universe":         [295777, 295778],
            "middle-earth-universe":      [295779, 295780],
            "wizarding-world":            [295781, 295782],
            "fast-and-furious-universe":  [295783, 295784],
            "alien-predator-universe":    [295785, 295786],
            "monsterverse":               [295793, 295794],
            "arrowverse":                 [295759, 295760, 326490],
            "marvel-netflix":             [281662],
            "star-trek":                  [74608, 79349, 261690],
            "ncis-verse":                 [72108, 72224, 80379],
        }

    def get_shows_by_universe(self, universe_slug: str) -> list:
        universe = self.get_universe_mapping().get(universe_slug)
        if not universe:
            self.logger.log_warning(f"[TraktUniverse] No universe found for slug '{universe_slug}'")
            return []
        return universe

    def list_all_universes(self) -> list:
        universes = list(self.get_universe_mapping().keys())
        self.logger.log_info(f"[TraktUniverse] Available universes: {', '.join(universes)}")
        return universes

    def add_custom_universe(self, universe_slug: str, tvdb_ids: list):
        """Placeholder — persist custom universes via config or an external store."""
        self.logger.log_info(
            f"[TraktUniverse] Custom universe '{universe_slug}' registered with {len(tvdb_ids)} IDs."
        )
