"""Django system checks for the indexing app."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, register

from .qdrant import healthcheck


@register()
def qdrant_connection_check(app_configs, **kwargs):  # pragma: no cover - executed via Django checks
    """Verify the configured Qdrant instance is reachable."""

    results: list[Error] = []
    if getattr(settings, "INDEXING_SKIP_QDRANT_HEALTHCHECK", False):
        return results
    status = healthcheck()
    if not status.get("ok"):
        detail = status.get("detail") or "Qdrant health endpoint reported a failure."
        version = status.get("version")
        commit = status.get("commit")
        if version:
            extra = f"version={version}"
            if commit:
                extra = f"{extra}, commit={commit}"
            detail = f"{detail} ({extra})"
        results.append(
            Error(
                f"Qdrant healthcheck failed: {detail}",
                hint="Ensure Qdrant is running and the QDRANT_URL/QDRANT_API_KEY settings are correct.",
                obj="indexing.qdrant",
                id="indexing.E001",
            )
        )
    return results
