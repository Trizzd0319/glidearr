import re

def normalize_component_name(name: str) -> str:
    """
    Convert 'SonarrEpisodeHistoryManager' → 'sonarr.episodes.history'
    """
    # Remove common suffixes
    cleaned = re.sub(r"(Manager|Handler|Helper)$", "", name)

    # Convert CamelCase to dot-separated
    parts = re.findall(r'[A-Z][a-z0-9]*', cleaned)
    return '.'.join(part.lower() for part in parts)
