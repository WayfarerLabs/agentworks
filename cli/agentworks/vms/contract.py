"""The VM contract: what every managed VM must satisfy before core relies on it.

One table of checks, each an id, a severity and a probe. :func:`check_base`
runs them over a :class:`VMAccess` and returns a :class:`BaseReport`;
:func:`enforce` shows its warnings and raises for its failures. A failure means
initialization cannot repair the VM, so it names recreation; a warning names
the command that fixes it.

A report takes three requests, in this order: the release (its own request,
first, so a VM that is not a recognized Debian is asked nothing else), one
unelevated script for the script-probed rows, and the elevated ``sudo`` check.
The script has proved python3 by the time ``sudo`` runs, so a missing ``sudo``
is never read as a missing python3.

What a request already proves has no row: python3 and agw-launch (every
request runs them, and ``require_completed`` types their absence as
``VMNotReady``), the admin account (the connection authenticated as it), and
Tailscale at the recorded address (the connection reached the VM there and
verified its host key).

This module imports nothing from the legacy transport stack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.debian import verify_os_release
from agentworks.errors import StateError
from agentworks.execution.access import Admin, Agent
from agentworks.execution.requests import Command, Script, Shell
from agentworks.execution.results import Completed, NotDelivered, NotDeliveredReason

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping, Sequence

    from agentworks.debian import DebianRelease
    from agentworks.execution.access import Identity, VMAccess


class Severity(StrEnum):
    """A failure leaves the VM not ready; a warning never fails the contract."""

    FAIL = "fail"
    WARN = "warn"


@dataclass(frozen=True)
class Check:
    """One row of the contract.

    ``probe`` is the row's own request, or its fragment of the base script.
    A fragment reports a problem by calling ``problem ID DETAIL`` and never
    exits.
    """

    id: str
    severity: Severity
    probe: Command | str


PROBE_DEADLINE = 60
"""Seconds each contract request may take: a hung VM must not hang create or reinit."""

ACCOUNTS_VARIABLE = "AGW_CONTRACT_ACCOUNTS"
"""The script's environment variable naming the accounts to check, space separated: data, never source."""

RELEASE = Check("release", Severity.FAIL, Command(("cat", "/etc/os-release"), deadline=PROBE_DEADLINE))

# sd_booted()'s own test.
SYSTEMD = Check(
    "systemd", Severity.FAIL, '[ -d /run/systemd/system ] || problem systemd "the system was not booted with systemd"'
)

SUDO = Check("sudo", Severity.FAIL, Command(("id", "-u"), elevated=True, deadline=PROBE_DEADLINE))

# What the transport and its callers run: install (hardening), tar (backup),
# setsid and nohup (the detached wrapper) and flock (the file lock).
BASE_TOOLS = Check(
    "base-tools",
    Severity.FAIL,
    """\
agw_missing=
for agw_tool in install flock nohup setsid tar; do
  command -v "$agw_tool" >/dev/null 2>&1 || agw_missing="$agw_missing $agw_tool"
done
[ -z "$agw_missing" ] || problem base-tools "${agw_missing# }"
""",
)

# One line per account, so a long list never overflows one detail. An empty
# passwd shell is /bin/sh, as login and sshd read it; an account with no
# passwd entry is reported like any other.
LOGIN_SHELLS = Check(
    "login-shells",
    Severity.WARN,
    f"""\
set -f
for agw_account in ${ACCOUNTS_VARIABLE}; do
  if agw_entry=$(getent passwd "$agw_account"); then
    agw_shell=${{agw_entry##*:}}
    agw_shell=${{agw_shell:-/bin/sh}}
    case ${{agw_shell##*/}} in sh|dash|bash|zsh) continue ;; esac
  fi
  problem login-shells "$agw_account"
done
set +f
""",
)

# agw-launch rebuilds every command's environment, so pam_env settings never
# reach it. A file counts when it holds anything but comments and blank lines.
# The admin's ~/.pam_environment counts only where an uncommented line of the
# sshd PAM stack loads pam_env.so with user_readenv=1 (Debian 12 does, 13 does
# not); only that file is read, not what it includes. /etc/default/locale is
# left out on purpose: every VM sets it and agw-launch sets the locale itself.
PAM_ENV = Check(
    "pam-env",
    Severity.WARN,
    r"""agw_pam_files=
agw_has_settings() { [ -r "$2" ] && grep -qEv '^[[:space:]]*(#|$)' "$2" && agw_pam_files="$agw_pam_files $1"; }
agw_has_settings /etc/environment /etc/environment
agw_has_settings /etc/security/pam_env.conf /etc/security/pam_env.conf
grep -qsE '^[^#]*pam_env\.so[^#]*user_readenv=1' /etc/pam.d/sshd &&
  agw_has_settings '~/.pam_environment' "$HOME/.pam_environment"
[ -z "$agw_pam_files" ] || problem pam-env "${agw_pam_files# }"
""",
)

