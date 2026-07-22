"""Phase 1 guard (resource-manifests SDD): Config resource reads are gone.

The consumer repoint moved every resource read from ``Config``
attributes to Registry queries so the operator source can swap from
TOML to YAML manifests without touching consumers. The only sanctioned
readers of Config resource attributes are the publishers
(``Config.publish_to`` in config/models.py and the ``publish_to``
operator publishers in apt.py / install_commands.py).

This is a source-level scan (same spirit as the Phase 0 vocabulary
guard): any ``config.<resource-attr>`` / ``cfg.<resource-attr>`` read
outside the allowlisted publisher modules fails the build. Comments and
docstrings count on purpose; prose that references the retired idiom is
stale prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import agentworks

_AGENTWORKS_ROOT = Path(agentworks.__file__).parent

_PUBLISHER_ALLOWLIST = {
    Path("config/models.py"),
    Path("apt.py"),
    Path("install_commands.py"),
}

_RESOURCE_ATTRS = (
    "secrets",
    "vm_templates",
    "agent_templates",
    "workspace_templates",
    "session_templates",
    "git_credentials",
    "secret_backends",
    "admin",
    "named_console",
    "vm",
    "agent",
    "apt_sources",
    "apt_packages",
    "system_install_commands",
    "user_install_commands",
)

_READ_RE = re.compile(r"\b(?:config|cfg)\.(?:" + "|".join(_RESOURCE_ATTRS) + r")\b")


def test_no_config_resource_reads_outside_publishers() -> None:
    offenders: list[str] = []
    for path in sorted(_AGENTWORKS_ROOT.rglob("*.py")):
        rel = path.relative_to(_AGENTWORKS_ROOT)
        if rel in _PUBLISHER_ALLOWLIST:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _READ_RE.search(line):
                offenders.append(f"{rel}:{line_no}: {line.strip()}")
    assert not offenders, (
        "Config resource reads outside the publishers "
        "(repoint them to Registry queries via agentworks.resources.access):\n" + "\n".join(offenders)
    )
