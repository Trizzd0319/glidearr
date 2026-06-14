"""
daemons — background worker supervision for Glidearr.

Kept import-light: ``daemon_paths`` (pure paths/constants) is imported by the
runtime Trakt cache manager, so this package must NOT eagerly pull in the
supervisor (which imports subprocess). Import the supervisor explicitly:

    from scripts.managers.factories.daemons.supervisor import EnrichDaemonSupervisor
"""
