"""Immutable final environment and sensitivity supplied by core composition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from agentworks.errors import ValidationError

from ._inline import _environment
from ._inline_request import _ENV_NAME


def _copy(values: Mapping[str, str]) -> dict[str, str]:
    """Snapshot external mappings without retaining their values in diagnostics."""
    if not isinstance(values, Mapping):
        raise ValidationError("Bound execution environment requires a string mapping")
    entries = _environment(values)
    copied = dict(entries)
    if len(copied) != len(entries) or any(_ENV_NAME.fullmatch(name) is None for name, _ in entries):
        raise ValidationError("Bound execution environment requires valid variable names")
    return copied


@dataclass(frozen=True, slots=True, repr=False)
class _BoundEnvironment:
    """Final payload values, core-protected entries and explicit provenance.

    Copies cross the composition boundary; immutable views retain no caller
    mapping. Sensitivity applies to the whole environment and cannot decrease
    when later values replace earlier ones. Helper-specific reserved names
    remain the invocation preparation layer's responsibility.
    """

    values: Mapping[str, str] = field(default_factory=dict)
    protected: Mapping[str, str] = field(default_factory=dict)
    sensitive: bool = False

    def __post_init__(self) -> None:
        if type(self.sensitive) is not bool:
            raise ValidationError("Bound execution environment sensitivity must be boolean")
        values, protected = _copy(self.values), _copy(self.protected)
        if any(name in values and values[name] != value for name, value in protected.items()):
            raise ValidationError("Execution environment conflicts with core-protected values")
        values.update(protected)
        object.__setattr__(self, "values", MappingProxyType(values))
        object.__setattr__(self, "protected", MappingProxyType(protected))

    def with_env(self, env: Mapping[str, str], *, sensitive: bool = False) -> _BoundEnvironment:
        """Merge an external override while retaining all protected provenance."""
        if type(sensitive) is not bool:
            raise ValidationError("Bound execution environment sensitivity must be boolean")
        return _BoundEnvironment(
            dict(self.values) | _copy(env),
            protected=self.protected,
            sensitive=self.sensitive or sensitive,
        )
