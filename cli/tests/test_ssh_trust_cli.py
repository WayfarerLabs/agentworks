"""The operator trust surface preserves policy and works without configured state."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust, resolve_trust, trust_status

pytestmark = pytest.mark.windows
runner = CliRunner()


@pytest.fixture
def policy(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path.resolve()
    first = root / "operator-known-hosts"
    second = root / "system-known-hosts"
    revoked = root / "revoked.krl"
    first.write_bytes(b"# complete snapshot\r\n|1|hashed|record key\r\n")
    second.write_bytes(b"@cert-authority *.example key\n@revoked host key\n")
    revoked.write_bytes(b"SSHKRL\n\0\0\xff\x80\r\n")
    return root / "bundle", first, second, revoked


def invoke(*args: str | Path):
    return runner.invoke(app, ["config", *(str(arg) for arg in args)])


def import_policy(policy: tuple[Path, Path, Path, Path]) -> ManagedSSHTrust:
    directory, first, second, revoked = policy
    result = invoke(
        "import-ssh-trust",
        directory,
        first,
        second,
        "--authority",
        "fixture maintainer",
        "--revoked-host-keys",
        revoked,
    )
    assert result.exit_code == 0, result.exception
    return ManagedSSHTrust(directory)


def test_cli_preserves_complete_policy_and_refreshes_only_observed_generation(policy) -> None:
    directory, first, second, revoked = policy
    originals = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second, revoked)}
    bundle = import_policy(policy)
    initial = trust_status(bundle)
    assert initial.generation is not None
    assert initial.authority == "fixture maintainer"
    admitted = resolve_trust(bundle)
    assert [path.read_bytes() for path in admitted.known_hosts] == [first.read_bytes(), second.read_bytes()]
    assert admitted.revoked_host_keys is not None
    assert admitted.revoked_host_keys.read_bytes() == revoked.read_bytes()
    described = invoke("describe-ssh-trust", directory)
    assert described.exit_code == 0
    assert initial.generation in described.stdout
    result = invoke("block-ssh-trust", directory, "--expected-generation", initial.generation)
    assert result.exit_code == 0, result.exception
    assert trust_status(bundle).blocked
    result = invoke(
        "refresh-ssh-trust",
        directory,
        second,
        first,
        "--authority",
        "replacement maintainer",
        "--expected-generation",
        initial.generation,
        "--revoked-host-keys",
        revoked,
    )
    assert result.exit_code == 0, result.exception
    current = trust_status(bundle)
    assert not current.blocked and current.generation != initial.generation
    assert current.authority == "replacement maintainer"
    refreshed = resolve_trust(bundle)
    assert [path.read_bytes() for path in refreshed.known_hosts] == [second.read_bytes(), first.read_bytes()]
    assert refreshed.revoked_host_keys is not None
    assert refreshed.revoked_host_keys.read_bytes() == revoked.read_bytes()
    assert all(
        path.read_bytes() == data and path.stat().st_mtime_ns == timestamp
        for path, (data, timestamp) in originals.items()
    )
    assert all(path.exists() for path in admitted.known_hosts)
    result = invoke("block-ssh-trust", directory, "--expected-generation", initial.generation)
    assert isinstance(result.exception, StateError)
    assert trust_status(bundle) == current


@pytest.mark.parametrize("token", ["none", "0" * 32, "invalid", ""])
def test_bad_or_stale_token_does_not_mutate_policy(policy, token: str) -> None:
    bundle = import_policy(policy)
    before = trust_status(bundle)
    result = invoke("block-ssh-trust", bundle.directory, "--expected-generation", token)
    assert result.exit_code != 0
    assert isinstance(result.exception, (StateError, ValidationError))
    assert trust_status(bundle) == before


def test_failed_refresh_keeps_old_evidence_blocked(policy) -> None:
    directory, first, _, _ = policy
    bundle = import_policy(policy)
    before = trust_status(bundle)
    assert before.generation is not None
    admitted = resolve_trust(bundle)
    result = invoke(
        "refresh-ssh-trust",
        directory,
        first,
        directory.parent / "missing",
        "--authority",
        "fixture",
        "--expected-generation",
        before.generation,
    )
    assert isinstance(result.exception, StateError)
    after = trust_status(bundle)
    assert after.blocked and after.generation == before.generation
    assert all(path.exists() for path in admitted.known_hosts)


def test_none_recovers_partial_initial_import(policy) -> None:
    directory, first, _, _ = policy
    result = invoke("import-ssh-trust", directory, first, directory.parent / "missing", "--authority", "fixture")
    assert isinstance(result.exception, StateError)
    bundle = ManagedSSHTrust(directory)
    assert trust_status(bundle).generation is None
    assert trust_status(bundle).blocked
    result = invoke("refresh-ssh-trust", directory, first, "--authority", "fixture", "--expected-generation", "none")
    assert result.exit_code == 0, result.exception
    assert trust_status(bundle).generation is not None
    assert not trust_status(bundle).blocked
    assert resolve_trust(bundle).known_hosts[0].read_bytes() == first.read_bytes()


def test_existing_bundle_is_not_overwritten(policy) -> None:
    bundle = import_policy(policy)
    before = trust_status(bundle)
    result = invoke("import-ssh-trust", bundle.directory, policy[1], "--authority", "different maintainer")
    assert isinstance(result.exception, StateError)
    assert trust_status(bundle) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain terminal control bytes")
def test_path_output_escapes_terminal_controls(policy) -> None:
    directory, first, _, _ = policy
    controlled = first.with_name("host\x1b[31m\nsource")
    controlled.write_bytes(first.read_bytes())
    result = invoke("import-ssh-trust", directory, controlled, "--authority", "fixture")
    assert result.exit_code == 0, result.exception
    assert "\x1b" not in result.stdout
    assert repr(str(controlled)) in result.stdout


def test_real_entrypoint_runs_all_maintenance_without_config_or_database(policy) -> None:
    directory, first, second, revoked = policy
    script = """
import sys
import agentworks.config
import agentworks.db
from agentworks.cli._entry import main
from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust, trust_status
from pathlib import Path

def refuse(*args, **kwargs):
    raise AssertionError("Maintenance accessed operator configuration or database")

agentworks.config.load_config = refuse
agentworks.db.Database.__init__ = refuse
bundle, first, second, revoked = sys.argv[1:]
def invoke(*args):
    sys.argv = ["agw", "config", *args]
    try:
        main()
    except SystemExit as error:
        return error.code
    return 0

assert invoke("import-ssh-trust", bundle, first, second, "--authority", "fixture",
              "--revoked-host-keys", revoked) == 0
assert invoke("describe-ssh-trust", bundle) == 0
generation = trust_status(ManagedSSHTrust(Path(bundle))).generation
assert invoke("block-ssh-trust", bundle, "--expected-generation", generation) == 0
assert invoke("refresh-ssh-trust", bundle, first, second, "--authority", "fixture",
              "--expected-generation", generation, "--revoked-host-keys", revoked) == 0
assert invoke("block-ssh-trust", bundle, "--expected-generation", generation) == 1
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(directory), str(first), str(second), str(revoked)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
