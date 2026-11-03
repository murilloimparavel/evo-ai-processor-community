"""Exercise the real transition functions without booting DB/ADK at import."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

ROOT = Path(__file__).parents[2]
source = ROOT / 'src/services/adk/tools/evo_crm/pipeline_manipulation.py'
parsed = ast.parse(source.read_text())
functions = [n for n in parsed.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
namespace = dict(Any=Any, Dict=Dict, List=List, Optional=Optional, EvoCrmClient=Mock(), ToolContext=object, FunctionTool=lambda func: NS(func=func), UUID=UUID, logger=Mock())
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
add, move = namespace['_add_to_pipeline'], namespace['_move_to_stage']
spec = importlib.util.spec_from_file_location('gate', ROOT / 'src/services/adk/agents/crm_context_gate.py')
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
PIPELINE = '11111111-1111-4111-8111-111111111111'
STAGE = '33333333-3333-4333-8333-333333333333'
CONVERSATION = '2aaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
CARD = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
CONTACT = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
RULES = [{'pipelineId': PIPELINE, 'stages': [{'stageId': STAGE, 'stageName': 'Selected'}]}]


class PipelineCatalogReadTest(IsolatedAsyncioTestCase):
    def projection(self):
        return {'data': {'pipeline_id': PIPELINE, 'source': 'catalog_v1', 'available': True,
            'catalog_hash': 'a' * 64, 'catalog_version': 1, 'purpose': 'Classify a synthetic journey',
            'stages': [{'id': STAGE, 'name': 'Selected', 'classification_profile': {
                'classification_enabled': True, 'definition': 'Customer is deciding',
                'entry_criteria': ['Company answered the current question']}}]}}

    async def test_reads_approved_configured_pipeline_only_and_never_writes(self):
        client = NS(get=AsyncMock(return_value=self.projection()), post=AsyncMock(), patch=AsyncMock())
        result = await namespace['_get_pipeline_criteria'](client, PIPELINE, RULES)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['criteria_source'], 'catalog_v1')
        self.assertFalse(result['details']['recommendation_is_financial_proof'])
        client.get.assert_awaited_once_with(f'/api/v1/pipelines/{PIPELINE}/classification_catalog/projection')
        client.post.assert_not_awaited(); client.patch.assert_not_awaited()

    async def test_unconfigured_pipeline_is_rejected_before_read(self):
        client = NS(get=AsyncMock())
        result = await namespace['_get_pipeline_criteria'](client, OTHER, RULES)
        self.assertEqual(result['status'], 'error')
        client.get.assert_not_awaited()

    async def test_only_explicit_legacy_source_permits_legacy_hints(self):
        client = NS(get=AsyncMock(return_value={'data': {'pipeline_id': PIPELINE, 'source': 'legacy', 'available': False}}))
        result = await namespace['_get_pipeline_criteria'](client, PIPELINE, RULES)
        self.assertEqual(result['criteria_source'], 'legacy')
        client.get.return_value = {'data': {'pipeline_id': PIPELINE, 'source': 'catalog_v1', 'available': False}}
        result = await namespace['_get_pipeline_criteria'](client, PIPELINE, RULES)
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('criteria_source', result)

    async def test_rejects_wrong_target_or_malformed_criteria(self):
        for mutation in ('target', 'criteria', 'version'):
            response = self.projection()
            if mutation == 'target': response['data']['pipeline_id'] = OTHER
            if mutation == 'criteria': response['data']['stages'][0]['classification_profile']['entry_criteria'] = 'execute arbitrary text'
            if mutation == 'version': response['data']['catalog_version'] = True
            client = NS(get=AsyncMock(return_value=response))
            self.assertEqual((await namespace['_get_pipeline_criteria'](client, PIPELINE, RULES))['status'], 'error')

    async def test_failure_does_not_expose_transport_error_or_fall_back(self):
        client = NS(get=AsyncMock(side_effect=RuntimeError('Bearer synthetic-secret')))
        result = await namespace['_get_pipeline_criteria'](client, PIPELINE, RULES)
        self.assertEqual(result['message'], 'approved_pipeline_criteria_unavailable')
        self.assertNotIn('synthetic-secret', str(result))

    async def test_excludes_disabled_stage_profiles_and_fails_closed_on_oversized_context(self):
        response = self.projection()
        response['data']['stages'].append({'id': OTHER, 'name': 'Ignored', 'classification_profile': {'classification_enabled': False}})
        client = NS(get=AsyncMock(return_value=response))
        result = await namespace['_get_pipeline_criteria'](client, PIPELINE, RULES)
        self.assertEqual(len(result['details']['stages']), 1)
        response['data']['stages'][0]['classification_profile'].update({key: ['x' * 1200] * 8 for key in ('entry_criteria', 'stay_criteria')})
        self.assertEqual((await namespace['_get_pipeline_criteria'](client, PIPELINE, RULES))['status'], 'error')


def create_response(conversation=CONVERSATION):
    # PipelineItemSerializer.serialize(include_entity: true), not a flattened mock.
    return {'success': True, 'data': {
        'id': CARD, 'pipeline_id': PIPELINE, 'pipeline_stage_id': STAGE,
        'stage_id': STAGE, 'conversation_id': conversation,
        'contact_id': None, 'item_id': conversation, 'type': 'conversation',
        'is_lead': False, 'conversation_display_id': 4494,
        'conversation': {'id': conversation, 'display_id': 4494, 'uuid': OTHER},
    }}


def move_response(conversation=CONVERSATION):
    # PipelineItem#push_event_data under data.pipeline_item.
    return {'success': True, 'data': {'pipeline_item': {
        'id': CARD, 'pipeline_id': PIPELINE, 'conversation_id': conversation,
        'contact_id': None, 'is_lead': False,
        'conversation_display_id': 4494, 'pipeline_stage': {'id': STAGE},
    }}}


class PipelineRecoveryTest(IsolatedAsyncioTestCase):
    async def test_existing_card_is_moved_once(self):
        client = NS(post=AsyncMock(side_effect=RuntimeError("HTTP 400 BUSINESS_RULE_VIOLATION Item already has an active journey in this pipeline")), patch=AsyncMock(return_value=move_response()))
        result = await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(client.post.await_count, 1)
        self.assertEqual(client.patch.await_count, 1)
        self.assertEqual(client.patch.call_args.kwargs['json_data']['new_stage_id'], STAGE)

    async def test_missing_card_is_created_at_exact_stage_once(self):
        client = NS(patch=AsyncMock(side_effect=RuntimeError('HTTP 404 Pipeline conversation c not found in pipeline Support')), post=AsyncMock(return_value=create_response()))
        result = await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(client.post.call_args.kwargs['json_data'], {'type': 'conversation', 'item_id': CONVERSATION, 'pipeline_stage_id': STAGE})
        self.assertEqual(client.patch.await_count, 1)

    async def test_invalid_stage_or_auth_or_server_failure_never_creates(self):
        for error in ['HTTP 404 Stage not found in this pipeline', 'HTTP 401 Unauthorized', 'HTTP 503 unavailable']:
            client = NS(patch=AsyncMock(side_effect=RuntimeError(error)), post=AsyncMock())
            result = await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES)
            self.assertEqual(result['status'], 'error')
            client.post.assert_not_awaited()

    async def test_recovery_does_not_loop_on_race(self):
        client = NS(post=AsyncMock(side_effect=RuntimeError('HTTP 400 BUSINESS_RULE_VIOLATION Item already has an active journey in this pipeline')), patch=AsyncMock(side_effect=RuntimeError('HTTP 404 Pipeline conversation c not found in pipeline Support')))
        self.assertEqual((await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES))['status'], 'error')
        self.assertEqual(client.post.await_count, 1)
        self.assertEqual(client.patch.await_count, 1)

    async def test_no_context_never_writes(self):
        client = NS(post=AsyncMock(), patch=AsyncMock())
        self.assertEqual((await move(client, PIPELINE, None, STAGE, None, RULES))['status'], 'error')
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


class PipelineIdentityTest(IsolatedAsyncioTestCase):
    async def test_actual_create_and_move_contracts_confirm_target(self):
        client = NS(post=AsyncMock(return_value=create_response()), patch=AsyncMock(return_value=move_response()))
        self.assertEqual((await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES))['status'], 'success')
        self.assertEqual((await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES))['status'], 'success')

    async def test_uuid_digit_letter_and_case_are_compared_as_uuid(self):
        for conversation in (CONVERSATION, OTHER, CONVERSATION.upper()):
            client = NS(patch=AsyncMock(return_value=move_response(conversation.lower())))
            result = await move(client, PIPELINE.upper(), conversation, STAGE.upper(), None, RULES)
            self.assertEqual(result['status'], 'success')

    async def test_other_conversation_card_is_never_success_and_never_recovered(self):
        for action in ('add_to_pipeline', 'move_to_stage'):
            response = create_response(OTHER) if action == 'add_to_pipeline' else move_response(OTHER)
            response['private_customer_data'] = 'must not escape'
            client = NS(post=AsyncMock(return_value=response), patch=AsyncMock(return_value=response))
            if action == 'add_to_pipeline':
                result = await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES)
                client.patch.assert_not_awaited()
            else:
                result = await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES)
                client.post.assert_not_awaited()
            self.assertEqual(result['error'], 'PIPELINE_IDENTITY_MISMATCH')
            self.assertNotIn('details', result)
            self.assertNotIn('must not escape', str(result))
            self.assertEqual(client.post.await_count + client.patch.await_count, 1)

    async def test_missing_or_malformed_success_is_not_acknowledged(self):
        for response in ({}, None, [], {'success': True, 'data': []}, {'success': False, 'data': create_response()['data']}, {'success': True, 'data': {'pipeline_item': []}}):
            client = NS(post=AsyncMock(return_value=response), patch=AsyncMock(return_value=response))
            self.assertEqual((await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES))['status'], 'error')
            self.assertEqual((await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES))['status'], 'error')
            self.assertEqual(client.post.await_count, 1)
            self.assertEqual(client.patch.await_count, 1)

    async def test_pipeline_stage_and_nested_identity_conflicts_fail_closed(self):
        mutations = (
            lambda item: item.update(pipeline_id=OTHER),
            lambda item: item.pop('pipeline_id'),
            lambda item: item.update(stage_id=OTHER),
            lambda item: item.update(pipeline_stage={'id': OTHER}),
            lambda item: item.update(pipeline_stage={'id': STAGE, 'pipeline_id': OTHER}),
            lambda item: item['conversation'].update(id=OTHER),
            lambda item: item['conversation'].update(display_id=2),
            lambda item: item.update(item_id=CARD),
            lambda item: item.update(is_lead=True),
            lambda item: item.update(type='contact'),
            lambda item: item.update(contact_id=CONTACT),
        )
        for mutate in mutations:
            response = create_response()
            mutate(response['data'])
            client = NS(post=AsyncMock(return_value=response))
            result = await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES)
            self.assertEqual(result['error'], 'PIPELINE_IDENTITY_MISMATCH')

    async def test_move_requires_conversation_pipeline_and_stage(self):
        for field in ('conversation_id', 'pipeline_id', 'pipeline_stage'):
            response = move_response()
            response['data']['pipeline_item'].pop(field)
            client = NS(patch=AsyncMock(return_value=response), post=AsyncMock())
            self.assertEqual((await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES))['status'], 'error')
            client.post.assert_not_awaited()

    async def test_decimal_request_requires_explicit_consistent_display_identity(self):
        for display in ('4494', 4494):
            response = move_response()
            response['data']['pipeline_item']['conversation_display_id'] = display
            result = await move(NS(patch=AsyncMock(return_value=response)), PIPELINE, '4494', STAGE, None, RULES)
            self.assertEqual(result['status'], 'success')
        for display in (None, 2, True, '4494junk'):
            response = move_response()
            response['data']['pipeline_item']['conversation_display_id'] = display
            result = await move(NS(patch=AsyncMock(return_value=response)), PIPELINE, '4494', STAGE, None, RULES)
            self.assertEqual(result['status'], 'error')
        response = move_response()
        response['data']['pipeline_item'].pop('conversation_display_id')
        result = await move(NS(patch=AsyncMock(return_value=response)), PIPELINE, '4494', STAGE, None, RULES)
        self.assertEqual(result['status'], 'error')

    async def test_nested_display_identity_is_sufficient_for_create(self):
        response = create_response()
        response['data'].pop('conversation_display_id')
        result = await add(NS(post=AsyncMock(return_value=response)), PIPELINE, None, '4494', STAGE, None, RULES)
        self.assertEqual(result['status'], 'success')

    async def test_invalid_or_card_id_cannot_be_acknowledged_as_conversation(self):
        for requested in (CARD, '2a-invalid', '2', '0', '-2', '4494junk', '2147483648'):
            client = NS(patch=AsyncMock(return_value=move_response()))
            result = await move(client, PIPELINE, requested, STAGE, None, RULES)
            self.assertEqual(result['status'], 'error')

    async def test_recovery_also_rejects_wrong_target_without_loop(self):
        client = NS(patch=AsyncMock(side_effect=RuntimeError('HTTP 404 Pipeline conversation c not found in pipeline Support')), post=AsyncMock(return_value=create_response(OTHER)))
        self.assertEqual((await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES))['error'], 'PIPELINE_IDENTITY_MISMATCH')
        self.assertEqual(client.patch.await_count, 1)
        self.assertEqual(client.post.await_count, 1)
        client = NS(post=AsyncMock(side_effect=RuntimeError('HTTP 409 BUSINESS_RULE_VIOLATION Item already has an active journey in this pipeline')), patch=AsyncMock(return_value=move_response(OTHER)))
        self.assertEqual((await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES))['error'], 'PIPELINE_IDENTITY_MISMATCH')
        self.assertEqual(client.patch.await_count, 1)
        self.assertEqual(client.post.await_count, 1)

    async def test_context_cannot_be_overridden_by_tool_argument(self):
        for context_state in ({'evoai_crm_data': {'conversation_id': CONVERSATION}}, {'evoai_crm_data': {'conversation': {'id': CONVERSATION}}}, {'conversationId': CONVERSATION}):
            client = NS(post=AsyncMock(), patch=AsyncMock())
            with patch.dict(namespace, EvoCrmClient=lambda: client):
                tool = namespace['create_pipeline_manipulation_tool'](RULES)
                result = await tool.func(action='move_to_stage', conversation_id=OTHER, stage_id=STAGE, tool_context=NS(state=context_state))
            self.assertEqual(result['reason'], 'context_conversation_mismatch')
            client.post.assert_not_awaited()
            client.patch.assert_not_awaited()

    async def test_context_identity_allows_uuid_case_and_is_used_implicitly(self):
        for explicit in (None, CONVERSATION.upper()):
            client = NS(patch=AsyncMock(return_value=move_response()))
            with patch.dict(namespace, EvoCrmClient=lambda: client):
                tool = namespace['create_pipeline_manipulation_tool'](RULES)
                result = await tool.func(action='move_to_stage', conversation_id=explicit, stage_id=STAGE, tool_context=NS(state={'conversation_id': CONVERSATION}))
            self.assertEqual(result['status'], 'success')
            self.assertIn(CONVERSATION, client.patch.call_args.kwargs['endpoint'])

    async def test_contact_only_create_checks_contact_without_confusing_card_id(self):
        response = create_response()
        response['data'].update(conversation_id=None, contact_id=CONTACT, item_id=CONTACT, type='contact', is_lead=True, contact={'id': CONTACT})
        response['data'].pop('conversation')
        client = NS(post=AsyncMock(return_value=response))
        self.assertEqual((await add(client, PIPELINE, CONTACT, None, STAGE, None, RULES))['status'], 'success')
        response['data']['contact']['id'] = OTHER
        self.assertEqual((await add(client, PIPELINE, CONTACT, None, STAGE, None, RULES))['status'], 'error')

    async def test_failure_responses_and_logs_do_not_echo_raw_details(self):
        client = NS(post=AsyncMock(side_effect=RuntimeError('HTTP 422 private_customer_data')))
        with patch.dict(namespace, logger=Mock()):
            logger = namespace['logger']
            result = await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES)
            self.assertNotIn('private_customer_data', str(result))
            self.assertNotIn('private_customer_data', str(logger.mock_calls))

    async def test_conflict_recovery_is_limited_to_active_card_400_or_409(self):
        for status in (400, 409):
            client = NS(post=AsyncMock(side_effect=RuntimeError(f'HTTP {status} BUSINESS_RULE_VIOLATION Item already has an active journey in this pipeline')), patch=AsyncMock(return_value=move_response()))
            self.assertEqual((await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES))['status'], 'success')
            self.assertEqual(client.post.await_count, 1)
            self.assertEqual(client.patch.await_count, 1)
        for status in (401, 422, 503):
            client = NS(post=AsyncMock(side_effect=RuntimeError(f'HTTP {status} BUSINESS_RULE_VIOLATION Item already has an active journey in this pipeline')), patch=AsyncMock())
            self.assertEqual((await add(client, PIPELINE, None, CONVERSATION, STAGE, None, RULES))['status'], 'error')
            client.patch.assert_not_awaited()

    async def test_recovery_requires_the_exact_configured_stage(self):
        client = NS(patch=AsyncMock(side_effect=RuntimeError('HTTP 404 Pipeline conversation c not found in pipeline Support')), post=AsyncMock())
        self.assertEqual((await move(client, PIPELINE, CONVERSATION, OTHER, None, RULES))['status'], 'error')
        client.post.assert_not_awaited()
        client = NS(post=AsyncMock(side_effect=RuntimeError('HTTP 409 BUSINESS_RULE_VIOLATION Item already has an active journey in this pipeline')), patch=AsyncMock())
        self.assertEqual((await add(client, PIPELINE, None, CONVERSATION, OTHER, None, RULES))['status'], 'error')
        client.patch.assert_not_awaited()


async def wrapped_client_error(status, body):
    """Run the real client's error wrapping with an offline HTTP transport double."""
    base_source = ROOT / 'src/services/adk/tools/evo_crm/base.py'
    base_class = next(n for n in ast.parse(base_source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'EvoCrmClient')
    method = next(n for n in base_class.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_make_request')

    class HTTPError(Exception):
        pass

    class HTTPStatusError(HTTPError):
        def __init__(self, response):
            super().__init__('offline HTTP status')
            self.response = response

    response = NS(status_code=status, text='offline response', json=Mock(return_value=body))
    response.raise_for_status = Mock(side_effect=HTTPStatusError(response))
    transport = AsyncMock()
    transport.__aenter__.return_value = NS(request=AsyncMock(return_value=response))
    httpx = NS(HTTPError=HTTPError, HTTPStatusError=HTTPStatusError, RequestError=type('RequestError', (HTTPError,), {}), AsyncClient=Mock(return_value=transport))
    base_namespace = dict(Any=Any, Dict=Dict, Optional=Optional, httpx=httpx, logger=Mock())
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(base_source), 'exec'), base_namespace)
    client = NS(api_token='offline-test', base_url='http://offline.invalid', timeout=1, _get_headers=lambda: {})
    try:
        await base_namespace['_make_request'](client, 'PATCH', '/pipelines/offline/pipeline_items/offline/move_to_stage')
    except HTTPError as error:
        return error
    raise AssertionError('Expected the real client to wrap its HTTP error')


