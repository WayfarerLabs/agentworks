"""Host-only Lima driver classification and configuration input fingerprints."""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentworks.errors import StateError

_INPUT_BOUND = 4 * 1024 * 1024
_BUILTIN = frozenset({"qemu", "vz", "wsl2"})


@dataclass(frozen=True, slots=True, repr=False)
class LimaConfiguration:
    """Only classifier inputs and hashes survive the local configuration read."""

    fingerprints: tuple[str | None, ...]
    drivers: tuple[str | None, ...]
    arch: str | None

    def classify(
        self, version: tuple[int, int] | None = None, *, executable: str | None = None
    ) -> frozenset[str] | None:
        """Return known builtins, or request version evidence for differing defaults."""
        explicit = next((value for value in self.drivers if value is not None), None)
        if explicit in _BUILTIN:
            return frozenset({explicit})
        if explicit not in (None, "", "default"):
            raise StateError("Lima native execution requires a supported built-in driver")
        if version is None:
            return None
        major, minor = version
        if major > 2 or major in (1, 2) and minor > 2 or major == 0 and minor > 23:
            raise StateError("Lima driver defaults are unconfirmed for this version")
        if major == 1:
            explicit = next((value for value in self.drivers if value not in (None, "", "default")), None)
        elif major == 0 or version in {(2, 0), (2, 1)}:
            if (explicit in ("", "default") and major == 0) or (explicit == "" and major == 2):
                explicit = None
        elif major != 2:
            raise StateError("Lima driver defaults are unconfirmed for this version")
        if explicit is not None:
            if explicit not in _BUILTIN:
                raise StateError("Lima native execution requires a supported built-in driver")
            return frozenset({explicit})
        if major < 2:
            return frozenset({"qemu", "vz"}) if major == 1 and sys.platform == "darwin" else frozenset({"qemu"})
        if sys.platform == "win32" and minor < 2:
            # 2.0 always selects the host default; 2.1 adds non-native-arch QEMU.
            if minor == 0 or self.arch in (None, "", "default"):
                return frozenset({"wsl2"})
            if self.arch == _windows_executable_arch(executable):
                return frozenset({"wsl2"})
        return frozenset({"qemu", "vz"}) if sys.platform == "darwin" else frozenset({"qemu"})


def read_configuration(home: str, instance: str) -> LimaConfiguration:
    """Bound local inputs used by Lima Load and its creation-version defaults.

    Existing instance bases were embedded during creation. Global default and
    override files remain live inputs. The version file affects Lima's user
    defaults, so native route checks pin it along with all three YAML files.
    """
    root = Path(home)
    paths = (
        root / "_config/override.yaml",
        root / instance / "lima.yaml",
        root / "_config/default.yaml",
        root / instance / "lima-version",
    )
    hashes: list[str | None] = []
    drivers: list[str | None] = []
    arches: list[str | None] = []
    try:
        for index, path in enumerate(paths):
            try:
                with path.open("rb") as stream:
                    data = stream.read(_INPUT_BOUND + 1)
            except FileNotFoundError:
                if index == 1:
                    raise StateError("Lima selected instance configuration is unavailable") from None
                hashes.append(None)
                if index < 3:
                    drivers.append(None)
                    arches.append(None)
                continue
            if len(data) > _INPUT_BOUND:
                raise ValueError
            hashes.append(hashlib.sha256(data).hexdigest())
            if index == 3:
                continue
            document = yaml.safe_load(data)
            if document is None:
                document = {}
            if type(document) is not dict:
                raise ValueError
            driver, arch = document.get("vmType"), document.get("arch")
            if any(value is not None and type(value) is not str for value in (driver, arch)):
                raise ValueError
            drivers.append(driver)
            arches.append(arch)
    except (OSError, ValueError, yaml.YAMLError, RecursionError, UnicodeError):
        raise StateError("Lima selected configuration inputs are unavailable or invalid") from None
    return LimaConfiguration(
        tuple(hashes), tuple(drivers), next((value for value in arches if value is not None), None)
    )


def decode_version(data: bytes) -> tuple[int, int]:
    """Validate the bounded limactl version response without retaining it."""
    match = re.fullmatch(rb"limactl version v?(\d{1,9})\.(\d{1,9})\.\d+(?:[-+][0-9A-Za-z.+-]+)?\s*", data)
    if match is None:
        raise StateError("Lima driver version observation is unavailable")
    return int(match[1]), int(match[2])


def _windows_executable_arch(executable: str | None) -> str:
    """Read the selected binary's PE machine, including emulated executables.

    Lima 2.1 compares arch with its own Go runtime, not the Python host. The
    Microsoft PE header provides that binary architecture without execution.
    """
    try:
        if executable is None:
            raise ValueError
        with Path(executable).open("rb") as stream:
            dos = stream.read(64)
            if len(dos) != 64 or dos[:2] != b"MZ":
                raise ValueError
            offset = int.from_bytes(dos[60:64], "little")
            if not 64 <= offset <= 65_530:
                raise ValueError
            stream.seek(offset)
            header = stream.read(6)
        if len(header) != 6 or header[:4] != b"PE\0\0":
            raise ValueError
        return {0x8664: "x86_64", 0xAA64: "aarch64"}[int.from_bytes(header[4:], "little")]
    except (OSError, ValueError, KeyError):
        raise StateError(
            "Lima 2.1 implicit driver requires a recognized limactl Windows executable architecture"
        ) from None
