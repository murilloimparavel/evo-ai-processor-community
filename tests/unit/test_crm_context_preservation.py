"""Context regressions using the pinned ADK without calling a model or a database."""
from types import SimpleNamespace

import pytest
from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.flows.llm_flows import contents
from google.adk.models.llm_request import LlmRequest
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from src.services.adk.runners.runner_utils import RunnerUtils


@pytest.mark.asyncio
async def test_turn_initialization_preserves_known_facts_and_initializes_missing_fields():
    service = InMemorySessionService()
    session = await service.create_session(
        app_name="reservations", user_id="contact", session_id="conversation",
        state={"check_in": "20/11", "check_out": "23/11", "children": 0, "confirmed": False},
    )
    # This method does not need a database or AgentBuilder.
    utils = RunnerUtils.__new__(RunnerUtils)
    await utils.setup_session_state(
        service, session, "!!",
        ["check_in", "check_out", "children", "confirmed", "guests", "user_input"],
    )
    stored = await service.get_session(
        app_name="reservations", user_id="contact", session_id="conversation",
    )
    assert stored.state["check_in"] == "20/11"
    assert stored.state["check_out"] == "23/11"
    assert stored.state["children"] == 0
    assert stored.state["confirmed"] is False
    assert stored.state["guests"] == ""
    assert stored.state["user_input"] == "!!"
    assert not any("check_in" in event.actions.state_delta for event in stored.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name,branch", [
    ("orchestrator", None), ("reservations", "orchestrator.reservations"),
])
async def test_synced_dates_survive_later_incomplete_batch_and_handoff(agent_name, branch):
    agent = LlmAgent(name=agent_name, model="unused")
    events = [
        Event(author="user", invocation_id="crm_message_dates", timestamp=1,
              content=Content(role="user", parts=[Part(text="Entrada 20/11 e saída 23/11")]),
              custom_metadata={"crm_message_id": "dates", "speaker": "customer"}),
        Event(author="user", invocation_id="batch", timestamp=2,
              content=Content(role="user", parts=[Part(text="5 adultos. Quais os valores?")])),
    ]
    invocation = SimpleNamespace(agent=agent, branch=branch, session=SimpleNamespace(events=events))
    request = LlmRequest()
    async for _ in contents.request_processor.run_async(invocation, request):
        pass
    texts = [part.text for item in request.contents for part in item.parts if part.text]
    assert texts == ["Entrada 20/11 e saída 23/11", "5 adultos. Quais os valores?"]
