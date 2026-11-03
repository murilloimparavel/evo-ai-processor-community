"""Fail-closed boundary for the opt-in isolated dispatch mode.

The provider is a trusted in-process dependency, never request metadata. No
provider is configured by default: until the authenticated transport and CRM
history reader exist, enabling this mode denies all dispatches.
"""

import os
from dataclasses import dataclass
from typing import Protocol

from src.services.adk.runners.isolated_tree import run_isolated_tree


@dataclass(frozen=True)
class AuthorizedCustomerHistory:
    root_agent_id: str
    conversation_id: str
    turns: tuple[str, ...]
    history_incomplete: bool = False


class IsolationAuthority(Protocol):
    async def resolve(self, *, agent_id: str, external_id: str) -> AuthorizedCustomerHistory: ...
    async def authorize_tree(self, *, context: AuthorizedCustomerHistory, root_agent: object) -> bool: ...
    async def authorize_executor(self, *, context: AuthorizedCustomerHistory, root_agent: object, agent_name: str) -> bool: ...


def isolation_required(agent_id: str | None = None) -> bool:
    # Unknown values cannot silently downgrade to the shared legacy session.
    from src.services.knowledge_runtime.pilot import isolated_root_required
    return isolated_root_required(agent_id)


def _terminal_handoff(events) -> bool:
    for event in events:
        content = getattr(event, "content", None)
        for part in getattr(content, "parts", None) or []:
            result = getattr(part, "function_response", None)
            response = getattr(result, "response", None)
            if (getattr(result, "name", None) == "transfer_to_human"
                    and isinstance(response, dict)
                    and response.get("status") == "success"
                    and response.get("terminal_handoff") is True):
                return True
    return False


async def run_required_isolation(*, authority: IsolationAuthority | None, utils, agent_id: str,
                                 external_id: str, files: list | None, timeout: float):
    if authority is None or files:
        raise PermissionError("isolated dispatch unavailable")
    context = await authority.resolve(agent_id=agent_id, external_id=external_id)
    if not isinstance(context, AuthorizedCustomerHistory) or context.root_agent_id != agent_id or not context.conversation_id:
        raise PermissionError("isolated dispatch unavailable")
    root, _ = await utils.get_and_build_agent(agent_id)
    if await authority.authorize_tree(context=context, root_agent=root) is not True:
        raise PermissionError("isolated dispatch unavailable")
    prepare_tree = getattr(authority, "prepare_tree", None)
    if callable(prepare_tree):
        prepare_tree(root)
    authorize_executor = getattr(authority, "authorize_executor", None)
    if not callable(authorize_executor):
        raise PermissionError("isolated dispatch unavailable")

    async def check_executor(name):
        return await authorize_executor(context=context, root_agent=root, agent_name=name)

    async def executor_history(name):
        return await authority.history_for_agent(root, name)

    history_provider = executor_history if os.getenv("KNOWLEDGE_RESPONSE_PROVENANCE_ENABLED") == "true" else None
    import asyncio
    dispatch = await asyncio.wait_for(run_isolated_tree(root, context.turns, authorize_agent=check_executor,
                                                       history_incomplete=context.history_incomplete, history_provider=history_provider,
                                                       trusted_conversation_id=context.conversation_id), timeout=timeout)
    text = ""
    for event in dispatch.events:
        # Do not expose tool results, thoughts or earlier specialists' answers.
        if event.author != dispatch.agents[-1] or not event.is_final_response():
            continue
        content = getattr(event, "content", None)
        for part in getattr(content, "parts", None) or []:
            if part.text and not part.thought and not part.function_call and not part.function_response:
                text += part.text
    terminal_handoff = _terminal_handoff(dispatch.events)
    if not text.strip() and terminal_handoff:
        text = "Vou encaminhar você para nossa equipe, que continuará o atendimento por aqui."
    if not text.strip():
        raise RuntimeError("isolated dispatch returned no customer response")
    provenance = None
    signer = getattr(authority, "response_provenance", None)
    if callable(signer):
        provenance = await signer(dispatch.agents[-1], text.strip())
    return {"knowledge_provenance": provenance, "final_response": text.strip(), "message_history": [],
            "terminal_handoff": terminal_handoff}
