"""The reinit alert for pam_env settings, which Agentworks does not apply.

``agw-launch`` rebuilds the environment of every command it runs, so values set
through pam_env never reach those commands. Agentworks does not support them;
this module only tells the operator when a VM has some.

``/etc/default/locale`` is left out on purpose: Debian's sshd PAM stack reads
it, every VM has it set, and ``agw-launch`` sets the locale itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output

if TYPE_CHECKING:
    from agentworks.transports import ExecTransport

PAM_ENV_FILES = ("/etc/environment", "/etc/security/pam_env.conf", "~/.pam_environment")
"""The files pam_env reads, as the operator is told about them (``~`` is the admin's home)."""

# One line per file that holds anything other than comments and blank lines.
# It always exits 0: an absent or unreadable file simply prints nothing.
PAM_ENV_PROBE = """\
agw_has_settings() { [ -r "$2" ] && grep -qEv '^[[:space:]]*(#|$)' "$2" && printf '%s\\n' "$1"; }
agw_has_settings /etc/environment /etc/environment
agw_has_settings /etc/security/pam_env.conf /etc/security/pam_env.conf
agw_has_settings '~/.pam_environment' "$HOME/.pam_environment"
true
"""


def parse_pam_env_probe(stdout: str) -> tuple[str, ...]:
    """The files the probe reported, in ``PAM_ENV_FILES`` order.

    The output comes from the guest, so only the known names are accepted.
    """
    reported = {line.strip() for line in stdout.splitlines()}
    return tuple(file for file in PAM_ENV_FILES if file in reported)


def warn_pam_env_settings(target: ExecTransport, vm_name: str) -> None:
    """Warn when the VM holds pam_env settings, or that they could not be checked.

    The probe always ends in ``true``, so a non-zero status means it did not run
    to its end (an admin login shell that cannot parse it, say). That only
    warns. A connection failure raises: reinit runs this on the connection it
    just used for the release probe.
    """
    result = target.run(PAM_ENV_PROBE, check=False)
    if result.returncode != 0:
        output.warn(
            f"Could not check VM '{vm_name}' for pam_env settings: the check exited with status {result.returncode}."
        )
        return
    files = parse_pam_env_probe(result.stdout)
    if files:
        output.warn(
            f"VM '{vm_name}' has pam_env settings in {', '.join(files)}. "
            "Agentworks does not apply pam_env settings to the commands it runs."
        )
