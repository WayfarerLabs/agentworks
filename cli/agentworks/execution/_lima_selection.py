"""Bounded local Lima selection and access-owned directory identity.

Directory replacement changes this locator. In-place restore and engineered
inode reuse require explicit workflow disposition; this is not a universal
uncopyable VM generation. Guest marker and boot observations remain separate.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

from agentworks.capabilities.vm_platform.base import ProviderLocator, provider_locator_remaining
from agentworks.db import VMStatus
from agentworks.errors import ConnectivityError, StateError, ValidationError
from agentworks.execution._borrowed_io import DiscardSink
from agentworks.execution._lima_configuration import decode_version, read_configuration
from agentworks.execution._wsl2_registration import (
    HostObservationSink,
    WSLRegistration,
    execution_power_from_listing,
    observe_registration,
)
from agentworks.execution.carrier import CarrierIO, SinkOutput
from agentworks.execution.carriers._subprocess import run_process

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._local_download_publication_windows import _WindowsAPI
    from agentworks.execution.carrier import Deadline

# Lima omits errors when its inspection-error slice is empty. Only the boolean
# crosses our output boundary; raw provider errors can contain config material.
_PROJECTION = '{"name": .name, "status": .status, "dir": .dir, "vmType": .vmType, "hasErrors": (.errors != null)}'
_NATIVE_PROJECTION = _PROJECTION[:-1] + ', "user": .config.user.name}'


@dataclass(frozen=True, slots=True, repr=False)
class LimaDirectoryIdentity:
    """Immutable selected directory snapshot, without owning its descriptor."""

    path: str
    device: int
    inode: int

    def check(self) -> None:
        try:
            current = os.stat(self.path)
        except OSError:
            raise StateError("Lima selected instance directory is unavailable") from None
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (self.device, self.inode):
            raise StateError("Lima selected instance directory changed")


@dataclass(slots=True, repr=False)
class LimaDirectoryCustody:
    """Retain the original directory while its access owner uses the route."""

    identity: LimaDirectoryIdentity
    descriptor: int | None
    _windows_api: _WindowsAPI | None = field(default=None, repr=False)
    _close_uncertain: bool = field(default=False, init=False, repr=False)

    @classmethod
    def open(cls, path: str) -> LimaDirectoryCustody:
        observed = os.stat(path)
        identity = LimaDirectoryIdentity(path, observed.st_dev, observed.st_ino)
        held = cls(identity, None)
        try:
            if sys.platform == "win32":
                from agentworks.execution._local_download_publication_windows import (
                    _BACKUP_SEMANTICS,
                    _LIST_DIRECTORY,
                    _OPEN_EXISTING,
                    _READ_ATTRIBUTES,
                    _SHARE_DELETE,
                    _SHARE_READ,
                    _SHARE_WRITE,
                )

                held._windows_api = _windows_directory_api()
                held.descriptor = held._windows_api.open(
                    Path(path),
                    _READ_ATTRIBUTES | _LIST_DIRECTORY,
                    _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE,
                    _OPEN_EXISTING,
                    _BACKUP_SEMANTICS,
                )
            else:
                held.descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            held.check()
            return held
        except BaseException as control:
            try:
                held.close()
            except BaseException:
                raise control from LimaDirectoryControlFact(held)
            raise

    def check(self) -> None:
        descriptor = self.descriptor
        if descriptor is None or self._close_uncertain:
            raise StateError("Lima selected directory custody is closed")
        if self._windows_api is None:
            held = os.fstat(descriptor)
            current = held.st_dev, held.st_ino
            directory = stat.S_ISDIR(held.st_mode)
        else:
            from agentworks.execution._local_download_publication_windows import _DIRECTORY

            native = self._windows_api.info(descriptor)
            current = native.identity.volume, int.from_bytes(native.identity.file_id, "little")
            directory = bool(native.attributes & _DIRECTORY)
        if not directory or current != (self.identity.device, self.identity.inode):
            raise StateError("Lima selected directory custody changed")
        self.identity.check()

    @property
    def settled(self) -> bool:
        return self.descriptor is None and not self._close_uncertain

    def close(self) -> bool:
        if self.descriptor is not None and not self._close_uncertain:
            # Match the existing local publication owner's close-uncertainty
            # rule. An ambiguously closed number is diagnostic, not retry custody.
            self._close_uncertain = True
            if self._windows_api is None:
                os.close(self.descriptor)
            else:
                self._windows_api.close(self.descriptor)
            self.descriptor = None
            self._close_uncertain = False
        return self.settled


def _windows_directory_api() -> _WindowsAPI:
    from agentworks.execution._local_download_publication_windows import _WindowsAPI

    return _WindowsAPI()


class LimaDirectoryControlFact(Exception):
    """Retain the actual directory owner after interrupted construction cleanup."""

    def __init__(self, custody: LimaDirectoryCustody) -> None:
        self.custody = custody
        super().__init__("Lima selected directory retains uncertain local closure")


@dataclass(frozen=True, slots=True, repr=False)
class LimaSelection:
    namespace: str
    home: str
    instance: str
    executable: str
    directory: LimaDirectoryIdentity
    vm_type: str
    power: VMStatus
    registration: WSLRegistration | None = None

    @property
    def locator(self) -> ProviderLocator:
        framed = json.dumps(
            (
                "agentworks.lima.provider-locator.v1",
                self.namespace,
                self.home,
                self.instance,
                self.directory.path,
                self.directory.device,
                self.directory.inode,
                self.vm_type,
                None
                if self.registration is None
                else (
                    self.registration.machine,
                    self.registration.sid,
                    self.registration.guid,
                    self.registration.name,
                    self.registration.base_path,
                ),
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return ProviderLocator("lima:v1:" + hashlib.sha256(framed).hexdigest())

    def require_same_route(self, other: LimaSelection) -> None:
        if (self.locator, self.vm_type, self.executable) != (
            other.locator,
            other.vm_type,
            other.executable,
        ):
            raise StateError("Lima selected native route changed")

    def require_activation_result(self, other: LimaSelection) -> None:
        """Allow only this access's successful first import to acquire its GUID."""
        from dataclasses import replace

        before, after = self.registration, other.registration
        if before is not None and before.guid is None and after is not None and after.guid is not None:
            if (before.machine, before.sid, before.name) != (after.machine, after.sid, after.name):
                raise StateError("Lima imported registration changed placement")
            self.require_same_route(replace(other, registration=before))
        else:
            self.require_same_route(other)


