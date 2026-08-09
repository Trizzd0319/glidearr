def split_components(all_components: dict, critical_keys: set, parent_name_match: str, logger, logger_context="ComponentSplitter", init_kwargs=None):
    """
    Split a component map into critical and noncritical groups.

    Args:
        all_components (dict): {name: class} map of all known components.
        critical_keys (set): names considered critical.
        parent_name_match (str): RETAINED FOR SIGNATURE COMPATIBILITY — no longer used.
        logger (LoggerManager): logger instance.
        logger_context (str): label for log context.
        init_kwargs (dict): RETAINED FOR SIGNATURE COMPATIBILITY — no longer used.

    Returns:
        (critical_dict, noncritical_dict): separated dictionaries.

    THE ``parent_name`` MATCH IS GONE. It used to decide noncritical membership by
    CONSTRUCTING a throwaway instance of every candidate and comparing its
    ``parent_name`` for equality. Measured on a live run, that dropped FOUR components
    that should have loaded, and zero that should not:

        [SonarrEpisodesManager]  expected 'SonarrEpisodesManager'
            file, monitoring, sharding   -> all reported 'SonarrEpisodes'
        [SonarrValidatorManager] expected 'SonarrServices'
            api_factory                  -> reported 'SonarrValidatorManager'

    Both are unwinnable. The episodes children report the value
    ``ManagerAttributionMixin`` INFERS FROM THEIR FILE PATH (services/sonarr/episodes/
    -> "Sonarr" + "Episodes"), which overwrites the class attribute — ``monitoring.py``
    literally declares ``parent_name = "SonarrEpisodesMonitoringManager"`` and the
    instance still reports "SonarrEpisodes". The validator pair is exactly INVERTED: the
    parent expects a name no child has, the child reports the parent's class name.

    Five mechanisms write ``parent_name`` with no precedence contract (class attribute /
    BaseManager kwarg overwrite / path inference / explicit assignment / register()'s
    preference for ``manager.__class__.__name__``), and one of them is THE FILE'S
    LOCATION ON DISK. An equality test over a value a directory move can change is not
    fixable by convention.

    The drop was also SILENT — a mismatch produced no output at all, which is why
    ``episodes/__init__.py`` grew a hand-written load for ``sharding``: nothing reported
    that it had gone, and ``file`` and ``monitoring`` were never noticed at all.

    Closes GLD-SPLIT-01 (throwaway construction), GLD-SPLIT-02 (accidental filter),
    GLD-STO-04 (is it wanted?), GLD-REP-02 (five semantics) and GLD-REP-03 (init_kwargs
    shaped by introspection rather than by what components need).

    The two parameters are kept so the nine call sites need no edit; drop them in a
    follow-up once the callers are cleaned up.
    """
    critical    = {k: v for k, v in all_components.items() if k in critical_keys}
    noncritical = {k: v for k, v in all_components.items() if k not in critical_keys}

    if noncritical:
        logger.log_debug(
            f"[{logger_context}] {len(critical)} critical, {len(noncritical)} noncritical: "
            f"{', '.join(sorted(noncritical))}"
        )
    return critical, noncritical
