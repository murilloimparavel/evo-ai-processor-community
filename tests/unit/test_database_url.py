import ssl
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.config.database_url import (
    configure_pg8000_keepalive,
    get_sync_database_config,
)


class SyncDatabaseConfigTests(unittest.TestCase):
    def test_converts_driver_and_preserves_credentials_and_query(self):
        url, args = get_sync_database_config(
            "postgresql://user:p%40ss@db.example:5432/app?application_name=worker"
        )
        self.assertEqual(url.drivername, "postgresql+pg8000")
        self.assertEqual(url.username, "user")
        self.assertEqual(url.password, "p@ss")
        self.assertEqual(url.query["application_name"], "worker")
        self.assertTrue(args["tcp_keepalive"])

    def test_converts_legacy_psycopg2_driver_url(self):
        url, _ = get_sync_database_config("postgresql+psycopg2://db/app")
        self.assertEqual(url.drivername, "postgresql+pg8000")

    def test_keepalive_configures_all_required_socket_values(self):
        class FakeSocket:
            def __init__(self):
                self.calls = []

            def setsockopt(self, level, option, value):
                self.calls.append((level, option, value))

        fake_socket = FakeSocket()
        connection = SimpleNamespace(_c=SimpleNamespace(_sock=fake_socket))
        configure_pg8000_keepalive(connection, None)
        self.assertEqual(
            fake_socket.calls,
            [
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10),
                (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5),
            ],
        )

    def test_keepalive_fails_closed_without_pg8000_socket(self):
        with self.assertRaisesRegex(RuntimeError, "active socket is unavailable"):
            configure_pg8000_keepalive(SimpleNamespace(_c=SimpleNamespace()), None)

    def test_sslmode_disable_disables_tls(self):
        _, args = get_sync_database_config("postgresql://db/app?sslmode=disable")
        self.assertIs(args["ssl_context"], False)

    def test_sslmode_require_requires_tls_without_certificate_verification(self):
        _, args = get_sync_database_config("postgresql://db/app?sslmode=require")
        self.assertIs(args["ssl_context"], True)

    def test_default_and_prefer_keep_libpq_prefer_semantics(self):
        for suffix in ("", "?sslmode=prefer"):
            with self.subTest(suffix=suffix):
                _, args = get_sync_database_config("postgresql://db/app" + suffix)
                self.assertIsNone(args["ssl_context"])

    def test_sslmode_allow_is_rejected_to_avoid_changing_tls_preference(self):
        with self.assertRaisesRegex(ValueError, "Unsupported PostgreSQL sslmode"):
            get_sync_database_config("postgresql://db/app?sslmode=allow")

    def test_require_with_root_ca_validates_certificate_without_hostname(self):
        context = ssl.create_default_context()
        with patch("src.config.database_url.ssl.create_default_context", return_value=context) as create:
            _, args = get_sync_database_config(
                "postgresql://db/app?sslmode=require&sslrootcert=%2Fetc%2Fdb-ca.pem"
            )
        create.assert_called_once_with(cafile="/etc/db-ca.pem")
        self.assertIs(args["ssl_context"], context)
        self.assertFalse(context.check_hostname)

    def test_verify_full_uses_hostname_verification_and_custom_ca(self):
        context = ssl.create_default_context()
        with patch("src.config.database_url.ssl.create_default_context", return_value=context) as create:
            _, args = get_sync_database_config(
                "postgresql://db/app?sslmode=verify-full&sslrootcert=%2Fetc%2Fdb-ca.pem"
            )
        create.assert_called_once_with(cafile="/etc/db-ca.pem")
        self.assertIs(args["ssl_context"], context)
        self.assertTrue(context.check_hostname)

    def test_verify_ca_disables_hostname_check(self):
        _, args = get_sync_database_config("postgresql://db/app?sslmode=verify-ca")
        self.assertFalse(args["ssl_context"].check_hostname)

    def test_unknown_sslmode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported PostgreSQL sslmode"):
            get_sync_database_config("postgresql://db/app?sslmode=unknown")

    def test_client_certificate_options_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Client certificate"):
            get_sync_database_config("postgresql://db/app?sslcert=%2Ftmp%2Fclient.pem")


if __name__ == "__main__":
    unittest.main()
