"""A failed native command names its operation without quoting the provider."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentworks.errors import ExternalError
from tests.native_setup_fixtures import LocalFixtureTransport, native_cli

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes the Linux guest command script locally")


@pytest.fixture
def transport(tmp_path: Path) -> LocalFixtureTransport:
    return LocalFixtureTransport(tmp_path / "fixture")


def native(transport: LocalFixtureTransport, body: str) -> Path:
    """A stand-in harness executable running ``body`` as a shell script."""
    path = transport.root / "native"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o700)
    return path


def test_a_failed_command_names_its_operation_and_where_to_inspect_it(transport: LocalFixtureTransport) -> None:
    with native_cli(transport, "claude", transport.home / ".claude") as cli:
        cli._resolved = (str(native(transport, "echo 'provider prose' >&2\nexit 3\n")), "/usr/bin:/bin")
        with pytest.raises(ExternalError) as caught:
            cli.command(["plugin", "marketplace", "list", "--json"], structured=True)

    message = str(caught.value)
    assert all(token in message for token in ("claude", "plugin", "marketplace", "list", cli.config_root))
    assert "provider prose" not in message


@pytest.mark.parametrize("reply", ["not json", "[" * 100_000], ids=["text", "too-deep"])
def test_a_structured_reply_that_is_not_json_is_an_external_error(transport: LocalFixtureTransport, reply: str) -> None:
    with native_cli(transport, "codex", transport.home / ".codex") as cli:
        cli._resolved = (str(native(transport, f"printf '%s' '{reply}'\n")), "/usr/bin:/bin")
        with pytest.raises(ExternalError):
            cli.command(["plugin", "list", "--json"], structured=True)
