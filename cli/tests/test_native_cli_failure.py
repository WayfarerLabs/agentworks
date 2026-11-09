"""Native command failures retain their identity when optional diagnostics fail."""

from __future__ import annotations

import shutil
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentworks.errors import ConnectivityError, ExternalError, StateError
from agentworks.native_files import NativeFiles
from agentworks.plugins._harness_native.native_cli import NativeCLI
from agentworks.ssh import SSHResult
from tests.native_setup_fixtures import LocalFixtureTransport


def command_fixture(tmp_path: Path, *, result: SSHResult, copy_from: Callable[..., None]) -> NativeCLI:
    files = SimpleNamespace(
        remote="/fixture/staging",
        runner=SimpleNamespace(run=lambda *args, **kwargs: result, copy_from=copy_from),
        slot=lambda: ("/fixture/staging/output", tmp_path / "output"),
    )
    cli = NativeCLI("claude", files, home="/fixture/home", config_root="/fixture/home/.claude")
    cli._resolved = ("/fixture/claude", "/fixture")
    return cli


@pytest.mark.parametrize("failure_type", [ExternalError, ConnectivityError, OSError, subprocess.TimeoutExpired])
def test_failed_native_command_keeps_primary_operation_when_diagnostic_copy_fails(tmp_path, failure_type):
    marker = uuid4().hex
    failure = failure_type(marker, 30) if failure_type is subprocess.TimeoutExpired else failure_type(marker)

    def copy_from(*args, **kwargs):
        raise failure

    cli = command_fixture(tmp_path, result=SSHResult(1, "", ""), copy_from=copy_from)
    with pytest.raises(ExternalError) as caught:
        cli.command(["plugin", "marketplace", "list", "--json"], structured=True)

    assert caught.value is not failure
    assert all(token in str(caught.value) for token in (cli.tool, "plugin", "marketplace", "list", cli.config_root))
    assert marker not in "".join(traceback.format_exception(caught.value))


@pytest.mark.windows
def test_failed_native_command_keeps_primary_operation_when_local_diagnostic_is_absent(tmp_path):
    cli = command_fixture(tmp_path, result=SSHResult(1, "", ""), copy_from=lambda *args: None)

    with pytest.raises(ExternalError) as caught:
        cli.command(["plugin", "marketplace", "list", "--json"], structured=True)

    assert all(token in str(caught.value) for token in (cli.tool, "plugin", "marketplace", "list", cli.config_root))
    assert not (tmp_path / "output").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="executes the Linux guest staging script locally")
def test_failed_staging_cd_keeps_native_operation_without_a_diagnostic_file(tmp_path):
    transport = LocalFixtureTransport(tmp_path / "fixture")
    with NativeFiles(transport) as files:
        cli = NativeCLI("claude", files, home=str(transport.home), config_root=str(transport.home / ".claude"))
        cli._resolved = ("/bin/false", "/usr/bin:/bin")
        shutil.rmtree(files.remote)
        with pytest.raises(ExternalError) as caught:
            cli.command(["plugin", "marketplace", "list", "--json"], structured=True)
        assert all(token in str(caught.value) for token in (cli.tool, "plugin", "marketplace", "list"))
    assert not Path(files.remote).exists()


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, RuntimeError, StateError])
def test_optional_native_diagnostic_does_not_suppress_cancellation_or_internal_failure(tmp_path, failure_type):
    failure = failure_type(uuid4().hex)

    def copy_from(*args, **kwargs):
        raise failure

    cli = command_fixture(tmp_path, result=SSHResult(1, "", ""), copy_from=copy_from)
    with pytest.raises(failure_type) as caught:
        cli.command(["plugin", "marketplace", "list", "--json"], structured=True)
    assert caught.value is failure


def test_successful_structured_native_command_requires_response_transfer(tmp_path):
    failure = ConnectivityError(uuid4().hex)

    def copy_from(*args, **kwargs):
        raise failure

    cli = command_fixture(tmp_path, result=SSHResult(0, "", ""), copy_from=copy_from)
    with pytest.raises(ConnectivityError) as caught:
        cli.command(["plugin", "marketplace", "list", "--json"], structured=True)
    assert caught.value is failure


@pytest.mark.windows
def test_successful_structured_native_command_requires_readable_response(tmp_path):
    transfers: list[str] = []

    def copy_from(remote, local):
        transfers.append(remote)

    cli = command_fixture(tmp_path, result=SSHResult(0, "", ""), copy_from=copy_from)
    with pytest.raises(ExternalError):
        cli.command(["plugin", "marketplace", "list", "--json"], structured=True)

    assert transfers == ["/fixture/staging/output"]
