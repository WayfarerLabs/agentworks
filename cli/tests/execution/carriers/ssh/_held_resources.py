"""Fixture callers that retain SSH resources through explicit bounded cleanup."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh.connection import SSHConnection
from agentworks.execution.carriers.ssh.enrollment import (
    SSHCreationProvenance,
    SSHEnrollmentCandidate,
    SSHEnrollmentCustody,
    enroll_new_target,
    recover_enrollment,
)
from agentworks.execution.carriers.ssh.forwarding import LocalForward, OwnedForwarding


class Closable(Protocol):
    def close(self, deadline: Deadline) -> bool: ...


class SSHResourceCaller:
    """One fixture owns coordinators and closes their shared native store last."""

    def __init__(self) -> None:
        self.delivery = LocalDeliveryCustody()
        self.resources: list[Closable] = []

    def close(self, deadline: Deadline) -> bool:
        settled = [resource.close(deadline) for resource in self.resources]
        if not all(settled):
            return False
        return self.delivery.close(deadline)


@contextmanager
def held_resource(resource: Closable) -> Iterator[None]:
    """Preserve an existing control exception if bounded fixture cleanup fails."""
    try:
        yield
    except BaseException as error:
        try:
            assert resource.close(Deadline.after(3))
        except BaseException as cleanup_error:
            if isinstance(error, Exception) and not isinstance(cleanup_error, Exception):
                raise
            error.add_note("Fixture resource cleanup did not complete; ownership remains retained.")
        raise
    else:
        assert resource.close(Deadline.after(3))


class ForwardingCaller:
    """Hold coordinators before startup and settle them before native storage."""

    def __init__(self, delivery: LocalDeliveryCustody) -> None:
        self.delivery = delivery
        self.resources: list[OwnedForwarding] = []

    @contextmanager
    def session(
        self,
        connection: SSHConnection,
        forwards: Sequence[LocalForward],
        *,
        deadline: Deadline,
    ) -> Iterator[OwnedForwarding]:
        resource = OwnedForwarding(connection, forwards)
        self.resources.append(resource)
        with held_resource(resource):
            resource.start(deadline=deadline, custody=self.delivery)
            yield resource

    def close(self, deadline: Deadline) -> bool:
        return all([resource.close(deadline) for resource in self.resources])


class EnrollmentCaller:
    """Retain every fixture maintenance resource before invoking production code."""

    def __init__(self, delivery: LocalDeliveryCustody) -> None:
        self.delivery = delivery
        self.resources: list[SSHEnrollmentCustody] = []

    def maintain(
        self,
        connection: SSHConnection,
        *,
        provenance: SSHCreationProvenance,
        deadline: Deadline,
        first_contact: bool,
    ) -> SSHEnrollmentCandidate:
        resource = SSHEnrollmentCustody(self.delivery)
        self.resources.append(resource)
        operation = enroll_new_target if first_contact else recover_enrollment
        with held_resource(resource):
            return operation(connection, provenance=provenance, deadline=deadline, custody=resource)

    def enroll(
        self, connection: SSHConnection, *, provenance: SSHCreationProvenance, deadline: Deadline
    ) -> SSHEnrollmentCandidate:
        return self.maintain(connection, provenance=provenance, deadline=deadline, first_contact=True)

    def recover(
        self, connection: SSHConnection, *, provenance: SSHCreationProvenance, deadline: Deadline
    ) -> SSHEnrollmentCandidate:
        return self.maintain(connection, provenance=provenance, deadline=deadline, first_contact=False)

    def close(self, deadline: Deadline) -> bool:
        return all([resource.close(deadline) for resource in self.resources])
