"""Registration index for the framework's resource kinds.

Kind definitions do NOT live here. Each kind is defined and registered in
the domain package next to the code that implements it (the declarable
row dataclasses AND the capability kinds live together with their
domain): ``agentworks.catalog`` (the catalog kinds),
``agentworks.secrets.kinds``, ``agentworks.capabilities.git_credential.kinds``,
``agentworks.capabilities.harness.kinds``,
``agentworks.vms.kinds``, ``agentworks.agents.kinds``,
``agentworks.workspaces.kinds``, ``agentworks.sessions.kinds``.

This module exists only because Python populates ``KIND_REGISTRY`` via
import side effects: importing each domain's kind module runs its
``KIND_REGISTRY[...] = ...`` registration. A single
``import agentworks.resources`` imports this index, which imports every
domain kind module, so the registry is fully populated. One line per
domain, no logic.
"""

from __future__ import annotations

import agentworks.agents.kinds  # noqa: F401
import agentworks.capabilities.git_credential.kinds  # noqa: F401
import agentworks.capabilities.harness.kinds  # noqa: F401
import agentworks.catalog  # noqa: F401
import agentworks.secrets.kinds  # noqa: F401
import agentworks.sessions.kinds  # noqa: F401
import agentworks.vms.kinds  # noqa: F401
import agentworks.workspaces.kinds  # noqa: F401
