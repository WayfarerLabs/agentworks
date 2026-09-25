"""Host-side identity plans and fixed launchers for private helpers."""

from __future__ import annotations

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


def build_clean_environment_argv(executable: str, *arguments: str) -> tuple[str, ...]:
    """Launch one fixed executable under the common cleared environment."""
    return (*_ENV, executable, *arguments)


def build_identity_argv(plan: IdentityPlan, fixed_argv: tuple[str, ...]) -> tuple[str, ...]:
    """Apply one validated identity transition to fixed inner argv."""
    expected = _validate_plan(plan)
    return _identity_argv(plan, expected, fixed_argv)


def _identity_argv(
    plan: IdentityPlan,
    expected: IdentityExpectation,
    fixed_argv: tuple[str, ...],
) -> tuple[str, ...]:
    if plan.mode is IdentityMode.DIRECT:
        return fixed_argv
    if plan.mode is IdentityMode.SUDO_ROOT:
        return (_SUDO, "-n", "--user=#0", "--", *fixed_argv)
    return (
        _SETPRIV,
        f"--reuid={expected.euid}",
        f"--regid={expected.egid}",
        f"--groups={','.join(str(group) for group in expected.groups)}",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--",
        *fixed_argv,
    )
