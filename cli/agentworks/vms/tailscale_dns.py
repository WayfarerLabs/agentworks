"""tailscaled cold-boot startup ordering + latched-state detection.

When tailscaled starts before the DNS layer is up, its DNS-manager
probe fails, it falls back to direct mode, and rewrites
``/etc/resolv.conf`` as a regular file pointing at ``100.100.100.100``
with no upstream resolver. The result is SERVFAIL for everything
non-tailnet. The state is self-perpetuating: subsequent
``systemctl restart tailscaled`` keeps re-detecting direct mode. See
GitHub issue #117 for the full root-cause analysis.

This module covers two things, both called from ``_phase_b_setup``:

1. ``apply_tailscaled_dns_fix`` -- prevention. Installs a
   ``tailscaled.service.d`` override that orders tailscaled after
   ``network-online.target`` and ``nss-lookup.target`` so its
   DNS-manager probe finds a resolver instead of falling back to
   direct mode. Takes effect on next cold boot. Idempotent, non-fatal.

2. ``detect_tailscaled_dns_latched`` -- diagnosis. Probes for the
   already-broken state and raises ``StateError`` with the manual
   heal block as a hint if found. Runs before the apt step so we
   abort with a clear error instead of failing cryptically on
   ``apt-get update``. No write side effects; the operator runs the
   heal manually, then re-runs ``vm reinit``.

The prevention drop-in deliberately uses ``Wants=`` (not
``Requires=``): a ``network-online`` failure should let tailscaled
fall back to its (broken-but-recoverable) default behavior rather
than block the unit and risk taking the VM off the tailnet entirely.

The prevention drop-in deliberately does NOT restart tailscaled.
Phase B runs over the tailnet, so restarting tailscaled would
disconnect us mid-init. Takes effect on next cold boot, which is
exactly when the race fires.

The detection is intentionally narrow: it gates on the specific
combination of ``/etc/resolv.conf`` being tailscaled-managed, libc
DNS not working, and ``systemd-resolved`` being the platform's
active resolver. The suggested heal restores the resolved stub
symlink, which is only the right move when resolved IS the resolver.
On platforms with a different resolver, detection emits a non-fatal
warning -- we saw the breakage, but the heal logic for this resolver
setup isn't implemented -- so the operator has a visible link
between the diagnosis and the apt failure that follows. We don't
attempt a heal we know would be wrong.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import StateError
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


# Probe target for the libc DNS check. Stable A record, frequently cached on
# real upstream resolvers so a working tailscaled forwarder will answer it,
# and it's what `apt-get update` will hit a few steps later anyway. If this
# specific lookup ever stops being stable, swap it -- the check just needs
# any external name that should resolve via libc.
_DNS_PROBE_NAME = "deb.debian.org"

_TAILSCALED_RESOLV_SIGNATURE = "generated by tailscale"

_LATCHED_HEAL_HINT = (
    "tailscaled has taken over /etc/resolv.conf but has no working DNS\n"
    "forwarder (issue #117 latched state). Heal manually via SSH:\n"
    "\n"
    "    sudo systemctl stop tailscaled\n"
    "    sudo rm /etc/resolv.conf\n"
    "    sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf\n"
    "    sudo systemctl start tailscaled\n"
    "\n"
    "Verify with: getent hosts www.google.com\n"
    "Then re-run `agw vm reinit <name>` to pick up the cold-boot\n"
    "ordering fix."
)


def detect_tailscaled_dns_latched(target: ExecTarget, logger: SSHLogger) -> None:
    """Abort phase B early if the VM is in the issue #117 latched state.

    Raises ``StateError`` with the manual heal block in the hint when all
    of these are simultaneously true:

    - ``/etc/resolv.conf`` is a regular file written by tailscaled
      (contains the tailscaled-generated signature comment).
    - A libc DNS lookup of a stable external name fails.
    - ``systemd-resolved`` is active on the VM (the platform whose heal
      sequence we know).

    Emits a non-fatal warning when the resolv.conf + DNS-probe signals
    fire but ``systemd-resolved`` isn't the active resolver. We've
    detected the broken state but don't have a heal implementation for
    this resolver setup; the warning ensures the operator gets a
    visible link between the diagnosis and the cryptic apt failure
    that will follow, rather than burying the lede under a ``detail``
    line.

    Returns silently when no sign of breakage is present (healthy VM,
    or a resolv.conf we don't recognize as tailscaled-managed).
    """
    logger.step("Tailscale DNS state")

    # Resolv.conf signature -- the cheapest check, and the only one that
    # uniquely points at tailscaled. /etc/resolv.conf is mode 0644 on
    # every standard distribution (including in the latched state, where
    # tailscaled rewrites it as a regular file with default mode), so no
    # sudo is needed here -- keeping the read footprint minimal makes the
    # read-only contract more obvious.
    resolv = target.run("cat /etc/resolv.conf", check=False)
    if not getattr(resolv, "ok", False):
        output.detail("Could not read /etc/resolv.conf; skipping latch check.")
        return
    if _TAILSCALED_RESOLV_SIGNATURE not in getattr(resolv, "stdout", ""):
        output.detail("/etc/resolv.conf is not tailscaled-managed; no latch.")
        return

    # libc DNS probe -- the actual failure mode the latch creates.
    probe = target.run(f"getent hosts {shlex.quote(_DNS_PROBE_NAME)}", check=False)
    if getattr(probe, "ok", False):
        output.detail("libc DNS works; tailscaled forwarder is healthy.")
        return

    # Platform gate. The heal we'd suggest restores the resolved stub
    # symlink, which is only correct when resolved IS the active
    # resolver. For other resolver setups we don't yet have a tested
    # heal implementation. Surface a warning -- we saw the breakage,
    # but the fix logic for this platform isn't implemented -- rather
    # than the StateError with a hint we know would be wrong.
    resolved = target.run("systemctl is-active --quiet systemd-resolved", check=False)
    if not getattr(resolved, "ok", False):
        msg = (
            f"tailscaled DNS appears latched (issue #117): /etc/resolv.conf is "
            f"tailscaled-managed and libc lookup of '{_DNS_PROBE_NAME}' failed. "
            f"No heal is currently implemented for this VM's resolver setup "
            f"(systemd-resolved is not the active resolver). Subsequent steps "
            f"that need external DNS (apt-get update, etc.) will likely fail."
        )
        logger.warning(msg)
        output.warn(msg)
        return

    raise StateError(
        f"tailscaled DNS is latched on this VM (issue #117): "
        f"/etc/resolv.conf is tailscaled-managed and libc lookup of "
        f"'{_DNS_PROBE_NAME}' failed.",
        entity_kind="vm",
        hint=_LATCHED_HEAL_HINT,
    )
