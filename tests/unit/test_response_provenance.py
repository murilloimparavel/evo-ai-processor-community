"""Response attestations are opaque, bounded and never contain dispatch grants."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import jwt

spec = importlib.util.spec_from_file_location("response_provenance_test", Path(__file__).resolve().parents[2] / "src/services/knowledge_runtime/response_provenance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Authority(module.ResponseProvenanceMixin):
    async def _lookup(self, scope, agent_id):
        return SimpleNamespace(config={"name": "synthetic"})


class ResponseProvenanceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.public = key.public_key()
        self.authority = Authority()
        self.authority._grant = SimpleNamespace(expires_at=int(time.time()) + 60, account_scope="community",
            conversation_id="conversation", bot_id="bot", inbox_id="inbox", contact_id="contact", root_agent_id="root", event_id="event")
        self.authority._token = jwt.encode({"request_sha256": "a" * 64}, self.pem, algorithm="RS256")
        config_hash = hashlib.sha256(json.dumps({"name": "synthetic"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.authority._provenance_state = {"root": {"agent_id": "agent", "config_sha256": config_hash,
            "profile_revision": 1, "sources": [], "tool_categories": [], "reusable": True}}
        self.env = patch.dict(os.environ, {"KNOWLEDGE_RESPONSE_PROVENANCE_ENABLED": "true",
            "KNOWLEDGE_EXECUTOR_ISSUER": "processor", "KNOWLEDGE_EXECUTOR_PRIVATE_KEY": self.pem})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_signs_exact_reply_without_embedding_dispatch_grant(self):
        token = await self.authority.response_provenance("root", "Resposta exata")
        proof = jwt.decode(token, self.public, algorithms=["RS256"], issuer="processor", audience="knowledge-response")
        self.assertNotIn("grant", proof)
        self.assertEqual(proof["body_sha256"], hashlib.sha256("Resposta exata".encode()).hexdigest())
        self.assertEqual(proof["request_sha256"], "a" * 64)
        self.assertLessEqual(proof["exp"], self.authority._grant.expires_at)

    async def test_expiration_or_unknown_sources_never_prevents_a_completed_reply(self):
        self.authority._grant.expires_at = int(time.time()) - 1
        self.assertIsNone(await self.authority.response_provenance("root", "Resposta pronta"))
        self.authority._grant.expires_at = int(time.time()) + 60
        self.authority._provenance_state["root"]["reusable"] = False
        self.assertIsNone(await self.authority.response_provenance("root", "Resposta pronta"))

    def test_source_changes_during_run_disable_reuse(self):
        self.authority.capture_search_provenance("root", {"profile_revision": 2, "results": []})
        self.assertFalse(self.authority._provenance_state["root"]["reusable"])
