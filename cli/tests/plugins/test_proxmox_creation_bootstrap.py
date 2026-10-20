"""One-shot prerequisite delivery and retained evidence, without live VM claims."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agentworks.db import Database
from agentworks.db.operations import (
    LifecycleObligation,
    LifecycleObligationState,
    OperationOwnership,
    OperationRepository,
    OperationResourceKind,
    OperationScope,
)
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline, ExitStatus
from agentworks.execution.carriers.proxmox import ProxmoxConnection, _ProxmoxWire
from agentworks.operations import OperationOwner
from agentworks.plugins.proxmox import _creation_bootstrap as module
from agentworks.plugins.proxmox._creation import CloneAssociation
from agentworks.plugins.proxmox._creation_bootstrap import BootstrapIntent, ProxmoxCreationBootstrap

MARKER = "a" * 32
SMBIOS = "87654321-4321-4321-4321-cba987654321"
GENERATION = "12345678-1234-1234-1234-123456789abc"
CONNECTION = ProxmoxConnection("https://pve.example:8006", "node1", 123, "worker@pve!creation", "secret-sentinel")
ASSOCIATION = CloneAssociation("node1", 123, SMBIOS)


@dataclass
class Harness:
    repository: OperationRepository
    owner: OperationOwner
    bootstrap: ProxmoxCreationBootstrap
    clock: list[float]
    calls: list[str] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[dict[str, object] | BaseException] = field(default_factory=list)
    config: dict[str, object] = field(
        default_factory=lambda: {
            "description": f"agentworks-instance:{MARKER}",
            "smbios1": f"uuid={SMBIOS}",
            "vmgenid": GENERATION,
        }
    )
    power: str = "running"

    def row(self) -> LifecycleObligation:
        row = self.repository.inspect_lifecycle_obligation(self.owner.ownership, self.bootstrap.obligation_id)
        assert row is not None
        return row

    def completed(self, **changes: object) -> dict[str, object]:
        intent = self.bootstrap.intent
        assert intent is not None
        return {
            "exited": True,
            "exitcode": 0,
            "out-data": f"AGW_CREATION_BOOTSTRAP_1:{intent.nonce}:complete:0\n",
            **changes,
        }


@pytest.fixture
def owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    custody = LocalDeliveryCustody()
    with closing(Database(tmp_path / "bootstrap.db")) as database:
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
        bootstrap = ProxmoxCreationBootstrap(
            owner,
            "vm-one",
            CONNECTION,
            ASSOCIATION,
            expected_vmgenid=GENERATION,
            instance_marker=MARKER,
            admin_username="operator",
            custody=custody,
            deadline=Deadline.after(5),
        )
        harness = Harness(repository, owner, bootstrap, clock)

        def config(wire: _ProxmoxWire, *, timeout: float, custody: LocalDeliveryCustody) -> dict[str, object]:
            assert wire._connection is CONNECTION and custody is bootstrap._custody and 0 < timeout <= 5
            harness.calls.append("config")
            return harness.config.copy()

        def power(wire: _ProxmoxWire, *, timeout: float, custody: LocalDeliveryCustody) -> dict[str, object]:
            assert wire._connection is CONNECTION and custody is bootstrap._custody and 0 < timeout <= 5
            harness.calls.append("power")
            return {"status": harness.power}

        def request(
            wire: _ProxmoxWire,
            method: str,
            suffix: str,
            *,
            custody: LocalDeliveryCustody,
            body: bytes | None = None,
            timeout: float | None,
        ) -> dict[str, object]:
            assert wire._connection is CONNECTION and custody is bootstrap._custody
            assert timeout is not None and 0 < timeout <= 5
            harness.calls.append(method)
            assert harness.row().state is LifecycleObligationState.POSSIBLE_EFFECT
            if method == "POST":
                assert suffix == "exec" and body is not None and len(body) <= 65_536
                harness.bodies.append(json.loads(body))
                return {"pid": 321}
            assert method == "GET" and suffix == "exec-status?pid=321" and body is None
            value = harness.statuses.pop(0) if harness.statuses else harness.completed()
            if isinstance(value, BaseException):
                raise value
            return value

        monkeypatch.setattr(_ProxmoxWire, "request_current_config", config)
        monkeypatch.setattr(_ProxmoxWire, "request_power", power)
        monkeypatch.setattr(_ProxmoxWire, "request", request)
        yield harness
        assert custody.close(Deadline.after(1))


def test_passive_exact_request_then_success_resolves_only_its_obligation(owned: Harness) -> None:
    bootstrap = owned.bootstrap
    assert owned.calls == [] and bootstrap.intent is None
    observation = bootstrap.start()
    assert observation.bootstrap_succeeded and observation.pid == 321
    assert observation.completion == ExitStatus(code=0)
    row = owned.row()
    assert row.state is LifecycleObligationState.RESOLVED and row.payload_revision == 2
    assert b"secret-sentinel" not in row.payload
    assert owned.repository.inspect(owned.owner.ownership.scope) is not None
    body = owned.bodies[0]
    argv = body["command"]
    assert argv[:9] == [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "LANG=C",
        "LC_ALL=C",
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
    ]
    intent = bootstrap.intent
    assert intent is not None
    assert argv[-3:] == [intent.nonce, "operator", "5"] and body["input-data"] == ""
    assert hashlib.sha256(argv[9].encode("ascii")).hexdigest() == intent.source_sha256
    assert hashlib.sha256(json.dumps(body).encode("ascii")).hexdigest() == intent.request_sha256
    assert json.loads(row.payload)["vmgenid"] == GENERATION
    assert owned.calls == ["config", "power", "POST", "GET"]
    bootstrap.reconcile(Deadline.after(1))
    assert bootstrap.observe(Deadline.after(1)) == observation
    with pytest.raises(StateError):
        bootstrap.start()
    assert owned.calls.count("POST") == 1


@pytest.mark.parametrize(
    "change",
    [
        {"lock": "resize"},
        {"description": "foreign"},
        {"smbios1": f"uuid={GENERATION}"},
        {"vmgenid": SMBIOS},
        {"vmgenid": None},
        {"smbios1": "uuid=invalid"},
    ],
)
def test_replaced_or_unproved_association_refuses_before_guest_work(owned: Harness, change: dict[str, object]) -> None:
    owned.config.update(change)
    with pytest.raises((StateError, ValidationError)):
        owned.bootstrap.start()
    assert owned.calls == ["config"] and owned.bootstrap.intent is None
    assert owned.owner.inspect_lifecycle_obligation(owned.bootstrap.obligation_id) is None


def test_stopped_vm_is_not_started_or_bootstrapped(owned: Harness) -> None:
    owned.power = "stopped"
    with pytest.raises(StateError):
        owned.bootstrap.start()
    assert owned.calls == ["config", "power"]


@pytest.mark.parametrize(
    "changes",
    [
        {"vm_name": "other"},
        {"association": replace(ASSOCIATION, vmid=124)},
        {"expected_vmgenid": "invalid"},
        {"instance_marker": "invalid"},
        {"admin_username": "operator; id"},
        {"deadline": Deadline(None)},
    ],
)
def test_invalid_public_inputs_refuse_passively(owned: Harness, changes: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {
        "owner": owned.owner,
        "vm_name": "vm-one",
        "connection": CONNECTION,
        "association": ASSOCIATION,
        "expected_vmgenid": GENERATION,
        "instance_marker": MARKER,
        "admin_username": "operator",
        "custody": owned.bootstrap._custody,
        "deadline": Deadline.after(5),
        **changes,
    }
    with pytest.raises(ValidationError):
        ProxmoxCreationBootstrap(**arguments)
    assert owned.calls == []


def test_cloud_wait_uses_original_remaining_fractional_budget(owned: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    original = owned.bootstrap._check_route

    def elapsed(deadline: Deadline) -> None:
        original(deadline)
        owned.clock[0] += 1.6

    monkeypatch.setattr(owned.bootstrap, "_check_route", elapsed)
    owned.bootstrap.start()
    intent = owned.bootstrap.intent
    assert intent is not None and intent.cloud_init_timeout == 4
    assert owned.bodies[0]["command"][-1] == "4"


def test_expired_original_deadline_prevents_all_provider_reads(owned: Harness) -> None:
    owned.clock[0] += 6
    with pytest.raises(TimeoutError):
        owned.bootstrap.start()
    assert owned.calls == []


def test_exact_serialized_body_limit_refuses_before_arming(owned: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_SCRIPT", "\\" * 40_000)
    with pytest.raises(ValidationError):
        owned.bootstrap.start()
    assert owned.calls == ["config", "power"]
    assert owned.owner.inspect_lifecycle_obligation(owned.bootstrap.obligation_id) is None


@pytest.mark.parametrize(
    "terminal,settled",
    [
        ({"exited": True, "exitcode": 1}, True),
        ({"exited": True, "signal": 15}, True),
        ({"exited": True, "exitcode": 0, "out-data": ""}, True),
        ({"exited": True, "exitcode": 0, "out-data": "non-ascii \u00e9"}, True),
        ({"exited": True, "exitcode": 0, "out-truncated": True}, True),
        ({"exited": False, "exitcode": 0}, False),
        ({"exited": "yes"}, False),
        ({"exited": True}, False),
    ],
)
def test_failed_or_unknown_prerequisites_settle_only_observed_invocations_without_another_get(
    owned: Harness,
    terminal: dict[str, object],
    settled: bool,
) -> None:
    owned.statuses.append(terminal)
    with pytest.raises(StateError):
        owned.bootstrap.start()
    observation = owned.bootstrap.observation
    assert not observation.bootstrap_succeeded
    assert (owned.row().state is LifecycleObligationState.RESOLVED) is settled
    owned.bootstrap.reconcile(Deadline.after(1))
    assert owned.bootstrap.observe(Deadline.after(1)) is observation
    assert (owned.row().state is LifecycleObligationState.RESOLVED) is settled
    assert owned.repository.inspect(owned.owner.ownership.scope) is not None
    assert owned.calls.count("POST") == owned.calls.count("GET") == 1


@pytest.mark.parametrize(
    "record",
    [
        f"AGW_CREATION_BOOTSTRAP_1:{'b' * 32}:complete:0\n",
        "AGW_CREATION_BOOTSTRAP_1:{nonce}:cloud_init:0\n",
        "AGW_CREATION_BOOTSTRAP_1:{nonce}:complete:1\n",
        "AGW_CREATION_BOOTSTRAP_1:{nonce}:complete:0\nextra",
    ],
)
def test_wrong_nonce_or_incomplete_script_never_succeeds(
    owned: Harness,
    monkeypatch: pytest.MonkeyPatch,
    record: str,
) -> None:
    original = owned.completed
    monkeypatch.setattr(
        owned,
        "completed",
        lambda **changes: original(**{"out-data": record.format(nonce=owned.bootstrap._nonce), **changes}),
    )
    with pytest.raises(StateError):
        owned.bootstrap.start()
    observation = owned.bootstrap.observation
    assert not observation.bootstrap_succeeded
    assert owned.row().state is LifecycleObligationState.RESOLVED
    assert owned.bootstrap.observe(Deadline.after(1)) is observation
    assert owned.calls.count("POST") == owned.calls.count("GET") == 1


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize("control", [False, True])
def test_lost_wire_response_never_replays(
    owned: Harness, monkeypatch: pytest.MonkeyPatch, method: str, control: bool
) -> None:
    original = _ProxmoxWire.request
    primary = KeyboardInterrupt("wire interrupted") if control else RuntimeError("wire response lost")

    def lost(wire: _ProxmoxWire, verb: str, suffix: str, **kwargs: Any) -> dict[str, object]:
        if verb == method:
            owned.calls.append(verb)
            raise primary
        return original(wire, verb, suffix, **kwargs)

    monkeypatch.setattr(_ProxmoxWire, "request", lost)
    with pytest.raises(KeyboardInterrupt if control else StateError) as caught:
        owned.bootstrap.start()
    if control:
        assert caught.value is primary
    owned.bootstrap.reconcile(Deadline.after(1))
    owned.bootstrap.observe(Deadline.after(1))
    assert owned.calls.count(method) == 1
    assert owned.row().state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("fact", ["acknowledgment", "terminal"])
@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_publication_reconciles_retained_facts_without_repeating_consumed_reads(
    owned: Harness,
    monkeypatch: pytest.MonkeyPatch,
    fact: str,
    committed: bool,
) -> None:
    original = owned.repository.publish_lifecycle_obligation_payload
    primary = KeyboardInterrupt("publication interrupted")

    def interrupted(*args: Any, **kwargs: Any) -> LifecycleObligation:
        if kwargs["expected_revision"] == (0 if fact == "acknowledgment" else 1):
            if committed:
                original(*args, **kwargs)
            raise primary
        return original(*args, **kwargs)

    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.bootstrap.start()
    assert caught.value is primary and owned.bootstrap.observation.pid == 321
    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", original)
    owned.bootstrap.reconcile(Deadline.after(1))
    if fact == "acknowledgment":
        assert owned.calls.count("GET") == 0
        owned.bootstrap.observe(Deadline.after(1))
    else:
        assert owned.bootstrap.observation.bootstrap_succeeded
        owned.bootstrap.observe(Deadline.after(1))
    assert owned.calls.count("POST") == owned.calls.count("GET") == 1
    assert owned.row().state is LifecycleObligationState.RESOLVED


def test_interrupted_acknowledgment_retention_still_observes_original_pid(owned: Harness) -> None:
    primary = KeyboardInterrupt("acknowledgment retention interrupted")

    class InterruptedHistory(list[BootstrapIntent]):
        armed = True

        def append(self, value: BootstrapIntent) -> None:
            super().append(value)
            if self.armed and value.observation.pid is not None:
                self.armed = False
                raise primary

    owned.bootstrap._history = InterruptedHistory(owned.bootstrap._history)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.bootstrap.start()
    assert caught.value is primary and owned.bootstrap.observation.pid == 321
    assert owned.calls.count("POST") == 1 and owned.calls.count("GET") == 0
    owned.bootstrap.reconcile(Deadline.after(1))
    assert owned.bootstrap.observe(Deadline.after(1)).bootstrap_succeeded
    assert owned.calls.count("POST") == owned.calls.count("GET") == 1
    assert owned.row().state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize("retained", [False, True])
def test_interruption_around_terminal_retention_cannot_invent_another_qga_record(
    owned: Harness,
    monkeypatch: pytest.MonkeyPatch,
    retained: bool,
) -> None:
    original = owned.bootstrap.record_status
    primary = KeyboardInterrupt("terminal retention interrupted")

    def interrupted(status: dict[str, object]) -> NoReturn:
        if retained:
            original(status)
        raise primary

    monkeypatch.setattr(owned.bootstrap, "record_status", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.bootstrap.start()
    assert caught.value is primary
    owned.bootstrap.reconcile(Deadline.after(1))
    owned.bootstrap.observe(Deadline.after(1))
    assert owned.calls.count("GET") == 1
    assert owned.bootstrap.observation.bootstrap_succeeded is retained
    assert (owned.row().state is LifecycleObligationState.RESOLVED) is retained


@pytest.mark.parametrize("stage", ["register", "mark", "resolve"])
@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_ledger_transitions_preserve_control_and_single_dispatch(
    owned: Harness,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    committed: bool,
) -> None:
    name = {
        "register": "register_lifecycle_obligation",
        "mark": "mark_lifecycle_obligation_possible_effect",
        "resolve": "resolve_lifecycle_obligation",
    }[stage]
    original: Callable[..., LifecycleObligation] = getattr(owned.repository, name)
    primary = KeyboardInterrupt("ledger interrupted")

    def interrupted(*args: Any, **kwargs: Any) -> NoReturn:
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(owned.repository, name, interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.bootstrap.start()
    assert caught.value is primary
    monkeypatch.setattr(owned.repository, name, original)
    owned.bootstrap.reconcile(Deadline.after(1))
    if stage != "register" or committed:
        assert owned.row().state is LifecycleObligationState.RESOLVED
    assert owned.calls.count("POST") == int(stage == "resolve")


def _late_acknowledgment(owned: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    original = _ProxmoxWire.request

    def late(wire: _ProxmoxWire, method: str, suffix: str, **kwargs: Any) -> dict[str, object]:
        result = original(wire, method, suffix, **kwargs)
        if method == "POST":
            owned.clock[0] += 6
        return result

    monkeypatch.setattr(_ProxmoxWire, "request", late)
    with pytest.raises(TimeoutError):
        owned.bootstrap.start()
    assert owned.bootstrap.observation.pid == 321 and owned.calls.count("GET") == 0


def test_fresh_cleanup_budget_observes_original_pid_after_original_deadline(
    owned: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _late_acknowledgment(owned, monkeypatch)
    owned.owner.stop_admission()
    assert owned.bootstrap.observe(Deadline.after(5)).bootstrap_succeeded
    assert owned.calls.count("POST") == owned.calls.count("GET") == 1
    assert owned.row().state is LifecycleObligationState.RESOLVED


def test_observed_running_record_allows_later_original_pid_observation(
    owned: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _ProxmoxWire.request
    owned.statuses.append({"exited": False})

    def running(wire: _ProxmoxWire, method: str, suffix: str, **kwargs: Any) -> dict[str, object]:
        result = original(wire, method, suffix, **kwargs)
        if method == "GET" and result.get("exited") is False:
            owned.clock[0] += 6
        return result

    monkeypatch.setattr(_ProxmoxWire, "request", running)
    with pytest.raises(TimeoutError):
        owned.bootstrap.start()
    assert owned.bootstrap.observe(Deadline.after(5)).bootstrap_succeeded
    assert owned.calls.count("POST") == 1 and owned.calls.count("GET") == 2


def test_success_record_cannot_override_nonzero_qga_completion(owned: Harness) -> None:
    owned.statuses.append(
        {
            "exited": True,
            "exitcode": 1,
            "out-data": f"AGW_CREATION_BOOTSTRAP_1:{owned.bootstrap._nonce}:complete:0\n",
        }
    )
    with pytest.raises(StateError):
        owned.bootstrap.start()
    assert owned.bootstrap.observation.completion == ExitStatus(code=1)
    assert not owned.bootstrap.observation.bootstrap_succeeded
    assert owned.row().state is LifecycleObligationState.RESOLVED


def test_stale_owner_cannot_read_provider_during_cleanup(owned: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    _late_acknowledgment(owned, monkeypatch)
    calls = owned.calls.copy()
    OperationOwner.recover(owned.repository, owned.owner.ownership, "b" * 32)
    with pytest.raises(StateError):
        owned.bootstrap.observe(Deadline.after(5))
    assert owned.calls == calls


def test_cleanup_refuses_replaced_generation_before_pid_observation(
    owned: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _late_acknowledgment(owned, monkeypatch)
    owned.config["vmgenid"] = SMBIOS
    with pytest.raises(StateError):
        owned.bootstrap.observe(Deadline.after(5))
    assert owned.calls.count("GET") == 0 and owned.calls.count("POST") == 1


@pytest.mark.parametrize("successful", [False, True])
def test_terminal_completion_waits_for_local_custody_before_resolution(
    owned: Harness, monkeypatch: pytest.MonkeyPatch, successful: bool
) -> None:
    original = owned.bootstrap.record_status
    settled = [True]
    monkeypatch.setattr(LocalDeliveryCustody, "settled", property(lambda _: settled[0]))

    def retained(status: dict[str, object]) -> None:
        settled[0] = False
        original(status)

    monkeypatch.setattr(owned.bootstrap, "record_status", retained)
    if successful:
        assert owned.bootstrap.start().bootstrap_succeeded
    else:
        owned.statuses.append({"exited": True, "exitcode": 1})
        with pytest.raises(StateError):
            owned.bootstrap.start()
        assert not owned.bootstrap.observation.bootstrap_succeeded
    assert owned.row().state is LifecycleObligationState.POSSIBLE_EFFECT
    settled[0] = True
    owned.bootstrap.reconcile(Deadline.after(1))
    assert owned.row().state is LifecycleObligationState.RESOLVED


def test_resolution_callback_observes_durable_terminal_facts_without_owner_reentry(
    owned: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = owned.repository.resolve_lifecycle_obligation

    def resolve(ownership: OperationOwnership, obligation_id: str) -> LifecycleObligation:
        row = owned.repository.inspect_lifecycle_obligation(ownership, obligation_id)
        assert row is not None and json.loads(row.payload)["observation"]["stage"] == "complete"
        return original(ownership, obligation_id)

    monkeypatch.setattr(owned.repository, "resolve_lifecycle_obligation", resolve)
    owned.bootstrap.start()


@pytest.mark.parametrize(
    ("fact", "corruption"),
    [("acknowledgment", "payload"), ("terminal", "payload"), ("acknowledgment", "version"), ("terminal", "version")],
)
def test_corrupt_persisted_payload_refuses_reconciliation(
    owned: Harness,
    monkeypatch: pytest.MonkeyPatch,
    fact: str,
    corruption: str,
) -> None:
    original = owned.repository.publish_lifecycle_obligation_payload
    published_payload: bytes | None = None

    def corrupt(*args: Any, **kwargs: Any) -> LifecycleObligation:
        nonlocal published_payload
        if kwargs["expected_revision"] == (0 if fact == "acknowledgment" else 1):
            published_payload = kwargs["payload"]
            if corruption == "version":
                kwargs["payload_version"] = 2
            else:
                kwargs["payload"] = b'{"foreign":true}'
        return original(*args, **kwargs)

    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", corrupt)
    with pytest.raises(StateError):
        owned.bootstrap.start()
    with pytest.raises(StateError):
        owned.bootstrap.reconcile(Deadline.after(1))
    assert owned.calls.count("GET") == int(fact == "terminal")
    row = owned.row()
    if corruption == "version":
        assert row.payload_version == 2 and row.payload == published_payload
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="requires guest Bash syntax")
def test_generated_payload_parses_as_bash(owned: Harness) -> None:
    owned.bootstrap.start()
    result = subprocess.run(["/bin/bash", "-n"], input=owned.bodies[0]["command"][9], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="requires guest Bash behavior")
def test_full_wrapper_retains_root_failure_record_outside_redirected_function(owned: Harness) -> None:
    owned.bootstrap.start()
    argv = owned.bodies[0]["command"]
    prefix = "id() { printf '1000\\n'; }\n"
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", prefix + argv[9], *argv[10:]],
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert result.stdout == f"AGW_CREATION_BOOTSTRAP_1:{argv[-3]}:root:1\n"


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="requires guest Bash behavior")
@pytest.mark.parametrize(
    ("account_exists", "group_exists"),
    [(False, False), (False, True), (True, False)],
    ids=["missing-group", "existing-group", "existing-account"],
)
def test_account_setup_reuses_existing_group_without_host_account_writes(
    tmp_path: Path,
    account_exists: bool,
    group_exists: bool,
) -> None:
    log = tmp_path / "account-calls"
    prefix = r"""set -euo pipefail
