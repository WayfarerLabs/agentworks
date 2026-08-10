"""Credential-free GCE startup-script construction and readiness."""

from __future__ import annotations

from agentworks.errors import ConfigError

GCE_BOOTSTRAP_MARKER = "/var/lib/agentworks/gce-bootstrap-v1.complete"
GCE_READINESS_COMMAND = f"until test -f {GCE_BOOTSTRAP_MARKER}; do sleep 2; done"
GCE_READINESS_LABEL = "GCE startup script"
GCE_STARTUP_SCRIPT_LIMIT_BYTES = 256 * 1024


def build_startup_script(bootstrap_script: str, *, instance_name: str) -> str:
    """Wrap the shared bootstrap in a success-only durable run-once gate.

    Compute Engine executes ``startup-script`` metadata on every boot. The
    marker check therefore precedes every mutation, and the marker is moved
    into place only after the complete shared bootstrap exits successfully.
    A failed or interrupted first run leaves no success marker and may retry on
    a later boot.
    """
    script = f"""\
#!/bin/bash
set -euo pipefail

MARKER={GCE_BOOTSTRAP_MARKER}
if test -f "$MARKER"; then
    exit 0
fi

install -d -m 0755 "$(dirname "$MARKER")"
BOOTSTRAP=/var/lib/agentworks/gce-bootstrap-v1.sh
cat > "$BOOTSTRAP" <<'AGW_GCE_BOOTSTRAP_EOF'
{bootstrap_script.rstrip()}
AGW_GCE_BOOTSTRAP_EOF
chmod 0700 "$BOOTSTRAP"
"$BOOTSTRAP"

MARKER_TMP=$(mktemp "$MARKER.tmp.XXXXXX")
trap 'rm -f "${{MARKER_TMP:-}}"' EXIT
chmod 0644 "$MARKER_TMP"
mv -f "$MARKER_TMP" "$MARKER"
MARKER_TMP=
"""
    encoded_size = len(script.encode("utf-8"))
    if encoded_size > GCE_STARTUP_SCRIPT_LIMIT_BYTES:
        raise ConfigError(
            f"GCE startup script for '{instance_name}' is {encoded_size} UTF-8 bytes, "
            f"over the {GCE_STARTUP_SCRIPT_LIMIT_BYTES}-byte metadata limit",
            hint="use a shorter admin SSH key or reduce vm-template input that grows the bootstrap",
        )
    return script
