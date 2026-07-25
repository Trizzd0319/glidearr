"""labels — watchability snapshot + label pipeline (pure logging, no decisions).

Append-only per-run snapshots of every scored title (score + per-signal-group
breakdown + context) land under ``<cache>/ml/snapshots/{service}/{YYYY-MM}.parquet``
so the forward-validation harness / weight refit / shadow challenger have a
temporal ground-truth join against Tautulli watch history. NOTHING here feeds a
live score, decision, or *arr write — snapshots are artifacts, labels are
computed offline by the CLI tools.
"""
