"""Attach dynamically built specialists using the ADK's bidirectional tree."""


def attach_sub_agents(parent, children):
    """Match constructor wiring when children are assigned after construction."""
    children = list(children)
    if len({child.name for child in children}) != len(children):
        raise ValueError('Duplicate subagent names are not allowed')
    for child in children:
        if child is parent:
            raise ValueError('An agent cannot be its own subagent')
        if child.parent_agent is not None and child.parent_agent is not parent:
            raise ValueError('A subagent already belongs to another parent')
    parent.sub_agents = children
    for child in children:
        child.parent_agent = parent