class PipelineContractRevisionTest(IsolatedAsyncioTestCase):
    async def test_new_rails_missing_card_reason_survives_real_client_wrapping(self):
        error = await wrapped_client_error(404, {'success': False, 'error': {'code': 'RESOURCE_NOT_FOUND', 'message': 'Pipeline item not found', 'details': {'reason': 'pipeline_item_not_found'}}})
        client = NS(patch=AsyncMock(side_effect=error), post=AsyncMock(return_value=create_response()))
        result = await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(client.patch.await_count, 1)
        self.assertEqual(client.post.await_count, 1)
        self.assertEqual(client.post.call_args.kwargs['json_data']['item_id'], CONVERSATION)

    async def test_old_rails_missing_card_reason_still_recovers_through_real_client(self):
        error = await wrapped_client_error(404, {'success': False, 'error': {'code': 'RESOURCE_NOT_FOUND', 'message': 'Pipeline conversation c not found in pipeline Support'}})
        client = NS(patch=AsyncMock(side_effect=error), post=AsyncMock(return_value=create_response()))
        self.assertEqual((await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES))['status'], 'success')
        self.assertEqual(client.post.await_count, 1)

    async def test_structured_stage_pipeline_and_auth_failures_do_not_recover(self):
        for status, code, message, details in (
            (404, 'RESOURCE_NOT_FOUND', 'Stage not found in this pipeline', {}),
            (404, 'RESOURCE_NOT_FOUND', 'Pipeline not found', {}),
            (404, 'RESOURCE_NOT_FOUND', 'Pipeline item not found', {}),
            (404, 'RESOURCE_NOT_FOUND', 'Pipeline conversation c not found in pipeline Support', {'reason': 'stage_not_found'}),
            (403, 'FORBIDDEN', 'Not authorized', {'reason': 'pipeline_item_not_found'}),
            (401, 'RESOURCE_NOT_FOUND', 'Not authorized', {'reason': 'pipeline_item_not_found'}),
        ):
            error = await wrapped_client_error(status, {'success': False, 'error': {'code': code, 'message': message, 'details': details}})
            client = NS(patch=AsyncMock(side_effect=error), post=AsyncMock())
            self.assertEqual((await move(client, PIPELINE, CONVERSATION, STAGE, None, RULES))['status'], 'error')
            client.post.assert_not_awaited()

    async def test_proven_context_alias_uses_uuid_for_both_write_actions(self):
        states = (
            {'evoai_crm_data': {'conversation_id': CONVERSATION, 'conversation': {'id': CONVERSATION, 'display_id': 4494}}},
            {'evoai_crm_data': {'conversation_id': CONVERSATION, 'conversation_display_id': 4494}},
            {'evoai_crm_data': {'conversation_id': '004494', 'conversation': {'id': CONVERSATION, 'display_id': 4494}}},
            {'conversationId': '004494', 'evoai_crm_data': {'conversation': {'id': CONVERSATION, 'display_id': 4494}}},
        )
        for state in states:
            for action in ('move_to_stage', 'add_to_pipeline'):
                client = NS(post=AsyncMock(return_value=create_response()), patch=AsyncMock(return_value=move_response()))
                with patch.dict(namespace, EvoCrmClient=lambda: client):
                    tool = namespace['create_pipeline_manipulation_tool'](RULES)
                    result = await tool.func(action=action, conversation_id='0004494', stage_id=STAGE, tool_context=NS(state=state))
                self.assertEqual(result['status'], 'success')
                if action == 'move_to_stage':
                    self.assertIn(CONVERSATION, client.patch.call_args.kwargs['endpoint'])
                else:
                    self.assertEqual(client.post.call_args.kwargs['json_data']['item_id'], CONVERSATION)

    async def test_unproven_or_different_alias_cannot_override_context(self):
        for state, requested in (
            ({'evoai_crm_data': {'conversation_id': CONVERSATION, 'conversation': {'id': CONVERSATION, 'display_id': 4494}}}, '2'),
            ({'evoai_crm_data': {'conversation_id': CONVERSATION}}, '4494'),
            ({'evoai_crm_data': {'conversation_id': CONVERSATION, 'conversation': {'display_id': 4494}}}, '4494'),
            ({'evoai_crm_data': {'conversation_id': CONVERSATION, 'conversation_display_id': 4494}}, OTHER),
        ):
            client = NS(post=AsyncMock(), patch=AsyncMock())
            with patch.dict(namespace, EvoCrmClient=lambda: client):
                tool = namespace['create_pipeline_manipulation_tool'](RULES)
                result = await tool.func(action='move_to_stage', conversation_id=requested, stage_id=STAGE, tool_context=NS(state=state))
            self.assertEqual(result['reason'], 'context_conversation_mismatch')
            client.post.assert_not_awaited()
            client.patch.assert_not_awaited()

    async def test_conflicting_metadata_never_writes_even_without_explicit_argument(self):
        for state in (
            {'evoai_crm_data': {'conversation_id': CONVERSATION, 'conversation': {'id': OTHER, 'display_id': 4494}}},
            {'evoai_crm_data': {'conversation_id': CONVERSATION, 'conversation_display_id': 2, 'conversation': {'id': CONVERSATION, 'display_id': 4494}}},
            {'evoai_crm_data': {'conversation_id': '2', 'conversation': {'id': CONVERSATION, 'display_id': 4494}}},
        ):
            client = NS(post=AsyncMock(), patch=AsyncMock())
            with patch.dict(namespace, EvoCrmClient=lambda: client):
                tool = namespace['create_pipeline_manipulation_tool'](RULES)
                result = await tool.func(action='move_to_stage', stage_id=STAGE, tool_context=NS(state=state))
            self.assertEqual(result['reason'], 'context_conversation_mismatch')
            client.post.assert_not_awaited()
            client.patch.assert_not_awaited()

    async def test_leading_zero_public_numbers_match_the_rails_contract(self):
        client = NS(patch=AsyncMock(return_value=move_response()))
        self.assertEqual((await move(client, PIPELINE, '0004494', STAGE, None, RULES))['status'], 'success')
        for value in ('0', '0000', '002147483648', ' 4494', '4494x', True):
            self.assertIsNone(namespace['_display_identity'](value))
        self.assertEqual(namespace['_display_identity']('002147483647'), '2147483647')
