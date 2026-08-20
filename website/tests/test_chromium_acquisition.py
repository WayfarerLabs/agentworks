import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chromium_test_support as chromium_support
from test_lander_phase4k_browser_cleanup import (
    _FakeProcess,
    _ProbeConnection,
    _ProbeProfile,
)


class _HandshakeEofSocket:
    def __init__(self) -> None:
        self.closed = False
        self.receive_count = 0

    def sendall(self, data: bytes) -> None:
        del data

    def recv(self, length: int) -> bytes:
        del length
        self.receive_count += 1
        return b""

    def close(self) -> None:
        self.closed = True


class _HandshakeSuccessSocket:
    def __init__(self) -> None:
        self.request = b""
        self.response_sent = False
        self.timeouts: list[float] = []

    def sendall(self, data: bytes) -> None:
        self.request = data

    def recv(self, length: int) -> bytes:
        del length
        if self.response_sent:
            return b""
        self.response_sent = True
        key = next(
            line.split(b": ", 1)[1]
            for line in self.request.split(b"\r\n")
            if line.startswith(b"Sec-WebSocket-Key:")
        )
        accept = chromium_support.base64.b64encode(
            chromium_support.hashlib.sha1(
                key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            ).digest()
        )
        return (
            b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: "
            + accept
            + b"\r\n\r\n"
        )

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        pass


class _DiscoveryConnection:
    def __init__(self, targets: list[dict[str, str]]) -> None:
        self.targets = targets
        self.closed = False
        self.calls: list[str] = []

    def call(self, method: str) -> dict[str, object]:
        self.calls.append(method)
        return {"targetInfos": self.targets}

    def close(self) -> None:
        self.closed = True


