"""tailscaled cold-boot startup ordering, so DNS detection finds a resolver.

When tailscaled starts before the DNS layer is up, its DNS-manager
probe fails, it falls back to direct mode, and rewrites
``/etc/resolv.conf`` as a regular file pointing at ``100.100.100.100``
with no upstream resolver. The result is SERVFAIL for everything
non-tailnet. The state is self-perpetuating: subsequent
``systemctl restart tailscaled`` keeps re-detecting direct mode. See
GitHub issue #117 for the full root-cause analysis.

The fix is a single idempotent step: drop in a
``tailscaled.service.d`` override that orders tailscaled after
``network-online.target`` (the canonical systemd signal that networking
is up, including DNS) and ``nss-lookup.target`` (the passive sync
point that NSS-providing resolvers declare ``Before=``). With this
ordering, by the time tailscaled starts, whichever DNS manager the VM
uses -- systemd-resolved, NetworkManager, dhcpcd-resolvconf, anything
else -- is up and detectable, so tailscaled picks the right mode
instead of falling back to direct.

Deliberately ``Wants=``, not ``Requires=``: a ``network-online``
failure should let tailscaled fall back to its (broken-but-recoverable)
default behavior rather than block the unit and risk taking the VM
off the tailnet entirely.

Deliberately does NOT restart tailscaled. Phase B runs over the tailnet,
so restarting tailscaled would disconnect us mid-init. The drop-in
takes effect on the next cold boot, which is exactly when the race
fires.

Recovery of VMs already latched in the broken state (resolv.conf is a
regular file, tailscaled keeps re-detecting direct mode on restart) is
left to the operator: clear the file once, reboot, and the new ordering
prevents recurrence.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.ssh import ExecTarget, SSHLogger


TAILSCALED_DROPIN_DIR = "/etc/systemd/system/tailscaled.service.d"
TAILSCALED_DROPIN_PATH = f"{TAILSCALED_DROPIN_DIR}/10-after-network-online.conf"
TAILSCALED_DROPIN_CONTENT = """\
# Managed by agentworks. Do not edit; rewritten on vm reinit.
# Source: GitHub issue #117. Ensures a DNS resolver is in place before
# tailscaled probes for one. Without this, tailscaled can mis-detect
# direct mode on cold boot and rewrite /etc/resolv.conf with no upstream
# resolver. Ordering against network-online.target (the canonical
# "network is up, DNS is configured" signal) catches whichever resolver
# manager is actually in use. nss-lookup.target adds passive ordering
# against NSS-providing resolvers that declare Before=nss-lookup.target.
# Wants= (not Requires=) lets tailscaled still come up if network-online
# never fires.
[Unit]
After=network-online.target nss-lookup.target
Wants=network-online.target
"""


def apply_tailscaled_dns_fix(target: ExecTarget, logger: SSHLogger) -> None:
    """Apply the tailscaled cold-boot DNS race fix.

    Idempotent: a second run is a no-op unless on-disk content differs.
    Called at vm create and re-applied at vm reinit. Non-fatal: failure
    warns and continues (matches the rest of phase B).
    """
    logger.step("Tailscale DNS")
    try:
        _ensure_tailscaled_dropin(target, logger)
    except SSHError as e:
        msg = f"tailscaled drop-in install failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _ensure_tailscaled_dropin(target: ExecTarget, logger: SSHLogger) -> None:
    """Install/refresh the tailscaled startup-ordering drop-in.

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

    output.detail("Installing tailscaled startup-ordering drop-in...")
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
