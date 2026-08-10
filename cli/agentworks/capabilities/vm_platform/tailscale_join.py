"""Credential-free Tailscale join delivery over provisioning transports."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ProvisioningError
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.transports import Transport

TAILSCALE_JOIN_STDIN_COMMAND = (
    'IFS= read -r TAILSCALE_AUTH_KEY && test -n "$TAILSCALE_AUTH_KEY" && tailscale up --auth-key "$TAILSCALE_AUTH_KEY"'
)
DEFAULT_READINESS_COMMAND = "cloud-init status --wait"
DEFAULT_READINESS_LABEL = "cloud-init"


def join_tailscale_ephemerally(
    target: Transport,
    auth_key: str,
    *,
    timeout: int | None = None,
) -> None:
    """Send one auth key on stdin to a fixed guest command.

    The resolved value never enters host or guest command text. Transport
    sensitive-input semantics also keep reflected output and native failures
    out of logs, return values, diagnostics, and chained exception objects.
    """
    target.run(
        TAILSCALE_JOIN_STDIN_COMMAND,
        sudo=True,
        timeout=timeout,
        input_text=f"{auth_key}\n",
    )


class EphemeralTailscaleBootstrap:
    """Finish a key-free cloud bootstrap through one post-boot stdin join."""

    def __init__(
        self,
        target: Transport,
        *,
        readiness_command: str = DEFAULT_READINESS_COMMAND,
        readiness_label: str = DEFAULT_READINESS_LABEL,
    ) -> None:
        self._target = target
        self._readiness_command = readiness_command
        self._readiness_label = readiness_label

    def complete(self, auth_key: str) -> str | None:
        """Wait for cloud-init, join exactly once, then discover the IP.

        Readiness failure raises before sending the key so the platform's
        create rollback can remove the partial VM. Once the join command
        succeeds, later IP-discovery failure remains a completed bootstrap so
        Phase A cannot deliver the credential a second time.
        """
        self._wait_for_readiness()

        join_tailscale_ephemerally(self._target, auth_key, timeout=30)
        return self._tailscale_ip()

    def _wait_for_readiness(self) -> None:
        output.detail(f"Waiting for {self._readiness_label} bootstrap to complete (this may take several minutes)...")

        for attempt in range(30):
            try:
                self._target.run("echo ok", check=True, timeout=10)
                break
            except SSHError as exc:
                if attempt == 29:
                    raise ProvisioningError("SSH did not become ready for create-time bootstrap") from exc
                time.sleep(10)

        try:
            self._target.run(self._readiness_command, check=True, timeout=600)
        except SSHError as exc:
            raise ProvisioningError(
                f"{self._readiness_label} did not complete successfully during create-time bootstrap"
            ) from exc

    def _tailscale_ip(self) -> str | None:
        try:
            result = self._target.run("tailscale ip -4", sudo=True, check=True, timeout=15)
        except SSHError as exc:
            output.warn(f"could not retrieve Tailscale IP: {exc}")
            output.warn("Tailscale is joined; Phase A will retry IP discovery without the auth key.")
            return None

        tailscale_ip = result.stdout.strip()
        output.detail(f"Tailscale IP: {tailscale_ip}")
        return tailscale_ip
