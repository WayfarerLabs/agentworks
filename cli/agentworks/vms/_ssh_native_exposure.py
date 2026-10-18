"""Access-local source discovery and returned HTTP-response custody."""

from __future__ import annotations

from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Protocol, cast
from urllib.error import HTTPError
from urllib.request import urlopen

from agentworks import output
from agentworks.capabilities.vm_platform.ssh_exposure import normalize_allow_cidrs
from agentworks.errors import AgentworksError, ConnectivityError, LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from collections.abc import Sequence


class _Response(Protocol):
    def read(self, limit: int) -> object: ...

    def close(self) -> None: ...


class OwnedSSHExposure:
    """One passive, single-use prefix owner, retained before discovery begins.

    Synchronous DNS, reads and public close use best-effort budgets. Normal
    response close does not establish physical socket drain or remote cancellation.
    """

    def __init__(self, vm_name: str) -> None:
        self._vm_name = vm_name
        self._response: _Response | None = None
        self._used = False

    @property
    def cleanup_incomplete(self) -> bool:
        return self._response is not None

    def _remaining(self, deadline: Deadline) -> float:
        self._require_finite(deadline)
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise LimitExceededError(
                "SSH source discovery deadline expired", entity_kind="vm", entity_name=self._vm_name
            )
        return remaining

    @staticmethod
    def _require_finite(deadline: Deadline) -> None:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("SSH source discovery requires a finite Deadline")

    def resolve(self, extra_cidrs: Sequence[str], deadline: Deadline) -> tuple[str, ...]:
        """Detect one current IPv4, then fold canonical configured prefixes once."""
        if self._used:
            raise StateError("SSH source discovery has already finished or begun")
        self._used = True
        extras = tuple(dict.fromkeys(normalize_allow_cidrs(extra_cidrs)))
        remaining = self._remaining(deadline)
        detected: str | None = None
        control: BaseException | None = None
        try:
            try:
                try:
                    self._response = cast(
                        "_Response",
                        urlopen("https://checkip.amazonaws.com", timeout=min(5.0, remaining)),  # noqa: S310
                    )
                except HTTPError as error:
                    # HTTPError is itself the actual returned response owner.
                    self._response = error
                    raise
                self._remaining(deadline)
                body = self._response.read(65)
                self._remaining(deadline)
                if not isinstance(body, bytes) or len(body) > 64:
                    raise ValueError("Invalid source-discovery response")
                detected = f"{IPv4Address(body.decode('ascii').strip())}/32"
            except Exception as error:
                if isinstance(error, AgentworksError):
                    raise
                if not extras:
                    raise ConnectivityError(
                        "Could not detect the operator's public IPv4 for scoped SSH access",
                        entity_kind="vm",
                        entity_name=self._vm_name,
                        hint="Configure operator.ssh_allow_cidrs with explicit IPv4 addresses or CIDRs",
                    ) from None
            except BaseException as error:
                control = error
                raise
            finally:
                self.close(deadline)
        except BaseException:
            if control is not None:
                raise control from None
            raise
        if self.cleanup_incomplete:
            raise StateError(
                "SSH source response retirement remains incomplete", entity_kind="vm", entity_name=self._vm_name
            )
        self._remaining(deadline)
        prefixes = tuple(dict.fromkeys((detected, *extras))) if detected is not None else extras
        if len(prefixes) > 64:
            raise LimitExceededError(
                "Scoped SSH access permits at most 64 source prefixes", entity_kind="vm", entity_name=self._vm_name
            )
        if detected is None:
            output.warn("Public IPv4 detection failed; using operator.ssh_allow_cidrs for scoped SSH access")
        self._remaining(deadline)
        return prefixes

    def close(self, deadline: Deadline) -> bool:
        """Terminal cleanup retries only public close of the retained original."""
        self._used = True
        self._require_finite(deadline)
        if self._response is None:
            return True
        if deadline.expired:
            return False
        try:
            self._response.close()
        except Exception:
            return False
        self._response = None
        return True
