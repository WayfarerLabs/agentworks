"""The VM contract: what every managed VM must satisfy before core relies on it.

The release comes first, as its own request; then one table of checks, each
an id, a severity and a probe (its fragment of one base script, or its own
request). :func:`check_base` runs them over a :class:`VMAccess` and returns a
:class:`BaseReport`;
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
"""

from __future__ import annotations

import re
import shlex
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
class ScriptCheck:
    """A row probed by its fragment of the base script.

    A fragment reports a problem by calling ``problem ID DETAIL`` and never
    exits.
    """

    id: str
    severity: Severity
    fragment: str


@dataclass(frozen=True)
class RequestCheck:
    """A row probed by a request of its own."""

    id: str
    severity: Severity
    request: Command


Check = ScriptCheck | RequestCheck


PROBE_DEADLINE = 60
"""Seconds each contract request may take: a hung VM must not hang create or reinit."""

ACCOUNTS_VARIABLE = "AGW_CONTRACT_ACCOUNTS"
"""The script's environment variable naming the accounts to check, space separated: data, never source."""

RELEASE_PROBE = Command(("cat", "/etc/os-release"), deadline=PROBE_DEADLINE)
"""The first request, before any row: with no recognized Debian release the rows mean nothing."""

# sd_booted()'s own test.
SYSTEMD = ScriptCheck(
    "systemd", Severity.FAIL, '[ -d /run/systemd/system ] || problem systemd "the system was not booted with systemd"'
)

