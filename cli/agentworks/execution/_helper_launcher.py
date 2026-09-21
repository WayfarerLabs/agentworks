"""Host-side identity plans and fixed launchers for private helpers."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from enum import StrEnum

from agentworks.errors import ValidationError
from agentworks.execution._helper_identity import IdentityExpectation

_MAX_ID = 2**32 - 1
_ENV = ("/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C")
_SUDO = "/usr/bin/sudo"
_SETPRIV = "/usr/bin/setpriv"


class IdentityMode(StrEnum):
    DIRECT = "direct"
    SUDO_ROOT = "sudo_root"
    DEMOTE = "demote"


@dataclass(frozen=True, slots=True)
class IdentityPlan:
    expected: IdentityExpectation
    mode: IdentityMode


def _validate_plan(plan: IdentityPlan) -> IdentityExpectation:
    if type(plan) is not IdentityPlan or type(plan.expected) is not IdentityExpectation:
        raise ValidationError("Helper launch requires a bound identity plan")
    expected = plan.expected
    if (
        type(expected.euid) is not int
        or not 0 <= expected.euid <= _MAX_ID
        or type(expected.egid) is not int
        or not 0 <= expected.egid <= _MAX_ID
        or type(expected.groups) is not tuple
        or not expected.groups
        or any(type(group) is not int or not 0 <= group <= _MAX_ID for group in expected.groups)
        or expected.groups != tuple(sorted(set(expected.groups)))
        or expected.egid not in expected.groups
        or type(plan.mode) is not IdentityMode
    ):
        raise ValidationError("Helper launch requires a valid identity plan")
    if plan.mode is IdentityMode.SUDO_ROOT and expected.euid != 0:
        raise ValidationError("Root entry requires an expected UID of zero")
    if plan.mode is IdentityMode.DEMOTE and expected.euid == 0:
        raise ValidationError("Identity demotion requires a non-root expected UID")
    return expected


def _validate_runtime_path(runtime_path: str) -> None:
    failed = False
    if type(runtime_path) is not str or "\0" in runtime_path:
        failed = True
    else:
        try:
            runtime_path.encode("utf-8")
        except UnicodeEncodeError:
            failed = True
    if failed or not posixpath.isabs(runtime_path) or "=" in runtime_path:
        raise ValidationError("Helper runtime must be an absolute non-assignment UTF-8 path")


def build_clean_environment_argv(executable: str, *arguments: str) -> tuple[str, ...]:
    """Launch one fixed executable under the common cleared environment."""
    return (*_ENV, executable, *arguments)


def build_clean_helper_argv(
    *,
    runtime_path: str,
    fixed_source: str,
    nonce: str,
) -> tuple[str, ...]:
    """Build one fixed helper launch under the carrier delivery identity."""
    _validate_runtime_path(runtime_path)
    return build_clean_environment_argv(runtime_path, "-I", "-S", "-B", "-c", fixed_source, nonce)


def build_helper_argv(
    plan: IdentityPlan,
    *,
    runtime_path: str,
    fixed_source: str,
    nonce: str,
) -> tuple[str, ...]:
    """Build one fixed helper launch with an explicit identity transition."""
    expected = _validate_plan(plan)
    helper = build_clean_helper_argv(runtime_path=runtime_path, fixed_source=fixed_source, nonce=nonce)
    if plan.mode is IdentityMode.DIRECT:
        return helper
    if plan.mode is IdentityMode.SUDO_ROOT:
        return (_SUDO, "-n", "--user=#0", "--", *helper)
    return (
        _SETPRIV,
        f"--reuid={expected.euid}",
        f"--regid={expected.egid}",
        f"--groups={','.join(str(group) for group in expected.groups)}",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--",
        *helper,
    )
