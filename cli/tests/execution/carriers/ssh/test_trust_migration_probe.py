"""Source-pure migration probe policy and custody regressions."""

from __future__ import annotations

import json
import signal
import subprocess
from functools import partial
from pathlib import Path

import pytest

from tests.execution.carriers.ssh import test_trust_migration_native as probe
from tests.execution.carriers.ssh.fixture_worker import fixture_call
from tests.execution.carriers.ssh.test_trust_migration_native import _policy_refusal, _probe_log

_UNKNOWN_CA = b"No ED25519 host key is known for [127.0.0.1]:12345 and you have requested strict checking."
_REVOKED = b"Host key ED25519 SHA256:fixture-fingerprint revoked by file /owned/generation/revoked-host-keys"
_TERMINAL = b"Host key verification failed."


@pytest.mark.parametrize(
    "diagnostics, expected",
    [
        (_UNKNOWN_CA + b"\n" + _TERMINAL + b"\n", "unknown_ca"),
        (_UNKNOWN_CA + b"\r\n" + _TERMINAL + b"\r\n", "unknown_ca"),
        (_REVOKED + b"\n" + _TERMINAL + b"\n", "host_revoked"),
        (_TERMINAL + b"\n", None),
        (b"fixture@127.0.0.1: Permission denied (publickey).\n", None),
        (b"ssh: connect to host 127.0.0.1 port 12345: Connection refused\n", None),
        (b"Error checking host key ED25519 in revoked keys file /owned/krl: invalid format\n" + _TERMINAL, None),
        (b"/owned/known-hosts:1: parse error in hostkeys file\n" + _UNKNOWN_CA + b"\n" + _TERMINAL, None),
        (_REVOKED.replace(b"fixture-fingerprint", b"another-key") + b"\n" + _TERMINAL, None),
        (_REVOKED.replace(b"/owned/generation/", b"/another-generation/") + b"\n" + _TERMINAL, None),
        (_UNKNOWN_CA + b"\n" + _REVOKED + b"\n" + _TERMINAL, None),
        (_UNKNOWN_CA + b"\nHost key verification fai", None),
    ],
)
def test_policy_refusal_requires_complete_matching_external_client_evidence(
    diagnostics: bytes, expected: str | None
) -> None:
    assert _policy_refusal(diagnostics, _UNKNOWN_CA, _REVOKED) == expected


@pytest.mark.parametrize(
    "payload", [None, b"x" * 2049, b"SYNTHETIC-PRIVATE-DIAGNOSTIC", b'{"unexpected": "synthetic key"}']
)
def test_probe_log_replaces_missing_overlarge_or_unrecognized_reports(tmp_path: Path, payload: bytes | None) -> None:
    path = tmp_path / "report.json"
    if payload is not None:
        path.write_bytes(payload)
    assert not _probe_log(path, 1)
    assert json.loads(path.read_bytes()) == {"probe_returncode": 1, "report_valid": False}
    assert len(path.read_bytes()) <= 2048


@pytest.mark.parametrize(
    "returncode, passed, retired, expected",
    [
        (0, True, False, True),
        (1, True, False, False),
        (None, True, False, False),
        (0, False, False, False),
        (0, True, True, False),
    ],
)
def test_probe_log_retains_only_valid_observations_and_requires_success(
    tmp_path: Path, returncode: int | None, passed: bool, retired: bool, expected: bool
) -> None:
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "policy_refusal": "host_revoked",
                "stderr_complete": True,
                "stdout_bytes": 0,
                "stderr_bytes": 120,
                "passed": passed,
                "retired_loaded": retired,
            }
        )
    )
    assert _probe_log(path, returncode) is expected
    assert len(path.read_bytes()) <= 2048
    assert json.loads(path.read_bytes())["report_valid"] is True
    if returncode is None:
        assert json.loads(path.read_bytes())["controller_observation_failed"] is True


def _causes(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [cause for child in error.exceptions for cause in _causes(child)]
    return [error]


@pytest.mark.parametrize("expires", [False, True])
def test_controller_expiry_requests_one_cooperative_signal_retains_resources_and_fails_late_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expires: bool
) -> None:
    order: list[str] = []
    expired = subprocess.TimeoutExpired(["synthetic-controller"], 15)
    with (tmp_path / "borrowed-key-evidence").open("wb") as borrowed:

        class Controller:
            def __init__(self, _argv, **options):
                assert not borrowed.closed
                assert options == {
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }
                order.append("constructed")

            def wait(self, *, timeout=None):
                assert not borrowed.closed
                order.append(f"wait-{timeout}")
                if timeout is not None and expires:
                    raise expired
                order.append("reaped")
                return 0

            def send_signal(self, requested):
                assert not borrowed.closed and requested == signal.SIGINT
                order.append("cooperative")

            def kill(self):
                pytest.fail("Controller custody was destroyed")

            def terminate(self):
                pytest.fail("Controller custody was destroyed")

        monkeypatch.setattr(subprocess, "Popen", Controller)
        if expires:
            with pytest.raises(BaseExceptionGroup) as caught:
                fixture_call(partial(probe._retained_probe, ["synthetic-controller"]))
            assert _causes(caught.value) == [expired]
            assert order == ["constructed", "wait-15", "cooperative", "wait-None", "reaped"]
        else:
            assert fixture_call(partial(probe._retained_probe, ["synthetic-controller"])) == 0
            assert order == ["constructed", "wait-15", "reaped"]
        assert not borrowed.closed
    assert borrowed.closed and order[-1] == "reaped"


def test_controller_preserves_initial_control_across_signal_and_reaping_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = KeyboardInterrupt("synthetic initial control")
    signal_control = SystemExit(17)
    reaping_control = KeyboardInterrupt("synthetic reaping control")
    requests: list[int] = []
    waits = [first, reaping_control, 0]
    with (tmp_path / "borrowed-key-evidence").open("wb") as borrowed:

        class Controller:
            def __init__(self, _argv, **_options):
                assert not borrowed.closed

            def wait(self, *, timeout=None):
                assert not borrowed.closed
                outcome = waits.pop(0) if waits else 0
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            def send_signal(self, requested):
                assert not borrowed.closed
                requests.append(requested)
                raise signal_control

        monkeypatch.setattr(subprocess, "Popen", Controller)
        with pytest.raises(BaseExceptionGroup) as caught:
            fixture_call(partial(probe._retained_probe, ["synthetic-controller"]))
        assert _causes(caught.value) == [first, signal_control, reaping_control]
        assert requests == [signal.SIGINT] and not waits and not borrowed.closed
    assert borrowed.closed


def test_strict_probe_records_controller_uncertainty_and_preserves_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution.carriers.ssh.connection import SSHConnection
    from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust

    control = KeyboardInterrupt("synthetic caller control")

    def interrupted(_work):
        raise control

    monkeypatch.setattr(probe, "fixture_call", interrupted)
    connection = SSHConnection(
        "fixture.invalid", "fixture", tmp_path / "identity", ManagedSSHTrust(tmp_path / "bundle")
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        probe._strict(connection, tmp_path / "effect", "blocked")
    assert caught.value is control
    logs = tuple(tmp_path.glob("effect-probe-*.json"))
    assert len(logs) == 1
    assert json.loads(logs[0].read_bytes())["controller_observation_failed"] is True
    assert not (tmp_path / "effect").exists()
