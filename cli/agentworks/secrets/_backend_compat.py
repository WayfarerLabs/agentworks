"""Private bridge from the pre-source resolver to the final backend classes.

This module exists only on the feature branch between the class-registry
cutover and the atomic source-model cutover. It is not exported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from agentworks.capabilities.config import registered_implementation
from agentworks.schema import config_error_from, extract_references, filled_defaults

if TYPE_CHECKING:
    from agentworks.capabilities.secret_backend.base import SecretBackend
    from agentworks.resources.reference import ConfigReference
    from agentworks.schema import RefOwner
    from agentworks.source_location import SourceLocation


def backend_class(name: str) -> type[SecretBackend] | None:
    """Return the class registered under the legacy backend selector."""
    impl = registered_implementation("secret-backend", name)
    return cast("type[SecretBackend] | None", impl)


def mapping_model(name: str) -> type[BaseModel] | None:
    """The mapping model used by the current production carrier.

    All implementations use their permanent mapping model except 1Password,
    whose old account-plus-reference table remains accepted until source
    config takes ownership of account in the atomic source cutover.
    """
    impl = backend_class(name)
    if impl is None:
        return None
    return cast("type[BaseModel]", getattr(impl, "_legacy_mapping_model", impl.mapping_model))


def validate_mapping(
    *,
    name: str,
    mapping: object,
    owner: RefOwner,
    location: SourceLocation | None,
) -> BaseModel | None:
    """Validate one mapping for the pre-source production path."""
    model = mapping_model(name)
    if model is None:
        return None
    try:
        return model.model_validate(filled_defaults(model, mapping, owner))
    except PydanticValidationError as exc:
        from agentworks.capabilities.config import reference_hint

        raise config_error_from(
            exc,
            model_cls=model,
            owner=owner,
            location=location,
            hint=reference_hint("secret-backend", name),
        ) from exc


def mapping_references(
    *,
    name: str,
    mapping: object,
    owner: RefOwner,
) -> tuple[ConfigReference, ...]:
    """Extract mapping references for the pre-source production path."""
    model = mapping_model(name)
    if model is None:
        return ()
    return extract_references(model, filled_defaults(model, mapping, owner))
