"""Experimental session boundary for a single, already-authorized specialist.

This is a runtime primitive, not an authorization mechanism. The caller must
authenticate the conversation, retrieve customer turns from the trusted CRM,
authorize the specialist and every tool, and never pass an agent's response as
another agent's input. It is not wired into the production runners.
"""

from collections.abc import Sequence
from uuid import uuid4

from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.runners import InMemoryRunner
from google.adk.tools.agent_tool import AgentTool
from google.genai import types


async def run_isolated_specialist(agent: LlmAgent, customer_turns: Sequence[str]) -> list[Event]:
    """Run one specialist with a new session and only customer-authored text.

    A new session, memory service and artifact store are created for *each*
    invocation. No model/tool output or state from a previous invocation is
    carried over, including after a permission revocation. This intentionally
    sacrifices assistant-side history; the caller must not synthesize it from
    the shared ADK session. Arbitrary tool callbacks and customer provenance
    still require independent authorization by the caller.
    """
    if not isinstance(agent, LlmAgent) or agent.sub_agents or agent.parent_agent:
        raise ValueError("an isolated specialist must be a single LlmAgent")
    if any(isinstance(tool, AgentTool) for tool in agent.tools):
        raise ValueError("agent tools cannot run inside an isolated specialist")
    if isinstance(customer_turns, (str, bytes)) or not customer_turns or any(
        not isinstance(turn, str) or not turn.strip() for turn in customer_turns
    ):
        raise ValueError("customer turns must be a nonempty sequence of text")
    if len(customer_turns) > 100 or sum(map(len, customer_turns)) > 100_000:
        raise ValueError("customer history exceeds the isolation boundary limit")

    # Unique app and user namespaces prevent a runner/provider implementation
    # from accidentally finding another invocation by an identifier collision.
    invocation_id = uuid4().hex
    app_name = f"isolated_{invocation_id}"
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=invocation_id
    )
    try:
        for turn in customer_turns[:-1]:
            await runner.session_service.append_event(
                session,
                Event(
                    invocation_id=uuid4().hex,
                    author="user",
                    content=types.Content(role="user", parts=[types.Part(text=turn)]),
                ),
            )
        events = []
        async for event in runner.run_async(
            user_id=invocation_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=customer_turns[-1])]),
        ):
            events.append(event)
        return events
    finally:
        await runner.session_service.delete_session(
            app_name=app_name, user_id=invocation_id, session_id=session.id
        )
        await runner.close()
