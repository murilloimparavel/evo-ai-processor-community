"""Experimental isolated dispatch for a trusted LlmAgent tree.

This module deliberately does not enable itself in the existing CRM runners.
Callers must authenticate customer turns, the agent tree and every tool before
use. The dispatcher only establishes a fresh ADK session per handoff; it is not
an access-control decision and is not a streaming/attachment adapter.
"""

from dataclasses import dataclass
from typing import Sequence
from uuid import UUID, uuid4

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event
from google.adk.runners import InMemoryRunner
from google.adk.tools.agent_tool import AgentTool
from google.genai import types


@dataclass
class IsolatedDispatch:
    events: list[Event]
    agents: list[str]


def transfer_to_agent(agent_name: str) -> str:
    """Transfer the conversation to a named adjacent specialist."""
    # The dispatcher consumes the model's function call *before* ADK can invoke
    # this tool. If that invariant changes, never silently fall back to a shared
    # session or return model-controlled text to another agent.
    raise RuntimeError("handoff was not intercepted")


def _tree(root: LlmAgent):
    if not isinstance(root, LlmAgent) or root.parent_agent is not None:
        raise ValueError("root must be an LlmAgent without a parent")
    agents = {}
    neighbours = {}

    def visit(agent, parent=None):
        if not isinstance(agent, LlmAgent) or agent.name in agents:
            raise ValueError("agent tree must have unique LlmAgent names")
        if any(isinstance(tool, AgentTool) for tool in agent.tools):
            raise ValueError("AgentTool is not supported by isolated dispatch")
        if any(getattr(tool, "name", None) == "transfer_to_agent" or
               getattr(tool, "__name__", None) == "transfer_to_agent" for tool in agent.tools):
            raise ValueError("a custom transfer_to_agent tool is not supported")
        agents[agent.name] = agent
        neighbours[agent.name] = ([parent.name] if parent else []) + [a.name for a in agent.sub_agents]
        for child in agent.sub_agents:
            visit(child, agent)

    visit(root)
    return agents, neighbours


async def run_isolated_tree(root: LlmAgent, customer_turns: Sequence[str], *, max_handoffs: int = 8,
                            authorize_agent=None, history_incomplete: bool = False, history_provider=None, trusted_conversation_id: str | None = None) -> IsolatedDispatch:
    """Dispatch native agent handoffs using a fresh isolated session per agent.

    Only verified customer-authored turns may be supplied. Tool results and
    model text are never used as the next agent's input. Revocation and tool
    authorization are the caller's responsibility, checked on each invocation.
    """
    agents, neighbours = _tree(root)
    # This value comes from the verified authority, never request metadata.
    state = {}
    if trusted_conversation_id is not None:
        try:
            state["conversation_id"] = str(UUID(trusted_conversation_id))
        except (ValueError, TypeError, AttributeError):
            raise PermissionError("invalid trusted conversation") from None
    if isinstance(customer_turns, (str, bytes)) or not customer_turns or any(
        not isinstance(turn, str) or not turn.strip() for turn in customer_turns
    ):
        raise ValueError("customer turns must be a nonempty sequence of text")
    if len(customer_turns) > 100 or sum(map(len, customer_turns)) > 100_000 or not 0 <= max_handoffs <= 32:
        raise ValueError("isolated dispatch limit exceeded")

    model_calls = 0

    async def enforce_request_model_budget(callback_context, llm_request):
        nonlocal model_calls
        model_calls += 1
        if model_calls > 12:
            raise PermissionError("isolated request model call budget exceeded")

    current = root.name
    events = []
    visited = []
    while True:
        original = agents[current]
        if authorize_agent is not None and await authorize_agent(current) is not True:
            raise PermissionError("isolated dispatch unavailable")
        typed_history = None
        if history_provider is not None:
            typed_history = await history_provider(current)
            if (not isinstance(typed_history, list) or not typed_history or len(typed_history) > 100
                    or any(not isinstance(t, dict) or t.get("role") not in {"user", "assistant"}
                           or not isinstance(t.get("text"), str) or not t["text"].strip() for t in typed_history)
                    or typed_history[-1]["role"] != "user" or sum(len(t["text"]) for t in typed_history) > 100_000):
                raise PermissionError("invalid authenticated history")
        adjacent = neighbours[current]
        directive = "\nAvailable transfer targets: " + ", ".join(adjacent) if adjacent else ""
        instruction = original.instruction
        if not isinstance(instruction, str):
            raise ValueError("dynamic agent instructions are not supported")
        if type(history_incomplete) is not bool:
            raise ValueError("invalid history completeness indicator")
        if history_incomplete:
            instruction += (
                "\nAviso do sistema: respostas anteriores do atendimento não estão disponíveis neste contexto. "
                "Não suponha o conteúdo delas; se o cliente fizer referência a uma opção ou informação anterior "
                "que não aparece nas mensagens recebidas, peça que ele a identifique."
            )
        callbacks = original.before_model_callback
        callbacks = list(callbacks) if isinstance(callbacks, list) else ([callbacks] if callbacks else [])
        detached = original.model_copy(update={
            "before_model_callback": [enforce_request_model_budget, *callbacks],
            "parent_agent": None,
            "sub_agents": [],
            "instruction": instruction + directive,
            "tools": [*original.tools, transfer_to_agent] if adjacent else list(original.tools),
        })
        invocation = uuid4().hex
        app_name = f"isolated_{invocation}"
        runner = InMemoryRunner(agent=detached, app_name=app_name)
        session = await runner.session_service.create_session(app_name=app_name, user_id=invocation, state=state.copy())
        target = None
        visited.append(current)
        try:
            history = typed_history or [{"role": "user", "text": turn} for turn in customer_turns]
            for turn in history[:-1]:
                assistant = turn["role"] == "assistant"
                await runner.session_service.append_event(session, Event(
                    invocation_id=uuid4().hex, author=current if assistant else "user",
                    content=types.Content(role="model" if assistant else "user", parts=[types.Part(text=turn["text"])]),
                ))
            stream = runner.run_async(
                user_id=invocation, session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=history[-1]["text"])]),
                run_config=RunConfig(max_llm_calls=12),
            )
            try:
                async for event in stream:
                    calls = [p.function_call for p in (event.content.parts if event.content and event.content.parts else [])
                             if p.function_call and p.function_call.name == "transfer_to_agent"]
                    if calls:
                        if len(calls) != 1 or len(event.content.parts) != 1:
                            raise ValueError("invalid handoff event")
                        args = calls[0].args or {}
                        target = args.get("agent_name")
                        if set(args) != {"agent_name"} or not isinstance(target, str) or target not in adjacent:
                            raise ValueError("handoff target is not an adjacent agent")
                        break
                    events.append(event)
            finally:
                await stream.aclose()
        finally:
            await runner.session_service.delete_session(app_name=app_name, user_id=invocation, session_id=session.id)
            await runner.close()
        if target is None:
            return IsolatedDispatch(events=events, agents=visited)
        if len(visited) > max_handoffs:
            raise ValueError("handoff limit exceeded")
        current = target
