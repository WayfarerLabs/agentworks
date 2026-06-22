"""Polymorphic transports for reaching VMs.

Phase 1 exposes the ``Transport`` ABC and the four concrete subclasses
(``SSHTransport``, ``LimaTransport``, ``RemoteLimaTransport``,
``WSL2Transport``). The named factory functions (``transport``,
``agent_transport``, ``provisioner_transport``) land in Phase 2 once
the package is in place.
"""

from agentworks.transports.base import Transport
from agentworks.transports.lima import LimaTransport
from agentworks.transports.remote_lima import RemoteLimaTransport
from agentworks.transports.ssh import SSHTransport
from agentworks.transports.wsl2 import WSL2Transport

__all__ = [
    "LimaTransport",
    "RemoteLimaTransport",
    "SSHTransport",
    "Transport",
    "WSL2Transport",
]
