from types import SimpleNamespace

from src.services.adk.runners.standard_runner import (
    StandardRunner,
    _apply_inactivity_action_reply,
    _should_persist_agent_event_to_memory,
)


def test_inactivity_action_disables_tools_throughout_agent_tree():
    specialist = SimpleNamespace(tools=[object()], sub_agents=[])
    root = SimpleNamespace(tools=[object()], sub_agents=[specialist])

    StandardRunner._disable_agent_tools(root)

    assert root.tools == []
    assert specialist.tools == []


def test_inactivity_reply_is_neutral_and_preserves_handoff_and_normal_messages():
    generated_reply = "Vou consultar opções e já retorno."

    assert _apply_inactivity_action_reply(generated_reply, None) == generated_reply
    assert _apply_inactivity_action_reply(
        generated_reply, {"evoai_crm_event": "message_created"}
    ) == generated_reply
    assert _apply_inactivity_action_reply(
        generated_reply, {"evoai_crm_event": "inactivity_action"}
    ) == "Posso ajudar em mais alguma coisa?"
    assert _apply_inactivity_action_reply(
        "O atendimento foi encaminhado para uma pessoa da equipe.",
        {
            "evoai_crm_event": "inactivity_action",
        },
        terminal_handoff=True,
    ) == "O atendimento foi encaminhado para uma pessoa da equipe."


def test_synthetic_inactivity_events_are_not_saved_to_shared_memory():
    assert not _should_persist_agent_event_to_memory(
        load_memory_enabled=True, is_inactivity_action=True
    )
    assert _should_persist_agent_event_to_memory(
        load_memory_enabled=True, is_inactivity_action=False
    )
    assert not _should_persist_agent_event_to_memory(
        load_memory_enabled=False, is_inactivity_action=False
    )
