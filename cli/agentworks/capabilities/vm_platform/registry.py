"""Concrete VM platform implementations, loaded only by registry consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform.base import VMPlatform

VM_PLATFORM_REGISTRY: dict[str, type[VMPlatform]] = {
    LimaPlatform.name: LimaPlatform,
    WSL2Platform.name: WSL2Platform,
}
