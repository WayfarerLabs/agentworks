"""Paired delivery, checkpoint and preflight for the remaining private families."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import (
    _file_inventory_bundle,
    _file_metadata_bundle,
    _file_object_bundle,
    _file_publication_bundle,
    _file_read_bundle,
    _file_stage_bundle,
    _guest_bootstrap,
)
from agentworks.execution._file_inventory_exchange import list_directory
from agentworks.execution._file_inventory_protocol import decode_file_inventory_request
from agentworks.execution._file_metadata_exchange import ensure_file_directory, set_file_metadata
from agentworks.execution._file_metadata_protocol import decode_file_metadata_request
from agentworks.execution._file_object_exchange import remove_file, stat_file
from agentworks.execution._file_object_protocol import decode_file_object_request
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_publication import Create, CreateMetadata, Match
from agentworks.execution._file_publication_exchange import publication_cleanup, publication_reconcile, publish
from agentworks.execution._file_publication_protocol import decode_file_publication_request
from agentworks.execution._file_read import read_file
from agentworks.execution._file_read_protocol import decode_file_read_request
from agentworks.execution._file_stage_exchange import stage_begin, stage_chunk, stage_cleanup, stage_reconcile
from agentworks.execution._file_stage_protocol import MAX_STAGE_CHUNK_BYTES, decode_file_stage_request
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
    build_root_guest_bootstrap_argv,
    build_runtime_identity_helper_argv,
)
from agentworks.execution._scratch import _cleanup_debt
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import CarrierIO, Deadline, ExitStatus, FiniteInput, PreparedInvocation, SinkOutput
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from tests.execution.files._file_stage_support import LocalCarrier
from tests.execution.files._runtime_support import runtime_nonce
from tests.execution.files.test_file_publication_protocol import _receipt_debt, _reference, _revision
from tests.execution.files.test_numeric_guest_helper_adoption import (
    _GUEST,
    _ROOT,
    _RUNTIME,
    _TOKEN,
    _CapturedCarrier,
    _gate,
    _plan,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="private numeric helpers require Linux")

_DATA = bytes(range(256)) + b"\x00\xffnumeric"
_BUNDLES = {
    "read": _file_read_bundle,
    "inventory": _file_inventory_bundle,
    "object": _file_object_bundle,
    "metadata": _file_metadata_bundle,
    "stage": _file_stage_bundle,
    "publication": _file_publication_bundle,
}
_DECODERS: dict[str, Callable[[bytes], Any]] = {
    "read": decode_file_read_request,
    "inventory": decode_file_inventory_request,
    "object": decode_file_object_request,
    "metadata": decode_file_metadata_request,
    "stage": decode_file_stage_request,
    "publication": decode_file_publication_request,
}
_CALLS: dict[str, Callable[..., Any]] = {
    "read.read": read_file,
    "inventory.list": list_directory,
    "object.stat": stat_file,
    "object.remove": remove_file,
    "metadata.set": set_file_metadata,
    "metadata.ensure": ensure_file_directory,
    "stage.begin": stage_begin,
    "stage.chunk": stage_chunk,
    "stage.reconcile": stage_reconcile,
    "stage.cleanup": stage_cleanup,
    "publication.publish": publish,
    "publication.reconcile": publication_reconcile,
    "publication.cleanup": publication_cleanup,
}
_FIRST_CASES = ("read.read", "inventory.list", "object.remove", "metadata.ensure", "stage.begin", "publication.publish")


def _arguments(case: str, plan: IdentityPlan) -> dict[str, Any]:
    arguments: dict[str, Any] = dict(trusted_root_path="/approved", relative_path="payload")
    reference = _reference(plan, length=len(_DATA))
    if case == "read.read":
        arguments["max_bytes"] = len(_DATA)
    elif case == "inventory.list":
        arguments.update(max_entries=4096, max_depth=8, max_encoded_bytes=4 * 1024 * 1024)
    elif case == "object.remove":
        arguments.update(expected_kind=FileKind.REGULAR, expected_revision=_revision())
    elif case.startswith("metadata."):
        arguments.update(uid=plan.expected.euid, gid=plan.expected.egid, mode=0o700)
    elif case.startswith("stage."):
        arguments["token"] = _TOKEN
        if case == "stage.begin":
            arguments["expected_length"] = len(_DATA)
        elif case == "stage.chunk":
            arguments.update(reference=reference, offset=0, data=_DATA, chunk_digest=hashlib.sha256(_DATA).digest())
        elif case == "stage.cleanup":
            arguments["cleanup_debt"] = _cleanup_debt(reference)
    elif case.startswith("publication."):
        arguments.update(token=_TOKEN, reference=reference)
        if case == "publication.publish":
            arguments.update(
                digest=hashlib.sha256(_DATA).digest(),
                condition=Create(),
                create_metadata=CreateMetadata(plan.expected.euid, plan.expected.egid, 0o600),
            )
        elif case == "publication.cleanup":
            arguments["cleanup_debt"] = _receipt_debt(reference)
    return arguments


def _call(case: str, carrier: Any, *, plan: IdentityPlan | None = None, **kwargs: Any) -> Any:
    selected = plan or _plan()
    arguments = _arguments(case, selected)
    arguments.update(plan=selected, deadline=Deadline.after(10), runtime_selection=_RUNTIME)
    arguments.update(kwargs)
    return _CALLS[case](carrier, **arguments)


@pytest.mark.parametrize("case", _CALLS)
@pytest.mark.parametrize("root_mode", [None, IdentityMode.DIRECT, IdentityMode.SUDO_ROOT])
def test_all_entry_points_pair_argv_and_prefix(case: str, root_mode: IdentityMode | None) -> None:
    plan = IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)
    context = None if root_mode is None else _NumericGuestBootstrap(replace(_ROOT, mode=root_mode), _GUEST)
    carrier = _CapturedCarrier()
    result = _call(case, carrier, plan=plan, bootstrap=context)
    bundle = _BUNDLES[case.split(".")[0]]
    assert carrier.invocation is not None and carrier.io is not None and isinstance(carrier.io.input, FiniteInput)
    nonce = runtime_nonce(carrier.invocation)
    if context is None:
        argv, _, _ = build_runtime_identity_helper_argv(
            plan,
            selection=_RUNTIME,
            fixed_source=bundle.FIXED_BUNDLE.bootstrap,
            nonce=nonce,
        )
        prefix = bundle.FIXED_BUNDLE.prefix
    else:
        argv, _, _ = build_root_guest_bootstrap_argv(
            context.root_entry,
            plan.expected,
            selection=_RUNTIME,
            program=bundle.ROOT_PROGRAM,
            nonce=nonce,
            expected_guest=context.guest,
        )
        prefix = bundle.ROOT_PROGRAM.prefix
    assert carrier.invocation.argv == argv
    assert carrier.io.input.data.startswith(prefix) and carrier.io.input.sensitive and carrier.io.sensitive
    request = _DECODERS[case.split(".")[0]](carrier.io.input.data[len(prefix) :])
    assert request.identity == plan.expected
    if case == "stage.chunk":
        assert request.data == _DATA
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.carrier_completion == ExitStatus(code=125)
    assert result.observation is not None and result.observation.state.value in {"incomplete", "uncertain"}
    assert carrier.calls == 1


@pytest.mark.parametrize("case", _CALLS)
@pytest.mark.parametrize(
    "runtime", [RuntimeSelection(RuntimeTargetOS.DARWIN), RuntimeSelection(RuntimeTargetOS.LINUX, "/tmp/python3")]
)
def test_provided_context_runtime_refusal_never_dispatches(case: str, runtime: RuntimeSelection) -> None:
    carrier = _CapturedCarrier()
    with pytest.raises(ValidationError):
        _call(case, carrier, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST), runtime_selection=runtime)
    assert carrier.calls == 0


@pytest.mark.parametrize("case", [case for case in _CALLS if case.startswith(("stage.", "publication."))])
def test_gate_guest_conflict_refuses_before_dispatch(case: str) -> None:
    carrier = _CapturedCarrier()
    with pytest.raises(ValidationError):
        _call(
            case,
            carrier,
            bootstrap=_NumericGuestBootstrap(_ROOT, replace(_GUEST, init_start_ticks=1235)),
            effect_gate=_gate(),
        )
    assert carrier.calls == 0


class _PackedCarrier(_CapturedCarrier):
    """Execute unchanged packed modules with mocked privileged admission and path facts."""

    def __init__(
        self,
        family: str,
        monkeypatch: pytest.MonkeyPatch,
        *,
        plan: IdentityPlan | None = None,
        observed: VMGuestIdentity = _GUEST,
        changed: bool = False,
        gate_namespace: Path | None = None,
    ) -> None:
        super().__init__()
        self.bundle, self.monkeypatch, self.plan = _BUNDLES[family], monkeypatch, plan or _plan()
        self.observed, self.changed, self.gate_namespace = observed, changed, gate_namespace
        self.events: list[str] = []
        self.reads = 0

    def _run(self, invocation: PreparedInvocation, io: CarrierIO) -> int:
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        payload, output, offset = io.input.data, io.output.stdout, 0
        checkpoint_reads = 0

        def read_init() -> bytes:
            nonlocal checkpoint_reads
            self.events.append("init")
            self.reads += 1
            checkpoint_reads += 1
            ticks = 1235 if self.changed and checkpoint_reads > 1 else self.observed.init_start_ticks
            return b"1 (init) S " + b"0 " * 18 + f"{ticks}\n".encode()

        @contextmanager
        def admit(uid: int, gid: int, groups: tuple[int, ...]) -> Iterator[Callable[[], bytes]]:
            assert IdentityExpectation(uid, gid, groups) == self.plan.expected
            self.events.append("drop")
            yield read_init

        def read(descriptor: int, count: int) -> bytes:
            nonlocal offset
            if descriptor != 0:
                return real_read(descriptor, count)
            self.events.append("prefix" if offset < len(self.bundle.ROOT_PROGRAM.prefix) else "request")
            block = payload[offset : offset + count]
            offset += len(block)
            return block

        def execute_module(source: Any, scope: dict[str, Any], local: dict[str, Any] | None = None) -> None:
            real_exec(source, scope, local)
            name = scope.get("__name__", "")
            if name == self.bundle._PACKAGE + "._vm_guest_identity_guest":
                scope["_read_marker"] = lambda *_args: self.observed.instance_marker
                scope["_read_boot_id"] = lambda *_args: self.observed.boot_id
            elif name.startswith(self.bundle._PACKAGE + ".") and not name.endswith("._vm_guest_identity_protocol"):
                self.events.append("remaining")
                if name.endswith("._file_effect_gate") and self.gate_namespace is not None:
                    scope["_GATE_NAMESPACE"], scope["_ROOT_UID"] = str(self.gate_namespace), os.geteuid()
                if name.endswith("_guest"):
                    real_main = scope["main"]

                    def main(nonce: str) -> int:
                        self.events.append("body")
                        result = real_main(nonce)
                        assert isinstance(result, int)
                        return result

                    scope["main"] = main

        real_exec, real_read, real_write = builtins.exec, os.read, os.write
        previous = {name: module for name, module in sys.modules.items() if name.startswith(self.bundle._PACKAGE)}
        try:
            with self.monkeypatch.context() as patch:
                patch.setattr(_guest_bootstrap, "_admit", admit)
                identity = self.plan.expected
                patch.setattr(os, "getresuid", lambda: (identity.euid,) * 3)
                patch.setattr(os, "getresgid", lambda: (identity.egid,) * 3)
                patch.setattr(os, "getegid", lambda: identity.egid)
                patch.setattr(os, "getgroups", lambda: list(identity.groups))
                patch.setattr(os, "read", read)
                patch.setattr(
                    os,
                    "write",
                    lambda fd, data: output.try_write(memoryview(data)) if fd == 1 else real_write(fd, data),
                )
                patch.setattr(sys, "argv", ["agentworks-fixed-helper", runtime_nonce(invocation)])
                patch.setattr(builtins, "exec", execute_module)
                return _guest_bootstrap.main(
                    identity.euid,
                    identity.egid,
                    identity.groups,
                    self.bundle.ROOT_PROGRAM.loader_source,
                    (_GUEST.instance_marker, _GUEST.boot_id, _GUEST.init_start_ticks),
                )
        finally:
            for name in tuple(sys.modules):
                if name.startswith(self.bundle._PACKAGE):
                    del sys.modules[name]
            sys.modules.update(previous)


@pytest.mark.parametrize("case", _FIRST_CASES)
@pytest.mark.parametrize("field", ["instance_marker", "boot_id", "init_start_ticks"])
def test_full_checkpoint_precedes_remaining_modules_and_effects(
    case: str,
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changes = {"instance_marker": "b" * 32, "boot_id": "223e4567-e89b-12d3-a456-426614174000", "init_start_ticks": 1235}
    carrier = _PackedCarrier(case.split(".")[0], monkeypatch, observed=replace(_GUEST, **{field: changes[field]}))
    result = _call(case, carrier, trusted_root_path=str(tmp_path), bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST))
    assert result.carrier_completion == ExitStatus(code=125)
    assert carrier.events == ["drop", "prefix", "init"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("case", _FIRST_CASES)
def test_generated_entry_refuses_nonroot_python311(case: str) -> None:
    runtime = Path("/usr/bin/python3.11")
    if os.geteuid() == 0 or not runtime.is_file():
        pytest.skip("requires nonroot Linux with distribution Python 3.11")
    carrier = _CapturedCarrier()
    context = _NumericGuestBootstrap(_ROOT, _GUEST)
    _call(case, carrier, bootstrap=context)
    assert carrier.invocation is not None and carrier.io is not None and isinstance(carrier.io.input, FiniteInput)
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", carrier.invocation.argv[-2]],
        input=carrier.io.input.data,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 125 and completed.stdout == completed.stderr == b""
    actual = _call(case, LocalCarrier(), bootstrap=context)
    assert actual.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert actual.carrier_completion == ExitStatus(code=125)
    assert actual.observation is not None and actual.observation.state.value in {"incomplete", "uncertain"}


@pytest.mark.parametrize("case", _FIRST_CASES)
@pytest.mark.parametrize("mode", [IdentityMode.DIRECT, IdentityMode.SUDO_ROOT])
def test_complete_maximum_manifest_uses_actual_provider_bound(
    case: str,
    mode: IdentityMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _CapturedCarrier()
    _call(case, captured, bootstrap=_NumericGuestBootstrap(replace(_ROOT, mode=mode), _GUEST))
    assert captured.invocation is not None and captured.io is not None
    bundle = _BUNDLES[case.split(".")[0]]
    io = replace(captured.io, input=FiniteInput(bundle.ROOT_PROGRAM.prefix + b"x" * 32768, sensitive=True))
    assert isinstance(io.input, FiniteInput)
    actual_size = len(
        json.dumps({"command": captured.invocation.argv, "input-data": io.input.data.decode("ascii")}).encode()
    )
    if actual_size <= 65536:
        assert len(ProxmoxCarrier._request_body(captured.invocation, io)) == actual_size
    else:
        with pytest.raises(ValidationError):
            ProxmoxCarrier._request_body(captured.invocation, io)
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.example:8006", "node-a", 101, "root@pam!token", "secret"))
    monkeypatch.setattr(carrier._wire, "request", lambda *_a, **_kw: pytest.fail("provider dispatch reached"))
    oversized = replace(io, input=FiniteInput(bundle.ROOT_PROGRAM.prefix + b"x" * 65536, sensitive=True))
    with pytest.raises(ValidationError):
        carrier.execute(captured.invocation, io=oversized, deadline=Deadline.after(1))


@pytest.mark.parametrize("case", _FIRST_CASES)
def test_valid_aggregate_excess_refuses_before_provider_dispatch(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    groups = tuple(sorted({1001} | {(index * 2654435761) % (2**32) for index in range(2900)}))
    plan = IdentityPlan(IdentityExpectation(1001, 1001, groups), IdentityMode.DIRECT)
    captured = _CapturedCarrier()
    _call(case, captured, plan=plan, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST))
    assert captured.invocation is not None and captured.io is not None and isinstance(captured.io.input, FiniteInput)
    bundle = _BUNDLES[case.split(".")[0]]
    manifest = captured.io.input.data[len(bundle.ROOT_PROGRAM.prefix) :]
    assert len(manifest) <= 32768
    _DECODERS[case.split(".")[0]](manifest)
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.example:8006", "node-a", 101, "root@pam!token", "secret"))
    monkeypatch.setattr(carrier._wire, "request", lambda *_a, **_kw: pytest.fail("provider dispatch reached"))
    with pytest.raises(ValidationError):
        _call(case, carrier, plan=plan, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST))


@pytest.mark.parametrize("case", ["stage.chunk", "publication.publish"])
@pytest.mark.parametrize("mode", [IdentityMode.DIRECT, IdentityMode.SUDO_ROOT])
def test_maximum_paths_and_stage_chunk_keep_complete_delivery_bound(case: str, mode: IdentityMode) -> None:
    plan = IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)
    fields: dict[str, Any] = dict(trusted_root_path="/" + "r" * 4094, relative_path="p" * 4096)
    if case == "stage.chunk":
        data = bytes(range(256)) * (MAX_STAGE_CHUNK_BYTES // 256)
        fields.update(
            data=data, chunk_digest=hashlib.sha256(data).digest(), reference=_reference(plan, length=len(data))
        )
    else:
        fields["condition"] = Match(_revision())
    carrier = _CapturedCarrier()
    _call(case, carrier, plan=plan, bootstrap=_NumericGuestBootstrap(replace(_ROOT, mode=mode), _GUEST), **fields)
    assert carrier.invocation is not None and carrier.io is not None and isinstance(carrier.io.input, FiniteInput)
    bundle = _BUNDLES[case.split(".")[0]]
    assert len(carrier.io.input.data) - len(bundle.ROOT_PROGRAM.prefix) <= 32768
    assert len(ProxmoxCarrier._request_body(carrier.invocation, carrier.io)) <= 65536
