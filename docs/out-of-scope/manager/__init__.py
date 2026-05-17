"""Runtime Manager — HTTP layer above OpenSandbox.

Exposes higher-level operations than OpenSandbox's raw container lifecycle:
DB migrations, app start, health probes, test runs, environment reset,
warm pool leasing, and sidecar attach.

Consumers (Temporal activities, ad-hoc CLI calls) interact with the Manager
via REST. The Manager translates each operation into one or more execd
calls against the underlying sandbox, hiding project-specific knowledge in
per-project handlers (`projects.py`).
"""
