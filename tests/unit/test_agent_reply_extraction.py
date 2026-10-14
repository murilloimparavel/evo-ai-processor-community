from types import SimpleNamespace
import asyncio

from src.services.adk.runners.standard_runner import _agent_event_text, _recover_agent_reply, _sendable_text


def test_collects_all_visible_model_parts_without_tool_or_thought_text():
    event = SimpleNamespace(content=SimpleNamespace(role="model", parts=[
        SimpleNamespace(text="raciocínio", thought=True, function_call=None, function_response=None),
        SimpleNamespace(text=None, thought=False, function_call={"name": "tool"}, function_response=None),
        SimpleNamespace(text="Olá", thought=False, function_call=None, function_response=None),
        SimpleNamespace(text="posso ajudar.", thought=False, function_call=None, function_response=None),
    ]))
    assert _agent_event_text(event) == "Olá\nposso ajudar."


def test_ignores_non_model_events_and_internal_marker():
    event = SimpleNamespace(content=SimpleNamespace(role="user", parts=[SimpleNamespace(text="oi")]))
    assert _agent_event_text(event) == ""
    assert not _sendable_text(" No response captured. ")
    assert not _sendable_text("Agent escalated: internal error")
    assert _sendable_text("Olá!")


def test_recovery_uses_one_isolated_tool_free_model_turn(monkeypatch):
    import google.adk.agents.llm_agent as llm_module
    import google.adk.runners as runners_module

    calls = {"agents": 0, "runs": 0}
    model = object()

    def fake_agent(**kwargs):
        calls["agents"] += 1
        assert kwargs["model"] is model
        assert kwargs["tools"] == []
        assert "sub_agents" not in kwargs
        return SimpleNamespace(**kwargs)

    class FakeSessionService:
        async def create_session(self, *, app_name, user_id):
            assert app_name == "reply_recovery"
            assert user_id == "recovery"
            return SimpleNamespace(id="temporary-session")

    class FakeRunner:
        def __init__(self, *, agent, app_name):
            assert app_name == "reply_recovery"
            self.session_service = FakeSessionService()

        async def run_async(self, *, user_id, session_id, new_message):
            calls["runs"] += 1
            assert user_id == "recovery"
            assert session_id == "temporary-session"
            assert "pedido do cliente" in new_message.parts[0].text
            yield SimpleNamespace(
                content=SimpleNamespace(role="model", parts=[SimpleNamespace(text="Resposta final", thought=False)]),
                is_final_response=lambda: True,
            )

    monkeypatch.setattr(llm_module, "LlmAgent", fake_agent)
    monkeypatch.setattr(runners_module, "InMemoryRunner", FakeRunner)
    answer = asyncio.run(_recover_agent_reply(model, "pedido do cliente", [], False))
    assert answer == "Resposta final"
    assert calls == {"agents": 1, "runs": 1}
