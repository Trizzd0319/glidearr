"""challenger — the shadow GBT watch-probability model (observe-only).

An optional LightGBM challenger trained offline from the Stage-1 labeled
snapshots. At runtime it ONLY logs divergence from the hand-weighted score and
stamps ``challenger_p`` into the snapshot rows — it never feeds a score, a
decision, or an *arr write. Config-gated DEFAULT OFF; degrades to a one-line
no-op when lightgbm is not installed.
"""
