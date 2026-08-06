"""``CapabilityBlock``: the tagged table an operator writes to select a
capability and configure it in one place.

``spec.platform: {name: lima, vm_host: ...}`` is one table with two
owners: ``name`` is the HOST kind's selector, and every other key belongs
to the capability the tag names. This class is the host's half, declared
once and shared by every hosting surface, so the split is spelled in one
place rather than per host kind.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agentworks.schema.base import _SHARED_SETTINGS, NonEmptyStr


class CapabilityBlock(BaseModel):
    """A tagged capability table: ``name`` selects the implementation and
    every other key is that implementation's own config.

    The extra keys are NOT validated here, and that is the contract, not
    a gap: they belong to a different owner, and the finalize pass checks
    them closed-world against that owner's declared model
    (``validate_capability_config``). Validating them twice, against two
    models, is how a host kind would end up encoding what its capabilities
    accept.
    """

    # A deliberate, visible local exception to the base's closed world,
    # which is the shape ``AgwModel``'s docstring sanctions: what is open
    # here is another owner's surface, and it is closed against that
    # owner's model at finalize. Spelled once, in this shared class, so
    # no host kind spells it for itself.
    model_config = ConfigDict(extra="allow", **_SHARED_SETTINGS)

    name: NonEmptyStr
    """The capability's registered name (``lima``, ``github``, ``codex``)."""

    @property
    def config(self) -> dict[str, object]:
        """The capability-owned keys: everything the operator wrote
        except the tag."""
        return dict(self.model_extra or {})

    @property
    def tagged(self) -> dict[str, object]:
        """The whole table as written, tag included: what the capability
        core validates and extracts references from."""
        return {"name": self.name, **self.config}
