import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

def make_json_safe(obj):
    """
    Recursively converts objects to types safe for JSON serialization.
    """
    if isinstance(obj, dict):
        return {str(k): make_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_safe(i) for i in obj]
    elif isinstance(obj, (datetime, Path)):
        return str(obj)
    elif isinstance(obj, (Decimal, set)):
        return list(obj)
    elif isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    else:
        return str(obj)  # fallback to string for unknown types