@dataclass(frozen=True, slots=True, repr=False)
class LimaNativeSelection:
    """Lima's resolved account, acquired only after authorized availability."""

    provider: LimaSelection
    user: str
    fingerprints: tuple[str | None, ...]


@dataclass(slots=True, repr=False)
class LimaLocalSelection:
    """Resolve local defaults once during explicit preparation, then pin them."""

    namespace: str
    instance: str
    vm_name: str
    home: str
    executable: str
    directory: LimaDirectoryCustody | None = field(default=None, init=False)

    @classmethod
    def resolve(cls, namespace: str, instance: str, vm_name: str, deadline: Deadline) -> LimaLocalSelection:
        provider_locator_remaining(deadline, vm_name=vm_name)
        executable = shutil.which("limactl")
        if executable is None:
            raise ConnectivityError("Local Lima native execution requires limactl")
        home = str(Path(os.environ.get("LIMA_HOME") or Path.home() / ".lima").resolve())
        provider_locator_remaining(deadline, vm_name=vm_name)
        return cls(namespace, instance, vm_name, home, executable)

    def observe(self, deadline: Deadline, custody: LocalDeliveryCustody) -> LimaSelection:
        provider_locator_remaining(deadline, vm_name=self.vm_name)
        if not custody.settled:
            raise StateError("Lima selection retains unsettled local delivery")
        self._hold_directory()
        configuration = read_configuration(self.home, self.instance)
        drivers = configuration.classify()
        if drivers is None:
            version = decode_version(self._query([self.executable, "--version"], deadline, custody))
            drivers = configuration.classify(version, executable=self.executable)
        assert drivers is not None and self.directory is not None
        registration = None
        if drivers == {"wsl2"}:
            if sys.platform != "win32":
                raise StateError("Lima WSL native access requires Windows")
            name = "lima-" + self.instance
            registration = observe_registration(name, self.vm_name, deadline, custody)
            if registration.guid is None:
                power = VMStatus.STOPPED
            else:
                if _registration_path(registration.base_path) != _registration_path(self.directory.identity.path):
                    raise StateError("Lima WSL registration belongs to a different instance directory")
                power = execution_power_from_listing(
                    self._query(["wsl", "--list", "--verbose"], deadline, custody), name
                )
        else:
            path, _, driver, power = _decode(self._list(deadline, custody), self.instance)
            if path != self.directory.identity.path or driver not in drivers:
                raise StateError("Lima selected provider placement or driver changed")
            drivers = frozenset({driver})
        self.directory.check()
        if read_configuration(self.home, self.instance).fingerprints != configuration.fingerprints:
            raise StateError("Lima configuration changed during provider observation")
        provider_locator_remaining(deadline, vm_name=self.vm_name)
        return LimaSelection(
            self.namespace,
            self.home,
            self.instance,
            self.executable,
            self.directory.identity,
            next(iter(drivers)),
            power,
            registration,
        )

    def _query(self, argv: list[str], deadline: Deadline, custody: LocalDeliveryCustody) -> bytes:
        provider_locator_remaining(deadline, vm_name=self.vm_name)
        projection = HostObservationSink()
        result = run_process(
            argv,
            io=CarrierIO(output=SinkOutput(projection, DiscardSink(), require_live=False)),
            deadline=deadline,
            custody=custody,
            env={**os.environ, "LIMA_HOME": self.home},
        )
        provider_locator_remaining(deadline, vm_name=self.vm_name)
        if (
            result.failure is not None
            or result.exit_status != 0
            or not result.stdout.complete
            or not custody.settled
            or projection.oversized
        ):
            raise StateError("Lima selected instance observation is unavailable")
        return bytes(projection.data)

    def _list(self, deadline: Deadline, custody: LocalDeliveryCustody, *, native: bool = False) -> bytes:
        return self._query(
            [
                self.executable,
                "list",
                "--format=json",
                "--yq",
                _NATIVE_PROJECTION if native else _PROJECTION,
                self.instance,
            ],
            deadline,
            custody,
        )

    def _hold_directory(self) -> None:
        path = str(Path(self.home) / self.instance)
        if self.directory is None:
            # Store custody immediately after acquisition, before any later check.
            try:
                self.directory = LimaDirectoryCustody.open(path)
            except BaseException as control:
                if isinstance(control.__cause__, LimaDirectoryControlFact):
                    self.directory = control.__cause__.custody
                if isinstance(control, OSError):
                    raise StateError("Lima selected instance directory is unavailable") from None
                raise
        if path != self.directory.identity.path:
            raise StateError("Lima selected instance directory route changed")
        self.directory.check()

    def resolve_native(
        self, selected: LimaSelection, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> LimaNativeSelection:
        """Let Lima resolve its account once, then check host inputs on later use."""
        current = self.observe(deadline, custody)
        selected.require_same_route(current)
        if current.power is not VMStatus.RUNNING:
            raise StateError("Lima native user resolution requires running availability")
        fingerprints = read_configuration(self.home, self.instance).fingerprints
        path, user, driver, power = _decode(self._list(deadline, custody, native=True), self.instance, native=True)
        if path != selected.directory.path or driver != selected.vm_type or power is not VMStatus.RUNNING:
            raise StateError("Lima native user observation changed provider route")
        current = self.observe(deadline, custody)
        selected.require_same_route(current)
        if (
            current.power is not VMStatus.RUNNING
            or read_configuration(self.home, self.instance).fingerprints != fingerprints
        ):
            raise StateError("Lima native user observation changed configuration or power")
        return LimaNativeSelection(current, user, fingerprints)

    def check_native(self, selected: LimaNativeSelection, deadline: Deadline, custody: LocalDeliveryCustody) -> None:
        if read_configuration(self.home, self.instance).fingerprints != selected.fingerprints:
            raise StateError("Lima native configuration inputs changed")
        current = self.observe(deadline, custody)
        selected.provider.require_same_route(current)
        if current.power is not VMStatus.RUNNING:
            raise StateError("Lima selected native route is no longer running")

    def close(self) -> bool:
        return self.directory is None or self.directory.close()


class LimaObservationControlFact(Exception):
    """Actual query and directory custody carried by escaping control flow."""

    def __init__(self, route: LimaLocalSelection, custody: LocalDeliveryCustody) -> None:
        self.route = route
        self.custody = custody
        super().__init__("Lima observation retains local custody")

    def close(self, deadline: Deadline) -> bool:
        if not self.custody.close(deadline):
            return False
        return self.route.close()


@overload
def observe_lima_once(
    namespace: str,
    instance: str,
    vm_name: str,
    deadline: Deadline,
    custody: LocalDeliveryCustody,
    *,
    native: Literal[False] = False,
) -> LimaSelection: ...


@overload
def observe_lima_once(
    namespace: str,
    instance: str,
    vm_name: str,
    deadline: Deadline,
    custody: LocalDeliveryCustody,
    *,
    native: Literal[True],
) -> LimaNativeSelection: ...


def observe_lima_once(
    namespace: str,
    instance: str,
    vm_name: str,
    deadline: Deadline,
    custody: LocalDeliveryCustody,
    *,
    native: bool = False,
) -> LimaSelection | LimaNativeSelection:
    """Return an immutable snapshot, or retain unfinished local resources in cause."""
    route = LimaLocalSelection.resolve(namespace, instance, vm_name, deadline)
    try:
        provider = route.observe(deadline, custody)
        selected = route.resolve_native(provider, deadline, custody) if native else provider
    except BaseException as control:
        if not custody.settled:
            raise control from LimaObservationControlFact(route, custody)
        try:
            closed = route.close()
        except BaseException:
            raise control from LimaObservationControlFact(route, custody)
        if not closed:
            raise control from LimaObservationControlFact(route, custody)
        raise
    if not custody.settled:
        raise StateError("Lima observation retains local custody") from LimaObservationControlFact(route, custody)
    try:
        closed = route.close()
    except BaseException as control:
        raise control from LimaObservationControlFact(route, custody)
    if not closed:
        raise StateError("Lima observation retains uncertain directory closure") from LimaObservationControlFact(
            route, custody
        )
    return selected


def _decode(data: bytes, instance: str, *, native: bool = False) -> tuple[str, str, str, VMStatus]:
    """Validate only the bounded projected external response, never full config."""
    try:
        value = json.loads(data.decode("utf-8"))
        fields = {"name", "status", "dir", "vmType", "hasErrors"}
        if native:
            fields.add("user")
        if type(value) is not dict or set(value) != fields:
            raise ValueError
        if value["hasErrors"] is not False:
            raise ValueError
        if any(type(value[key]) is not str or not value[key] or "\0" in value[key] for key in fields - {"hasErrors"}):
            raise ValueError
        if value["name"] != instance or not os.path.isabs(value["dir"]):
            raise ValueError
        user = value["user"] if native else ""
        user.encode("utf-8")
        power = {"Running": VMStatus.RUNNING, "Stopped": VMStatus.STOPPED}.get(value["status"], VMStatus.UNKNOWN)
        return value["dir"], user, value["vmType"], power
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise StateError("Lima selected instance projection is invalid") from None


def _registration_path(path: str | None) -> str:
    if path is None or not ntpath.isabs(path):
        raise StateError("Lima WSL registration path is invalid")
    return ntpath.normcase(ntpath.normpath(path.removeprefix("\\\\?\\")))
