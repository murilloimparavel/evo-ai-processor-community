"""Opaque response attestations; never put these tokens in model state/events."""
import hashlib
import json
import os
import time
from uuid import uuid4

import httpx
import jwt


class ResponseProvenanceMixin:
    def _history_proof(self, agent_id, config_hash, audience, **extra):
        now = int(time.time())
        claims = {"iss": os.environ["KNOWLEDGE_EXECUTOR_ISSUER"], "aud": audience,
                  "iat": now, "exp": min(now + 60, self._grant.expires_at),
                  "agent_id": agent_id, "config_sha256": config_hash, "grant": self._token,
                  "conversation_id": self._grant.conversation_id, "bot_id": self._grant.bot_id,
                  "account_scope": self._grant.account_scope, **extra}
        if audience == "knowledge-response":
            # Dispatch grants must never be embedded in cached response artifacts.
            claims.pop("grant", None)
            for field in ("inbox_id", "contact_id", "root_agent_id", "event_id"):
                claims[field] = getattr(self._grant, field)
            claims["request_sha256"] = jwt.decode(self._token, options={"verify_signature": False})["request_sha256"]
        if claims["exp"] <= now:
            raise PermissionError("authority expired")
        return jwt.encode(claims, os.environ["KNOWLEDGE_EXECUTOR_PRIVATE_KEY"], algorithm="RS256")

    async def history_for_agent(self, root, name):
        from src.services.knowledge_runtime.delegation import verify_delegation_tree
        self._fresh()
        names = await verify_delegation_tree(grant=self._grant, root=root, lookup=self._lookup)
        agent_id = names.get(name)
        if not agent_id:
            raise PermissionError("unknown executor")
        record = await self._lookup(self._grant.account_scope, agent_id)
        digest = hashlib.sha256(json.dumps(record.config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        pending = [root]
        node = None
        while pending:
            candidate = pending.pop()
            if candidate.name == name:
                node = candidate
                break
            pending.extend(candidate.sub_agents or [])
        raw_categories = sorted({getattr(tool, "name", "unknown") for tool in node.tools or []}) if node else ["unknown"]
        custom_names = {tool_name for (executor, tool_name) in getattr(self, "_custom_tools", {}) if executor == name}
        categories = sorted({"external_live_read" if category in custom_names else category for category in raw_categories})
        proof = self._history_proof(agent_id, digest, "knowledge-history-executor", tool_categories=categories)
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.post(os.environ["KNOWLEDGE_RUNTIME_HISTORY_URL"], json={}, headers={
                "X-Knowledge-Grant": self._token, "X-Knowledge-Executor": proof,
                "X-Knowledge-History-Token": os.environ["KNOWLEDGE_RUNTIME_HISTORY_TOKEN"]})
        response.raise_for_status()
        data = response.json()
        turns = data.get("typed_turns")
        if (not isinstance(turns, list) or not 1 <= len(turns) <= 100
                or any(not isinstance(t, dict) or t.get("role") not in {"user", "assistant"}
                       or not isinstance(t.get("text"), str) or not t["text"].strip() for t in turns)
                or turns[-1]["role"] != "user" or sum(len(t["text"]) for t in turns) > 100_000
                or type(data.get("profile_revision")) is not int or not isinstance(data.get("provenance_sources"), list)):
            raise PermissionError("invalid executor history")
        node = root
        pending = [root]
        while pending:
            candidate = pending.pop()
            if candidate.name == name:
                node = candidate
                break
            pending.extend(candidate.sub_agents or [])
        reusable = set(categories) <= {"search_crm_knowledge", "manage_conversation_labels", "pipeline_manipulation", "transfer_to_human"}
        self._provenance_state = getattr(self, "_provenance_state", {})
        self._provenance_state[name] = {"agent_id": agent_id, "config_sha256": digest,
            "tool_categories": categories, "profile_revision": data["profile_revision"], "sources": data["provenance_sources"], "reusable": reusable,
            "volatile_custom": "external_live_read" in categories}
        return turns

    def capture_search_provenance(self, name, payload):
        state = getattr(self, "_provenance_state", {}).get(name)
        if not state:
            return
        if payload.get("profile_revision") != state["profile_revision"]:
            state["reusable"] = False
        for item in payload.get("results", []):
            source = {"id": item.get("id"), "body_sha256": item.get("body_sha256"), "updated_at": item.get("source_updated_at"), "base_version": item.get("base_version")}
            if (not all(isinstance(source[k], str) and source[k] for k in ("id", "body_sha256", "updated_at"))
                    or type(source["base_version"]) is not int):
                state["reusable"] = False
            elif source not in state["sources"]:
                state["sources"].append(source)
        if len(state["sources"]) > 100:
            state["reusable"] = False

    async def response_provenance(self, name, text):
        if os.getenv("KNOWLEDGE_RESPONSE_PROVENANCE_ENABLED") != "true":
            return None
        state = getattr(self, "_provenance_state", {}).get(name)
        if not state or (not state["reusable"] and not state.get("volatile_custom")):
            return None
        if time.time() >= self._grant.expires_at:
            return None
        try:
            record = await self._lookup(self._grant.account_scope, state["agent_id"])
        except PermissionError:
            return None
        current_hash = hashlib.sha256(json.dumps(record.config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if current_hash != state["config_sha256"]:
            return None
        try:
            return self._history_proof(state["agent_id"], current_hash, "knowledge-response", jti=str(uuid4()),
                body_sha256=hashlib.sha256(text.encode()).hexdigest(), profile_revision=state["profile_revision"],
                sources=state["sources"], tool_categories=state["tool_categories"], reusable=state["reusable"])
        except PermissionError:
            # A completed reply remains deliverable without reuse authority.
            return None
