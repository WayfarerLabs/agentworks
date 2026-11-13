"""The workstation's Tailscale preflight: commands that reach a VM at its tailnet address need it."""

from __future__ import annotations

import subprocess

from agentworks.errors import ConnectivityError
from agentworks.programs import resolve_program


def verify_tailscale_available() -> None:
    """Pre-flight: verify the local machine is on Tailscale."""
    try:
        result = subprocess.run(
            [resolve_program("tailscale"), "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except FileNotFoundError:
        raise ConnectivityError("'tailscale' command not found. Install Tailscale on this machine.") from None
    except subprocess.TimeoutExpired:
        raise ConnectivityError("'tailscale status' timed out. Is Tailscale running?") from None

    if result.returncode != 0:
        raise ConnectivityError(
            "This machine is not connected to Tailscale. "
            "Agentworks reaches VMs over SSH at their tailnet addresses. Run 'tailscale up' first."
        )
