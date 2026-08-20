# ruff: noqa: F405

import threading

import chromium_test_support as chromium_support
import lander_chromium_phase4k as phase4k_browser
from site_test_support import *  # noqa: F403


class _FakeProcess:
    def __init__(self, *, stuck: bool = False) -> None:
        self.alive = True
        self.stuck = stuck
        self.terminated = False
        self.killed = False
        self.waits = 0

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.waits += 1
        if self.stuck and not self.killed:
            raise subprocess.TimeoutExpired("chromium-test", 5)
        self.alive = False
        return 0

    def kill(self) -> None:
        self.killed = True
        self.alive = False


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


class _NullRootRaceConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.expressions: list[str] = []
        self.closed = False

    def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append((method, params))
        return {}

    def evaluate(self, expression: str) -> bool:
        self.expressions.append(expression)
        if "document.documentElement?.dataset" not in expression:
            raise RuntimeError("null document root was accessed without a guard")
        return False

    def close(self) -> None:
        self.closed = True


class _ProbeProfile:
    def __init__(
        self,
        *,
        name: str = "/tmp/chromium-probe-profile",
        cleanup_failures: int = 0,
    ) -> None:
        self.name = name
        self.cleaned = False
        self.cleanup_failures = cleanup_failures
        self.cleanup_calls = 0

    def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self.cleanup_calls <= self.cleanup_failures:
            raise OSError("profile still active")
        self.cleaned = True


class _ProbeConnection:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.closed = False
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.expressions: list[str] = []

    def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append((method, params))
        return {}

    def evaluate(self, expression: str) -> object:
        self.expressions.append(expression)
        if expression.startswith("JSON.parse"):
            return {"ready": True}
        return self.ready

    def close(self) -> None:
        self.closed = True


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


