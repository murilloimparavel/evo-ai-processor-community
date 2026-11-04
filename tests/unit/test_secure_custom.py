"""Security contract for approved, read-only isolated HTTP tools (no network)."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

path = Path(__file__).resolve().parents[2] / 'src/services/knowledge_runtime/secure_custom.py'
spec = importlib.util.spec_from_file_location('secure_custom_unit', path)
secure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(secure)


def record():
    return SimpleNamespace(id='11111111-1111-4111-8111-111111111111', name='reservation_status', description='Read reservation',
        method='POST', endpoint='https://service.example/reservations/{code}', headers={'Authorization': 'Bearer fixture-secret'},
        path_params={'code': {'type': 'string', 'required': True}}, query_params={},
        body_params={'persons': {'type': 'integer'}, 'conversation': {'type': 'string'}}, values={}, error_handling={})


def policy(tool):
    return {'tool_id': str(tool.id), 'fingerprint': secure.fingerprint_config(tool), 'effect': 'read',
        'purpose': 'reservation_lookup', 'method': 'POST', 'origin': 'https://service.example',
        'path': '/reservations/{code}', 'response_fields': ['status', 'code'], 'context_params': {'conversation': 'conversation_id'}}


class Response:
    def __init__(self, status=200, content=None):
        self.status_code = status
        self.headers = {'content-type': 'application/json'}
        self.content = content or json.dumps({'status': 'pending', 'code': 'Bearer fixture-secret', 'secret': 'hidden'}).encode()
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def aiter_bytes(self): yield self.content
    async def aclose(self): pass


class Client:
    calls = []
    response = None
    def __init__(self, **kwargs): self.options = kwargs
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def handle_async_request(self, request):
        self.calls.append((self.options, request))
        return self.response or Response()


class SecureCustomTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'KNOWLEDGE_CUSTOM_TOOLS_ENABLED': 'true', 'KNOWLEDGE_CUSTOM_EGRESS_NETWORKS': '{}'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tool = record()
        self.policy = policy(self.tool)
        self.grant = SimpleNamespace(conversation_id='verified-conversation', contact_id='verified-contact', event_id='verified-event')
        Client.calls = []
        Client.response = None

    def test_only_explicit_read_policy_and_exact_configuration_are_authorized(self):
        secure.validate_policy(self.tool, self.policy)
        for key, value in [('effect', 'write'), ('purpose', 'payment_create'), ('method', 'DELETE'), ('tool_id', 'different')]:
            with self.subTest(key=key), self.assertRaises(secure.CustomDenied):
                secure.validate_policy(self.tool, {**self.policy, key: value})
        self.tool.headers['Authorization'] = 'changed'
        with self.assertRaises(secure.CustomDenied): secure.validate_policy(self.tool, self.policy)
        os.environ['KNOWLEDGE_CUSTOM_TOOLS_ENABLED'] = 'false'
        with self.assertRaises(secure.CustomDenied): secure.validate_policy(self.tool, policy(self.tool))

    def test_headers_and_destinations_cannot_be_expanded_by_model(self):
        for url in ['http://service.example/a', 'https://u:p@service.example/a', 'https://service.example/a?q=1',
                    'https://service.example/a#x', 'https://service.example/a/../charge', 'https://service.example/%2fcharge']:
            tool = record(); tool.endpoint = url
            candidate = policy(tool); candidate['path'] = secure.urlsplit(url).path
            with self.subTest(url=url), self.assertRaises(secure.CustomDenied): secure.validate_policy(tool, candidate)
        for header in [{'Host': 'evil.example'}, {'Authorization': 'Bearer {code}'}, {'X-Test': 'a\r\nb'}]:
            tool = record(); tool.headers = header
            with self.assertRaises(secure.CustomDenied): secure.validate_policy(tool, policy(tool))

    def test_context_is_hidden_and_arguments_are_strict(self):
        self.assertNotIn('conversation', secure.signature(self.tool, self.policy).parameters)
        path, _, body = secure.request_data(self.tool, self.policy, {'code': 'BOOK123', 'persons': 2}, self.grant)
        self.assertEqual(path, '/reservations/BOOK123')
        self.assertEqual(body['conversation'], 'verified-conversation')
        for args in [{'code': '../charge'}, {'code': '%2fcharge'}, {'code': 'ok', 'conversation': 'forged'},
                     {'code': 'ok', 'persons': True}, {'code': 'ok', 'extra': 'x'}, {}]:
            with self.subTest(args=args), self.assertRaises(secure.CustomDenied):
                secure.request_data(self.tool, self.policy, args, self.grant)

    async def test_adk_raw_arguments_are_validated_before_filtering(self):
        calls = []
        attempts = []
        async def lookup(code: str):
            calls.append(code)
            return {"status": "pending"}
        tool = secure.StrictCustomFunctionTool(lookup, lambda: attempts.append(True))
        with self.assertRaises(secure.CustomDenied):
            await tool.run_async(args={"code": "ABC", "endpoint": "https://evil.example"}, tool_context=None)
        self.assertEqual(calls, [])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(await tool.run_async(args={"code": "ABC"}, tool_context=None), {"status": "pending"})

    def test_keyword_alias_is_unambiguous_and_server_defaults_not_exposed(self):
        self.tool.body_params["from"] = {"type": "string", "required": True}
        self.tool.values["persons"] = 12345
        self.policy = policy(self.tool)
        exposed = secure.signature(self.tool, self.policy).parameters
        self.assertIn("from_", exposed)
        self.assertIsNone(exposed["persons"].default)
        _, _, body = secure.request_data(self.tool, self.policy, {"code": "ABC", "from_": "2026-11-06"}, self.grant)
        self.assertEqual(body["from"], "2026-11-06")
        self.tool.body_params["from_"] = {"type": "string"}
        with self.assertRaises(secure.CustomDenied): secure.signature(self.tool, policy(self.tool))

    async def test_private_dns_is_denied_unless_operator_approves_exact_origin_cidr(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[(2, 1, 6, '', ('127.0.0.1', 443))])):
            with self.assertRaises(secure.CustomDenied): await secure.pinned_address(secure.urlsplit(self.tool.endpoint))
            os.environ['KNOWLEDGE_CUSTOM_EGRESS_NETWORKS'] = json.dumps({'https://service.example': ['127.0.0.1/32']})
            self.assertEqual(await secure.pinned_address(secure.urlsplit(self.tool.endpoint)), '127.0.0.1')
            os.environ['KNOWLEDGE_CUSTOM_EGRESS_NETWORKS'] = json.dumps({'https://service.example': ['10.0.0.0/8']})
            with self.assertRaises(secure.CustomDenied): await secure.pinned_address(secure.urlsplit(self.tool.endpoint))

    async def test_ipv6_and_multicast_destinations_are_not_public_egress(self):
        loop = asyncio.get_running_loop()
        for address in ['224.0.0.1', 'ff02::1', '::', '::ffff:93.184.216.34', 'fc00::1']:
            with self.subTest(address=address), patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[(10, 1, 6, '', (address, 443))])):
                with self.assertRaises(secure.CustomDenied): await secure.pinned_address(secure.urlsplit(self.tool.endpoint))
        os.environ['KNOWLEDGE_CUSTOM_EGRESS_NETWORKS'] = json.dumps({'https://service.example': ['0.0.0.0/0', '::/0']})
        for address in ['224.0.0.1', 'ff02::1', '::', '::ffff:93.184.216.34']:
            with self.subTest(explicit=address), patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[(10, 1, 6, '', (address, 443))])):
                with self.assertRaises(secure.CustomDenied): await secure.pinned_address(secure.urlsplit(self.tool.endpoint))

    async def test_pins_address_preserves_tls_name_and_redacts_credentials(self):
        reauthorize = AsyncMock()
        with patch.object(secure, 'pinned_address', AsyncMock(return_value='93.184.216.34')), patch.object(secure.httpx, 'AsyncHTTPTransport', Client):
            result = await secure.execute(self.tool, self.policy, {'code': 'ABC'}, self.grant, reauthorize)
        options, request = Client.calls[0]
        self.assertEqual(str(request.url), 'https://93.184.216.34/reservations/ABC')
        self.assertEqual(request.headers['Host'], 'service.example')
        self.assertEqual(request.extensions['sni_hostname'], 'service.example')
        self.assertEqual(options['retries'], 0)
        self.assertEqual(result['result'], {'status': 'pending', 'code': '[redacted]'})
        reauthorize.assert_awaited_once()

    def test_reflected_credentials_in_keys_deny_instead_of_colliding(self):
        self.assertEqual(secure._redact({"status": {"note": "fixture-secret"}}, ["fixture-secret"]), {"status": {"note": "[redacted]"}})
        for value in [{"fixture-secret": "approved"}, {"[redacted]": "pending", "fixture-secret": "approved"},
                      {"status": [{"fixture-secret": "approved"}]}]:
            with self.assertRaises(secure.CustomDenied): secure._redact(value, ["fixture-secret"])

    async def test_encoded_response_is_denied_before_decompression(self):
        compressed = Response()
        compressed.headers['content-encoding'] = 'gzip'
        compressed.aiter_bytes = AsyncMock(side_effect=AssertionError('must not read encoded stream'))
        Client.response = compressed
        with patch.object(secure, 'pinned_address', AsyncMock(return_value='93.184.216.34')), patch.object(secure.httpx, 'AsyncHTTPTransport', Client):
            with self.assertRaises(secure.CustomDenied):
                await secure.execute(self.tool, self.policy, {'code': 'ABC'}, self.grant, AsyncMock())
        self.assertEqual(Client.calls[0][1].headers['accept-encoding'], 'identity')
        compressed.aiter_bytes.assert_not_called()

    async def test_redirect_oversized_result_and_revocation_fail_closed(self):
        with patch.object(secure, 'pinned_address', AsyncMock(return_value='93.184.216.34')), patch.object(secure.httpx, 'AsyncHTTPTransport', Client):
            for response in [Response(status=302), Response(content=b' ' * (secure.MAX_BYTES + 1))]:
                Client.response = response
                with self.assertRaises(secure.CustomDenied):
                    await secure.execute(self.tool, self.policy, {'code': 'ABC'}, self.grant, AsyncMock())
            before = len(Client.calls)
            with self.assertRaises(secure.CustomDenied):
                await secure.execute(self.tool, self.policy, {'code': 'ABC'}, self.grant, AsyncMock(side_effect=secure.CustomDenied()))
            self.assertEqual(len(Client.calls), before)


if __name__ == '__main__': unittest.main()
