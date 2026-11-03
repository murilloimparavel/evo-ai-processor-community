import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

fake_models = ModuleType("src.models.models")
fake_models.Agent = object
fake_gate = ModuleType("src.services.adk.runners.isolation_gate")
fake_gate.AuthorizedCustomerHistory = object
fake_delegation = ModuleType("src.services.knowledge_runtime.delegation")
fake_delegation.InvalidDelegation = ValueError
fake_delegation.verify_delegation_tree = AsyncMock()
fake_grant = ModuleType("src.services.knowledge_runtime.grant")
fake_grant.VerifiedGrant = object
spec = importlib.util.spec_from_file_location("isolated_authority_test", Path(__file__).resolve().parents[2] / "src/services/knowledge_runtime/isolation_authority.py")
authority_module = importlib.util.module_from_spec(spec)
provenance_spec = importlib.util.spec_from_file_location("src.services.knowledge_runtime.response_provenance", Path(__file__).resolve().parents[2] / "src/services/knowledge_runtime/response_provenance.py")
provenance_module = importlib.util.module_from_spec(provenance_spec)
provenance_spec.loader.exec_module(provenance_module)
secure_spec = importlib.util.spec_from_file_location("src.services.knowledge_runtime.secure_custom", Path(__file__).resolve().parents[2] / "src/services/knowledge_runtime/secure_custom.py")
secure_module = importlib.util.module_from_spec(secure_spec)
secure_spec.loader.exec_module(secure_module)
with patch.dict(sys.modules, {"src.models.models": fake_models, "src.services.adk.runners.isolation_gate": fake_gate,
                              "src.services.knowledge_runtime.delegation": fake_delegation,
                              "src.services.knowledge_runtime.response_provenance": provenance_module,
                              "src.services.knowledge_runtime.secure_custom": secure_module,
                              "src.services.knowledge_runtime.grant": fake_grant}):
    spec.loader.exec_module(authority_module)


class ScopedToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.authority = authority_module.VerifiedIsolationAuthority.__new__(authority_module.VerifiedIsolationAuthority)
        self.authority._grant = SimpleNamespace(conversation_id="trusted-conversation", contact_id="trusted-contact", event_id="trusted-event")
        self.root = LlmAgent(name="root")
        self.authority._current_config = AsyncMock()
        self.authority._mutation_request = AsyncMock(return_value={"status": "success"})

    def test_custom_attempt_budget_is_shared_and_hard_bounded(self):
        for _ in range(6):
            self.authority._consume_custom_attempt()
        with self.assertRaises(PermissionError):
            self.authority._consume_custom_attempt()
        with self.assertRaises(PermissionError):
            self.authority._consume_custom_attempt()
        self.assertEqual(self.authority._custom_attempts, 8)

    async def test_custom_seventh_attempt_and_duplicate_do_not_reach_egress(self):
        record = SimpleNamespace(id="11111111-1111-4111-8111-111111111111", name="availability",
            description="Read availability", method="POST", endpoint="https://service.example/search",
            headers={}, path_params={}, query_params={}, body_params={"code": {"type": "string", "required": True}},
            values={}, error_handling={})
        policy = {"tool_id": str(record.id), "fingerprint": secure_module.fingerprint_config(record),
            "effect": "read", "purpose": "availability", "method": "POST", "origin": "https://service.example",
            "path": "/search", "response_fields": ["available"]}
        self.authority._custom_tools = {("root", "availability"): (record, policy)}
        self.authority._custom_for = AsyncMock(return_value=(record, policy))
        wrapper = secure_module.StrictCustomFunctionTool(self.authority._make_custom(self.root, "root", "availability"),
                                                         self.authority._consume_custom_attempt)
        egress = AsyncMock(return_value={"status": "success"})
        with patch.object(authority_module, "execute", egress):
            with self.assertLogs("knowledge_runtime.audit", level="INFO") as audit:
                for number in range(6):
                    await wrapper.run_async(args={"code": str(number)}, tool_context=None)
            self.assertEqual(len(audit.output), 6)
            self.assertTrue(all("event_id=trusted-event" in line and "status=success" in line for line in audit.output))
            self.assertTrue(all("code=" not in line and "service.example" not in line for line in audit.output))
            with self.assertRaises(PermissionError):
                await wrapper.run_async(args={"code": "seventh"}, tool_context=None)
            self.assertEqual(egress.await_count, 6)
            self.authority._custom_attempts = 0
            self.authority._custom_requests = set()
            egress.reset_mock()
            self.authority._custom_for.reset_mock()
            with self.assertLogs("knowledge_runtime.audit", level="INFO") as audit:
                await wrapper.run_async(args={"code": "same"}, tool_context=None)
            self.assertNotIn("same", audit.output[0])
            with self.assertRaises(PermissionError):
                await wrapper.run_async(args={"code": "same"}, tool_context=None)
            self.assertEqual(egress.await_count, 1)
            self.assertEqual(self.authority._custom_for.await_count, 2)

    async def test_labels_always_use_verified_conversation(self):
        original = AsyncMock(return_value={"status": "success"})
        fake = ModuleType("src.services.adk.tools.evo_crm.manage_conversation_labels")
        fake.create_manage_conversation_labels_tool = lambda: SimpleNamespace(func=original)
        wrapped = self.authority._make_labels(self.root, "root")
        self.assertNotIn("conversation_id", FunctionTool(wrapped)._get_declaration().parameters.properties)
        with patch.dict(sys.modules, {fake.__name__: fake}):
            await wrapped("add", ["foo"])
        self.authority._mutation_request.assert_awaited_once_with(self.root, "root", {"tool": "labels", "action": "add", "labels": ["foo"]})

    async def test_transfer_requires_configured_route_and_verified_conversation(self):
        original = AsyncMock(return_value={"status": "success"})
        fake = ModuleType("src.services.adk.tools.evo_crm.transfer_to_human")
        fake._resolve_transfer_target = lambda rule: (None, rule.get("targetId"))
        wrapped = self.authority._make_transfer(self.root, "root")
        self.authority._current_config.return_value = {"transfer_rules": [{"id": "route_1", "targetId": "team"}]}
        with patch.dict(sys.modules, {fake.__name__: fake}):
            await wrapped("route_1", "help")
            self.authority._mutation_request.assert_awaited_once_with(self.root, "root", {"tool": "handoff", "team_id": "team"})
            self.authority._current_config.return_value = {"transfer_rules": []}
            with self.assertRaises(PermissionError):
                await wrapped()

    async def test_pipeline_rejects_unconfigured_destinations_and_scopes_ids(self):
        original = AsyncMock(return_value={"status": "success"})
        fake = ModuleType("src.services.adk.tools.evo_crm.pipeline_manipulation")
        fake.create_pipeline_manipulation_tool = lambda **kwargs: SimpleNamespace(func=original)
        wrapped = self.authority._make_pipeline(self.root, "root")
        self.authority._current_config.return_value = {"pipeline_rules": [{"pipelineId": "p1", "stages": [{"stageId": "s1", "stageName": "Quote"}]}]}
        with patch.dict(sys.modules, {fake.__name__: fake}):
            with self.assertRaises(PermissionError):
                await wrapped("move_to_stage", "other", "s1")
            with self.assertRaises(PermissionError):
                await wrapped("move_to_stage", "p1", "other")
            with self.assertRaises(PermissionError):
                await wrapped("complete_task", "p1")
            with self.assertRaises(PermissionError):
                await wrapped("move_to_stage", "p1", "s1", "Not quote")
            await wrapped("move_to_stage", "p1", "s1")
        self.authority._mutation_request.assert_awaited_once_with(self.root, "root", {"tool": "pipeline", "action": "move_to_stage", "pipeline_id": "p1", "stage_id": "s1"})

    async def test_pipeline_name_resolves_to_unique_id_and_rejects_ambiguous(self):
        original = AsyncMock(return_value={"status": "success"})
        fake = ModuleType("src.services.adk.tools.evo_crm.pipeline_manipulation")
        fake.create_pipeline_manipulation_tool = lambda **kwargs: SimpleNamespace(func=original)
        wrapped = self.authority._make_pipeline(self.root, "root")
        stages = [{"stageId": "s1", "stageName": "Quote"}]
        self.authority._current_config.return_value = {"pipeline_rules": [{"pipelineId": "p1", "stages": stages}]}
        with patch.dict(sys.modules, {fake.__name__: fake}):
            await wrapped("add_to_pipeline", "p1", stage_name="Quote")
            self.assertEqual(self.authority._mutation_request.await_args.args[2]["stage_id"], "s1")
            stages.append({"stageId": "s2", "stageName": "Quote"})
            with self.assertRaises(PermissionError):
                await wrapped("add_to_pipeline", "p1", stage_name="Quote")

    async def test_tree_with_unknown_tool_is_denied_before_execution(self):
        def arbitrary_tool():
            return None
        root = LlmAgent(name="root", tools=[FunctionTool(arbitrary_tool)])
        self.assertFalse(await self.authority.authorize_tree(context=None, root_agent=root))

    async def test_revoked_capability_is_checked_before_any_call(self):
        self.authority._current_config.side_effect = PermissionError("revoked")
        with self.assertRaises(PermissionError):
            await self.authority._make_labels(self.root, "root")("add", ["foo"])

    async def test_current_configuration_rechecks_live_capability(self):
        del self.authority._current_config
        self.authority._grant.account_scope = "account"
        fake_delegation.verify_delegation_tree.return_value = {"root": "agent"}
        record = SimpleNamespace(config={"allow_manage_labels": True})
        self.authority._lookup = AsyncMock(return_value=record)
        with patch.object(authority_module, "verify_delegation_tree", fake_delegation.verify_delegation_tree):
            await self.authority._current_config(self.root, "root", "allow_manage_labels")
            record.config["allow_manage_labels"] = False
            with self.assertRaises(PermissionError):
                await self.authority._current_config(self.root, "root", "allow_manage_labels")


if __name__ == "__main__":
    unittest.main()
