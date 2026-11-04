"""Real ADK test of the experimental isolated specialist primitive; no network."""
import importlib.util
from pathlib import Path
import unittest

try:
    from google.adk.agents import LlmAgent
    from google.adk.models import BaseLlm, LlmResponse
    from google.adk.tools.agent_tool import AgentTool
    from google.genai import types
except ImportError:
    LlmAgent = None

if LlmAgent is not None:
    spec = importlib.util.spec_from_file_location(
        "isolated_specialist",
        Path(__file__).resolve().parents[2] / "src/services/adk/runners/isolated_specialist.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run_isolated_specialist = module.run_isolated_specialist


@unittest.skipIf(LlmAgent is None, "google-adk 1.19 is not installed")
class IsolatedSpecialistTest(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_agents_and_revocation_have_fresh_context(self):
        captured = []

        def private_lookup() -> str:
            """Return simulated knowledge available only to A."""
            return "private-tool-result-A"

        class LocalModel(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                body = llm_request.model_dump_json(exclude_none=True)
                captured.append((self.model, body))
                if self.model == "a" and "private-tool-result-A" not in body:
                    part = types.Part(function_call=types.FunctionCall(name="private_lookup", args={}))
                elif self.model == "a":
                    part = types.Part(text="private-derived-A")
                else:
                    part = types.Part(text="ok")
                yield LlmResponse(content=types.Content(role="model", parts=[part]))

        agent_a = LlmAgent(name="agent_a", model=LocalModel(model="a"), tools=[private_lookup])
        agent_b = LlmAgent(name="agent_b", model=LocalModel(model="b"))
        root = LlmAgent(name="root", model=LocalModel(model="root"))
        await run_isolated_specialist(agent_a, ["quero reservar"])
        await run_isolated_specialist(agent_b, ["quero reservar", "ajuda"])
        await run_isolated_specialist(root, ["quero reservar", "ajuda"])
        # Revoked: build an agent with no knowledge tool in a later invocation.
        revoked = LlmAgent(name="agent_a", model=LocalModel(model="revoked"))
        await run_isolated_specialist(revoked, ["quero reservar", "ajuda", "voltei"])
        for name, body in captured:
            if name != "a":
                self.assertNotIn("private-tool-result-A", body)
                self.assertNotIn("private-derived-A", body)
        self.assertTrue(any("private-tool-result-A" in body for name, body in captured if name == "a"))

    async def test_rejects_nested_agents_and_delegation_tools(self):
        class LocalModel(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ok")]))

        child = LlmAgent(name="child", model=LocalModel(model="child"))
        root = LlmAgent(name="root", model=LocalModel(model="root"), sub_agents=[child])
        with self.assertRaises(ValueError):
            await run_isolated_specialist(root, ["ola"])
        with self.assertRaises(ValueError):
            await run_isolated_specialist(child, ["ola"])
        tool_child = LlmAgent(name="tool_child", model=LocalModel(model="child"))
        tool_root = LlmAgent(name="tool_root", model=LocalModel(model="root"), tools=[AgentTool(agent=tool_child)])
        with self.assertRaises(ValueError):
            await run_isolated_specialist(tool_root, ["ola"])

    async def test_rejects_non_customer_history_shapes(self):
        class LocalModel(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ok")]))

        agent = LlmAgent(name="root", model=LocalModel(model="root"))
        for bad in ([], "text", ["ok", {"role": "model", "text": "private"}], [""]):
            with self.assertRaises(ValueError):
                await run_isolated_specialist(agent, bad)
