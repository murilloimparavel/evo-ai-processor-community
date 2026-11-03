"""Request-local authority backed by a verified grant, CRM and server agents.

This provider must only be constructed after A2A signature and body validation.
It supports only an explicitly attested single-account Community Core.
"""
import os
import time
import hashlib
import json
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import jwt
from google.adk.tools import FunctionTool

from src.models.models import Agent
from src.services.adk.runners.isolation_gate import AuthorizedCustomerHistory
from src.services.knowledge_runtime.delegation import InvalidDelegation, verify_delegation_tree
from src.services.knowledge_runtime.grant import VerifiedGrant
from src.services.knowledge_runtime.secure_custom import (CustomDenied, StrictCustomFunctionTool, validate_policy, config_dict, signature, execute)
from types import SimpleNamespace


from src.services.knowledge_runtime.response_provenance import ResponseProvenanceMixin


class VerifiedIsolationAuthority(ResponseProvenanceMixin):
    _SUPPORTED_TOOLS = frozenset({"search_crm_knowledge", "manage_conversation_labels",
                                  "transfer_to_human", "pipeline_manipulation"})
    def __init__(self, *, grant: VerifiedGrant, token: str, db):
        scope = os.getenv("KNOWLEDGE_CORE_DEDICATED_ACCOUNT_SCOPE", "")
        if not isinstance(grant, VerifiedGrant) or not scope or scope != grant.account_scope:
            raise PermissionError("authority unavailable")
        self._grant, self._token, self._db = grant, token, db

    def _fresh(self):
        if time.time() >= self._grant.expires_at:
            raise PermissionError("authority unavailable")

    async def resolve(self, *, agent_id, external_id):
        self._fresh()
        if agent_id != self._grant.root_agent_id or external_id != self._grant.conversation_id:
            raise PermissionError("authority unavailable")
        url = os.getenv("KNOWLEDGE_RUNTIME_HISTORY_URL", "")
        secret = os.getenv("KNOWLEDGE_RUNTIME_HISTORY_TOKEN", "")
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path != "/internal/knowledge_runtime/history" or parts.query or parts.fragment
                or len(secret.encode()) < 32):
            raise PermissionError("authority unavailable")
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.post(url, headers={"X-Knowledge-Grant": self._token,
                    "X-Knowledge-History-Token": secret}, json={})
            if response.status_code != 200:
                raise PermissionError("authority unavailable")
            payload = response.json()
            turns = payload.get("turns")
            incomplete = payload.get("history_incomplete")
            if (not isinstance(turns, list) or not turns or len(turns) > 100
                    or type(incomplete) is not bool
                    or any(not isinstance(t, str) or not t.strip() for t in turns)
                    or sum(map(len, turns)) > 100_000):
                raise PermissionError("authority unavailable")
        except (httpx.HTTPError, ValueError, TypeError):
            raise PermissionError("authority unavailable") from None
        self._fresh()
        return AuthorizedCustomerHistory(agent_id, self._grant.conversation_id, tuple(turns), incomplete)

    async def _lookup(self, scope, agent_id):
        self._fresh()
        if scope != self._grant.account_scope:
            return None
        return self._db.query(Agent).populate_existing().filter(Agent.id == UUID(agent_id)).first()

    async def authorize_tree(self, *, context, root_agent):
        try:
            pending = [root_agent]
            self._custom_tools = {}
            while pending:
                node = pending.pop()
                for tool in getattr(node, "tools", None) or []:
                    tool_name = getattr(tool, "name", None)
                    if tool_name not in self._SUPPORTED_TOOLS:
                        record, policy = await self._custom_for(root_agent, node.name, tool_name)
                        self._custom_tools[(node.name, tool_name)] = (record, policy)
                pending.extend(getattr(node, "sub_agents", None) or [])
            await verify_delegation_tree(grant=self._grant, root=root_agent, lookup=self._lookup)
            return True
        except (InvalidDelegation, PermissionError, ValueError, KeyError, TypeError):
            return False

    def prepare_tree(self, root_agent):
        """Replace legacy knowledge tools with proof-bearing server wrappers."""
        pending = [root_agent]
        while pending:
            node = pending.pop()
            if node.tools:
                name = node.name
                factories = {"search_crm_knowledge": self._make_search,
                             "manage_conversation_labels": self._make_labels,
                             "transfer_to_human": self._make_transfer,
                             "pipeline_manipulation": self._make_pipeline}
                node.tools = [FunctionTool(func=factories[tool.name](root_agent, name)) if tool.name in factories
                              else StrictCustomFunctionTool(func=self._make_custom(root_agent, name, tool.name), consume_attempt=self._consume_custom_attempt) for tool in node.tools]
            pending.extend(node.sub_agents or [])

    async def _custom_for(self, root, executor_name, tool_name):
        if os.getenv("KNOWLEDGE_CUSTOM_TOOLS_ENABLED") != "true":
            raise CustomDenied()
        names = await verify_delegation_tree(grant=self._grant, root=root, lookup=self._lookup)
        agent_id = names.get(executor_name)
        record = await self._lookup(self._grant.account_scope, agent_id) if agent_id else None
        config = getattr(record, "config", None)
        if not isinstance(config, dict) or not isinstance(config.get("secure_custom_tools"), list):
            raise CustomDenied()
        attached = config.get("custom_tool_ids")
        if not isinstance(attached, list):
            raise CustomDenied()
        from src.models.models import CustomTool
        candidates = []
        for policy in config["secure_custom_tools"]:
            if not isinstance(policy, dict) or policy.get("tool_id") not in attached:
                continue
            try:
                tool = self._db.query(CustomTool).populate_existing().filter(CustomTool.id == UUID(policy["tool_id"])).first()
            except (ValueError, TypeError, KeyError):
                raise CustomDenied() from None
            if tool and tool.name == tool_name:
                # Detach configuration from mutable ORM identity-map objects.
                snapshot = SimpleNamespace(id=tool.id, **json.loads(json.dumps(config_dict(tool))))
                trusted_policy = json.loads(json.dumps(policy))
                validate_policy(snapshot, trusted_policy)
                candidates.append((snapshot, trusted_policy))
        if len(candidates) != 1:
            raise CustomDenied()
        return candidates[0]

    def _consume_custom_attempt(self):
        self._custom_attempts = getattr(self, "_custom_attempts", 0) + 1
        if self._custom_attempts > 6:
            raise CustomDenied()

    def _make_custom(self, root, name, tool_name):
        record, policy = self._custom_tools[(name, tool_name)]

        async def custom_read(**kwargs):
            current, current_policy = await self._custom_for(root, name, tool_name)
            # A schema/policy change invalidates the callable already exposed to
            # the model. Rebuilding a tree is required; never expand inputs live.
            if config_dict(current) != config_dict(record) or current_policy != policy:
                raise CustomDenied()
            digest = hashlib.sha256(json.dumps([policy["tool_id"], kwargs], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self._custom_requests = getattr(self, "_custom_requests", set())
            if digest in self._custom_requests:
                raise CustomDenied()
            self._custom_requests.add(digest)
            state = getattr(self, "_provenance_state", {}).get(name)
            if state:
                state["reusable"] = False
                state["volatile_custom"] = True

            async def reauthorize():
                latest, latest_policy = await self._custom_for(root, name, tool_name)
                if config_dict(latest) != config_dict(record) or latest_policy != policy:
                    raise CustomDenied()
            try:
                return await execute(current, current_policy, kwargs, self._grant, reauthorize)
            except CustomDenied:
                return {"status": "error", "error": "Custom tool unavailable; no reservation or payment is confirmed."}
        custom_read.__name__ = tool_name
        custom_read.__doc__ = record.description or "Query an explicitly approved read-only external service."
        custom_read.__signature__ = signature(record, policy)
        return custom_read

    def _make_search(self, root_agent, name):
        async def search_crm_knowledge(query: str, limit: int = 5):
            """Search authorized business knowledge for the current specialist."""
            return await self._search(root_agent, name, query, limit)
        return search_crm_knowledge

    async def _current_config(self, root_agent, name, flag):
        names = await verify_delegation_tree(grant=self._grant, root=root_agent, lookup=self._lookup)
        agent_id = names.get(name)
        record = await self._lookup(self._grant.account_scope, agent_id) if agent_id else None
        config = getattr(record, "config", None)
        enabled = isinstance(config, dict) and (config.get(flag) is True or
                   (flag == "transfer_to_human_enabled" and config.get("transfer_to_human") is True))
        if not enabled:
            raise PermissionError("authority unavailable")
        return config

    def _make_labels(self, root_agent, name):
        async def manage_conversation_labels(action: str, labels: list[str] | None = None):
            """Manage labels for this authenticated conversation."""
            await self._current_config(root_agent, name, "allow_manage_labels")
            return await self._mutation_request(root_agent, name, {"tool": "labels", "action": action, "labels": labels or []})
        return manage_conversation_labels

    def _make_transfer(self, root_agent, name):
        async def transfer_to_human(route_key: str | None = None, reason: str | None = None):
            """Transfer this conversation using a configured human route."""
            config = await self._current_config(root_agent, name, "transfer_to_human_enabled")
            rules = config.get("transfer_rules")
            if not isinstance(rules, list) or not rules:
                raise PermissionError("authority unavailable")
            from src.services.adk.tools.evo_crm.transfer_to_human import _resolve_transfer_target
            matches = [rule for index, rule in enumerate(rules, 1) if isinstance(rule, dict)
                       and (str(rule.get("id", f"route_{index}")) == route_key if route_key else len(rules) == 1)]
            if len(matches) != 1:
                raise PermissionError("authority unavailable")
            assignee, team = _resolve_transfer_target(matches[0])
            if bool(assignee) == bool(team):
                raise PermissionError("authority unavailable")
            payload = {"tool": "handoff", "assignee_id": assignee} if assignee else {"tool": "handoff", "team_id": team}
            return await self._mutation_request(root_agent, name, payload)
        return transfer_to_human

    def _make_pipeline(self, root_agent, name):
        async def pipeline_manipulation(action: str, pipeline_id: str | None = None,
                                        stage_id: str | None = None, stage_name: str | None = None):
            """Read pipeline criteria or move this authenticated conversation."""
            config = await self._current_config(root_agent, name, "allow_pipeline_manipulation")
            rules = config.get("pipeline_rules")
            if not isinstance(rules, list) or not rules or action not in {"get_pipeline_criteria", "add_to_pipeline", "move_to_stage"}:
                raise PermissionError("authority unavailable")
            selected = [r for r in rules if isinstance(r, dict) and r.get("pipelineId") == pipeline_id]
            if not pipeline_id and len(rules) == 1 and isinstance(rules[0], dict):
                selected = [rules[0]]
            if len(selected) != 1:
                raise PermissionError("authority unavailable")
            stages = selected[0].get("stages")
            if not isinstance(stages, list):
                raise PermissionError("authority unavailable")
            if stage_name:
                matches = [s for s in stages if isinstance(s, dict) and s.get("stageName") == stage_name and s.get("stageId")]
                if len(matches) != 1 or (stage_id and stage_id != matches[0]["stageId"]):
                    raise PermissionError("authority unavailable")
                stage_id = matches[0]["stageId"]
            if stage_id and not any(isinstance(s, dict) and s.get("stageId") == stage_id for s in stages):
                raise PermissionError("authority unavailable")
            if action == "get_pipeline_criteria":
                return {"status": "success", "rules": selected}
            if not stage_id:
                raise PermissionError("authority unavailable")
            return await self._mutation_request(root_agent, name, {"tool": "pipeline", "action": action,
                "pipeline_id": selected[0]["pipelineId"], "stage_id": stage_id})
        return pipeline_manipulation

    async def _search(self, root_agent, name, query, limit):
        self._fresh()
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(limit) is not int or not 1 <= limit <= 10:
            return {"status": "error", "error": "Invalid search request."}
        names = await verify_delegation_tree(grant=self._grant, root=root_agent, lookup=self._lookup)
        agent_id = names.get(name)
        if not agent_id:
            raise PermissionError("authority unavailable")
        url = os.getenv("KNOWLEDGE_RUNTIME_RETRIEVAL_URL", "")
        secret = os.getenv("KNOWLEDGE_RUNTIME_RETRIEVAL_TOKEN", "")
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path != "/internal/knowledge_runtime/retrieve" or parts.query or parts.fragment
                or len(secret.encode()) < 32):
            raise PermissionError("authority unavailable")
        body = json.dumps({"query": query, "limit": limit}, separators=(",", ":"), ensure_ascii=False).encode()
        now = int(time.time())
        expires = min(now + 30, self._grant.expires_at)
        if expires <= now:
            raise PermissionError("authority unavailable")
        claims = {"iss": os.environ["KNOWLEDGE_EXECUTOR_ISSUER"], "aud": "knowledge-retrieval", "iat": now, "exp": expires,
                  "account_scope": self._grant.account_scope, "conversation_id": self._grant.conversation_id,
                  "root_agent_id": self._grant.root_agent_id, "event_id": self._grant.event_id,
                  "agent_id": agent_id, "body_sha256": hashlib.sha256(body).hexdigest()}
        proof = jwt.encode(claims, os.environ["KNOWLEDGE_EXECUTOR_PRIVATE_KEY"], algorithm="RS256")
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.post(url, content=body, headers={"Content-Type": "application/json",
                "X-Knowledge-Grant": self._token, "X-Knowledge-Executor": proof,
                "X-Knowledge-Retrieval-Token": secret})
        response.raise_for_status()
        payload = response.json()
        self.capture_search_provenance(name, payload)
        return {"status": "success", "results": payload["results"]}

    async def authorize_executor(self, *, context, root_agent, agent_name):
        try:
            names = await verify_delegation_tree(grant=self._grant, root=root_agent, lookup=self._lookup)
            return agent_name in names
        except (InvalidDelegation, PermissionError, ValueError):
            return False

    async def _mutation_request(self, root_agent, name, payload):
        self._fresh()
        names = await verify_delegation_tree(grant=self._grant, root=root_agent, lookup=self._lookup)
        agent_id = names.get(name)
        if not agent_id:
            raise PermissionError("authority unavailable")
        url = os.getenv("KNOWLEDGE_RUNTIME_ACTIONS_URL", "")
        secret = os.getenv("KNOWLEDGE_RUNTIME_ACTIONS_TOKEN", "")
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path != "/internal/knowledge_runtime/actions" or parts.query or parts.fragment
                or len(secret.encode()) < 32):
            raise PermissionError("authority unavailable")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        now = int(time.time())
        expiry = min(now + 30, self._grant.expires_at)
        if expiry <= now:
            raise PermissionError("authority unavailable")
        claims = {"iss": os.environ["KNOWLEDGE_EXECUTOR_ISSUER"], "aud": "knowledge-action", "iat": now, "exp": expiry,
            "account_scope": self._grant.account_scope, "conversation_id": self._grant.conversation_id,
            "inbox_id": self._grant.inbox_id, "contact_id": self._grant.contact_id, "bot_id": self._grant.bot_id,
            "root_agent_id": self._grant.root_agent_id, "event_id": self._grant.event_id,
            "agent_id": agent_id, "action_id": str(uuid4()), "body_sha256": hashlib.sha256(body).hexdigest()}
        proof = jwt.encode(claims, os.environ["KNOWLEDGE_EXECUTOR_PRIVATE_KEY"], algorithm="RS256")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(url, content=body, headers={"Content-Type": "application/json",
                "X-Knowledge-Grant": self._token, "X-Knowledge-Executor": proof, "X-Knowledge-Actions-Token": secret})
        response.raise_for_status()
        return response.json()
