"""Fail-closed gate and dispatch through the real ADK, with no network."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types

root = Path(__file__).resolve().parents[2] / "src/services/adk/runners"
tree_spec = importlib.util.spec_from_file_location("src.services.adk.runners.isolated_tree", root / "isolated_tree.py")
tree = importlib.util.module_from_spec(tree_spec)
sys.modules[tree_spec.name] = tree
tree_spec.loader.exec_module(tree)
gate_spec = importlib.util.spec_from_file_location("isolation_gate_test", root / "isolation_gate.py")
gate = importlib.util.module_from_spec(gate_spec)
sys.modules[gate_spec.name] = gate
gate_spec.loader.exec_module(gate)


class Model(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Olá")]))


class Utils:
    def __init__(self):
        self.built = 0

    async def get_and_build_agent(self, agent_id):
        self.built += 1
        return LlmAgent(name="root", model=Model(model="local")), []


class Authority:
    def __init__(self, root_id="agent", allowed=True):
        self.root_id = root_id
        self.allowed = allowed

    async def resolve(self, *, agent_id, external_id):
        return gate.AuthorizedCustomerHistory(self.root_id, "22222222-2222-4222-8222-222222222222", ("customer message",))

    async def authorize_tree(self, *, context, root_agent):
        return self.allowed

    async def authorize_executor(self, *, context, root_agent, agent_name):
        return self.allowed


class IsolationGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_or_missing_context_does_not_build_or_execute(self):
        utils = Utils()
        for authority in (None, Authority(root_id="different")):
            with self.assertRaises(PermissionError):
                await gate.run_required_isolation(authority=authority, utils=utils, agent_id="agent",
                                                  external_id="arbitrary", files=None, timeout=5)
        self.assertEqual(utils.built, 0)
        with self.assertRaises(PermissionError):
            await gate.run_required_isolation(authority=Authority(), utils=utils, agent_id="agent",
                                              external_id="arbitrary", files=["attachment"], timeout=5)
        self.assertEqual(utils.built, 0)

    async def test_tree_authorization_required_before_real_dispatch(self):
        utils = Utils()
        with self.assertRaises(PermissionError):
            await gate.run_required_isolation(authority=Authority(allowed=False), utils=utils, agent_id="agent",
                                              external_id="arbitrary", files=None, timeout=5)
        result = await gate.run_required_isolation(authority=Authority(), utils=utils, agent_id="agent",
                                                   external_id="arbitrary", files=None, timeout=5)
        self.assertEqual(result["final_response"], "Olá")
        self.assertEqual(result["message_history"], [])

    async def test_executor_revoked_after_tree_authorization_is_denied(self):
        authority = Authority()
        async def deny(**kwargs):
            return False
        authority.authorize_executor = deny
        with self.assertRaises(PermissionError):
            await gate.run_required_isolation(authority=authority, utils=Utils(), agent_id="agent",
                                              external_id="arbitrary", files=None, timeout=5)

    def test_unknown_mode_does_not_downgrade(self):
        with patch.dict(os.environ, {"KNOWLEDGE_RUNTIME_ISOLATION_MODE": "bad-value"}):
            self.assertTrue(gate.isolation_required())
        with patch.dict(os.environ, {"KNOWLEDGE_RUNTIME_ISOLATION_MODE": "off"}):
            self.assertFalse(gate.isolation_required())

    def test_only_confirmed_human_handoff_sets_terminal_flag(self):
        def event(name, response):
            return Event(author="specialist", content=types.Content(role="user", parts=[
                types.Part(function_response=types.FunctionResponse(name=name, response=response))
            ]))

        successful = {"status": "success", "terminal_handoff": True}
        self.assertTrue(gate._terminal_handoff([event("transfer_to_human", successful)]))
        self.assertFalse(gate._terminal_handoff([event("unrelated_tool", successful)]))
        self.assertFalse(gate._terminal_handoff([event("transfer_to_human", {"status": "error", "terminal_handoff": True})]))

    async def test_confirmed_handoff_without_final_text_has_safe_fallback(self):
        response = types.FunctionResponse(name="transfer_to_human", response={"status": "success", "terminal_handoff": True})
        event = Event(author="root", content=types.Content(role="user", parts=[types.Part(function_response=response)]))
        dispatch = tree.IsolatedDispatch(events=[event], agents=["root"])
        with patch.object(gate, "run_isolated_tree", return_value=dispatch):
            result = await gate.run_required_isolation(authority=Authority(), utils=Utils(), agent_id="agent",
                                                       external_id="conversation", files=None, timeout=5)
        self.assertTrue(result["terminal_handoff"])
        self.assertIn("equipe", result["final_response"])
