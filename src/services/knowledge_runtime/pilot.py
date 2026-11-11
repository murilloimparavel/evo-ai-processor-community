"""Deployment-owned pilot selection; never infer this from request metadata."""
import os
from uuid import UUID


def _canonical_uuid(value):
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid knowledge pilot configuration") from None
    if value != canonical:
        raise ValueError("invalid knowledge pilot configuration")
    return canonical


def pilot_root_selected(agent_id):
    raw = os.getenv("KNOWLEDGE_RUNTIME_PILOT_ROOT_IDS")
    if raw is None:
        raise ValueError("knowledge pilot roots must be explicitly configured")
    values = [item.strip() for item in raw.split(",")]
    if not 1 <= len(values) <= 100:
        raise ValueError("invalid knowledge pilot configuration")
    roots = {_canonical_uuid(item) for item in values}
    if len(roots) != len(values):
        raise ValueError("invalid knowledge pilot configuration")
    return _canonical_uuid(agent_id) in roots


def bound_grants_required(agent_id):
    enabled = os.getenv("KNOWLEDGE_A2A_BOUND_GRANTS_ENABLED", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("invalid bound-grant configuration")
    return enabled == "true" and pilot_root_selected(agent_id)


def isolated_root_required(agent_id):
    mode = os.getenv("KNOWLEDGE_RUNTIME_ISOLATION_MODE", "off")
    if mode not in {"off", "required"}:
        raise ValueError("invalid knowledge isolation mode")
    return mode == "required" and pilot_root_selected(agent_id)
