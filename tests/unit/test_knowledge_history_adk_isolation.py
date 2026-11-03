"""Characterize the unresolved ADK history boundary before enabling scoped RAG.

These expected failures are release blockers for scoped retrieval, not evidence
that the shared ADK runner provides isolation. No external models are invoked.
"""
import unittest

try:
    from google.adk.agents import LlmAgent
    from google.adk.events import Event
    from google.adk.models import BaseLlm, LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
except ImportError:
    LlmAgent = None


@unittest.skipIf(LlmAgent is None, "google-adk 1.19 is not installed")
class SharedHistoryIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        captured = []
        self.captured = captured

        class DeterministicLlm(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                captured.append((self.model, llm_request.model_dump_json(exclude_none=True)))
                count = sum(name == self.model for name, _ in captured)
                if self.model == "root" and count == 1:
                    part = types.Part(function_call=types.FunctionCall(
                        name="transfer_to_agent", args={"agent_name": "agent_a"}))
                elif self.model == "agent_a" and count == 1:
                    part = types.Part(text="derived-private-a")
                elif self.model == "agent_a":
                    part = types.Part(function_call=types.FunctionCall(
                        name="transfer_to_agent", args={"agent_name": "agent_b"}))
                elif self.model == "agent_b" and count > 1:
                    part = types.Part(function_call=types.FunctionCall(
                        name="transfer_to_agent", args={"agent_name": "root"}))
                else:
                    part = types.Part(text="ok")
                yield LlmResponse(content=types.Content(role="model", parts=[part]))

        agent_a = LlmAgent(name="agent_a", model=DeterministicLlm(model="agent_a"), description="A")
        agent_b = LlmAgent(name="agent_b", model=DeterministicLlm(model="agent_b"), description="B")
        root = LlmAgent(name="root", model=DeterministicLlm(model="root"), sub_agents=[agent_a, agent_b])
        service = InMemorySessionService()
        session = await service.create_session(app_name="test", user_id="user", session_id="session")
        # This mirrors the legacy preloader: a system-authored event in the same session.
        await service.append_event(session, Event(invocation_id="preload_knowledge_test", author="system",
            content=types.Content(role="user", parts=[types.Part(text="preloaded-private-a")])))
        runner = Runner(agent=root, app_name="test", session_service=service)
        for text in ("first", "transfer to B", "return to root"):
            async for _ in runner.run_async(user_id="user", session_id="session",
                    new_message=types.Content(role="user", parts=[types.Part(text=text)])):
                pass

    @unittest.expectedFailure
    def test_subagent_b_must_not_receive_a_derived_or_preloaded_material(self):
        requests = [body for name, body in self.captured if name == "agent_b"]
        self.assertTrue(requests)
        for body in requests:
            self.assertNotIn("derived-private-a", body)
            self.assertNotIn("preloaded-private-a", body)

    @unittest.expectedFailure
    def test_returned_root_must_not_receive_other_agents_private_material(self):
        requests = [body for name, body in self.captured if name == "root"]
        self.assertEqual(len(requests), 2)
        self.assertNotIn("derived-private-a", requests[-1])
        self.assertNotIn("preloaded-private-a", requests[-1])