log=$1
admin=operator
account_exists=$2
group_exists=$3
account_created=0
group_created=0
getent() {
    [ "$#" -eq 2 ] && [ "$1" = passwd ] && [ "$2" = "$admin" ] || exit 99
    if [ "$account_exists" = 1 ]; then return 0; fi
    return 2
}
/usr/sbin/groupadd() {
    printf 'groupadd %s\n' "$*" >> "$log"
    [ "$1" = -f ] || return 99
    shift
    if [ "${1-}" = -- ]; then shift; fi
    [ "$#" -eq 1 ] && [ "$1" = "$admin" ] || return 99
    if [ "$group_exists" = 0 ]; then
        group_exists=1
        group_created=1
    fi
}
/usr/sbin/useradd() {
    printf 'useradd %s\n' "$*" >> "$log"
    local primary_group=
    local create_group=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -U) create_group=1; shift ;;
            -m) shift ;;
            -g) primary_group=$2; shift 2 ;;
            -s) [ "$2" = /bin/bash ] || return 99; shift 2 ;;
            --) shift; break ;;
            *) return 99 ;;
        esac
    done
    if [ "$create_group" = 1 ]; then
        [ "$group_exists" = 0 ] || return 9
        group_exists=1
        group_created=1
        primary_group=$admin
    fi
    [ "$primary_group" = "$admin" ] && [ "$#" -eq 1 ] && [ "$1" = "$admin" ] || return 99
    [ "$group_exists" = 1 ] || return 6
    [ "$account_exists" = 0 ] || return 9
    account_exists=1
    account_created=1
}
"""
    report = '\nprintf "%s:%s:%s:%s\\n" "$account_exists" "$group_exists" "$account_created" "$group_created"'
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            prefix + module._ACCOUNT_SETUP + report,
            "account-test",
            str(log),
            str(int(account_exists)),
            str(int(group_exists)),
        ],
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    expected_group = group_exists or not account_exists
    expected_created = not account_exists
    expected_new_group = not account_exists and not group_exists
    assert result.stdout == f"1:{int(expected_group)}:{int(expected_created)}:{int(expected_new_group)}\n"
    if account_exists:
        assert not log.exists()


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="requires guest Bash behavior")
@pytest.mark.parametrize(
    ("wait_exit", "query_exit", "status", "admitted"),
    [
        (0, 0, "status: done", True),
        (0, 0, "status: disabled", False),
        (2, 0, "status: done", False),
        (127, 0, "status: done", False),
        (0, 1, "status: done", False),
        (0, 0, "status: running", False),
    ],
)
def test_cloud_init_gate_requires_both_successful_wait_and_literal_done(
    tmp_path: Path,
    wait_exit: int,
    query_exit: int,
    status: str,
    admitted: bool,
) -> None:
    log = tmp_path / "calls"
    prefix = r"""set -euo pipefail
log=$1
wait_exit=$2
query_exit=$3
query_status=$4
cloud_wait=5
/usr/bin/timeout() {
    printf '%s\n' "$*" >> "$log"
    if [ "$*" = '--kill-after=5s 5s /usr/bin/cloud-init status --wait' ]; then
        return "$wait_exit"
    fi
    [ "$*" = '--kill-after=5s 5s /usr/bin/cloud-init status' ] || exit 99
    printf '%s\n' "$query_status"
    return "$query_exit"
}
"""
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            prefix + module._CLOUD_INIT_GATE + '\nprintf "admitted\\n" >> "$log"',
            "gate-test",
            str(log),
            str(wait_exit),
            str(query_exit),
            status,
        ],
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) is admitted, result.stderr
    assert ("admitted" in log.read_text().splitlines()) is admitted
    assert len(log.read_text().splitlines()) == (3 if admitted else 2 if wait_exit == 0 else 1)
