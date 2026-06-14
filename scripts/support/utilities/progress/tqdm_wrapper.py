# support/utilities/tqdm_wrapper.py
import sys

from tqdm import tqdm as base_tqdm


def tqdm(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    return base_tqdm(*args, **kwargs)