class ChromiumAcquisitionTests(unittest.TestCase):
    def test_websocket_handshake_eof_fails_once_and_closes_the_socket(self) -> None:
        eof_socket = _HandshakeEofSocket()
        with (
            mock.patch.object(
                chromium_support.socket, "create_connection", return_value=eof_socket
            ),
            self.assertRaises(ConnectionError),
        ):
            chromium_support.DevToolsConnection("ws://127.0.0.1:9222/devtools/page/1")
        self.assertEqual(eof_socket.receive_count, 1)
        self.assertTrue(eof_socket.closed)

    def test_websocket_restores_the_retained_operation_timeout_after_handshake(
        self,
    ) -> None:
        socket = _HandshakeSuccessSocket()
        with mock.patch.object(
            chromium_support.socket, "create_connection", return_value=socket
        ):
            chromium_support.DevToolsConnection(
                "ws://127.0.0.1:9222/devtools/page/1",
                timeout=0.25,
                operation_timeout=10,
            )
        self.assertEqual(socket.timeouts, [10])

    def test_devtools_target_waits_for_complete_port_and_page_publication(self) -> None:
        process = _FakeProcess()
        connections = (
            _DiscoveryConnection([]),
            _DiscoveryConnection([{"type": "page", "targetId": "page-1"}]),
        )
        connection_calls: list[tuple[str, float, float]] = []

        def connect(
            url: str, *, timeout: float, operation_timeout: float
        ) -> _DiscoveryConnection:
            connection_calls.append((url, timeout, operation_timeout))
            return connections[len(connection_calls) - 1]

        with tempfile.TemporaryDirectory() as directory:
            port_path = Path(directory) / "DevToolsActivePort"
            port_path.write_text("", encoding="utf-8")
            sleeps = 0

            def publish(seconds: float) -> None:
                nonlocal sleeps
                del seconds
                sleeps += 1
                if sleeps == 1:
                    port_path.write_text(
                        "9222\n/devtools/browser/browser-1\n", encoding="utf-8"
                    )

            with mock.patch.object(chromium_support.time, "sleep", side_effect=publish):
                target = chromium_support.devtools_target(
                    Path(directory), process, connection_factory=connect
                )

        self.assertEqual(target, "ws://127.0.0.1:9222/devtools/page/page-1")
        self.assertEqual(sleeps, 2)
        self.assertEqual(
            [url for url, _, _ in connection_calls],
            ["ws://127.0.0.1:9222/devtools/browser/browser-1"] * 2,
        )
        self.assertTrue(all(0 < timeout <= 0.25 for _, timeout, _ in connection_calls))
        self.assertTrue(
            all(timeout == operation for _, timeout, operation in connection_calls)
        )
        self.assertEqual(
            [connection.calls for connection in connections],
            [["Target.getTargets"]] * 2,
        )
        self.assertTrue(all(connection.closed for connection in connections))

    def test_devtools_target_bounds_an_accepting_but_stalled_browser_socket(
        self,
    ) -> None:
        process = _FakeProcess()
        elapsed = 0.0

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        def stall(url: str, *, timeout: float, operation_timeout: float) -> None:
            del url
            self.assertEqual(operation_timeout, timeout)
            advance(timeout)
            raise TimeoutError

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "DevToolsActivePort").write_text(
                "9222\n/devtools/browser/browser-1\n", encoding="utf-8"
            )
            with (
                mock.patch.object(
                    chromium_support.time, "monotonic", side_effect=lambda: elapsed
                ),
                mock.patch.object(chromium_support.time, "sleep", side_effect=advance),
                self.assertRaises(AssertionError),
            ):
                chromium_support.devtools_target(
                    Path(directory), process, connection_factory=stall
                )

        self.assertLessEqual(elapsed, chromium_support.DEVTOOLS_STARTUP_ATTEMPT_TIMEOUT)

    def test_devtools_target_bounds_each_fresh_startup_attempt(self) -> None:
        process = _FakeProcess()
        elapsed = 0.0

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    chromium_support.time, "monotonic", side_effect=lambda: elapsed
                ),
                mock.patch.object(chromium_support.time, "sleep", side_effect=advance),
                self.assertRaises(AssertionError),
            ):
                chromium_support.devtools_target(Path(directory), process)

        self.assertEqual(
            elapsed,
            chromium_support.DEVTOOLS_STARTUP_ATTEMPT_TIMEOUT,
        )

    def test_acquire_chromium_exhausts_two_fresh_attempts_and_cleans_both(self) -> None:
        processes = [_FakeProcess(), _FakeProcess()]
        process_queue = list(processes)
        profiles = [
            _ProbeProfile(name="/tmp/chromium-exhausted-profile-1"),
            _ProbeProfile(name="/tmp/chromium-exhausted-profile-2"),
        ]
        profile_queue = list(profiles)
        failures = [AssertionError(), OSError()]
        target_calls = 0

        def target(path: Path, process: _FakeProcess, *, timeout: float) -> str:
            nonlocal target_calls
            self.assertEqual(path, Path(profiles[target_calls].name))
            self.assertIs(process, processes[target_calls])
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, chromium_support.DEVTOOLS_STARTUP_ATTEMPT_TIMEOUT)
            failure = failures[target_calls]
            target_calls += 1
            raise failure

        with self.assertRaises(AssertionError) as captured:
            chromium_support.acquire_chromium(
                "chromium-test",
                popen_factory=lambda *args, **kwargs: process_queue.pop(0),
                target_factory=target,
                tempdir_factory=lambda: profile_queue.pop(0),
                sleep=lambda seconds: None,
            )

        self.assertEqual(target_calls, chromium_support.DEVTOOLS_STARTUP_ATTEMPTS)
        self.assertIs(captured.exception.__cause__, failures[-1])
        self.assertTrue(all(process.terminated for process in processes))
        self.assertTrue(all(profile.cleaned for profile in profiles))

    def test_json_probe_retries_one_fresh_browser_after_startup_failure(self) -> None:
        processes = [_FakeProcess(), _FakeProcess()]
        process_queue = list(processes)
        profiles = [
            _ProbeProfile(name="/tmp/chromium-probe-profile-1"),
            _ProbeProfile(name="/tmp/chromium-probe-profile-2"),
        ]
        profile_queue = list(profiles)
        connection = _ProbeConnection(ready=True)
        connection_timeouts: list[tuple[float, float]] = []
        target_timeouts: list[float] = []
        target_calls = 0

        def target(path: Path, process: _FakeProcess, *, timeout: float) -> str:
            nonlocal target_calls
            target_timeouts.append(timeout)
            self.assertEqual(path, Path(profiles[target_calls].name))
            self.assertIs(process, processes[target_calls])
            if target_calls == 1:
                self.assertTrue(processes[0].terminated)
                self.assertTrue(profiles[0].cleaned)
            target_calls += 1
            if target_calls == 1:
                raise AssertionError("first Chromium startup remained unresponsive")
            return "ws://chromium.test"

        def connect(
            url: str, *, timeout: float, operation_timeout: float
        ) -> _ProbeConnection:
            del url
            connection_timeouts.append((timeout, operation_timeout))
            return connection

        result = chromium_support.browser_json_probe(
            "chromium-test",
            "http://127.0.0.1:8000/lander/",
            960,
            "#result",
            connection_factory=connect,
            popen_factory=lambda *args, **kwargs: process_queue.pop(0),
            target_factory=target,
            tempdir_factory=lambda: profile_queue.pop(0),
            sleep=lambda seconds: None,
        )

        self.assertEqual(result, {"ready": True})
        self.assertEqual(target_calls, 2)
        self.assertEqual(len(target_timeouts), 2)
        self.assertTrue(
            all(
                0 < timeout <= chromium_support.DEVTOOLS_STARTUP_ATTEMPT_TIMEOUT
                for timeout in target_timeouts
            )
        )
        self.assertEqual(len(connection_timeouts), 1)
        self.assertGreater(connection_timeouts[0][0], 0)
        self.assertLessEqual(connection_timeouts[0][0], 1)
        self.assertEqual(connection_timeouts[0][1], 10)
        self.assertTrue(connection.closed)
        self.assertTrue(all(process.terminated for process in processes))
        self.assertTrue(all(profile.cleaned for profile in profiles))

    def test_acquire_chromium_bounds_and_retries_a_stalled_page_connection(
        self,
    ) -> None:
        elapsed = 0.0
        processes = [_FakeProcess(), _FakeProcess()]
        process_queue = list(processes)
        profiles = [
            _ProbeProfile(name="/tmp/chromium-page-profile-1"),
            _ProbeProfile(name="/tmp/chromium-page-profile-2"),
        ]
        profile_queue = list(profiles)
        connection = _ProbeConnection(ready=True)
        target_calls = 0
        connection_timeouts: list[tuple[float, float]] = []
        target_timeouts: list[float] = []

        def target(path: Path, process: _FakeProcess, *, timeout: float) -> str:
            nonlocal elapsed, target_calls
            target_timeouts.append(timeout)
            self.assertEqual(path, Path(profiles[target_calls].name))
            self.assertIs(process, processes[target_calls])
            if target_calls == 0:
                elapsed += 19.75
            else:
                self.assertTrue(processes[0].terminated)
                self.assertTrue(profiles[0].cleaned)
            target_calls += 1
            return "ws://chromium.test"

        def connect(
            url: str, *, timeout: float, operation_timeout: float
        ) -> _ProbeConnection:
            nonlocal elapsed
            del url
            connection_timeouts.append((timeout, operation_timeout))
            if len(connection_timeouts) == 1:
                elapsed += timeout
                raise TimeoutError
            return connection

        with mock.patch.object(
            chromium_support.time, "monotonic", side_effect=lambda: elapsed
        ):
            profile, process, acquired = chromium_support.acquire_chromium(
                "chromium-test",
                connection_factory=connect,
                popen_factory=lambda *args, **kwargs: process_queue.pop(0),
                target_factory=target,
                tempdir_factory=lambda: profile_queue.pop(0),
                sleep=lambda seconds: None,
            )

        self.assertIs(acquired, connection)
        self.assertIs(process, processes[1])
        self.assertIs(profile, profiles[1])
        self.assertEqual(target_calls, 2)
        self.assertEqual(target_timeouts, [20, 20])
        self.assertAlmostEqual(connection_timeouts[0][0], 0.25)
        self.assertEqual(connection_timeouts[1][0], 1)
        self.assertEqual([operation for _, operation in connection_timeouts], [10, 10])
        self.assertEqual(elapsed, 20)
        acquired.close()
        chromium_support.stop_process(process)
        chromium_support.cleanup_profile(profile)


if __name__ == "__main__":
    unittest.main()
