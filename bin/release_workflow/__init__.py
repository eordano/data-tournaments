"""Production release workflow (Temporal) — promoted from
spikes/temporal-unity-release/ (ADR 0001 §4 step 6).

This package intentionally keeps temporalio imports OUT of __init__ and
models.py so the main dev shell (which has no temporalio) can still import
`bin.release_workflow` / `bin.release_workflow.models`. Modules that need
temporalio: workflow.py, activities.py, worker.py, client.py.
"""
