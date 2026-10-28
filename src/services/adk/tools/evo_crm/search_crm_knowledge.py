"""Native retrieval of published CRM knowledge for agent responses."""

import os
import uuid
from typing import Any, Dict, Optional

import httpx
from google.adk.tools import FunctionTool, ToolContext

from src.utils.logger import setup_logger
from src.services.adk.tools.evo_crm.transfer_to_human import _extract_conversation_id_from_metadata

logger = setup_logger(__name__)


def create_search_crm_knowledge_tool(default_limit: int = 5) -> FunctionTool:
    """Build a tool scoped by the CRM service token to published articles only."""

    async def search_crm_knowledge(
        query: str,
        limit: Optional[int] = None,
        tool_context: Optional[ToolContext] = None,
    ) -> Dict[str, Any]:
        """Search confirmed business facts, rules, FAQs and property descriptions.

        Use this for stable information. For prices, availability and current
        reservation status, call the relevant live system tool instead.

        Args:
            query: Specific factual question or search terms.
            limit: Optional maximum number of articles (1 to 10).
            tool_context: Runtime context provided by ADK.
        """
        query = (query or "").strip()
        if not query:
            return {"status": "error", "error": "A search query is required."}
        if len(query) > 500:
            return {"status": "error", "error": "Search query is too long."}
        try:
            effective_limit = int(limit if limit is not None else default_limit)
        except (ValueError, TypeError):
            return {"status": "error", "error": "limit must be an integer."}
        if not 1 <= effective_limit <= 10:
            return {"status": "error", "error": "limit must be between 1 and 10."}

        conversation_id = _extract_conversation_id_from_metadata(tool_context)
        try:
            conversation_id = str(uuid.UUID(conversation_id or ""))
        except ValueError:
            return {"status": "error", "error": "Conversation context is unavailable."}

        token = os.getenv("EVOAI_CRM_API_TOKEN")
        if not token:
            logger.warning("CRM knowledge search unavailable: service token missing")
            return {"status": "error", "error": "Knowledge search is unavailable."}

        base_url = os.getenv("EVO_AI_CRM_URL", "http://localhost:3000").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
                response = await client.get(
                    f"{base_url}/api/v1/knowledge_entries/search",
                    headers={"X-Service-Token": token, "Accept": "application/json"},
                    params={"q": query, "limit": effective_limit, "conversation_id": conversation_id},
                )
            response.raise_for_status()
            payload = response.json()
            articles = payload.get("data") or []
            if not isinstance(articles, list):
                raise ValueError("Invalid knowledge search response")
            results = [
                {
                    "source_id": item.get("id"),
                    "title": item.get("title"),
                    "content": item.get("body"),
                    "content_type": item.get("content_type"),
                    "source": item.get("source"),
                    "updated_at": item.get("updated_at"),
                }
                for item in articles[:effective_limit] if isinstance(item, dict)
            ]
            return {"status": "success", "total": len(results), "results": results}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("CRM knowledge search failed: %s", type(exc).__name__)
            return {"status": "error", "error": "Knowledge search is temporarily unavailable."}

    return FunctionTool(func=search_crm_knowledge)
