import time
import hashlib
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("knowledge_runtime_grant", Path(__file__).resolve().parents[2] / "src/services/knowledge_runtime/grant.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
GrantVerifier, InvalidGrant = module.GrantVerifier, module.InvalidGrant


class GrantVerifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()).decode()
        cls.public_key = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def setUp(self):
        now = int(time.time())
        self.claims = {"iss": "crm", "aud": "knowledge-retrieval", "iat": now, "exp": now + 60,
                       "jti": str(uuid4()), "account_scope": "account-1", "conversation_id": str(uuid4()),
                       "inbox_id": str(uuid4()), "contact_id": str(uuid4()), "bot_id": str(uuid4()), "root_agent_id": str(uuid4()),
                       "event_id": str(uuid4())}
        self.verifier = GrantVerifier(public_keys={"key-1": self.public_key}, issuer="crm", audience="knowledge-retrieval", expected_account_scope="account-1")

    def token(self, claims=None, key=None, kid="key-1"):
        return jwt.encode(claims or self.claims, key or self.private_key, algorithm="RS256", headers={"kid": kid})

    def verify(self, token):
        return self.verifier.verify(token, expected_root_agent_id=self.claims["root_agent_id"],
                                    expected_conversation_id=self.claims["conversation_id"], expected_contact_id=self.claims["contact_id"])

    def test_rejects_unproven_tenant_scope(self):
        token = self.token({**self.claims, "account_scope": "other"})
        with self.assertRaises(InvalidGrant):
            self.verifier.verify(token, expected_root_agent_id=self.claims["root_agent_id"],
                                 expected_conversation_id=self.claims["conversation_id"], expected_contact_id=self.claims["contact_id"])

    def test_request_binding_requires_exact_signed_body(self):
        body = b'{"method":"message/send","params":{"message":"hi"}}'
        token = self.token({**self.claims, "request_sha256": hashlib.sha256(body).hexdigest()})
        args = dict(expected_root_agent_id=self.claims["root_agent_id"],
                    expected_conversation_id=self.claims["conversation_id"], expected_contact_id=self.claims["contact_id"])
        self.verifier.verify_request(token, request_body=body, **args)
        for candidate, supplied in [(token, body + b" "), (self.token(), body), (token, b"")]:
            with self.assertRaises(InvalidGrant):
                self.verifier.verify_request(candidate, request_body=supplied, **args)

    def test_valid_grant_is_immutable_and_bound_to_conversation_and_root(self):
        result = self.verify(self.token())
        self.assertEqual(result.conversation_id, self.claims["conversation_id"])
        with self.assertRaises(AttributeError):
            result.account_scope = "other"
        with self.assertRaises(InvalidGrant):
            self.verifier.verify(self.token(), expected_root_agent_id=str(uuid4()), expected_conversation_id=result.conversation_id, expected_contact_id=result.contact_id)
        with self.assertRaises(InvalidGrant):
            self.verifier.verify(self.token(), expected_root_agent_id=result.root_agent_id, expected_conversation_id=str(uuid4()), expected_contact_id=result.contact_id)
        with self.assertRaises(InvalidGrant):
            self.verifier.verify(self.token(), expected_root_agent_id=result.root_agent_id, expected_conversation_id=result.conversation_id, expected_contact_id=str(uuid4()))

    def test_rejects_tampered_signature_wrong_key_and_unknown_key_id(self):
        token = self.token()
        parts = token.split('.')
        parts[1] = parts[1][:-1] + ('A' if parts[1][-1] != 'A' else 'B')
        for candidate in ('.'.join(parts), self.token(kid='unknown')):
            with self.assertRaises(InvalidGrant):
                self.verify(candidate)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(InvalidGrant):
            self.verify(self.token(key=other))

    def test_rejects_unsigned_and_wrong_algorithm_and_accepts_rotated_key(self):
        unsigned = jwt.encode(self.claims, key='', algorithm='none', headers={'kid': 'key-1'})
        hmac = jwt.encode(self.claims, key='not-an-rsa-key', algorithm='HS256', headers={'kid': 'key-1'})
        for candidate in (unsigned, hmac):
            with self.assertRaises(InvalidGrant):
                self.verify(candidate)
        rotated = GrantVerifier(public_keys={'old': self.public_key, 'new': self.public_key},
                                issuer='crm', audience='knowledge-retrieval', expected_account_scope='account-1')
        self.assertEqual(rotated.verify(self.token(kid='new'), expected_root_agent_id=self.claims['root_agent_id'],
                                        expected_conversation_id=self.claims['conversation_id'], expected_contact_id=self.claims['contact_id']).grant_id, self.claims['jti'])

    def test_expired_future_issuer_audience_and_long_ttl_are_rejected(self):
        now = int(time.time())
        for mutation in ({'iat':now-120,'exp':now-60},{'iat':now+10,'exp':now+60},
                         {'iss':'other'},{'aud':'other'},{'exp':now+61}, {'jti':'invalid'}):
            with self.subTest(mutation=mutation), self.assertRaises(InvalidGrant):
                self.verify(self.token({**self.claims, **mutation}))

    def test_missing_fields_and_unconfigured_keys_fail_closed(self):
        for field in self.claims:
            claims = dict(self.claims)
            del claims[field]
            with self.subTest(field=field), self.assertRaises(InvalidGrant):
                self.verify(self.token(claims))
        with self.assertRaises(InvalidGrant):
            GrantVerifier(public_keys={}, issuer='crm', audience='knowledge-retrieval', expected_account_scope='account-1')
        with self.assertRaises(InvalidGrant):
            GrantVerifier(public_keys={'key-1': self.public_key}, issuer='crm', audience='knowledge-retrieval', expected_account_scope='')

    def test_configured_keyring_accepts_overlap_then_revokes_old_key(self):
        new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        new_public = new_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        environment = {'KNOWLEDGE_RUNTIME_GRANT_KID': 'new', 'KNOWLEDGE_RUNTIME_GRANT_PUBLIC_KEY': new_public,
                       'KNOWLEDGE_RUNTIME_HISTORY_PUBLIC_KEYS': json.dumps({'old': self.public_key})}
        args = dict(expected_root_agent_id=self.claims['root_agent_id'],
                    expected_conversation_id=self.claims['conversation_id'], expected_contact_id=self.claims['contact_id'])
        with patch.dict('os.environ', environment, clear=True):
            verifier = GrantVerifier(public_keys=module.configured_public_keys(), issuer='crm',
                                     audience='knowledge-retrieval', expected_account_scope='account-1')
            verifier.verify(self.token(kid='old'), **args)
            verifier.verify(self.token(key=new_key, kid='new'), **args)
            with patch.dict('os.environ', {'KNOWLEDGE_RUNTIME_HISTORY_PUBLIC_KEYS': '{}'}):
                verifier = GrantVerifier(public_keys=module.configured_public_keys(), issuer='crm',
                                         audience='knowledge-retrieval', expected_account_scope='account-1')
                with self.assertRaises(InvalidGrant):
                    verifier.verify(self.token(kid='old'), **args)
            with patch.dict('os.environ', {'KNOWLEDGE_RUNTIME_HISTORY_PUBLIC_KEYS': json.dumps({'new': self.public_key})}):
                with self.assertRaises(InvalidGrant):
                    module.configured_public_keys()


if __name__ == '__main__':
    unittest.main()