# What the transport and its callers run: install (hardening), tar (backup),
# setsid and nohup (the detached wrapper) and flock (the file lock).
BASE_TOOLS = ScriptCheck(
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

NO_PASSWD_ENTRY = "no-passwd-entry"

# One line per account, so a long list never overflows one detail: the account,
# followed by NO_PASSWD_ENTRY when it has none. An empty passwd shell is
# /bin/sh, as login and sshd read it.
LOGIN_SHELLS = ScriptCheck(
    "login-shells",
    Severity.WARN,
    f"""\
set -f
for agw_account in ${ACCOUNTS_VARIABLE}; do
  if agw_entry=$(getent passwd "$agw_account"); then
    agw_shell=${{agw_entry##*:}}
    agw_shell=${{agw_shell:-/bin/sh}}
    case ${{agw_shell##*/}} in sh|dash|bash|zsh) continue ;; esac
    problem login-shells "$agw_account"
  else
    problem login-shells "$agw_account {NO_PASSWD_ENTRY}"
  fi
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
PAM_ENV = ScriptCheck(
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

SUDO = RequestCheck("sudo", Severity.FAIL, Command(("id", "-u"), elevated=True, deadline=PROBE_DEADLINE))

SCRIPT_ROWS = (SYSTEMD, BASE_TOOLS, LOGIN_SHELLS, PAM_ENV)

LINE = re.compile(r"AGW-CHECK(\s+)(\S+)(.*)", re.DOTALL)
END_LINE = "AGW-CHECK-END"

BASE_SCRIPT = (
    'problem() { printf \'AGW-CHECK %s %s\\n\' "$1" "$2"; }\n'
    + "".join(check.fragment + "\n" for check in SCRIPT_ROWS)
    + f"printf '{END_LINE}\\n'\n"
)
"""The one unelevated script: a line for each problem, then one closing line. It never exits non-zero."""

DETAIL = re.compile(r"[A-Za-z0-9_ ./:~$-]{0,80}")
"""What a detail from the guest may hold to be shown."""

DETAIL_NOT_SHOWN = "(detail not shown)"

_SUDO_REFUSED = (NotDelivered(NotDeliveredReason.LAUNCHER_FAILED), NotDelivered(NotDeliveredReason.PYTHON_MISSING))
"""An elevated request's ``sudo -n`` that cannot run as root: it exits 1 (no passwordless sudo) or 127 (no sudo)."""


@dataclass(frozen=True)
class Finding:
    """A problem one row found. ``details`` holds one entry per thing it named, in the order reported."""

    check: Check
    details: tuple[str, ...]


@dataclass(frozen=True)
class ScriptIncomplete:
    """The base script did not run to its end: one failure in place of every script row, naming its exit status."""

    status: int


Problem = Finding | ScriptIncomplete


@dataclass(frozen=True)
class BaseReport:
    """The release and every problem the base checks found, with the accounts the checks covered."""

    release: DebianRelease
    findings: tuple[Problem, ...]
    accounts: Mapping[str, Identity]


def observe_release(access: VMAccess, *, expected: DebianRelease | None) -> DebianRelease:
    """The VM's live Debian release, read over ``access``; ``expected`` refuses any other.

    The one observation function: the contract's first request, and what
    ``vm confirm-release`` runs alone. An unrecognized or mismatched release
    raises its ``StateError``, and nothing else is asked of the VM.
    """
    result = access.run(RELEASE_PROBE, check=True)
    return verify_os_release(result.stdout.decode("utf-8", errors="replace"), expected=expected)


def check_base(
    access: VMAccess, *, expected_release: DebianRelease | None, admin: str, agents: Sequence[Agent]
) -> BaseReport:
    """Observe the release, then run the rows, and report every problem in table order.

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
    return BaseReport(release, tuple(findings), accounts)


def parse_script_output(stdout: bytes, *, status: int, accounts: Collection[str]) -> tuple[Problem, ...]:
    """The problems in the base script's output, in table order.

    Boundary: the output comes from the guest. A line counts only when its id
    is a script row's, and repeated lines for a row aggregate into one
    finding. A ``login-shells`` line counts only when its detail is one this
    parser expects, an account in ``accounts`` alone or followed by
    :data:`NO_PASSWD_ENTRY`; anything else is dropped, so the guest cannot
    invent an account. For the other rows, a line that is malformed (a
    separator other than one space, or a detail outside :data:`DETAIL`) still
    reports its row, as :data:`DETAIL_NOT_SHOWN`, so it never hides a problem.
    Without the closing line the script did not complete: the one problem is
    :class:`ScriptIncomplete`, naming the exit status and no stream text.
    """
    lines = [line.removesuffix("\r") for line in stdout.decode("utf-8", errors="replace").split("\n")]
    if END_LINE not in lines:
        return (ScriptIncomplete(status),)
    rows = {check.id: check for check in SCRIPT_ROWS}
    expected_accounts = {*accounts, *(f"{account} {NO_PASSWD_ENTRY}" for account in accounts)}
    reported: dict[str, list[str]] = {}
    for line in lines:
        match = LINE.fullmatch(line)
        if match is None or match[2] not in rows:
            continue
        separator, check_id, rest = match.groups()
        well_formed = separator == " " and (rest == "" or rest.startswith(" "))
        detail = rest.removeprefix(" ")
        if check_id == LOGIN_SHELLS.id:
            if not well_formed or detail not in expected_accounts:
                continue
        elif not well_formed or not DETAIL.fullmatch(detail):
            detail = DETAIL_NOT_SHOWN
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
    result = access.run(SUDO.request)
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
    failures: list[str] = []
    incomplete = False
    for problem in report.findings:
        if isinstance(problem, ScriptIncomplete):
            incomplete = True
            failures.append(f"the checks did not complete (exit status {problem.status})")
        elif problem.check.severity is Severity.WARN:
            for warning in _warnings(problem, report, vm_name):
                output.warn(warning)
        else:
            failures.append(f"{problem.check.id}: {', '.join(problem.details)}")
    if not failures:
        return
    hints = []
    if incomplete:
        hints.append(f"See the operation log, and run 'agw vm verify-connection {vm_name}'.")
    if len(failures) > int(incomplete):
        hints.append(f"Initialization cannot repair this: run 'agw vm delete {vm_name}', then create the VM again.")
    raise StateError(
        f"VM '{vm_name}' does not meet the Agentworks VM contract: {'; '.join(failures)}",
        entity_kind="vm",
        entity_name=vm_name,
        hint=" ".join(hints),
    )


def shell_fix_command(identity: Identity, vm_name: str) -> tuple[str, ...]:
    """The command that sets an account's login shell from its template: agent reinit, or vm reinit for the admin."""
    if isinstance(identity, Agent):
        return ("agw", "agent", "reinit", identity.name)
    return ("agw", "vm", "reinit", vm_name)


def _warnings(finding: Finding, report: BaseReport, vm_name: str) -> Iterator[str]:
    if finding.check is LOGIN_SHELLS:
        for detail in finding.details:
            account, _, no_entry = detail.partition(" ")
            yield _login_shell_warning(report.accounts[account], account, vm_name, missing=bool(no_entry))
        return
    assert finding.check is PAM_ENV, "every warning row has its own wording"
    yield (
        f"VM '{vm_name}' has pam_env settings in {', '.join(_files(finding.details))}. "
        "Agentworks does not apply pam_env settings to the commands it runs."
    )


def _login_shell_warning(identity: Identity, account: str, vm_name: str, *, missing: bool) -> str:
    who = (
        f"agent '{identity.name}' (account '{account}')"
        if isinstance(identity, Agent)
        else f"the admin account '{account}'"
    )
    problem = (
        "has no passwd entry"
        if missing
        else "has a login shell Agentworks does not support (it supports sh, dash, bash and zsh)"
    )
    if missing and not isinstance(identity, Agent):
        # The connection authenticated as the admin, so its entry exists but the guest's lookup failed.
        return f"On VM '{vm_name}', {who} {problem}."
    fix = shlex.join(shell_fix_command(identity, vm_name))
    return f"On VM '{vm_name}', {who} {problem}. '{fix}' sets it up from its template."


def _files(details: Sequence[str]) -> Iterator[str]:
    """Each file the ``pam-env`` details name: a detail holds several, space separated, unless it is not shown."""
    for detail in details:
        if detail == DETAIL_NOT_SHOWN:
            yield detail
        else:
            yield from detail.split()
