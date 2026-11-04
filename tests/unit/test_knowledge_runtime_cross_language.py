"""Synthetic Rails -> processor JWT contract; no CRM DB, runtime or network."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('knowledge_runtime_grant_cross', ROOT / 'src/services/knowledge_runtime/grant.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@unittest.skipUnless(os.environ.get('KNOWLEDGE_CRM_ROOT') and os.environ.get('KNOWLEDGE_RUBY'),
                     'Set KNOWLEDGE_CRM_ROOT and KNOWLEDGE_RUBY for the synthetic cross-language contract')
class CrossLanguageContractTest(unittest.TestCase):
    def test_ruby_issuer_and_python_verifier_key_rotation(self):
        keys = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048) for kid in ('old', 'new')}
        public = {kid: key.public_key().public_bytes(serialization.Encoding.PEM,
                 serialization.PublicFormat.SubjectPublicKeyInfo).decode() for kid, key in keys.items()}
        ids = {name: str(uuid4()) for name in ('conversation_id', 'inbox_id', 'contact_id', 'bot_id', 'root_agent_id', 'event_id')}
        ruby = os.environ['KNOWLEDGE_RUBY']
        for kid, key in keys.items():
            private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
            body = json.dumps({'message': 'mensagem de teste', 'id': ids['event_id']}, ensure_ascii=False).encode()
            input_data = {**ids, 'kid': kid, 'private_key': private, 'request_sha256': hashlib.sha256(body).hexdigest()}
            result = subprocess.run([ruby, str(ROOT / 'tests/fixtures/issue_knowledge_grant.rb')],
                                    input=json.dumps(input_data), text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=os.environ['KNOWLEDGE_CRM_ROOT'], check=True)
            token = result.stdout
            verifier = module.GrantVerifier(public_keys=public, issuer='crm', audience='knowledge-retrieval', expected_account_scope='account-1')
            grant = verifier.verify_request(token, request_body=body, expected_root_agent_id=ids['root_agent_id'],
                                            expected_conversation_id=ids['conversation_id'], expected_contact_id=ids['contact_id'])
            self.assertEqual(grant.event_id, ids['event_id'])
            self.assertEqual(grant.inbox_id, ids['inbox_id'])
            with self.assertRaises(module.InvalidGrant):
                verifier.verify_request(token, request_body=body + b' ', expected_root_agent_id=ids['root_agent_id'],
                                        expected_conversation_id=ids['conversation_id'], expected_contact_id=ids['contact_id'])
            without_key = module.GrantVerifier(public_keys={k: v for k, v in public.items() if k != kid},
                                              issuer='crm', audience='knowledge-retrieval', expected_account_scope='account-1')
            with self.assertRaises(module.InvalidGrant):
                without_key.verify(token, expected_root_agent_id=ids['root_agent_id'],
                                   expected_conversation_id=ids['conversation_id'], expected_contact_id=ids['contact_id'])
            with self.assertRaises(module.InvalidGrant):
                verifier.verify(token, expected_root_agent_id=str(uuid4()), expected_conversation_id=ids['conversation_id'], expected_contact_id=ids['contact_id'])


if __name__ == '__main__':
    unittest.main()
