"""Native CRM knowledge retrieval contract and tool registration."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: F401 - import order for local OpenSSL
from starlette.requests import Request  # noqa: F401 - import order for local OpenSSL
from src.api import session_routes  # noqa: F401 - initialize project modules in production order

from src.services.adk.tool_builder import ToolBuilder
from src.services.adk.tools.evo_crm.search_crm_knowledge import create_search_crm_knowledge_tool


@pytest.mark.asyncio
async def test_search_uses_conversation_scope_and_returns_source_ids(monkeypatch):
    conversation_id = str(uuid4())
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{
            "id": "article-1", "title": "Endereço", "body": "Rua Um",
            "content_type": "fact", "source": "manual", "updated_at": "2026-10-24",
        }], "meta": {"total_count": 1}})

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=transport, **kwargs))
    monkeypatch.setenv("EVOAI_CRM_API_TOKEN", "test-token")
    monkeypatch.setenv("EVO_AI_CRM_URL", "http://crm.local")
    tool = create_search_crm_knowledge_tool()
    context = SimpleNamespace(state={"evoai_crm_data": {"conversation_id": conversation_id}})

    result = await tool.func(query="Qual é o endereço?", tool_context=context)

    assert result["status"] == "success"
    assert result["results"][0]["source_id"] == "article-1"
    assert seen[0].url.params["conversation_id"] == conversation_id
    assert seen[0].headers["X-Service-Token"] == "test-token"


@pytest.mark.asyncio
async def test_search_fails_closed_without_conversation(monkeypatch):
    monkeypatch.setenv("EVOAI_CRM_API_TOKEN", "test-token")
    tool = create_search_crm_knowledge_tool()
    result = await tool.func(query="endereço", tool_context=SimpleNamespace(state={}))
    assert result["status"] == "error"
    assert "Conversation context" in result["error"]


def test_tool_builder_registers_native_search_for_knowledge_enabled_agent():
    builder = ToolBuilder()
    tools = builder.build_tools({"load_knowledge": True, "knowledge_max_results": 5})
    assert "search_crm_knowledge" in {tool.func.__name__ for tool in tools}
