from types import SimpleNamespace

from src.services.adk.runners.standard_runner import StandardRunner


def test_followup_tool_disabling_clears_tools_from_entire_agent_tree():
    specialist = SimpleNamespace(tools=[object()], sub_agents=[])
    root = SimpleNamespace(tools=[object()], sub_agents=[specialist])

    StandardRunner._disable_agent_tools(root)

    assert root.tools == []
    assert specialist.tools == []
