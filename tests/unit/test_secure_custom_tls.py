"""Real TLS verifies the approved hostname while connecting to a pinned IP."""
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import ssl
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

path = Path(__file__).resolve().parents[2] / 'src/services/knowledge_runtime/secure_custom.py'
spec = importlib.util.spec_from_file_location('secure_tls_test', path)
secure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(secure)


class Handler(BaseHTTPRequestHandler):
    calls = 0
    def do_GET(self):
        type(self).calls += 1
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"payment_status":"pending"}')
    def log_message(self, *args): pass


class TlsTest(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_ip_requires_original_hostname_certificate(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'approved.example')])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('approved.example')]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory(prefix='custom-tls-test-') as directory:
            cert_path, key_path = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            Handler.calls = 0
            try:
                with patch.dict(os.environ, {'SSL_CERT_FILE': str(cert_path), 'KNOWLEDGE_CUSTOM_TOOLS_ENABLED': 'true'}), \
                     patch.object(secure, 'pinned_address', AsyncMock(return_value='127.0.0.1')):
                    for hostname, valid in [('wrong.example', False), ('approved.example', True)]:
                        origin = f'https://{hostname}:{server.server_port}'
                        record = SimpleNamespace(id='11111111-1111-4111-8111-111111111111', name='payment_status', description='Read status',
                            method='GET', endpoint=origin + '/status', headers={}, path_params={}, query_params={}, body_params={}, values={}, error_handling={})
                        policy = {'tool_id': str(record.id), 'fingerprint': secure.fingerprint_config(record), 'effect': 'read',
                            'purpose': 'payment_status', 'method': 'GET', 'origin': origin, 'path': '/status', 'response_fields': ['payment_status']}
                        if not valid:
                            with self.assertRaises(secure.CustomDenied): await secure.execute(record, policy, {}, None, AsyncMock())
                            self.assertEqual(Handler.calls, 0)
                        else:
                            result = await secure.execute(record, policy, {}, None, AsyncMock())
                            self.assertEqual(result['result']['payment_status'], 'pending')
                            self.assertEqual(Handler.calls, 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == '__main__': unittest.main()
