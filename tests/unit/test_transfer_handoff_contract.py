from src.services.adk.tools.evo_crm.transfer_to_human import _resolve_transfer_target
from src.services.adk.runners.standard_runner import (
    _event_has_successful_terminal_handoff,
)


def test_new_team_target_resolves_only_as_team():
    assert _resolve_transfer_target(
        {"target_type": "team", "target_id": "team-1"}
    ) == (None, "team-1")


def test_new_agent_target_resolves_only_as_assignee():
    assert _resolve_transfer_target(
        {"target_type": "agent", "target_id": "agent-1"}
    ) == ("agent-1", None)


def test_legacy_target_is_temporarily_supported_without_guessing():
    assert _resolve_transfer_target(
        {"transferTo": "team", "teamId": "team-legacy"}
    ) == (None, "team-legacy")
    assert _resolve_transfer_target(
        {"target_type": "team", "teamId": "wrong-shape"}
    ) == (None, None)


def test_handoff_marker_requires_successful_transfer_tool_result():
    event = {
        "content": {
            "parts": [
                {
                    "function_response": {
                        "name": "transfer_to_human",
                        "response": {
                            "status": "success",
                            "terminal_handoff": True,
                        },
                    }
                }
            ]
        }
    }
    assert _event_has_successful_terminal_handoff(event)


def test_handoff_marker_ignores_failed_or_unrelated_tool_results():
    failed = {
        "content": {
            "parts": [
                {
                    "function_response": {
                        "name": "transfer_to_human",
                        "response": {"status": "error", "terminal_handoff": True},
                    }
                }
            ]
        }
    }
    unrelated = {
        "content": {
            "parts": [
                {
                    "function_response": {
                        "name": "manage_conversation_labels",
                        "response": {
                            "status": "success",
                            "terminal_handoff": True,
                        },
                    }
                }
            ]
        }
    }
    assert not _event_has_successful_terminal_handoff(failed)
    assert not _event_has_successful_terminal_handoff(unrelated)
