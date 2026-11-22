"""SSH authorized_keys reconciliation, host-key preservation, and the
Apple-vz SVE trap mask."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution.requests import Command, Script, Shell
from agentworks.vms.home_layout import SSH_DIR

from .steps import STEP_FAILURES, status, warn, warn_step

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.execution.files import Files
    from agentworks.ssh_identity import SSHIdentity

    from .steps import InitLog

AUTHORIZED_KEYS_HEADER = """\
# Managed by agentworks -- manual edits will be overwritten on reinit.
# To add keys, use operator.extra_ssh_public_keys in your agentworks config.
"""


@dataclass(frozen=True, slots=True)
class AuthorizedKeysApplied:
    """The configured identity was accepted by the remote key write."""

    identity: SSHIdentity
    private_key_ref: str


@dataclass(frozen=True, slots=True)
class AuthorizedKeysUnproven:
    """The admin key write could not prove its remote outcome."""


AuthorizedKeysOutcome = AuthorizedKeysApplied | AuthorizedKeysUnproven


def authorized_keys_content(public_text: str, extra_keys: Sequence[Path]) -> str:
    """A managed ``authorized_keys`` file: the header, the configured key, then each extra key file's content.

    Reinit and ``agw vm recover`` both write exactly this, so a key it does
    not list (one the configured key replaced, or a manual edit) is removed.
    """
    keys = [public_text, *(path.read_text().strip() for path in extra_keys)]
    return AUTHORIZED_KEYS_HEADER + "\n".join(keys) + "\n"


def _reconcile_authorized_keys(
    files: Files,
    config: Config,
    home: str,
    log: InitLog,
    *,
    owner: str | None = None,
) -> AuthorizedKeysOutcome:
    """Reconcile ``<home>/.ssh/authorized_keys`` with the configured key set, through the file program.

    Writes the primary ssh_public_key plus any extra_ssh_public_keys from
    config. Full overwrite so that removed keys are cleaned up on reinit.

    When ``owner`` is None (default), ``files`` is the account's own and the
    file is written as it, 0600 under a 0700 ``.ssh``. Any step failure is
    downgraded to a warning and an unproven outcome, because the operator
    can recover on the next ``vm reinit``.

    When ``owner`` is set, ``files`` is the admin's and the work is root's:
    ``.ssh`` is placed with ``ensure_directory`` and the file with ``write``,
    each with the owner, its group and its mode set through descriptors
    opened without following a link, before the file is published. A link
    at either is refused. Failure on this path RAISES: the call is
    load-bearing for whether the operator can SSH to the agent at all, so a
    silent failure here would leave the caller running downstream requests
    that all fail the same way.
    """
    from agentworks.vms.applied_state import prepare_configured_ssh_identity

    prepared = prepare_configured_ssh_identity(
        config.operator.ssh_public_key,
        config.operator.ssh_private_key,
    )

    log.step("SSH authorized keys")

    content = authorized_keys_content(prepared.public_text, config.operator.extra_ssh_public_keys).encode()

    extra_count = len(config.operator.extra_ssh_public_keys)
    label = f"1 primary + {extra_count} extra" if extra_count else "1 primary"
    if owner is not None:
        label = f"{label} for {owner}"
    output.info(f"Reconciling authorized_keys ({label})...")

    path = f"{home}/{SSH_DIR}/authorized_keys"
    if owner is None:
        try:
            files.write(path, content, mode=0o600, parent_mode=0o700)
        except STEP_FAILURES as error:
            # The error's own message only: ``write`` logs its streams, and this warning shows none of them.
            warn(log, f"authorized_keys reconciliation failed: {log.sanitize(str(error))}")
            return AuthorizedKeysUnproven()
        return AuthorizedKeysApplied(prepared.identity, prepared.private_key_ref)

    # `useradd -m` creates no .ssh (it is not in /etc/skel).
    files.ensure_directory(f"{home}/{SSH_DIR}", mode=0o700, owner=owner, group=owner, elevated=True)
    files.write(path, content, mode=0o600, parent_mode=0o700, owner=owner, group=owner, elevated=True)
    return AuthorizedKeysApplied(prepared.identity, prepared.private_key_ref)


def _preserve_ssh_host_keys(files: Files, log: InitLog) -> None:
    """Stop cloud-init from regenerating SSH host keys on stop/start.

    Writes the cloud-init drop-in that pins existing host keys. This also runs
    during create-time bootstrap, but reconciling it here means VMs provisioned
    before the drop-in existed get repaired on ``vm reinit``: otherwise their
    host key changes on the next reboot and no longer matches the key recorded
    in the VM's host-key store, so every connection is refused.

    Inert on platforms without cloud-init (e.g. WSL2): the file is simply never
    read. Written unconditionally to keep the step platform-agnostic, matching
    the create-time bootstrap step.
    """
    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SSH_PRESERVE_KEYS_CONTENT,
        SSH_PRESERVE_KEYS_PATH,
    )

    log.step("Preserve SSH host keys")
    output.info("Ensuring SSH host key preservation...")
    try:
        files.write(
            SSH_PRESERVE_KEYS_PATH, SSH_PRESERVE_KEYS_CONTENT.encode(), mode=0o644, parent_mode=0o755, elevated=True
        )
    except STEP_FAILURES as error:
        warn_step(log, "SSH host key preservation failed", error)


def _apply_sve_mask(files: Files, log: InitLog) -> None:
    """Repair the Apple-vz SVE trap on VMs provisioned before the mask existed.

    Apple's Virtualization.framework advertises SVE the guest cannot execute,
    so the first SVE instruction traps as SIGILL, surfacing in OpenSSL and thus
    apt-over-https, git, and Python cryptography. Platform-owned create-time
    bootstrap masks this via an ``arm64.nosve`` grub drop-in; this reconcile
    step installs the same drop-in on an already-running VM so ``vm reinit``
    repairs one provisioned before the mask existed. The drop-in path and
    content are shared with the create-time bootstrap step so the two writers
    cannot drift.

    Gated to Apple Virtualization guests that still advertise SVE, so it is a
    silent no-op everywhere else and on VMs already masked (``arm64.nosve``
    strips SVE from the HWCAP, closing the gate). Non-fatal: failure warns and
    continues, matching the rest of Phase B.

    The mask takes effect only after a reboot, and Phase B runs over the
    tailnet, so this does not restart the VM: doing so would drop us mid-init,
    and the remaining crypto-dependent steps would still run on the unmasked
    kernel. It installs the fix and tells the operator to restart, then reinit
    again (matching ``apply_tailscaled_dns_fix``, which likewise defers to the
    next boot).
    """
    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SVE_APPLE_VZ_GREP,
        SVE_CPUINFO_GREP,
        SVE_NOSVE_GRUB_CONTENT,
        SVE_NOSVE_GRUB_PATH,
    )

    log.step("Mask SVE")
    try:
        # Gate: an Apple Virtualization guest still advertising SVE. Both greps
        # read /sys and /proc unelevated; either one failing (non-Apple host, or
        # a VM already masked and rebooted) makes this a silent no-op.
        if status(
            files.access, Script(f"{SVE_APPLE_VZ_GREP} && {SVE_CPUINFO_GREP}", Shell.SH, deadline=files.deadline)
        ):
            return
        output.info("Masking unusable SVE (arm64.nosve)...")
        files.write(SVE_NOSVE_GRUB_PATH, SVE_NOSVE_GRUB_CONTENT.encode(), mode=0o644, parent_mode=0o755, elevated=True)
        if status(files.access, Command(("update-grub",), elevated=True, deadline=files.deadline)):
            msg = "SVE mask: update-grub failed; VM may crash on SVE (SIGILL)."
            warn(log, msg)
            return
        # The drop-in is installed but the running kernel picked it up only if
        # arm64.nosve is already on the cmdline (i.e. the VM was rebooted since).
        # We cannot reboot from here without dropping the tailnet mid-init, so tell
        # the operator; a restart plus one more reinit converges the VM.
        active = status(files.access, Command(("grep", "-qw", "arm64.nosve", "/proc/cmdline"), deadline=files.deadline))
    except STEP_FAILURES as error:
        warn_step(log, "SVE mask failed", error)
        return
    if active:
        output.warn(
            "Masked unusable SVE (arm64.nosve). Restart the VM and reinit "
            "again to apply it; until then, OpenSSL/git/apt may crash (SIGILL)."
        )
