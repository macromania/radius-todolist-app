import io
import json
import os
import socket
import ssl
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import test_acceptance as base
from redis.connection import Connection, SSLConnection
from redis.exceptions import ConnectionError as RedisConnectionError

from plane_demo.shared import settings

ROOT = Path(__file__).resolve().parents[2]


class RedisIdentityProbeTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.ping.return_value = True
        self.output = io.StringIO()

    def connected(
        self, connection_type=SSLConnection, socket_type=ssl.SSLSocket, version="TLSv1.3"
    ):
        connection = connection_type(
            host="redis.internal",
            port=10000,
            username="synthetic-user",
            password="synthetic-secret",
        )
        connected = Mock(spec=socket_type)
        connected.getpeername.return_value = ("10.42.1.4", 10000)
        connected.version = Mock(return_value=version)
        connection._sock = connected
        self.addCleanup(setattr, connection, "_sock", None)
        self.store.connection_pool.get_connection.return_value = connection
        return connection

    def execute(self):
        with (
            patch.object(settings.Settings, "from_env") as from_env,
            patch.object(settings, "redis_client", return_value=self.store) as client,
            patch.object(sys, "argv", ["identity-probe", "data-api"]),
            redirect_stdout(self.output),
        ):
            exec(compile(base.runner_module.IDENTITY_PROBE, "<identity-probe>", "exec"), {})
        from_env.assert_called_once_with("data_api")
        client.assert_called_once_with(from_env.return_value)
        return json.loads(self.output.getvalue())

    def test_negotiated_ssl_socket_is_tls_without_legacy_connection_attribute(self):
        connection = self.connected()
        self.assertIsInstance(connection, SSLConnection)
        self.assertFalse(hasattr(connection, "ssl_cert_reqs"))
        self.assertEqual(
            self.execute(),
            {
                "redis": {
                    "host": "redis.internal",
                    "port": 10000,
                    "peer_address": "10.42.1.4",
                    "tls": True,
                }
            },
        )
        self.store.ping.assert_called_once_with()
        connection._sock.version.assert_called_once_with()
        self.store.connection_pool.release.assert_called_once_with(connection)
        self.store.close.assert_called_once_with()

    def test_plain_socket_is_not_tls_even_with_misleading_connection_attributes(self):
        for connection_type in (Connection, SSLConnection):
            with self.subTest(connection_type=connection_type):
                connection = self.connected(connection_type, socket.socket)
                connection.ssl_cert_reqs = ssl.CERT_REQUIRED
                self.output = io.StringIO()
                self.assertIs(self.execute()["redis"]["tls"], False)
                connection._sock.version.assert_not_called()

    def test_ssl_socket_without_negotiated_version_is_not_tls(self):
        self.connected(version=None)
        self.assertIs(self.execute()["redis"]["tls"], False)

    def test_failed_ping_is_checked_before_identity_sampling(self):
        self.connected()
        self.store.ping.return_value = False
        with self.assertRaisesRegex(RuntimeError, "^redis_ping_failed$"):
            self.execute()
        self.store.ping.assert_called_once_with()
        self.store.connection_pool.get_connection.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_ping_exception_does_not_produce_identity_evidence(self):
        self.store.ping.side_effect = RedisConnectionError("synthetic_ping_failure")
        with self.assertRaisesRegex(RedisConnectionError, "^synthetic_ping_failure$"):
            self.execute()
        self.store.ping.assert_called_once_with()
        self.store.connection_pool.get_connection.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_optimized_python_still_executes_and_checks_ping(self):
        result = subprocess.run(
            [
                sys.executable,
                "-OO",
                "-m",
                "unittest",
                f"{__name__}.RedisIdentityProbeTests."
                "test_negotiated_ssl_socket_is_tls_without_legacy_connection_attribute",
                f"{__name__}.RedisIdentityProbeTests.test_failed_ping_is_checked_before_identity_sampling",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests/harness"))),
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 2 tests", result.stderr)
