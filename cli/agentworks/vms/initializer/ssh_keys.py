"""SSH authorized_keys reconciliation, host-key preservation, and the
Apple-vz SVE trap mask."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.ssh import SSHError, SSHLogger

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.transports import Transport

AUTHORIZED_KEYS_HEADER = """\
# Managed by agentworks -- manual edits will be overwritten on reinit.
# To add keys, use operator.extra_ssh_public_keys in your agentworks config.
"""


def _reconcile_authorized_keys(
    target: Transport,
    config: Config,
    home: str,
    logger: SSHLogger,
    *,
    owner: str | None = None,
) -> None:
    """Reconcile <home>/.ssh/authorized_keys with the configured key set.

    Writes the primary ssh_public_key plus any extra_ssh_public_keys from
    config. Full overwrite so that removed keys are cleaned up on reinit.

    When ``owner`` is None (default), writes directly via the connected SSH
    user (admin writing to admin's home). Failure is downgraded to a warning
    because the operator can recover on the next ``vm reinit``.

    When ``owner`` is set to a Linux username different from the SSH user
    (e.g. an agent's username), uses a stage-and-install path: ensures
    ``<home>/.ssh`` exists with correct ownership, scp's the file content
    to a private mktemp path with 0600 perms, then ``sudo install``s the
    staged file atomically into place with the requested owner / group /
    mode. The staging file is removed in a ``finally`` block so a partial
    failure doesn't leak it. Failure on this path RAISES (``SSHError``):
    the call is load-bearing for whether the operator can SSH to the agent
    at all, so a silent failure here would leave the caller running
    downstream commands that all fail with a cryptic ``exit 255``.
    """
    logger.step("SSH authorized keys")

    keys: list[str] = [config.operator.ssh_public_key.read_text().strip()]
    for path in config.operator.extra_ssh_public_keys:
        keys.append(path.read_text().strip())

    extra_count = len(keys) - 1
    label = f"1 primary + {extra_count} extra" if extra_count else "1 primary"
    if owner is not None:
        label = f"{label} for {owner}"
    output.info(f"Reconciling authorized_keys ({label})...")

    content = AUTHORIZED_KEYS_HEADER + "\n".join(keys) + "\n"

    if owner is None:
        # Direct-write: the SSH user writes to its own home.
        try:
            target.write_file(f"{home}/.ssh/authorized_keys", content, mode="600")
        except SSHError as e:
            msg = f"authorized_keys reconciliation failed: {e}"
            logger.warning(msg)
            output.warn(msg)
        return

    # Stage-and-install: admin writes for a non-self uid (agent).
    quoted_owner = shlex.quote(owner)
    # Ensure <home>/.ssh exists with correct ownership/mode.
    # `useradd -m` doesn't create .ssh (not in /etc/skel), and install -d
    # is idempotent (creates if missing; sets owner/mode either way).
    target.run(
        f"install -d -o {quoted_owner} -g {quoted_owner} -m 0700 {home}/.ssh",
        sudo=True,
    )
    mktemp_result = target.run("mktemp --tmpdir agw-ak.XXXXXX")
    staging = (getattr(mktemp_result, "stdout", "") or "").strip()
    if not staging:
        raise SSHError("mktemp produced empty path")
    try:
        # Restrict the staging file before content lands; mktemp's
        # randomized suffix plus 0600 perms keep the contents private
        # between admin's write and the atomic install.
        target.write_file(staging, content, mode="0600")
        target.run(
            f"install -o {quoted_owner} -g {quoted_owner} -m 0600 {shlex.quote(staging)} {home}/.ssh/authorized_keys",
            sudo=True,
        )
    finally:
        target.run(f"rm -f {shlex.quote(staging)}", check=False)


def _preserve_ssh_host_keys(
    target: Transport,
    logger: SSHLogger,
) -> None:
    """Stop cloud-init from regenerating SSH host keys on stop/start.

    Writes the cloud-init drop-in that pins existing host keys. This also runs
    during Phase A bootstrap, but reconciling it here means VMs provisioned
    before the drop-in existed get repaired on ``vm reinit`` -- otherwise their
    host key changes on the next reboot and SSH fails with a changed-host-key
    error until the operator clears known_hosts by hand.

    Inert on platforms without cloud-init (e.g. WSL2): the file is simply never
    read. Written unconditionally to keep the step platform-agnostic, matching
    the Phase A bootstrap step.
    """
    from pathlib import PurePosixPath

    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SSH_PRESERVE_KEYS_LINES,
        SSH_PRESERVE_KEYS_PATH,
    )

    logger.step("Preserve SSH host keys")
    output.info("Ensuring SSH host key preservation...")

    parent = str(PurePosixPath(SSH_PRESERVE_KEYS_PATH).parent)
    printf_args = " ".join(shlex.quote(line) for line in SSH_PRESERVE_KEYS_LINES)
    try:
        target.run(
            f"mkdir -p {shlex.quote(parent)} && printf '%s\\n' {printf_args} > {shlex.quote(SSH_PRESERVE_KEYS_PATH)}",
            sudo=True,
        )
    except SSHError as e:
        msg = f"SSH host key preservation failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _apply_sve_mask(target: Transport, logger: SSHLogger) -> None:
    """Repair the Apple-vz SVE trap on VMs provisioned before the mask existed.

    Apple's Virtualization.framework advertises SVE the guest cannot execute,
    so the first SVE instruction traps as SIGILL, surfacing in OpenSSL and thus
    apt-over-https, git, and Python cryptography. Phase A masks this at create
    via an ``arm64.nosve`` grub drop-in; this reconcile step installs the same
    drop-in on an already-running VM so ``vm reinit`` repairs one provisioned
    before the mask existed. The drop-in path and content are shared with the
    Phase A step so the two writers cannot drift.

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
    from pathlib import PurePosixPath

    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SVE_APPLE_VZ_GREP,
        SVE_CPUINFO_GREP,
        SVE_NOSVE_GRUB_LINES,
        SVE_NOSVE_GRUB_PATH,
    )

    logger.step("Mask SVE")

    # Gate: an Apple Virtualization guest still advertising SVE. Both greps
    # read /sys and /proc (no sudo); either one failing (non-Apple host, or a
    # VM already masked and rebooted) makes this a silent no-op.
    gate = target.run(f"{SVE_APPLE_VZ_GREP} && {SVE_CPUINFO_GREP}", check=False)
    if not gate.ok:
        return

    output.info("Masking unusable SVE (arm64.nosve)...")
    parent = str(PurePosixPath(SVE_NOSVE_GRUB_PATH).parent)
    printf_args = " ".join(shlex.quote(line) for line in SVE_NOSVE_GRUB_LINES)
    try:
        target.run(
            f"mkdir -p {shlex.quote(parent)} && printf '%s\\n' {printf_args} > {shlex.quote(SVE_NOSVE_GRUB_PATH)}",
            sudo=True,
        )
        update = target.run("update-grub", sudo=True, check=False)
        if not update.ok:
            msg = "SVE mask: update-grub failed; VM may crash on SVE (SIGILL)."
            logger.warning(msg)
            output.warn(msg)
            return
    except SSHError as e:
        msg = f"SVE mask failed: {e}"
        logger.warning(msg)
        output.warn(msg)
        return

    # The drop-in is installed but the running kernel picked it up only if
    # arm64.nosve is already on the cmdline (i.e. the VM was rebooted since).
    # We cannot reboot from here without dropping the tailnet mid-init, so tell
    # the operator; a restart plus one more reinit converges the VM.
    active = target.run("grep -qw arm64.nosve /proc/cmdline", check=False)
    if not active.ok:
        output.warn(
            "Masked unusable SVE (arm64.nosve). Restart the VM and reinit "
            "again to apply it; until then, OpenSSL/git/apt may crash (SIGILL)."
        )
