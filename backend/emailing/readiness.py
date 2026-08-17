"""Readiness checks for email generation, delivery, and reply detection.

All outbound and inbound email goes through Microsoft Graph. The
SMTP/IMAP readiness paths have been removed; ``provider_type()``
still accepts the legacy ``"smtp"`` / ``"imap"`` strings and coerces
them to ``"graph"`` for backward compat.
"""

from __future__ import annotations

from typing import Any


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (int, float)):
        return value <= 0
    return False


def _missing_fields(settings: Any, required_fields: list[tuple[str, str]]) -> list[str]:
    missing: list[str] = []
    for attr_name, label in required_fields:
        if _is_missing(getattr(settings, attr_name, "")):
            missing.append(label)
    return missing


def provider_type(settings: Any) -> str:
    """Return the active email provider. Only ``"graph"`` is supported.

    Legacy values (``"smtp"``, ``"imap"``, empty string) are coerced to
    ``"graph"`` so old settings / DB rows keep working without a
    migration.
    """
    raw = str(getattr(settings, "email_provider_type", "graph") or "graph").strip().lower()
    if raw in {"", "smtp", "imap"}:
        return "graph"
    return raw


def graph_readiness(settings: Any) -> dict[str, Any]:
    missing = _missing_fields(
        settings,
        [
            ("graph_tenant_id", "GRAPH_TENANT_ID"),
            ("graph_client_id", "GRAPH_CLIENT_ID"),
            ("graph_client_secret", "GRAPH_CLIENT_SECRET"),
            ("graph_mailbox_upn", "GRAPH_MAILBOX_UPN"),
        ],
    )
    return {
        "ready": not missing,
        "missing_fields": missing,
        "message": (
            "Microsoft Graph is not configured. Missing: " + ", ".join(missing)
            if missing
            else "Microsoft Graph is configured."
        ),
    }


def graph_test_readiness(settings: Any) -> dict[str, Any]:
    configured = graph_readiness(settings)
    tested_at = str(getattr(settings, "graph_last_test_at", "") or "").strip()
    ready = bool(configured["ready"] and tested_at)
    return {
        "ready": ready,
        "tested_at": tested_at,
        "message": (
            "Microsoft Graph connection has not been verified yet. Please test Graph in Settings before enabling auto send."
            if configured["ready"] and not tested_at
            else "Microsoft Graph connection verified."
        ),
    }


# ---------------------------------------------------------------------------
# Provider-aware dispatch. All three flavors now resolve to Graph.
# ---------------------------------------------------------------------------

def outbound_readiness(settings: Any) -> dict[str, Any]:
    return graph_readiness(settings)


def inbound_readiness(settings: Any) -> dict[str, Any]:
    # Graph covers send AND receive with the same shared mailbox, so
    # the inbound check is just the graph config itself.
    return graph_readiness(settings)


def outbound_test_readiness(settings: Any) -> dict[str, Any]:
    return graph_test_readiness(settings)


def inbound_test_readiness(settings: Any) -> dict[str, Any]:
    return graph_test_readiness(settings)


def ensure_graph_ready(settings: Any) -> None:
    status = graph_readiness(settings)
    if not status["ready"]:
        raise ValueError(str(status["message"]))


def ensure_graph_tested(settings: Any) -> None:
    ensure_graph_ready(settings)
    status = graph_test_readiness(settings)
    if not status["ready"]:
        raise ValueError(str(status["message"]))


def ensure_outbound_ready(settings: Any) -> None:
    status = outbound_readiness(settings)
    if not status["ready"]:
        raise ValueError(str(status["message"]))


def ensure_inbound_ready(settings: Any) -> None:
    status = inbound_readiness(settings)
    if not status["ready"]:
        raise ValueError(str(status["message"]))


def ensure_outbound_tested(settings: Any) -> None:
    """Raise unless Graph is configured AND verified."""
    status = outbound_test_readiness(settings)
    if not status["ready"]:
        config_status = outbound_readiness(settings)
        raise ValueError(str(config_status["message"] if not config_status["ready"] else status["message"]))


def ensure_inbound_tested(settings: Any) -> None:
    """Raise unless Graph is configured AND verified (for reply detection)."""
    status = inbound_test_readiness(settings)
    if not status["ready"]:
        config_status = inbound_readiness(settings)
        raise ValueError(str(config_status["message"] if not config_status["ready"] else status["message"]))


# ---------------------------------------------------------------------------
# Backward-compat shims. Older callers used SMTP/IMAP-specific helpers
# and we want to keep them working without touching every call site.
# ---------------------------------------------------------------------------

def smtp_readiness(settings: Any) -> dict[str, Any]:
    return graph_readiness(settings)


def imap_readiness(settings: Any) -> dict[str, Any]:
    return graph_readiness(settings)


def smtp_test_readiness(settings: Any) -> dict[str, Any]:
    return graph_test_readiness(settings)


def imap_test_readiness(settings: Any) -> dict[str, Any]:
    return graph_test_readiness(settings)


def ensure_smtp_ready(settings: Any) -> None:
    ensure_graph_ready(settings)


def ensure_imap_ready(settings: Any) -> None:
    ensure_graph_ready(settings)


def ensure_smtp_tested(settings: Any) -> None:
    ensure_graph_tested(settings)


def ensure_imap_tested(settings: Any) -> None:
    ensure_graph_tested(settings)


def ensure_outbound_tested(settings: Any) -> None:  # noqa: F811 — redefined below
    ensure_graph_tested(settings)
