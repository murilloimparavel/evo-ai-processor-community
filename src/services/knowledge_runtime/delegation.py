"""Fail-closed validation of a built ADK tree against server-owned agents.

This is not an authentication mechanism. The caller must supply a verified
grant, a scope-authorized server lookup and an ADK tree built from that same
server. Never obtain the lookup, root ID or edges from A2A metadata or a model.
"""

from uuid import UUID


class InvalidDelegation(ValueError):
    pass


def _id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise InvalidDelegation("invalid_delegation") from None


async def verify_delegation_tree(*, grant, root, lookup, max_agents=100):
    """Return server-verified ADK-name→agent-ID mapping; deny ambiguous graphs.

    `lookup(account_scope, id)` must itself enforce the grant's account, and
    return freshly loaded server records. This function does not make the
    Community Core's currently absent tenant guarantee. Grant freshness and
    replay must also be validated by the caller immediately before use.
    """
    if not getattr(grant, "account_scope", None) or not callable(lookup):
        raise InvalidDelegation("invalid_delegation")
    names = {}
    ids = set()

    async def visit(agent_id, node):
        agent_id = _id(agent_id)
        if agent_id in ids or len(ids) >= max_agents:
            raise InvalidDelegation("invalid_delegation")
        record = await lookup(grant.account_scope, agent_id)
        if record is None or _id(getattr(record, "id", None)) != agent_id:
            raise InvalidDelegation("invalid_delegation")
        name = getattr(record, "name", None)
        if not isinstance(name, str) or not name or name != getattr(node, "name", None) or name in names:
            raise InvalidDelegation("invalid_delegation")
        config = getattr(record, "config", None)
        if not isinstance(config, dict):
            raise InvalidDelegation("invalid_delegation")
        children = config.get("sub_agents") or []
        if not isinstance(children, list):
            raise InvalidDelegation("invalid_delegation")
        children_ids = [_id(child) for child in children]
        if len(children_ids) != len(set(children_ids)):
            raise InvalidDelegation("invalid_delegation")
        built_children = getattr(node, "sub_agents", None)
        if not isinstance(built_children, list) or len(children_ids) != len(built_children):
            raise InvalidDelegation("invalid_delegation")
        ids.add(agent_id)
        names[name] = agent_id
        for child_id, child in zip(children_ids, built_children):
            await visit(child_id, child)

    await visit(grant.root_agent_id, root)
    return names
