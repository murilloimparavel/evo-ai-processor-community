"""An actual ADK model/tool loop must stop at the isolated runtime budget."""
import importlib.util
from pathlib import Path
import sys
import unittest

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types

path = Path(__file__).resolve().parents[2] / 'src/services/adk/runners/isolated_tree.py'
spec = importlib.util.spec_from_file_location('isolated_budget_test', path)
tree = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tree
spec.loader.exec_module(tree)


class LoopModel(BaseLlm):
    calls: int = 0
    transfer_after: int = 0
    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        target = types.FunctionCall(name='transfer_to_agent', args={'agent_name': 'child'}) if self.transfer_after and self.calls > self.transfer_after else types.FunctionCall(name='query_status', args={})
        yield LlmResponse(content=types.Content(role='model', parts=[types.Part(function_call=target)]))


class RuntimeBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_adk_loop_cannot_exceed_twelve_model_calls(self):
        calls = []
        def query_status() -> dict:
            """Return an unavailable status."""
            calls.append(True)
            return {'status': 'error', 'error': 'No result'}
        model = LoopModel(model='deterministic-local')
        agent = LlmAgent(name='root', model=model, tools=[query_status])
        with self.assertRaisesRegex(Exception, '12|limit|exceed'):
            await tree.run_isolated_tree(agent, ['Check status'])
        self.assertLessEqual(model.calls, 12)
        self.assertLessEqual(len(calls), 12)
        self.assertGreaterEqual(model.calls, 1)

    async def test_request_budget_survives_handoff_and_preserves_callbacks(self):
        seen = []
        async def original_callback(callback_context, llm_request):
            seen.append(True)
        def query_status() -> dict:
            """Query a synthetic status."""
            return {"status": "pending"}
        root_model = LoopModel(model='local-root', transfer_after=6)
        child_model = LoopModel(model='local-child')
        child = LlmAgent(name='child', model=child_model, tools=[query_status], before_model_callback=original_callback)
        agent = LlmAgent(name='root', model=root_model, tools=[query_status], sub_agents=[child], before_model_callback=[original_callback])
        with self.assertRaisesRegex(PermissionError, 'request model call budget'):
            await tree.run_isolated_tree(agent, ['Check status'])
        self.assertEqual(root_model.calls, 7)
        self.assertEqual(child_model.calls, 5)
        self.assertEqual(len(seen), 12)


if __name__ == '__main__': unittest.main()
