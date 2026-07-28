"""labels — watchability snapshot + label pipeline (pure logging, no decisions).

Append-only per-run snapshots of every scored title (score + per-signal-group
breakdown + context) land under ``<cache>/ml/snapshots/{service}/{YYYY-MM}.parquet``
so the forward-validation harness / weight refit / shadow challenger have a
temporal ground-truth join against Tautulli watch history. NOTHING here feeds a
live score, decision, or *arr write — snapshots are artifacts, labels are
computed offline by the CLI tools.

``first_run`` is the one exception to "offline only", and a deliberate one: on a
FRESH install the store is empty and stays evidence-free for a whole horizon, so
it fires ``ml_backfill_snapshots`` (the truncated-replay CLI) exactly once, at
the end of the first run, to turn the Tautulli history the household already
owns into labels. Still no decision, still no *arr write — it only appends the
same artifacts, and it is one-shot, bounded, config-gated and fault-isolated.
"""