CHECKS = (RELEASE, SYSTEMD, SUDO, BASE_TOOLS, LOGIN_SHELLS, PAM_ENV)
"""The contract, in report order."""

SCRIPT_ROWS = tuple(check for check in CHECKS if isinstance(check.probe, str))

LINE_PREFIX = "AGW-CHECK "
END_LINE = "AGW-CHECK-END"


def _fragment(check: Check) -> str:
    assert isinstance(check.probe, str)
    return check.probe


BASE_SCRIPT = (
    'problem() { printf \'AGW-CHECK %s %s\\n\' "$1" "$2"; }\n'
    + "".join(_fragment(check) + "\n" for check in SCRIPT_ROWS)
    + f"printf '{END_LINE}\\n'\n"
)
"""The one unelevated script: a line for each problem, then one closing line. It never exits non-zero."""

BASE_SCRIPT_INCOMPLETE = Check("base-script", Severity.FAIL, BASE_SCRIPT)
"""Not a row: the one failure reported, instead of any script row, when the script did not run to its end."""

DETAIL = re.compile(r"[A-Za-z0-9_ ./:~$-]{0,80}")
"""What a detail from the guest may hold to be shown."""

DETAIL_NOT_SHOWN = "(detail not shown)"

_SUDO_REFUSED = (NotDelivered(NotDeliveredReason.LAUNCHER_FAILED), NotDelivered(NotDeliveredReason.PYTHON_MISSING))
"""An elevated request's ``sudo -n`` that cannot run as root: it exits 1 (no passwordless sudo) or 127 (no sudo)."""


@dataclass(frozen=True)
class Finding:
    """A problem one check found. ``details`` holds one entry per thing it named, in the order reported."""

    check: Check
    details: tuple[str, ...]


@dataclass(frozen=True)
class BaseReport:
    """The release and every finding of the base checks, with the accounts the checks covered."""

    release: DebianRelease
    findings: tuple[Finding, ...]
    accounts: Mapping[str, Identity]


def observe_release(access: VMAccess, *, expected: DebianRelease | None) -> DebianRelease:
    """The VM's live Debian release, read over ``access``; ``expected`` refuses any other.

    The one observation function: the contract's first request, and what
    ``vm confirm-release`` runs alone. An unrecognized or mismatched release
    raises its ``StateError``, and nothing else is asked of the VM.
    """
    assert isinstance(RELEASE.probe, Command)
    result = access.run(RELEASE.probe, check=True)
    return verify_os_release(result.stdout.decode("utf-8", errors="replace"), expected=expected)


def check_base(
    access: VMAccess, *, expected_release: DebianRelease | None, admin: str, agents: Sequence[Agent]
) -> BaseReport:
    """Run the base checks, release first, and report every finding.

    ``admin`` and ``agents`` are the accounts whose login shells are checked:
    the admin's Linux user name and each agent on the VM.
    """
    release = observe_release(access, expected=expected_release)
    accounts: dict[str, Identity] = {admin: Admin(), **{agent.linux_user: agent for agent in agents}}
    script = Script(BASE_SCRIPT, Shell.SH, env={ACCOUNTS_VARIABLE: " ".join(accounts)}, deadline=PROBE_DEADLINE)
    result = access.run(script)
    status = result.require_completed(access.vm_name)
    findings = [*parse_script_output(result.stdout, status=status, accounts=accounts.keys())]
    sudo = _sudo_finding(access)
    if sudo is not None:
        findings.append(sudo)
    order = (BASE_SCRIPT_INCOMPLETE, *CHECKS)
    findings.sort(key=lambda finding: order.index(finding.check))
    return BaseReport(release, tuple(findings), accounts)


