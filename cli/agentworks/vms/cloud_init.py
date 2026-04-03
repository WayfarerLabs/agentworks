"""Cloud-init wrapper for Azure VM provisioning.

Wraps a bootstrap script in a #cloud-config YAML that delivers and executes
it via write_files + runcmd. This lets Azure VMs run the same bootstrap script
that Lima uses via its provision block.
"""

from __future__ import annotations

import textwrap

# Minimal packages needed during provisioning (cloud-init/bootstrap).
# Only what is required to get the VM to a state where init can SSH in.
PROVISIONING_PACKAGES = [
    "openssh-server",
    "curl",
    "sudo",
    "ca-certificates",
    "gnupg",
]

# System packages installed during init (every create/reinit).
# These are always installed regardless of user config.
INIT_SYSTEM_PACKAGES = [
    "git",
    "unzip",
    "tmux",
    "tmuxinator",
    "acl",
    "jq",
    "mise",
]


def generate_cloud_init(bootstrap_script: str) -> str:
    """Generate a #cloud-config YAML that runs the bootstrap script.

    Uses write_files to place the script on disk and runcmd to execute it.
    This is the delivery mechanism for Azure; the script itself is the same
    one Lima embeds in its provision block.
    """
    # Indent the script content for YAML embedding (8 spaces for write_files content block)
    indented = textwrap.indent(bootstrap_script, "        ")

    return (
        "#cloud-config\n"
        "write_files:\n"
        "  - path: /tmp/agentworks-bootstrap.sh\n"
        "    permissions: '0755'\n"
        "    content: |\n"
        f"{indented}\n"
        "runcmd:\n"
        '  - ["/bin/bash", "/tmp/agentworks-bootstrap.sh"]\n'
    )
