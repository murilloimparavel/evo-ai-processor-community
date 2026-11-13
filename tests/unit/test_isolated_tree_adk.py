"""Real ADK transfer interception; local fake models, no external calls."""
import importlib.util
from pathlib import Path
import unittest

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types

spec = importlib.util.spec_from_file_location(
    "isolated_tree", Path(__file__).resolve().parents[2] / "src/services/adk/runners/isolated_tree.py"
)
# Dataclasses requires a registered module when executing outside the package.
import sys
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class IsolatedTreeTest(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_history_is_announced_in_system_instruction(self):
        seen = []

        class Model(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                seen.append(llm_request.model_dump_json(exclude_none=True))
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Qual opção?")]))

        root = LlmAgent(name="root", model=Model(model="local"))
        await module.run_isolated_tree(root, ["Quero a segunda"], history_incomplete=True)
        self.assertIn("respostas anteriores do atendimento", seen[0])
        self.assertIn("peça que ele a identifique", seen[0])

    async def test_actual_transfer_keeps_prior_agent_tool_data_out_of_next_context(self):
        seen = []

        def lookup() -> str:
            """Read A's synthetic private knowledge."""
            return "SECRET_FROM_AGENT_A"

        class Model(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                content = llm_request.model_dump_json(exclude_none=True)
                seen.append((self.model, content))
                if self.model == "root":
                    part = types.Part(function_call=types.FunctionCall(name="transfer_to_agent", args={"agent_name": "a"}))
                elif self.model == "a" and "SECRET_FROM_AGENT_A" not in content:
                    part = types.Part(function_call=types.FunctionCall(name="lookup", args={}))
                elif self.model == "a":
                    part = types.Part(function_call=types.FunctionCall(name="transfer_to_agent", args={"agent_name": "b"}))
                else:
                    part = types.Part(text="Concluído")
                yield LlmResponse(content=types.Content(role="model", parts=[part]))

        b = LlmAgent(name="b", model=Model(model="b"))
        a = LlmAgent(name="a", model=Model(model="a"), tools=[lookup], sub_agents=[b])
        root = LlmAgent(name="root", model=Model(model="root"), sub_agents=[a])
        result = await module.run_isolated_tree(root, ["Oi", "Quero ajuda"])
        self.assertEqual(result.agents, ["root", "a", "b"])
        self.assertTrue(any("SECRET_FROM_AGENT_A" in body for name, body in seen if name == "a"))
        self.assertTrue(any(name == "b" for name, _ in seen))
        for name, body in seen:
            if name != "a":
                self.assertNotIn("SECRET_FROM_AGENT_A", body)

    async def test_typed_history_is_loaded_separately_for_each_executor(self):
        seen = []

        class Model(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                seen.append((self.model, llm_request.contents))
                part = (types.Part(function_call=types.FunctionCall(name="transfer_to_agent", args={"agent_name": "a"}))
                        if self.model == "root" else types.Part(text="Confirmado"))
                yield LlmResponse(content=types.Content(role="model", parts=[part]))

        async def history(name):
            return [{"role": "user", "text": "Quais opções?"},
                    {"role": "assistant", "text": "ROOT_ONLY" if name == "root" else "A_ONLY"},
                    {"role": "user", "text": "Quero a segunda"}]

        root = LlmAgent(name="root", model=Model(model="root"), sub_agents=[LlmAgent(name="a", model=Model(model="a"))])
        await module.run_isolated_tree(root, ["Quero a segunda"], history_provider=history)
        for name, contents in seen:
            body = str(contents)
            self.assertIn("ROOT_ONLY" if name == "root" else "A_ONLY", body)
            self.assertNotIn("A_ONLY" if name == "root" else "ROOT_ONLY", body)
            self.assertTrue(any(content.role == "model" for content in contents))

    async def test_transfer_rejects_unrelated_agent_and_repeated_handoff(self):
        class Model(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(
                    function_call=types.FunctionCall(name="transfer_to_agent", args={"agent_name": self.model})
                )]))

        root = LlmAgent(name="root", model=Model(model="unrelated"),
                        sub_agents=[LlmAgent(name="a", model=Model(model="root"))])
        with self.assertRaises(ValueError):
            await module.run_isolated_tree(root, ["Oi"])
        looping = LlmAgent(name="root", model=Model(model="a"),
                           sub_agents=[LlmAgent(name="a", model=Model(model="root"))])
        with self.assertRaisesRegex(ValueError, "handoff limit"):
            await module.run_isolated_tree(looping, ["Oi"], max_handoffs=1)