def parse_script_output(stdout: bytes, *, status: int, accounts: Collection[str]) -> tuple[Finding, ...]:
    """The findings in the base script's output, in table order.

    Boundary: the output comes from the guest. Only a line for a
    script-probed row counts, and repeated lines for a row aggregate into one
    finding. A detail outside :data:`DETAIL` still reports its row, as
    :data:`DETAIL_NOT_SHOWN`, so a malformed line never hides a problem. A
    ``login-shells`` detail must be one of ``accounts``, so a name the guest
    invents is dropped. Without the closing line the probe did not complete:
    the one finding is :data:`BASE_SCRIPT_INCOMPLETE`, naming the exit status
    and no stream text.
    """
    lines = [line.removesuffix("\r") for line in stdout.decode("utf-8", errors="replace").split("\n")]
    if END_LINE not in lines:
        return (Finding(BASE_SCRIPT_INCOMPLETE, (f"exit status {status}",)),)
    rows = {check.id: check for check in SCRIPT_ROWS}
    reported: dict[str, list[str]] = {}
    for line in lines:
        if not line.startswith(LINE_PREFIX):
            continue
        check_id, _, detail = line.removeprefix(LINE_PREFIX).partition(" ")
        if check_id not in rows:
            continue
        if not DETAIL.fullmatch(detail):
            detail = DETAIL_NOT_SHOWN
        elif check_id == LOGIN_SHELLS.id and detail not in accounts:
            continue
        details = reported.setdefault(check_id, [])
        if detail not in details:
            details.append(detail)
    return tuple(Finding(check, tuple(reported[check.id])) for check in SCRIPT_ROWS if check.id in reported)


def _sudo_finding(access: VMAccess) -> Finding | None:
    """The ``sudo`` row from its elevated request, mapped here rather than by ``require_completed``.

    ``sudo -n`` that cannot run as root leaves no start marker, which reads
    as a launcher failure (no passwordless sudo) or a missing python3 (no
    ``sudo``, an elevated 127); both are this row's failure. Any other
    outcome that is not a completion raises its typed error.
    """
    assert isinstance(SUDO.probe, Command)
    result = access.run(SUDO.probe)
    completion = result.completion
    if not isinstance(completion, Completed) and completion not in _SUDO_REFUSED:
        result.require_completed(access.vm_name)
    if completion == Completed(0) and result.stdout.strip() == b"0":
        return None
    return Finding(SUDO, ("sudo -n did not run as root",))


def enforce(report: BaseReport, vm_name: str) -> None:
    """Warn for each warning and raise one ``StateError`` listing every failure.

    The only function here that prints. A failure names recreation, except a
    base script that did not complete, which points at the operation log and
    ``agw vm verify-connection`` instead.
    """
    failures: list[Finding] = []
    for finding in report.findings:
        if finding.check.severity is Severity.WARN:
            for warning in _warnings(finding, report, vm_name):
                output.warn(warning)
        else:
            failures.append(finding)
    if not failures:
        return
    listed = "; ".join(f"{finding.check.id}: {', '.join(finding.details)}" for finding in failures)
    hints = []
    if any(finding.check is BASE_SCRIPT_INCOMPLETE for finding in failures):
        hints.append(
            f"The checks did not run to their end; see the operation log, and run 'agw vm verify-connection {vm_name}'."
        )
    if any(finding.check is not BASE_SCRIPT_INCOMPLETE for finding in failures):
        hints.append(f"Initialization cannot repair this: run 'agw vm delete {vm_name}', then create the VM again.")
    raise StateError(
        f"VM '{vm_name}' does not meet the Agentworks VM contract: {listed}",
        entity_kind="vm",
        entity_name=vm_name,
        hint=" ".join(hints),
    )


def _warnings(finding: Finding, report: BaseReport, vm_name: str) -> Iterator[str]:
    if finding.check is LOGIN_SHELLS:
        for account in finding.details:
            yield _login_shell_warning(report.accounts.get(account), account, vm_name)
    elif finding.check is PAM_ENV:
        yield (
            f"VM '{vm_name}' has pam_env settings in {', '.join(_files(finding.details))}. "
            "Agentworks does not apply pam_env settings to the commands it runs."
        )
    else:
        yield f"VM '{vm_name}': {finding.check.id}: {', '.join(finding.details)}"


def _login_shell_warning(identity: Identity | None, account: str, vm_name: str) -> str:
    unsupported = "has a login shell Agentworks does not support (it supports sh, dash, bash and zsh)"
    if isinstance(identity, Agent):
        return (
            f"On VM '{vm_name}', agent '{identity.name}' (account '{account}') {unsupported}. "
            f"'agw agent reinit {identity.name}' sets it from the agent's template."
        )
    if isinstance(identity, Admin):
        return (
            f"On VM '{vm_name}', the admin account '{account}' {unsupported}. "
            f"'agw vm reinit {vm_name}' sets it from the admin template."
        )
    return f"On VM '{vm_name}', an account {unsupported}: {DETAIL_NOT_SHOWN}."


def _files(details: Sequence[str]) -> Iterator[str]:
    """Each file the ``pam-env`` details name: a detail holds several, space separated, unless it is not shown."""
    for detail in details:
        if detail == DETAIL_NOT_SHOWN:
            yield detail
        else:
            yield from detail.split()
