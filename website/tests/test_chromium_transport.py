import unittest
from unittest import mock

import chromium_test_support as chromium_support


class _TestSocket:
    def __init__(self) -> None:
        self.closed = False
        self.receive_count = 0
        self.timeout: float | None = None

    def close(self) -> None:
        self.closed = True

    def recv(self, length: int) -> bytes:
        del length
        self.receive_count += 1
        return b""

    def sendall(self, data: bytes) -> None:
        del data

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class ChromiumTransportTests(unittest.TestCase):
    def test_websocket_handshake_eof_fails_once_and_closes_the_socket(self) -> None:
        test_socket = _TestSocket()
        with (
            mock.patch.object(
                chromium_support.socket,
                "create_connection",
                return_value=test_socket,
            ),
            self.assertRaises(ConnectionError),
        ):
            chromium_support.DevToolsConnection("ws://127.0.0.1:9222/devtools/page/1")
        self.assertEqual(test_socket.receive_count, 1)
        self.assertTrue(test_socket.closed)

    def test_deadline_bounds_dripped_handshake_and_frame_bytes(self) -> None:
        elapsed = 0.0

        class DripSocket(_TestSocket):
            def recv(self, length: int) -> bytes:
                nonlocal elapsed
                del length
                elapsed += 1
                return b"x"

        handshake_socket = DripSocket()
        with (
            mock.patch.object(chromium_support.time, "monotonic", side_effect=lambda: elapsed),
            mock.patch.object(
                chromium_support.socket,
                "create_connection",
                return_value=handshake_socket,
            ),
            self.assertRaises(TimeoutError),
        ):
            chromium_support.DevToolsConnection("ws://127.0.0.1:9222/devtools/page/1", timeout=3)
        self.assertEqual(elapsed, 3)
        self.assertTrue(handshake_socket.closed)

        elapsed = 0.0
        connection = object.__new__(chromium_support.DevToolsConnection)
        connection.socket = DripSocket()
        with (
            mock.patch.object(chromium_support.time, "monotonic", side_effect=lambda: elapsed),
            self.assertRaises(TimeoutError),
        ):
            connection._read_exact(10, deadline=3)
        self.assertEqual(elapsed, 3)

    def test_deadline_bounds_a_continuous_ping_stream(self) -> None:
        elapsed = 0.0

        class PingSocket(_TestSocket):
            def recv(self, length: int) -> bytes:
                nonlocal elapsed
                if length != 2:
                    raise AssertionError("Ping fixture received an unexpected read length")
                elapsed += 1
                return b"\x89\x00"

        connection = object.__new__(chromium_support.DevToolsConnection)
        connection.socket = PingSocket()
        with (
            mock.patch.object(chromium_support.time, "monotonic", side_effect=lambda: elapsed),
            self.assertRaises(TimeoutError),
        ):
            connection._receive(deadline=3)
        self.assertEqual(elapsed, 3)

    def test_deadline_bounds_a_stalled_close_write(self) -> None:
        elapsed = 2.0

        class CloseStallSocket(_TestSocket):
            def sendall(self, data: bytes) -> None:
                nonlocal elapsed
                del data
                if self.timeout is None:
                    raise AssertionError("Close write has no timeout")
                elapsed += self.timeout
                raise TimeoutError

        test_socket = CloseStallSocket()
        connection = object.__new__(chromium_support.DevToolsConnection)
        connection.socket = test_socket
        with (
            mock.patch.object(chromium_support.time, "monotonic", side_effect=lambda: elapsed),
            self.assertRaises(TimeoutError),
        ):
            connection.close(deadline=3)
        self.assertEqual(elapsed, 3)
        self.assertTrue(test_socket.closed)


if __name__ == "__main__":
    unittest.main()
