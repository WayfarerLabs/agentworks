"""Private same-invocation Python selection and admission evidence."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._helper_launcher import build_clean_environment_argv, build_identity_argv

if TYPE_CHECKING:
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import ByteSink

_SHELL = "/bin/sh"
_LINUX_CANDIDATES = ("/usr/bin/python3",)
_DARWIN_CANDIDATES = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")
_DARWIN_SYSTEM_SHIM = "/usr/bin/python3"
_MAX_RECORD_BYTES = 128

_TRAMPOLINE = r"""import sys
n=sys.argv[1]
i=sys.argv[2]
s=sys.argv[3]
if sys.version_info < (3,11):
 sys.stdout.buffer.write(('AGW_RUNTIME_1:%s:unsupported_version:%s\n' % (n,i)).encode('ascii'))
 sys.stdout.buffer.flush()
else:
 try:
  import base64,bz2,hashlib,json,os,sys,types
 except ImportError:
  sys.stdout.buffer.write(('AGW_RUNTIME_1:%s:missing_modules:%s\n' % (n,i)).encode('ascii'))
  sys.stdout.buffer.flush()
 else:
  sys.stdout.buffer.write(('AGW_RUNTIME_1:%s:ready:%s\n' % (n,i)).encode('ascii'))
  sys.stdout.buffer.flush()
  sys.argv=['agentworks-fixed-helper',n]
  exec(compile(s,'<agentworks-fixed-helper>','exec'),{'__name__':'__main__'})
"""

_SHELL_SOURCE = r"""
nonce=$1
target_os=$2
shim=$3
trampoline=$4
helper=$5
shift 5

selected=
selected_index=
index=0
for candidate do
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
        selected=$candidate
        selected_index=$index
        break
    fi
    index=$((index + 1))
done

if [ -z "$selected" ]; then
    if [ "$target_os" = darwin ] && { [ -e "$shim" ] || [ -L "$shim" ]; }; then
        printf 'AGW_RUNTIME_1:%s:shim:s\n' "$nonce"
    else
        printf 'AGW_RUNTIME_1:%s:missing:-\n' "$nonce"
    fi
    exit 0
fi

if [ ! -f "$selected" ] || [ ! -x "$selected" ]; then
    printf 'AGW_RUNTIME_1:%s:unusable:%s\n' "$nonce" "$selected_index"
    exit 0
fi

if [ "$target_os" = darwin ] && { [ -e "$shim" ] || [ -L "$shim" ]; } && [ "$selected" -ef "$shim" ]; then
    printf 'AGW_RUNTIME_1:%s:shim:%s\n' "$nonce" "$selected_index"
    exit 0
fi

