"""Exercise the real transition functions without booting DB/ADK at import."""
import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).parents[2]
source = ROOT / 'src/services/adk/tools/evo_crm/pipeline_manipulation.py'
parsed = ast.parse(source.read_text())
functions = [n for n in parsed.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {'_add_to_pipeline', '_move_to_stage'}]
namespace = dict(Any=Any, Dict=Dict, List=List, Optional=Optional, EvoCrmClient=object, logger=Mock())
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
add, move = namespace['_add_to_pipeline'], namespace['_move_to_stage']
spec = importlib.util.spec_from_file_location('gate', ROOT / 'src/services/adk/agents/crm_context_gate.py')
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
RULES = [{'pipelineId': 'p', 'stages': [{'stageId': 's', 'stageName': 'Selected'}]}]


class PipelineRecoveryTest(IsolatedAsyncioTestCase):
    async def test_existing_card_is_moved_once(self):
        client = NS(post=AsyncMock(side_effect=RuntimeError("HTTP 400 BUSINESS_RULE_VIOLATION Item already has an active journey in this pipeline")), patch=AsyncMock(return_value={}))
        result = await add(client, 'p', None, 'c', 's', None, RULES)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(client.post.await_count, 1)
        self.assertEqual(client.patch.await_count, 1)
        self.assertEqual(client.patch.call_args.kwargs['json_data']['new_stage_id'], 's')

    async def test_missing_card_is_created_at_exact_stage_once(self):
        client = NS(patch=AsyncMock(side_effect=RuntimeError('HTTP 404 Pipeline conversation c not found in pipeline Support')), post=AsyncMock(return_value={}))
        result = await move(client, 'p', 'c', 's', None, RULES)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(client.post.call_args.kwargs['json_data'], {'type': 'conversation', 'item_id': 'c', 'pipeline_stage_id': 's'})
        self.assertEqual(client.patch.await_count, 1)

    async def test_invalid_stage_or_auth_or_server_failure_never_creates(self):
        for error in ['HTTP 404 Stage not found in this pipeline', 'HTTP 401 Unauthorized', 'HTTP 503 unavailable']:
            client = NS(patch=AsyncMock(side_effect=RuntimeError(error)), post=AsyncMock())
            result = await move(client, 'p', 'c', 's', None, RULES)
            self.assertEqual(result['status'], 'error')
            client.post.assert_not_awaited()

    async def test_recovery_does_not_loop_on_race(self):
        client = NS(post=AsyncMock(side_effect=RuntimeError('HTTP 400 BUSINESS_RULE_VIOLATION Item already has an active journey in this pipeline')), patch=AsyncMock(side_effect=RuntimeError('HTTP 404 Pipeline conversation c not found in pipeline Support')))
        self.assertEqual((await add(client, 'p', None, 'c', 's', None, RULES))['status'], 'error')
        self.assertEqual(client.post.await_count, 1)
        self.assertEqual(client.patch.await_count, 1)

    async def test_no_context_never_writes(self):
        client = NS(post=AsyncMock(), patch=AsyncMock())
        self.assertEqual((await move(client, 'p', None, 's', None, RULES))['status'], 'error')
        client.post.assert_not_awaited(); client.patch.assert_not_awaited()

    async def test_unbound_request_prunes_only_crm_tools_without_mutating_agent(self):
        original = NS(function_declarations=[NS(name='pipeline_manipulation'), NS(name='consultar_reserva_crcn'), NS(name='transfer_to_agent')])
        request = NS(tools_dict={x.name: object() for x in original.function_declarations}, config=NS(tools=[original], tool_config=None))
        await gate.restrict_unbound_crm_tools(NS(state={'contactId': 'contact', 'session_id': 'session', 'user_input': 'conversation_id=fake'}), request)
        self.assertEqual(set(request.tools_dict), {'consultar_reserva_crcn', 'transfer_to_agent'})
        self.assertEqual(len(original.function_declarations), 3)
        self.assertEqual(len(request.config.tools[0].function_declarations), 2)

    async def test_real_crm_context_preserves_tools(self):
        request = NS(tools_dict={'pipeline_manipulation': object()}, config=NS(tools=[object()]))
        before = request.config.tools
        await gate.restrict_unbound_crm_tools(NS(state={'evoai_crm_data': {'conversation': {'id': 'c'}}}), request)
        self.assertIs(request.config.tools, before)
        self.assertIn('pipeline_manipulation', request.tools_dict)
