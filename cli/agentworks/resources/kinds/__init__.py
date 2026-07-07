"""Concrete ``ResourceKind`` implementations.

Each kind module declares its kind instance and self-registers into
``KIND_REGISTRY`` at import. This module imports every kind so a single
``import agentworks.resources`` populates the registry.
"""

from __future__ import annotations

from agentworks.resources.kinds import (  # noqa: F401
    admin_template,
    agent_template,
    catalog,
    git_credential,
    git_credential_provider,
    named_console_template,
    secret,
    secret_backend,
    session_template,
    vm_template,
    workspace_template,
)
