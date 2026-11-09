"""Credential-free GCE startup-script construction and readiness."""

from __future__ import annotations

from agentworks.capabilities.vm_platform.bootstrap_script import run_once_script
from agentworks.errors import ConfigError

GCE_BOOTSTRAP_MARKER = "/var/lib/agentworks/gce-bootstrap-v1.complete"
GCE_BOOTSTRAP_PATH = "/var/lib/agentworks/gce-bootstrap-v1.sh"
GCE_READINESS_COMMAND = f"until test -f {GCE_BOOTSTRAP_MARKER}; do sleep 2; done"
GCE_READINESS_LABEL = "GCE startup script"
GCE_STARTUP_SCRIPT_LIMIT_BYTES = 256 * 1024


def build_startup_script(bootstrap_script: str, *, instance_name: str) -> str:
    """Wrap the shared bootstrap in a success-only durable run-once gate.

    Compute Engine executes ``startup-script`` metadata on every boot, so the
    bootstrap runs inside :func:`run_once_script`, whose marker is also what
    :data:`GCE_READINESS_COMMAND` waits for. The result is refused when its
    UTF-8 size exceeds the metadata limit.
    """
    script = run_once_script(bootstrap_script, marker=GCE_BOOTSTRAP_MARKER, script_path=GCE_BOOTSTRAP_PATH)
    encoded_size = len(script.encode("utf-8"))
    if encoded_size > GCE_STARTUP_SCRIPT_LIMIT_BYTES:
        raise ConfigError(
            f"GCE startup script for '{instance_name}' is {encoded_size} UTF-8 bytes, "
            f"over the {GCE_STARTUP_SCRIPT_LIMIT_BYTES}-byte metadata limit",
            hint="use a shorter admin SSH key or reduce vm-template input that grows the bootstrap",
        )
    return script