class Phase4KBrowserCleanupTests(RepositoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.output = self.build()
        self.before = snapshot(self.output)
        self.profile_paths: list[Path] = []

    def profile_factory(self) -> tempfile.TemporaryDirectory[str]:
        profile = tempfile.TemporaryDirectory()
        self.profile_paths.append(Path(profile.name))
        return profile

    def assert_harness_clean(self) -> None:
        self.assertEqual(snapshot(self.output), self.before)
        self.assertFalse((self.output / "phase4k-browser.js").exists())
        self.assertTrue(all(not path.exists() for path in self.profile_paths))
        self.assertFalse(any(
            thread.name == "phase4k-browser-server" and thread.is_alive()
            for thread in threading.enumerate()
        ))

    def test_websocket_handshake_eof_fails_once_and_closes_the_socket(self) -> None:
        eof_socket = _HandshakeEofSocket()
        with (
            mock.patch.object(chromium_support.socket, "create_connection", return_value=eof_socket),
            self.assertRaises(ConnectionError),
        ):
            chromium_support.DevToolsConnection("ws://127.0.0.1:9222/devtools/page/1")
        self.assertEqual(eof_socket.receive_count, 1)
        self.assertTrue(eof_socket.closed)

    def test_devtools_target_waits_for_complete_port_and_page_publication(self) -> None:
        process = _FakeProcess()
        connections = (
            _DiscoveryConnection([]),
            _DiscoveryConnection([{"type": "page", "targetId": "page-1"}]),
        )
        connection_calls: list[tuple[str, float]] = []

        def connect(url: str, *, timeout: float) -> _DiscoveryConnection:
            connection_calls.append((url, timeout))
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
                    port_path.write_text("9222\n/devtools/browser/browser-1\n", encoding="utf-8")

            with mock.patch.object(chromium_support.time, "sleep", side_effect=publish):
                target = chromium_support.devtools_target(
                    Path(directory), process, connection_factory=connect
                )

        self.assertEqual(target, "ws://127.0.0.1:9222/devtools/page/page-1")
        self.assertEqual(sleeps, 2)
        self.assertEqual(
            [url for url, _ in connection_calls],
            ["ws://127.0.0.1:9222/devtools/browser/browser-1"] * 2,
        )
        self.assertTrue(all(0 < timeout <= 0.25 for _, timeout in connection_calls))
        self.assertEqual(
            [connection.calls for connection in connections],
            [["Target.getTargets"]] * 2,
        )
        self.assertTrue(all(connection.closed for connection in connections))

    def test_devtools_target_bounds_an_accepting_but_stalled_browser_socket(self) -> None:
        process = _FakeProcess()
        elapsed = 0.0

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        def stall(url: str, *, timeout: float) -> None:
            del url
            advance(timeout)
            raise TimeoutError

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "DevToolsActivePort").write_text(
                "9222\n/devtools/browser/browser-1\n", encoding="utf-8"
            )
            with (
                mock.patch.object(chromium_support.time, "monotonic", side_effect=lambda: elapsed),
                mock.patch.object(chromium_support.time, "sleep", side_effect=advance),
                self.assertRaises(AssertionError),
            ):
                chromium_support.devtools_target(
                    Path(directory), process, connection_factory=stall
                )

        self.assertLessEqual(elapsed, chromium_support.DEVTOOLS_STARTUP_TIMEOUT)

    def test_devtools_target_bounds_each_fresh_startup_attempt(self) -> None:
        process = _FakeProcess()
        elapsed = 0.0

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(chromium_support.time, "monotonic", side_effect=lambda: elapsed),
                mock.patch.object(chromium_support.time, "sleep", side_effect=advance),
                self.assertRaises(AssertionError),
            ):
                chromium_support.devtools_target(Path(directory), process)

        self.assertEqual(
            elapsed,
            chromium_support.DEVTOOLS_STARTUP_TIMEOUT
            / chromium_support.DEVTOOLS_STARTUP_ATTEMPTS,
        )

    def test_json_probe_retries_one_fresh_browser_after_startup_failure(self) -> None:
        processes = [_FakeProcess(), _FakeProcess()]
        process_queue = list(processes)
        profiles = [
            _ProbeProfile(name="/tmp/chromium-probe-profile-1"),
            _ProbeProfile(name="/tmp/chromium-probe-profile-2"),
        ]
        profile_queue = list(profiles)
        connection = _ProbeConnection(ready=True)
        target_calls = 0

        def target(path: Path, process: _FakeProcess) -> str:
            nonlocal target_calls
            self.assertEqual(path, Path(profiles[target_calls].name))
            self.assertIs(process, processes[target_calls])
            target_calls += 1
            if target_calls == 1:
                raise AssertionError("first Chromium startup remained unresponsive")
            return "ws://chromium.test"

        result = chromium_support.browser_json_probe(
            "chromium-test",
            "http://127.0.0.1:8000/lander/",
            960,
            "#result",
            connection_factory=lambda url: connection,
            popen_factory=lambda *args, **kwargs: process_queue.pop(0),
            target_factory=target,
            tempdir_factory=lambda: profile_queue.pop(0),
            sleep=lambda seconds: None,
        )

        self.assertEqual(result, {"ready": True})
        self.assertEqual(target_calls, 2)
        self.assertTrue(connection.closed)
        self.assertTrue(all(process.terminated for process in processes))
        self.assertTrue(all(profile.cleaned for profile in profiles))

    def test_json_probe_owns_browser_and_retries_profile_cleanup(self) -> None:
        process = _FakeProcess()
        connection = _ProbeConnection(ready=True)
        profile = _ProbeProfile(cleanup_failures=1)
        result = chromium_support.browser_json_probe(
            "chromium-test",
            "http://127.0.0.1:8000/lander/",
            960,
            "#result",
            connection_factory=lambda url: connection,
            popen_factory=lambda *args, **kwargs: process,
            target_factory=lambda path, owned: "ws://chromium.test",
            tempdir_factory=lambda: profile,
            sleep=lambda seconds: None,
        )

        self.assertEqual(result, {"ready": True})
        self.assertTrue(connection.closed)
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertEqual(process.waits, 1)
        self.assertTrue(profile.cleaned)
        self.assertEqual(profile.cleanup_calls, 2)

    def test_json_probe_bounds_readiness_and_kills_a_stuck_browser(self) -> None:
        process = _FakeProcess(stuck=True)
        connection = _ProbeConnection(ready=False)
        profile = _ProbeProfile()
        with self.assertRaises(AssertionError):
            chromium_support.browser_json_probe(
                "chromium-test",
                "http://127.0.0.1:8000/lander/",
                960,
                "#result",
                connection_factory=lambda url: connection,
                popen_factory=lambda *args, **kwargs: process,
                target_factory=lambda path, owned: "ws://chromium.test",
                tempdir_factory=lambda: profile,
                sleep=lambda seconds: None,
            )

        self.assertEqual(len(connection.expressions), 200)
        self.assertTrue(connection.closed)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, 2)
        self.assertTrue(profile.cleaned)

    def test_server_allocation_failure_restores_the_artifact(self) -> None:
        sentinel = RuntimeError()

        def fail_server(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise sentinel

        with self.assertRaises(RuntimeError) as caught:
            phase4k_browser.browser_phase4k_contract(
                self.output, server_factory=fail_server, chromium_path="chromium-test"
            )
        self.assertIs(caught.exception, sentinel)
        self.assert_harness_clean()

    def test_profile_and_spawn_failures_stop_the_server_and_restore_artifacts(self) -> None:
        profile_sentinel = RuntimeError()

        def fail_profile() -> tempfile.TemporaryDirectory[str]:
            raise profile_sentinel

        with self.assertRaises(RuntimeError) as caught:
            phase4k_browser.browser_phase4k_contract(
                self.output, tempdir_factory=fail_profile, chromium_path="chromium-test"
            )
        self.assertIs(caught.exception, profile_sentinel)
        self.assert_harness_clean()

        spawn_sentinel = RuntimeError()

        def fail_spawn(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise spawn_sentinel

        with self.assertRaises(RuntimeError) as caught:
            phase4k_browser.browser_phase4k_contract(
                self.output,
                popen_factory=fail_spawn,
                tempdir_factory=self.profile_factory,
                chromium_path="chromium-test",
            )
        self.assertIs(caught.exception, spawn_sentinel)
        self.assert_harness_clean()

    def test_acquisition_and_connection_failures_terminate_the_owned_browser(self) -> None:
        processes: list[_FakeProcess] = []

        def spawn(*args: object, **kwargs: object) -> _FakeProcess:
            del args, kwargs
            process = _FakeProcess()
            processes.append(process)
            return process

        target_sentinel = RuntimeError()

        def fail_target(profile: Path, process: object) -> str:
            del profile, process
            raise target_sentinel

        with self.assertRaises(RuntimeError) as caught:
            phase4k_browser.browser_phase4k_contract(
                self.output,
                popen_factory=spawn,
                tempdir_factory=self.profile_factory,
                target_factory=fail_target,
                chromium_path="chromium-test",
            )
        self.assertIs(caught.exception, target_sentinel)
        self.assertTrue(processes[-1].terminated)
        self.assert_harness_clean()

        connection_sentinel = RuntimeError()

        def fail_connection(url: str) -> None:
            del url
            raise connection_sentinel

        with self.assertRaises(RuntimeError) as caught:
            phase4k_browser.browser_phase4k_contract(
                self.output,
                connection_factory=fail_connection,
                popen_factory=spawn,
                tempdir_factory=self.profile_factory,
                target_factory=lambda profile, process: "ws://phase4k.invalid",
                chromium_path="chromium-test",
            )
        self.assertIs(caught.exception, connection_sentinel)
        self.assertTrue(processes[-1].terminated)
        self.assert_harness_clean()

    def test_null_root_navigation_race_times_out_without_evaluation_or_cleanup_failure(self) -> None:
        process = _FakeProcess()
        connection = _NullRootRaceConnection()
        with (
            mock.patch.object(phase4k_browser.time, "sleep", return_value=None),
            self.assertRaises(AssertionError),
        ):
            phase4k_browser.browser_phase4k_contract(
                self.output,
                connection_factory=lambda url: connection,
                popen_factory=lambda *args, **kwargs: process,
                tempdir_factory=self.profile_factory,
                target_factory=lambda profile, owned: "ws://phase4k.invalid",
                chromium_path="chromium-test",
            )
        navigation = next(params for method, params in connection.calls if method == "Page.navigate")
        loaded_url = str(navigation["url"])
        self.assertEqual(connection.expressions, [phase4k_browser._readiness_expression(loaded_url)] * 200)
        readiness = connection.expressions[0]
        self.assertIn(f'location.href === "{loaded_url}"', readiness)
        self.assertIn("document.readyState === 'complete'", readiness)
        self.assertIn("document.documentElement?.dataset.phase4kReady", readiness)
        self.assertTrue(connection.closed)
        self.assertTrue(process.terminated)
        self.assert_harness_clean()

    def test_probe_uses_browser_events_instead_of_controller_input_shortcuts(self) -> None:
        probe = phase4k_browser._probe_source()
        for shortcut in ("controller.onKeyDown", "controller.onPointer", "controller.frame("):
            self.assertNotIn(shortcut, probe)


if __name__ == "__main__":
    unittest.main()
