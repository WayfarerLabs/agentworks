# ruff: noqa: F405

import threading

import lander_chromium_phase4k as phase4k_browser
from site_test_support import *  # noqa: F403


class _FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
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
            mock.patch.object(phase4k_browser.socket, "create_connection", return_value=eof_socket),
            self.assertRaises(ConnectionError),
        ):
            phase4k_browser.DevToolsConnection("ws://127.0.0.1:9222/devtools/page/1")
        self.assertEqual(eof_socket.receive_count, 1)
        self.assertTrue(eof_socket.closed)

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

    def test_probe_uses_browser_events_instead_of_controller_input_shortcuts(self) -> None:
        probe = phase4k_browser._probe_source()
        for shortcut in ("controller.onKeyDown", "controller.onPointer", "controller.frame("):
            self.assertNotIn(shortcut, probe)


if __name__ == "__main__":
    unittest.main()
