"""Small complete instance-state facts for operational projection tests."""

from agentworks.instance_description import DeclarationSlot, InstanceSpec, InstanceStateDescription
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.resolved_spec import ResolvedSpec


def stub_instance_state(*names: str) -> InstanceStateDescription:
    """Build resolved declaration slots without involving config or database state."""
    return InstanceStateDescription(
        declarations=tuple(
            DeclarationSlot(
                name=name,
                selection=ResourceIdentity(f"{name}-template", "default"),
                instance_spec=InstanceSpec("absent"),
                current=ResolvedSpec(spec={}, provenance=()),
            )
            for name in names
        )
    )
