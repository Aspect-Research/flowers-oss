"""Optional adapter templates for flowers.

These optional adapters (Postgres store, E2B sandbox, Langfuse telemetry, Brave search) are
NOT wired into the default ``build_app``. They satisfy the same seam Protocols as the
wired defaults; to use one, construct it in place of the default in ``flowers/app.py``. They are kept
as importable, lint-clean reference templates, not maintained as a supported surface.
"""