exec "$selected" -I -S -B -c "$trampoline" "$nonce" "$selected_index" "$helper"
"""


class RuntimeTargetOS(StrEnum):
    """Core-bound destination operating system for runtime selection."""

    LINUX = "linux"
    DARWIN = "darwin"


@dataclass(frozen=True, slots=True)
class RuntimeSelection:
    """One destination-bound fixed candidate set or sole explicit path."""

    target_os: RuntimeTargetOS
    explicit_path: str | None = None

    def __post_init__(self) -> None:
        if type(self.target_os) is not RuntimeTargetOS:
            raise ValidationError("Runtime selection requires an explicit target operating system")
        if self.explicit_path is not None:
            _validate_path(self.explicit_path)


class RuntimePrerequisiteState(StrEnum):
    """Closed prerequisite fact observed before helper output."""

    READY = "ready"
    MISSING = "missing"
    SHIM = "shim"
    UNUSABLE = "unusable"
    UNSUPPORTED_VERSION = "unsupported_version"
    MISSING_MODULES = "missing_modules"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RuntimePrerequisiteObservation:
    state: RuntimePrerequisiteState
    selected_path: str | None


def _validate_path(path: str) -> None:
    failed = type(path) is not str or "\0" in path
    if not failed:
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            failed = True
    if failed or not posixpath.isabs(path):
        raise ValidationError("Runtime candidate must be an absolute UTF-8 path")


def _candidates(selection: RuntimeSelection) -> tuple[str, ...]:
    if selection.explicit_path is not None:
        return (selection.explicit_path,)
    if selection.target_os is RuntimeTargetOS.LINUX:
        return _LINUX_CANDIDATES
    return _DARWIN_CANDIDATES


def build_runtime_helper_argv(
    *,
    selection: RuntimeSelection,
    fixed_source: str,
    nonce: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """Build one selector and helper invocation with its observation mapping."""
    candidates = _candidates(selection)
    system_shim = _DARWIN_SYSTEM_SHIM if selection.target_os is RuntimeTargetOS.DARWIN else None
    shim_argument = system_shim or "-"
    argv = build_clean_environment_argv(
        _SHELL,
        "-c",
        _SHELL_SOURCE,
        "agentworks-runtime-prerequisite",
        nonce,
        selection.target_os.value,
        shim_argument,
        _TRAMPOLINE,
        fixed_source,
        *candidates,
    )
    return argv, candidates, system_shim


def build_runtime_identity_helper_argv(
    plan: IdentityPlan,
    *,
    selection: RuntimeSelection,
    fixed_source: str,
    nonce: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """Build one runtime-selected helper invocation under an identity plan."""
    argv, candidates, system_shim = build_runtime_helper_argv(
        selection=selection,
        fixed_source=fixed_source,
        nonce=nonce,
    )
    return build_identity_argv(plan, argv), candidates, system_shim


_RECORD = re.compile(
    rb"AGW_RUNTIME_1:([0-9a-f]{32}):(ready|missing|shim|unusable|unsupported_version|missing_modules):(-|s|[0-9]+)\n"
)


@dataclass(slots=True)
class RuntimePrefixSink:
    """Consume one bounded runtime record, then pass admitted bytes through."""

    nonce: str
    candidates: tuple[str, ...]
    downstream: ByteSink = field(repr=False)
    system_shim: str | None = None
    _prefix: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _observation: RuntimePrerequisiteObservation | None = field(default=None, init=False, repr=False)
    _invalid: bool = field(default=False, init=False, repr=False)

    def try_write(self, data: memoryview) -> int | None:
        if not data:
            return 0
        if self._invalid:
            return len(data)
        if self._observation is not None:
            if self._observation.state is not RuntimePrerequisiteState.READY:
                self._invalid = True
                return len(data)
            return self.downstream.try_write(data)

        remaining = _MAX_RECORD_BYTES - len(self._prefix)
        scan_length = min(len(data), remaining + 1)
        newline = bytes(data[:scan_length]).find(b"\n")
        consumed = len(data) if newline < 0 else newline + 1
        if newline < 0 and len(data) <= remaining:
            self._prefix.extend(data)
            return len(data)
        if newline < 0 or len(self._prefix) + consumed > _MAX_RECORD_BYTES:
            self._prefix.clear()
            self._invalid = True
            return len(data)
        self._prefix.extend(data[:consumed])

        self._observation = _interpret_record(
            bytes(self._prefix),
            nonce=self.nonce,
            candidates=self.candidates,
            system_shim=self.system_shim,
        )
        self._prefix.clear()
        if self._observation.state is RuntimePrerequisiteState.UNKNOWN:
            self._invalid = True
            return len(data)
        remainder = data[consumed:]
        if not remainder:
            return consumed
        if self._observation.state is not RuntimePrerequisiteState.READY:
            self._invalid = True
            return len(data)
        written = self.downstream.try_write(remainder)
        if written is None:
            return consumed
        return consumed + written

    @property
    def observation(self) -> RuntimePrerequisiteObservation:
        if self._invalid or self._observation is None:
            return RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
        return self._observation

    def clear(self) -> None:
        self._prefix.clear()
        self._observation = None
        self._invalid = False


def _interpret_record(
    record: bytes,
    *,
    nonce: str,
    candidates: tuple[str, ...],
    system_shim: str | None,
) -> RuntimePrerequisiteObservation:
    match = _RECORD.fullmatch(record)
    if match is None or match.group(1).decode("ascii") != nonce:
        return RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    state = RuntimePrerequisiteState(match.group(2).decode("ascii"))
    token = match.group(3)
    if state is RuntimePrerequisiteState.SHIM and system_shim is None:
        return RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    if state is RuntimePrerequisiteState.MISSING:
        if token == b"-":
            return RuntimePrerequisiteObservation(state, None)
        return RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    if state is RuntimePrerequisiteState.SHIM and token == b"s":
        assert system_shim is not None
        return RuntimePrerequisiteObservation(state, system_shim)
    if not token.isdigit():
        return RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    index = int(token)
    if index >= len(candidates):
        return RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    return RuntimePrerequisiteObservation(state, candidates[index])
