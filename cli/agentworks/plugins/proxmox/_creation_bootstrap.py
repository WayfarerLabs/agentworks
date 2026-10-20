"""Retained public prerequisites for one associated, newly created Proxmox VM.

This fixed root bootstrap precedes ordinary guest/account preparation. It grants
no prepared identity, rollback authority or ownership finalization.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.capabilities.vm_platform.bootstrap_script import generate_instance_marker_installer
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Capture, CarrierIO, Deadline, Dispatch, ExitStatus, PreparedInvocation
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, _status_report
from agentworks.naming import VM_USER_RE
from agentworks.operations import LifecycleObligation
from agentworks.plugins.proxmox._creation import CloneAssociation, _canonical_uuid, _smbios_uuid
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import Failure
    from agentworks.execution.carriers.proxmox import ProxmoxConnection
    from agentworks.operations import OperationOwner

OBLIGATION_KIND = "proxmox-creation-bootstrap"
PAYLOAD_VERSION = 1
_RECORD = re.compile(
    rb"AGW_CREATION_BOOTSTRAP_1:([0-9a-f]{32}):"
    rb"(root|cloud_init|scratch|packages|account|sudo|runtime|marker|complete):([0-9]{1,3})\n"
)
_IO = CarrierIO(output=Capture(max_bytes=512))
_CLOUD_INIT_GATE = r"""    /usr/bin/timeout --kill-after=5s "${cloud_wait}s" /usr/bin/cloud-init status --wait
    status=$(/usr/bin/timeout --kill-after=5s "${cloud_wait}s" /usr/bin/cloud-init status)
    [ "$status" = 'status: done' ]"""
_ACCOUNT_SETUP = r"""    if ! getent passwd "$admin"; then
        /usr/sbin/groupadd -f -- "$admin"
        /usr/sbin/useradd -m -g "$admin" -s /bin/bash -- "$admin"
    fi"""
_SCRIPT = r"""set -euo pipefail
nonce=$1
admin=$2
cloud_wait=$3
stage=root
trap 'rc=$?; printf "AGW_CREATION_BOOTSTRAP_1:%s:%s:%s\n" "$nonce" "$stage" "$rc"' EXIT
bootstrap() {
    [ "$(id -u)" = 0 ]
    [ -x /bin/sh ] && [ -x /bin/bash ]
    stage=cloud_init
__CLOUD_INIT_GATE__

    stage=scratch
    [ ! -L /tmp ]
    if [ -e /tmp ]; then
        [ -d /tmp ] && [ "$(stat -c %u /tmp)" = 0 ]
    else
        mkdir -m 1777 /tmp
    fi
    chown 0:0 /tmp
    chmod 1777 /tmp

    stage=packages
    if ! /usr/bin/python3 -I -c 'import base64,bz2,hashlib,json,os,sys,types; assert sys.version_info >= (3,11)' \
        || [ ! -x /usr/bin/sudo ] || [ ! -x /usr/bin/setpriv ]; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends python3 sudo util-linux
    fi

    stage=account
__ACCOUNT_SETUP__
    admin_uid=$(id -u "$admin")
    admin_gid=$(id -g "$admin")
    admin_groups=$(id -G "$admin")
    admin_groups=${admin_groups// /,}

    stage=sudo
    [ ! -L /etc/sudoers.d ] && [ -d /etc/sudoers.d ]
    [ "$(stat -c '%u:%g' /etc/sudoers.d)" = 0:0 ]
    mode=$(stat -c %a /etc/sudoers.d)
    [ "$((8#$mode & 0022))" = 0 ]
    sudo_file=/etc/sudoers.d/$admin
    [ ! -L "$sudo_file" ]
    if [ -e "$sudo_file" ]; then
        [ -f "$sudo_file" ]
        [ "$(stat -c '%u:%g:%h' "$sudo_file")" = 0:0:1 ]
    fi
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$admin" > "$sudo_file"
    chown 0:0 "$sudo_file"
    chmod 0440 "$sudo_file"
    /usr/sbin/visudo -cf "$sudo_file"
    /usr/bin/setpriv --reuid="$admin_uid" --regid="$admin_gid" --groups="$admin_groups" \
        --inh-caps=-all --ambient-caps=-all -- \
        /usr/bin/sudo -n --user='#0' -- /bin/sh -c '[ "$(id -u)" = 0 ]'

    stage=runtime
    /bin/sh -c ':'
    /bin/bash --noprofile --norc -c ':'
    [ "$(stat -c '%u:%g:%a' /tmp)" = 0:0:1777 ]
    /usr/bin/setpriv --reuid="$admin_uid" --regid="$admin_gid" --groups="$admin_groups" \
        --inh-caps=-all --ambient-caps=-all -- \
        /usr/bin/python3 -I -c 'import base64,bz2,hashlib,json,os,sys,types; assert sys.version_info >= (3,11)'
    stage=marker
__MARKER_INSTALLER__
    stage=complete
}
bootstrap >/dev/null 2>&1
""".replace("__CLOUD_INIT_GATE__", _CLOUD_INIT_GATE).replace("__ACCOUNT_SETUP__", _ACCOUNT_SETUP)


@dataclass(frozen=True, slots=True)
class BootstrapObservation:
    """Known QGA and fixed-script facts, without descendant quiescence claims."""

    pid: int | None = None
    completion: ExitStatus | None = None
    failure: Failure | None = None
    stage: str | None = None
    script_exit: int | None = None

    @property
    def bootstrap_succeeded(self) -> bool:
        return (
            self.completion == ExitStatus(code=0)
            and self.failure is None
            and self.stage == "complete"
            and self.script_exit == 0
        )


@dataclass(frozen=True, slots=True, repr=False)
class BootstrapIntent:
    """One bounded public request and its acknowledged facts, never credentials."""

    origin: str
    node: str
    vmid: int
    api_identity: str
    smbios_uuid: str
    vmgenid: str
    instance_marker: str
    admin_username: str
    nonce: str
    cloud_init_timeout: int
    source_sha256: str
    request_sha256: str
    observation: BootstrapObservation = BootstrapObservation()


def _encode(intent: BootstrapIntent) -> bytes:
    encoded = json.dumps(
        {"version": PAYLOAD_VERSION, **asdict(intent)}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(encoded) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("Proxmox creation bootstrap intent exceeds lifecycle bound")
    return encoded


class ProxmoxCreationBootstrap:
    """Retain before start; submit one fixed root bootstrap under original custody.

    Fresh observation budgets can read only an acknowledged, still available QGA
    record. An uncertain GET is never retried. Terminal completion settles only
    this invocation; failed prerequisites still refuse admission and retain the
    separate resource obligation for rollback. This controller never releases its owner.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        connection: ProxmoxConnection,
        association: CloneAssociation,
        *,
        expected_vmgenid: str,
        instance_marker: str,
        admin_username: str,
        custody: LocalDeliveryCustody,
        deadline: Deadline,
    ) -> None:
        if (
            owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
            or type(association) is not CloneAssociation
            or association.node != connection.node
            or association.vmid != connection.vmid
        ):
            raise ValidationError("Proxmox creation bootstrap requires exact associated VM ownership")
        if type(admin_username) is not str or VM_USER_RE.fullmatch(admin_username) is None:
            raise ValidationError("Proxmox creation bootstrap requires a valid administrative account")
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Proxmox creation bootstrap requires an original finite deadline")
        self._association = replace(association, smbios_uuid=_canonical_uuid(association.smbios_uuid))
        self._vmgenid = _canonical_uuid(expected_vmgenid)
        self._marker = validate_vm_instance_marker(instance_marker)
        self._admin = admin_username
        self._owner = owner
        self._connection = connection
        self._carrier = ProxmoxCarrier(connection)
        self._custody = custody
        self._deadline = deadline
        self._read_deadline = deadline
        self._nonce = uuid4().hex
        self._obligation_id = uuid4().hex
        self._history: list[BootstrapIntent] = []
        self._lock = Lock()
        self._attempted = False
        self._execute_entered = False
        self._not_sent = False
        self._status_available = False

    @property
    def intent(self) -> BootstrapIntent | None:
        return self._history[-1] if self._history else None

    @property
    def observation(self) -> BootstrapObservation:
        intent = self.intent
        return BootstrapObservation() if intent is None else intent.observation

    @property
    def obligation_id(self) -> str:
        return self._obligation_id

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("Proxmox creation bootstrap requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("Proxmox creation bootstrap observation deadline expired")
        return remaining

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("Proxmox creation bootstrap transition deadline expired")

    def _check_route(self, deadline: Deadline) -> None:
        self._owner.list_pending_lifecycle_obligations()
        wire = self._carrier._wire
        config = wire.request_current_config(timeout=self._remaining(deadline), custody=self._custody)
        self._remaining(deadline)
        if (
            "lock" in config
            or config.get("description") != f"agentworks-instance:{self._marker}"
            or _smbios_uuid(config) != self._association.smbios_uuid
            or config.get("vmgenid") != self._vmgenid
        ):
            raise StateError("Proxmox creation bootstrap association changed or is unavailable")
        self._owner.list_pending_lifecycle_obligations()
        power = wire.request_power(timeout=self._remaining(deadline), custody=self._custody)
        self._remaining(deadline)
        if power.get("status") != "running" or not self._custody.settled:
            raise StateError("Proxmox creation bootstrap requires observed running power")

    def start(self) -> BootstrapObservation:
        """Check the association, arm one exact intent and execute once."""
        self._acquire(self._deadline)
        try:
            if self._attempted:
                raise StateError("Proxmox creation bootstrap was already attempted")
            self._attempted = True
            self._check_route(self._deadline)
            wait_seconds = math.ceil(self._remaining(self._deadline))
            source = _SCRIPT.replace(
                "__MARKER_INSTALLER__", generate_instance_marker_installer(instance_marker=self._marker)
            )
            invocation = PreparedInvocation(
                (
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "LANG=C",
                    "LC_ALL=C",
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    source,
                    "agentworks-creation-bootstrap",
                    self._nonce,
                    self._admin,
                    str(wait_seconds),
                )
            )
            request = self._carrier._request_body(invocation, _IO)
            connection = self._connection
            self._history.append(
                BootstrapIntent(
                    connection.api_url,
                    connection.node,
                    connection.vmid,
                    connection.token_id,
                    self._association.smbios_uuid,
                    self._vmgenid,
                    self._marker,
                    self._admin,
                    self._nonce,
                    wait_seconds,
                    hashlib.sha256(source.encode("ascii")).hexdigest(),
                    hashlib.sha256(request).hexdigest(),
                )
            )
            self._remaining(self._deadline)
            intent = self.intent
            assert intent is not None
            obligation = self._owner.register_lifecycle_obligation(
                OBLIGATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=_encode(intent),
                obligation_id=self._obligation_id,
            )
            self._remaining(self._deadline)
            obligation.mark_possible_effect()
            self._remaining(self._deadline)
            self._execute_entered = True
            report = self._carrier._execute(
                invocation, io=_IO, deadline=self._deadline, custody=self._custody, observer=self
            )
            self._not_sent = report.dispatch is Dispatch.NOT_SENT
            self._reconcile_locked()
            self._remaining(self._deadline)
            if not self.observation.bootstrap_succeeded:
                stage = self.observation.stage or "delivery"
                raise StateError(f"Proxmox creation bootstrap prerequisite completion is unproved ({stage})")
            return self.observation
        finally:
            self._lock.release()

    def acknowledge(self, pid: int) -> None:
        intent = self.intent
        assert intent is not None
        self._status_available = True
        self._history.append(replace(intent, observation=BootstrapObservation(pid=pid)))
        self._reconcile_locked()

    def before_status(self) -> None:
        self._reconcile_locked()
        self._remaining(self._read_deadline)
        self._status_available = False

    def record_status(self, status: dict[str, object]) -> None:
        report = _status_report(status, _IO, Deadline(None))
        if report is None:
            self._status_available = True
            return
        stage: str | None = None
        script_exit: int | None = None
        match = _RECORD.fullmatch(report.stdout.data)
        if match is not None and match[1].decode("ascii") == self._nonce:
            stage, script_exit = match[2].decode("ascii"), int(match[3])
        observation = BootstrapObservation(self.observation.pid, report.completion, report.failure, stage, script_exit)
        intent = self.intent
        assert intent is not None
        self._history.append(replace(intent, observation=observation))
        self._reconcile_locked()

    def observe(self, deadline: Deadline) -> BootstrapObservation:
        """Observe only this acknowledged PID with a fresh finite cleanup budget."""
        self._acquire(deadline)
        try:
            self._reconcile_locked()
            observation = self.observation
            if observation.completion is not None or not self._status_available or observation.pid is None:
                return observation
            if not self._custody.settled:
                raise StateError("Proxmox creation bootstrap retains unsettled local delivery")
            self._check_route(deadline)
            self._read_deadline = deadline
            self.before_status()
            status = self._carrier._wire.request(
                "GET",
                f"exec-status?pid={observation.pid}",
                custody=self._custody,
                timeout=self._remaining(deadline),
            )
            self.record_status(status)
            self._remaining(deadline)
            return self.observation
        finally:
            self._lock.release()

    def reconcile(self, deadline: Deadline) -> None:
        """Reconcile retained local facts with the ledger, without provider reads."""
        self._acquire(deadline)
        try:
            self._reconcile_locked()
            self._remaining(deadline)
        finally:
            self._lock.release()

    def _reconcile_locked(self) -> None:
        if not self._attempted:
            raise StateError("Proxmox creation bootstrap has no registration attempt")
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        if row is None:
            if not self._execute_entered and self._custody.settled:
                return
            raise StateError("Proxmox creation bootstrap registration remains unavailable")
        revision = row.payload_revision
        if (
            row.obligation_kind != OBLIGATION_KIND
            or row.payload_version != PAYLOAD_VERSION
            or not 0 <= revision < len(self._history)
            or row.payload != _encode(self._history[revision])
        ):
            raise StateError("Proxmox creation bootstrap custody conflicts")
        obligation = LifecycleObligation(self._owner, row)
        # Invocation completion does not establish prerequisites or discharge the resource.
        can_resolve = self._custody.settled and (
            not self._execute_entered or self._not_sent or self.observation.completion is not None
        )
        if row.state is LifecycleObligationState.RESOLVED:
            if not can_resolve or revision != len(self._history) - 1:
                raise StateError("Proxmox creation bootstrap custody was prematurely resolved")
            return
        if not self._execute_entered:
            if can_resolve:
                obligation.resolve()
            return
        if row.state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("Proxmox creation bootstrap dispatch has no durable arm")
        for next_revision in range(revision + 1, len(self._history)):
            obligation.publish_payload(
                expected_revision=next_revision - 1,
                payload_version=PAYLOAD_VERSION,
                payload=_encode(self._history[next_revision]),
            )
        if can_resolve:
            latest = self._owner.inspect_lifecycle_obligation(self._obligation_id)
            if (
                latest is None
                or latest.payload_revision != len(self._history) - 1
                or latest.payload != _encode(self._history[-1])
            ):
                raise StateError("Proxmox creation bootstrap durable completion evidence conflicts")
            LifecycleObligation(self._owner, latest).resolve()
