"""tailscaled cold-boot DNS race fix.

On cold boot, ``tailscaled`` can start before ``systemd-resolved`` has
registered its D-Bus interface. Its DNS-manager probe then fails to
reach resolved, falls through to ``resolvconf``/openresolv detection
(satisfied by resolved's own compat shim at ``/usr/sbin/resolvconf``),
selects direct mode, and rewrites ``/etc/resolv.conf`` as a regular
file pointing at ``100.100.100.100`` with no upstream. The result is
SERVFAIL for everything non-tailnet. The state is self-perpetuating:
subsequent ``systemctl restart tailscaled`` keeps re-detecting direct
mode until the stub symlink is restored. See GitHub issue #117 for the
full root-cause analysis.

This module applies three idempotent steps at init time:

1. ``systemctl enable --now systemd-resolved`` -- belt and suspenders.
2. Restore ``/etc/resolv.conf`` to the resolved stub symlink. This
   breaks the latch on any VM that is currently stuck in direct mode.
3. Drop a ``tailscaled.service.d/10-after-resolved.conf`` override that
   adds ``After=`` and ``Wants=systemd-resolved.service``. Step 3 is
   the actual race fix; steps 1 and 2 recover already-broken VMs.

Deliberately NOT ``Requires=`` -- a resolved failure should degrade
DNS, not take tailscaled (and thus our SSH transport) offline.

Deliberately does NOT restart tailscaled. Phase B runs over the tailnet,
so restarting tailscaled would disconnect us mid-init. The drop-in
takes effect on the next cold boot, which is exactly when the race
fires.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.ssh import ExecTarget, SSHLogger


TAILSCALED_DROPIN_DIR = "/etc/systemd/system/tailscaled.service.d"
TAILSCALED_DROPIN_PATH = f"{TAILSCALED_DROPIN_DIR}/10-after-resolved.conf"
TAILSCALED_DROPIN_CONTENT = """\
# Managed by agentworks. Do not edit; rewritten on vm reinit.
# Source: GitHub issue #117 -- orders tailscaled after systemd-resolved
# so its DNS-manager probe finds the resolved D-Bus interface and
# picks systemd-resolved mode instead of falling back to direct mode.
# Wants= (not Requires=) keeps the VM reachable if resolved ever fails.
[Unit]
After=systemd-resolved.service
Wants=systemd-resolved.service
"""

RESOLV_CONF_PATH = "/etc/resolv.conf"
RESOLVED_STUB_PATH = "/run/systemd/resolve/stub-resolv.conf"


def apply_tailscaled_dns_fix(target: ExecTarget, logger: SSHLogger) -> None:
    """Apply the tailscaled cold-boot DNS race fix.

    Idempotent: a second run is a no-op unless on-disk state differs.
    Called at vm create and re-applied at vm reinit. Non-fatal: each
    step's failure warns and continues (matches the rest of phase B).
    """
    logger.step("Tailscale DNS")
    try:
        _ensure_systemd_resolved_enabled(target, logger)
    except SSHError as e:
        msg = f"systemd-resolved enable failed: {e}"
        logger.warning(msg)
        output.warn(msg)
    try:
        _ensure_resolv_conf_symlink(target, logger)
    except SSHError as e:
        msg = f"{RESOLV_CONF_PATH} symlink repair failed: {e}"
        logger.warning(msg)
        output.warn(msg)
    try:
        _ensure_tailscaled_dropin(target, logger)
    except SSHError as e:
        msg = f"tailscaled drop-in install failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _ensure_systemd_resolved_enabled(target: ExecTarget, logger: SSHLogger) -> None:
    """Ensure systemd-resolved is enabled and running.

    ``systemctl enable --now`` is itself idempotent (no-op if already
    enabled and active), so no pre-check is needed.
    """
    target.run("systemctl enable --now systemd-resolved", sudo=True)


def _ensure_resolv_conf_symlink(target: ExecTarget, logger: SSHLogger) -> None:
    """Ensure ``/etc/resolv.conf`` is the resolved stub symlink.

    Restoring this symlink is the only thing that breaks tailscaled out
    of the latched direct-mode state where it has rewritten resolv.conf
    as a regular file. ``ln -sf`` is atomic via rename, so racing
    readers see either the old or new target, never a missing file.
    """
    current = target.run(f"readlink {RESOLV_CONF_PATH}", check=False)
    if getattr(current, "ok", False) and getattr(current, "stdout", "").strip() == RESOLVED_STUB_PATH:
        output.detail(f"{RESOLV_CONF_PATH} already points at resolved stub; no change.")
        return

    output.detail(f"Restoring {RESOLV_CONF_PATH} symlink to resolved stub...")
    target.run(
        f"ln -sf {shlex.quote(RESOLVED_STUB_PATH)} {RESOLV_CONF_PATH}",
        sudo=True,
    )


def _ensure_tailscaled_dropin(target: ExecTarget, logger: SSHLogger) -> None:
    """Install/refresh the tailscaled After=resolved drop-in.

    Writes only when content differs; runs ``daemon-reload`` only when
    the file was actually rewritten.
    """
    existing = target.run(f"cat {TAILSCALED_DROPIN_PATH}", sudo=True, check=False)
    if (
        getattr(existing, "ok", False)
        and getattr(existing, "stdout", "") == TAILSCALED_DROPIN_CONTENT
    ):
        output.detail("tailscaled drop-in already installed; no change.")
        return

    output.detail("Installing tailscaled After=resolved drop-in...")
    target.run(
        f"install -d -m 0755 -o root -g root {TAILSCALED_DROPIN_DIR}",
        sudo=True,
    )
    mktemp_result = target.run("mktemp --tmpdir agw-tsdns.XXXXXX")
    staging = (getattr(mktemp_result, "stdout", "") or "").strip()
    if not staging:
        raise SSHError("mktemp produced empty path for tailscaled drop-in staging")
    try:
        target.write_file(staging, TAILSCALED_DROPIN_CONTENT)
        target.run(
            f"install -m 0644 -o root -g root {shlex.quote(staging)} {TAILSCALED_DROPIN_PATH}",
            sudo=True,
        )
    finally:
        target.run(f"rm -f {shlex.quote(staging)}", check=False)
    target.run("systemctl daemon-reload", sudo=True)
