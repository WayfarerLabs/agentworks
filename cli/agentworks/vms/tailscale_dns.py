"""tailscaled cold-boot startup ordering + latched-state detection.

When tailscaled starts before the DNS layer is up, its DNS-manager
probe fails, it falls back to direct mode, and rewrites
``/etc/resolv.conf`` as a regular file pointing at ``100.100.100.100``
with no upstream resolver. The result is SERVFAIL for everything
non-tailnet. The state is self-perpetuating: subsequent
``systemctl restart tailscaled`` keeps re-detecting direct mode. See
GitHub issue #117 for the full root-cause analysis.

This module covers two things, both called from ``_phase_b_setup``:

1. ``apply_tailscaled_dns_fix``: prevention. Installs a
   ``tailscaled.service.d`` override that orders tailscaled after
   ``network-online.target`` and ``nss-lookup.target`` so its
   DNS-manager probe finds a resolver instead of falling back to
   direct mode. Takes effect on next cold boot. Idempotent, non-fatal.

2. ``check_vm_dns``: health check. Probes VM DNS with a libc lookup
   and, when DNS is broken, tries to diagnose the failure as the
   issue #117 latched state (tailscaled-managed ``/etc/resolv.conf``
   with no working forwarder). Runs before any phase B step that
   needs external DNS so we surface a clear error or warning instead
   of failing cryptically on ``apt-get update``. Read-only; the
   operator runs the heal manually, then re-runs ``vm reinit``.

The prevention drop-in deliberately uses ``Wants=`` (not
``Requires=``): a ``network-online`` failure should let tailscaled
fall back to its (broken-but-recoverable) default behavior rather
than block the unit and risk taking the VM off the tailnet entirely.

The prevention drop-in deliberately does NOT restart tailscaled.
Phase B runs over the tailnet, so restarting tailscaled would
disconnect us mid-init. Takes effect on next cold boot, which is
exactly when the race fires.

The DNS check is ordered DNS-first. On a healthy VM (the common
case) it does one libc probe and returns. Only when DNS is broken
does the function dig into the latch-specific diagnosis: read
``/etc/resolv.conf``, look for the tailscaled signature, check
which resolver is active. The suggested heal restores the resolved
stub symlink, which is only the right move when resolved IS the
resolver; on other resolver setups the function emits a non-fatal
warning ("we saw the breakage, but no heal is implemented for this
resolver setup") rather than raising with a hint we know would be
wrong. Either way the operator gets a visible link between the DNS
diagnosis and the apt failure that follows.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import StateError
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport


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


def apply_tailscaled_dns_fix(target: Transport, logger: SSHLogger) -> None:
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


def _ensure_tailscaled_dropin(target: Transport, logger: SSHLogger) -> None:
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


# Probe target for the libc DNS check. Stable A record, frequently cached on
# real upstream resolvers so a working tailscaled forwarder will answer it,
# and it's what `apt-get update` will hit a few steps later anyway. If this
# specific lookup ever stops being stable, swap it. The check just needs
# any external name that should resolve via libc.
_DNS_PROBE_NAME = "deb.debian.org"

_TAILSCALED_RESOLV_SIGNATURE = "generated by tailscale"

_LATCHED_HEAL_HINT = (
    "tailscaled has taken over /etc/resolv.conf but has no working DNS\n"
    "forwarder (issue #117 latched state). Open a platform-native shell\n"
    "(which does NOT go through Tailscale, so the heal sequence survives\n"
    "stopping tailscaled):\n"
    "\n"
    "    agw vm shell --platform <vm-name>\n"
    "\n"
    "Then in that shell:\n"
    "\n"
    "    sudo systemctl stop tailscaled\n"
    "    sudo rm /etc/resolv.conf\n"
    "    sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf\n"
    "    sudo systemctl start tailscaled\n"
    "    getent hosts www.google.com   # verify\n"
    "\n"
    "Then re-run `agw vm reinit <name>` to pick up the cold-boot\n"
    "ordering fix.\n"
    "\n"
    "Note: `--platform` requires the platform-native transport to be\n"
    "available. Lima and WSL2 work transparently. Azure transparently\n"
    "attaches and then detaches a temporary public IP for the session.\n"
    "Proxmox doesn't expose an interactive transport via its QEMU guest\n"
    "agent; use the Proxmox web UI's serial console for the same VM\n"
    "(VM > Console in the Proxmox VE web UI), then run the heal block\n"
    "above in that session."
)


def check_vm_dns(target: Transport, logger: SSHLogger) -> None:
    """Check whether VM DNS is working and surface a diagnosis if it isn't.

    Called before any phase B step that needs external DNS (apt-get
    update, source fetches, etc.). The function's primary job is to
    answer "can the VM resolve external names through libc?". If yes,
    it returns silently. If no, it tries to recognize the specific
    failure shape from issue #117 (tailscaled has taken over
    ``/etc/resolv.conf`` and has no working forwarder) so we can
    suggest a heal; otherwise it surfaces a warning so the operator
    has a visible link between the DNS diagnosis and the apt failure
    that will follow.

    Outcomes by case:

    - DNS resolves: return silently. The VM is fine.
    - DNS fails and we recognize it as the issue #117 latched state
      (``/etc/resolv.conf`` is tailscaled-managed AND ``systemd-resolved``
      is the active resolver, which is the configuration our heal
      sequence is written for): raise ``StateError`` with the heal
      block as a hint.
    - DNS fails and matches the issue #117 shape EXCEPT
      ``systemd-resolved`` isn't the active resolver: warn that we saw
      the breakage but no heal is implemented for this resolver setup.
    - DNS fails for any other reason (resolv.conf unreadable, or not
      tailscaled-managed): warn that DNS is broken so the operator can
      diagnose before subsequent apt steps fail cryptically.
    """
    logger.step("VM DNS")

    # The actual question: can the VM resolve external names?
    # Everything else is diagnosis-of-failure. Announce before we
    # block on the lookup; getent over libc -> tailscaled -> upstream
    # can take a few seconds and the operator should know what we're
    # waiting on.
    output.detail(f"Checking DNS resolution ({_DNS_PROBE_NAME})...")
    probe = target.run(f"getent hosts {shlex.quote(_DNS_PROBE_NAME)}", check=False)
    if getattr(probe, "ok", False):
        # No "VM DNS is healthy." line on the happy path: phase B's
        # convention is "say what we're doing, stay silent on success,
        # warn or raise on failure."
        return

    # DNS is broken. Subsequent steps that need external resolution
    # (apt-get update, source fetches, etc.) will fail. From here on,
    # every return path surfaces something so the operator doesn't get
    # to apt with no visible link back to the actual cause.
    #
    # The resolv.conf read is mode 0644 on every standard distribution
    # (including in the latched state, where tailscaled rewrites it as
    # a regular file with default mode); no sudo is needed.
    resolv = target.run("cat /etc/resolv.conf", check=False)
    if not getattr(resolv, "ok", False):
        msg = (
            f"VM DNS lookup of '{_DNS_PROBE_NAME}' failed, and "
            f"/etc/resolv.conf could not be read for further diagnosis. "
            f"Subsequent steps that need external DNS will likely fail."
        )
        logger.warning(msg)
        output.warn(msg)
        return

    if _TAILSCALED_RESOLV_SIGNATURE not in getattr(resolv, "stdout", ""):
        msg = (
            f"VM DNS lookup of '{_DNS_PROBE_NAME}' failed. The failure "
            f"shape doesn't match the issue #117 latch (/etc/resolv.conf "
            f"isn't tailscaled-managed), so the known heal doesn't apply. "
            f"Investigate the platform resolver; subsequent steps that "
            f"need external DNS will likely fail."
        )
        logger.warning(msg)
        output.warn(msg)
        return

    # /etc/resolv.conf IS tailscaled-managed and libc DNS is broken.
    # This matches the issue #117 latched shape. Platform gate: the
    # heal we'd suggest restores the resolved stub symlink, which is
    # only correct when resolved IS the active resolver. For other
    # resolver setups we don't yet have a tested heal implementation,
    # so warn rather than raise with a hint we know would be wrong.
    resolved = target.run("systemctl is-active --quiet systemd-resolved", check=False)
    if not getattr(resolved, "ok", False):
        msg = (
            f"VM DNS appears latched (issue #117): /etc/resolv.conf is "
            f"tailscaled-managed and libc lookup of '{_DNS_PROBE_NAME}' "
            f"failed. No heal is currently implemented for this VM's "
            f"resolver setup (systemd-resolved is not the active "
            f"resolver). Subsequent steps that need external DNS will "
            f"likely fail."
        )
        logger.warning(msg)
        output.warn(msg)
        return

    raise StateError(
        f"VM DNS is latched (issue #117): /etc/resolv.conf is "
        f"tailscaled-managed and libc lookup of '{_DNS_PROBE_NAME}' "
        f"failed.",
        entity_kind="vm",
        hint=_LATCHED_HEAL_HINT,
    )
