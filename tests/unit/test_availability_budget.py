"""HTTP budget invariants: no vendor request beyond two, even across builders."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.adk.availability_budget import (
    availability_budget_scope, budgeted_response, budgeted_stream,
    consume_availability_query, is_availability_http_tool,
)
from src.services.adk.custom_tools import CustomToolBuilder
from src.services.adk.tool_builder import ToolBuilder
from src.services.knowledge_runtime import secure_custom


def config(name='availability'):
    return {'name': name, 'description': 'Availability', 'method': 'POST',
            'endpoint': 'http://localhost/api/v1/search', 'parameters': {'body_params': {
                'from': {'type': 'string', 'required': True, 'description': 'Date'},
                'to': {'type': 'string', 'required': True, 'description': 'Date'},
                'persons': {'type': 'integer', 'required': True, 'description': 'Guests'},
                'page': {'type': 'integer', 'description': 'Page'}}}}


@pytest.mark.parametrize('builder', [CustomToolBuilder, ToolBuilder])
def test_third_call_denied_before_http_and_cannot_reset_via_arguments(builder):
    tool = builder()._create_http_tool(config())
    reply = Mock(status_code=200)
    reply.json.return_value = {'sucesso': True}
    with patch('requests.request', return_value=reply) as request, availability_budget_scope():
        for page in [1, 2]:
            assert json.loads(tool.func(from_='2026-12-23', to='2026-12-26', persons=4, page=page))['sucesso']
        denied = json.loads(tool.func(from_='2026-12-23', to='2026-12-26', persons=99, page=99, limit=999))
    assert denied['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED'
    assert request.call_count == 2


def test_aliases_and_builders_share_one_bucket():
    tools = [CustomToolBuilder()._create_http_tool(config('first')),
             ToolBuilder()._create_http_tool(config('second'))]
    reply = Mock(status_code=200)
    reply.json.return_value = {'sucesso': True}
    with patch('requests.request', return_value=reply) as request, availability_budget_scope():
        for tool in tools:
            tool.func(from_='2026-12-23', to='2026-12-26', persons=4)
        recreated = ToolBuilder()._create_http_tool(config('third'))
        assert json.loads(recreated.func(from_='2026-12-23', to='2026-12-26', persons=5))['sucesso'] is False
    assert request.call_count == 2


def test_missing_scope_denies_network_and_failure_does_not_refund():
    tool = CustomToolBuilder()._create_http_tool(config())
    with patch('requests.request', side_effect=TimeoutError('synthetic')) as request:
        assert json.loads(tool.func())['codigo'] == 'AVAILABILITY_BUDGET_UNAVAILABLE'
        request.assert_not_called()
        with availability_budget_scope():
            tool.func()
            tool.func()
            assert json.loads(tool.func())['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED'
        assert request.call_count == 2


def test_other_tools_not_classified_as_availability():
    for change in [{'endpoint': 'https://example.com/reservations'}, {'method': 'GET'},
                   {'parameters': {'body_params': {'query': {}}}}]:
        assert not is_availability_http_tool({**config(), **change})


@pytest.mark.asyncio
async def test_parallel_workers_share_atomic_budget():
    @budgeted_response
    async def turn():
        return await asyncio.gather(*(asyncio.to_thread(consume_availability_query) for _ in range(20)))
    replies = await turn()
    assert replies.count(None) == 2
    assert sum(r is not None for r in replies) == 18


@pytest.mark.asyncio
async def test_turn_reset_nested_delegation_and_customer_isolation():
    @budgeted_response
    async def child():
        return consume_availability_query()

    @budgeted_response
    async def turn():
        await asyncio.sleep(0)
        return [await child(), await child(), await child()]
    first, second = await asyncio.gather(turn(), turn())
    for replies in [first, second, await turn()]:
        assert replies[:2] == [None, None]
        assert replies[2]['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED'
    assert consume_availability_query()['codigo'] == 'AVAILABILITY_BUDGET_UNAVAILABLE'


@pytest.mark.asyncio
async def test_exception_and_cancellation_cleanup():
    @budgeted_response
    async def failing():
        consume_availability_query()
        raise RuntimeError('synthetic')
    with pytest.raises(RuntimeError):
        await failing()
    assert consume_availability_query()['codigo'] == 'AVAILABILITY_BUDGET_UNAVAILABLE'

    @budgeted_response
    async def cancelled():
        consume_availability_query()
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await cancelled()
    assert consume_availability_query()['codigo'] == 'AVAILABILITY_BUDGET_UNAVAILABLE'


@pytest.mark.asyncio
async def test_stream_shares_budget_without_leaking_into_consumer():
    @budgeted_stream
    async def stream():
        for _ in range(3):
            yield consume_availability_query()
    result = []
    async for item in stream():
        result.append(item)
        assert consume_availability_query()['codigo'] == 'AVAILABILITY_BUDGET_UNAVAILABLE'
    assert result[:2] == [None, None]
    assert result[2]['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED'


@pytest.mark.asyncio
async def test_secure_path_shares_budget_and_blocks_before_dns():
    record = SimpleNamespace(method='POST', endpoint='https://example.com/api/v1/search')
    with patch.object(secure_custom, 'validate_policy'), patch.object(secure_custom, 'pinned_address', AsyncMock()) as dns:
        with availability_budget_scope():
            assert consume_availability_query() is None
            assert consume_availability_query() is None
            result = await secure_custom.execute(record, {'purpose': 'availability'}, {}, None, AsyncMock())
        assert result['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED'
        dns.assert_not_awaited()

@pytest.mark.asyncio
async def test_adk_function_tool_cannot_dispatch_third_network_request():
    tool = CustomToolBuilder()._create_http_tool(config())
    reply = Mock(status_code=200)
    reply.json.return_value = {'sucesso': True}
    args = {'from_': '2026-12-23', 'to': '2026-12-26', 'persons': 4}
    with availability_budget_scope(), patch('requests.request', return_value=reply) as request:
        results = await asyncio.gather(*(tool.run_async(args={**args, 'page': i}, tool_context=None) for i in range(1, 6)))
    assert request.call_count == 2
    assert all(json.loads(r)['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED' for r in results[2:])


def test_real_http_server_observes_only_two_requests_and_next_turn_resets():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            data = b'{"sucesso":true}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        value = config()
        value['endpoint'] = f'http://127.0.0.1:{server.server_port}/api/v1/search'
        tool = ToolBuilder()._create_http_tool(value)
        for _ in range(2):
            with availability_budget_scope():
                for page in [1, 2]:
                    assert json.loads(tool.func(from_='2026-12-23', to='2026-12-26', persons=4, page=page))['sucesso']
                assert json.loads(tool.func(page=3))['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED'
        assert len(received) == 4
        assert [r['page'] for r in received] == [1, 2, 1, 2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

@pytest.mark.asyncio
async def test_live_budget_resets_on_server_completion_not_input_or_interruption():
    from src.services.adk.runners.live_runner import LiveRunner
    websocket = SimpleNamespace(send_text=AsyncMock())
    runner = LiveRunner.__new__(LiveRunner)

    async def events():
        assert consume_availability_query() is None
        assert consume_availability_query() is None
        yield SimpleNamespace(turn_complete=False, interrupted=True)
        assert consume_availability_query()['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED'
        yield SimpleNamespace(turn_complete=True, interrupted=False)
        assert consume_availability_query() is None
        assert consume_availability_query() is None
        assert consume_availability_query()['codigo'] == 'AVAILABILITY_QUERY_LIMIT_REACHED'
        raise asyncio.CancelledError()

    with availability_budget_scope():
        with pytest.raises(asyncio.CancelledError):
            await runner.agent_to_client_messaging(websocket, events())
    assert consume_availability_query()['codigo'] == 'AVAILABILITY_BUDGET_UNAVAILABLE'
