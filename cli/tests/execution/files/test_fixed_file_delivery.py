"""Cross-family checks for fixed stdin-delivered file-helper bundles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_inventory_bundle import FIXED_BUNDLE as INVENTORY_BUNDLE
from agentworks.execution._file_metadata_bundle import FIXED_BUNDLE as METADATA_BUNDLE
from agentworks.execution._file_object_bundle import FIXED_BUNDLE as OBJECT_BUNDLE
from agentworks.execution._file_read_bundle import FIXED_BUNDLE as READ_BUNDLE
from agentworks.execution._file_snapshot_bundle import FIXED_BUNDLE as SNAPSHOT_BUNDLE
from agentworks.execution._file_stage_bundle import FIXED_BUNDLE as STAGE_BUNDLE
from agentworks.execution._helper_bundle import FixedFileHelperBundle
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan, build_helper_argv
from agentworks.execution.carrier import CarrierIO, Deadline, FiniteInput, PreparedInvocation, SinkOutput
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from agentworks.execution.carriers.ssh.connection import SSHConnection, build_ssh_argv

_NONCE = "0" * 32
_BUNDLES = (
    ("read", READ_BUNDLE),
    ("object", OBJECT_BUNDLE),
    ("metadata", METADATA_BUNDLE),
    ("inventory", INVENTORY_BUNDLE),
    ("stage", STAGE_BUNDLE),
    ("snapshot", SNAPSHOT_BUNDLE),
)


class _Sink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


def _invocation(bundle: FixedFileHelperBundle, plan: IdentityPlan) -> PreparedInvocation:
    return PreparedInvocation(
        build_helper_argv(
            plan,
            runtime_path="/usr/bin/python3",
            fixed_source=bundle.bootstrap,
            nonce=_NONCE,
        )
    )


@pytest.mark.parametrize(("family", "bundle"), _BUNDLES)
def test_every_fixed_family_executes_in_isolated_distribution_python311(
    family: str,
    bundle: FixedFileHelperBundle,
) -> None:
    runtime = Path("/usr/bin/python3.11")
    if not runtime.is_file():
        pytest.skip("distribution Python 3.11 is unavailable")
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", bundle.bootstrap, _NONCE],
        input=bundle.prefix + b"{}",
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, family
    assert completed.stdout and completed.stderr == b""
    assert bundle.prefix not in completed.stdout


def test_bootstrap_accepts_fragmented_prefix_and_preserves_manifest_boundary() -> None:
    runtime = Path("/usr/bin/python3.11")
    if not runtime.is_file():
        pytest.skip("distribution Python 3.11 is unavailable")
    process = subprocess.Popen(
        [str(runtime), "-I", "-S", "-B", "-c", READ_BUNDLE.bootstrap, _NONCE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pipesize=4096,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    for offset in range(0, len(READ_BUNDLE.prefix), 17):
        process.stdin.write(READ_BUNDLE.prefix[offset : offset + 17])
        process.stdin.flush()
    manifest = b"raise SystemExit(42)"
    process.stdin.write(manifest)
    process.stdin.close()
    stdout = process.stdout.read()
    stderr = process.stderr.read()
    returncode = process.wait(timeout=10)

    assert returncode == 0
    assert stdout and stderr == b""
    assert manifest not in stdout


@pytest.mark.parametrize(
    "payload",
    [READ_BUNDLE.prefix[:-1], bytes([READ_BUNDLE.prefix[0] ^ 1]) + READ_BUNDLE.prefix[1:] + b"{}"],
    ids=["short", "changed"],
)
def test_bootstrap_refuses_unverified_prefix_without_protocol_output(payload: bytes) -> None:
    completed = subprocess.run(
        ["/usr/bin/python3.11", "-I", "-S", "-B", "-c", READ_BUNDLE.bootstrap, _NONCE],
        input=payload,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 125
    assert completed.stdout == completed.stderr == b""


@pytest.mark.parametrize(("family", "bundle"), _BUNDLES)
@pytest.mark.parametrize(
    "plan",
    [
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
        IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE),
    ],
    ids=["direct", "root", "demote"],
)
def test_complete_provider_body_fits_and_oversize_refuses_before_wire(
    family: str,
    bundle: FixedFileHelperBundle,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = _invocation(bundle, plan)
    maximum_manifest = b"x" * 32_768
    representative = json.dumps(
        {"command": invocation.argv, "input-data": (bundle.prefix + maximum_manifest).decode("ascii")}
    ).encode("ascii")
    assert len(representative) < 65_536, family

    carrier = ProxmoxCarrier(
        ProxmoxConnection(
            "https://pve.example:8006",
            "node-a",
            101,
            "root@pam!token",
            "secret",
        )
    )

    def unexpected_wire(*args: object, **kwargs: object) -> object:
        raise AssertionError("oversized provider body reached the wire")

    monkeypatch.setattr(carrier._wire, "request", unexpected_wire)
    provider_maximum = bundle.prefix + b"x" * (65_536 - len(bundle.prefix))
    io = CarrierIO(
        input=FiniteInput(provider_maximum, sensitive=True),
        output=SinkOutput(_Sink(), _Sink(), require_live=False),
        sensitive=True,
    )
    with pytest.raises(ValidationError):
        carrier.execute(invocation, io=io, deadline=Deadline.after(1))


@pytest.mark.parametrize(("family", "bundle"), _BUNDLES)
@pytest.mark.parametrize(
    "plan",
    [
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
        IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE),
    ],
    ids=["direct", "root", "demote"],
)
def test_windows_ssh_command_contains_only_short_fixed_bootstrap(
    family: str,
    bundle: FixedFileHelperBundle,
    plan: IdentityPlan,
) -> None:
    connection = SSHConnection(
        "host.example",
        "agent",
        Path("/keys/identity"),
        Path("/keys/known-hosts"),
    )
    argv = build_ssh_argv(connection, _invocation(bundle, plan))
    windows_command = subprocess.list2cmdline(argv)

    assert len(windows_command) < 32_767, family
    assert bundle.prefix.decode("ascii") not in windows_command


def test_dynamic_request_values_never_enter_fixed_bundle_or_safe_representation() -> None:
    canary = b"dynamic-file-operation-secret"

    assert canary not in READ_BUNDLE.prefix
    assert canary.decode("ascii") not in READ_BUNDLE.bootstrap
    assert canary.decode("ascii") not in repr(READ_BUNDLE)
