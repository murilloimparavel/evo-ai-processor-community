import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import uuid4

from google.adk.agents import LlmAgent

spec = importlib.util.spec_from_file_location("delegation_test", Path(__file__).resolve().parents[2] / "src/services/knowledge_runtime/delegation.py")
delegation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delegation)


class DelegationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root_id, self.child_id = str(uuid4()), str(uuid4())
        self.grant = SimpleNamespace(root_agent_id=self.root_id, account_scope="account-a")
        self.records = {
            self.root_id: SimpleNamespace(id=self.root_id, name="root", config={"sub_agents": [self.child_id]}),
            self.child_id: SimpleNamespace(id=self.child_id, name="specialist", config={}),
        }
        self.tree = LlmAgent(name="root", sub_agents=[LlmAgent(name="specialist")])

    async def lookup(self, account_scope, agent_id):
        return self.records.get(agent_id) if account_scope == "account-a" else None

    async def test_real_adk_tree_matches_server_graph(self):
        result = await delegation.verify_delegation_tree(grant=self.grant, root=self.tree, lookup=self.lookup)
        self.assertEqual(result, {"root": self.root_id, "specialist": self.child_id})

    async def test_missing_or_replaced_child_is_denied(self):
        self.records.pop(self.child_id)
        with self.assertRaises(delegation.InvalidDelegation):
            await delegation.verify_delegation_tree(grant=self.grant, root=self.tree, lookup=self.lookup)

    async def test_stale_tree_and_cross_account_are_denied(self):
        self.records[self.root_id].config["sub_agents"] = []
        with self.assertRaises(delegation.InvalidDelegation):
            await delegation.verify_delegation_tree(grant=self.grant, root=self.tree, lookup=self.lookup)
        self.grant.account_scope = "account-b"
        with self.assertRaises(delegation.InvalidDelegation):
            await delegation.verify_delegation_tree(grant=self.grant, root=self.tree, lookup=self.lookup)
