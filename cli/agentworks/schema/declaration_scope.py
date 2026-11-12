"""Field metadata restricting where an operator may declare a value."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, RootModel

from agentworks.errors import ConfigError
from agentworks.schema._shape import spine_metadata
from agentworks.schema.errors import located
from agentworks.schema.shorthand import scalar_shorthand_of
from agentworks.value_provenance import longest_prefix_value

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

    from agentworks.schema.markers import RefOwner
    from agentworks.source_location import SourceLocation
    from agentworks.value_provenance import ProvenancePath


@dataclass(frozen=True)
class InstanceOnly:
    """A field may be authored on an instance, never on a template.

    Template kinds use the resource convention ``*-template``. This checks
    declaration ownership, not the runtime owner of an effective config.
    Even an explicit null is a declaration; an omitted field is not.
    """

    def __get_pydantic_json_schema__(
        self,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        schema["x-agw-instance-only"] = True
        return schema


def validate_declaration_scope(
    validated: BaseModel,
    authored: object,
    *,
    owner: RefOwner,
    provenance: Mapping[ProvenancePath, RefOwner] | None = None,
    location: SourceLocation | None = None,
    hint: str | None = None,
) -> None:
    """Check authored capability config after its shape has been validated.

    The original payload distinguishes explicit declarations from defaults.
    The validated values select nested model and union types, so scope checks
    follow precisely the arm accepted by validation. Provenance identifies
    the declarer of each field in an effective, inherited config.
    """
    pending: list[tuple[object, object, ProvenancePath]] = [(validated, authored, ())]
    while pending:
        value, raw, path = pending.pop()
        if isinstance(raw, RootModel):
            raw = raw.root
        elif isinstance(raw, BaseModel):
            raw = {name: getattr(raw, name) for name in raw.model_fields_set}
        if isinstance(value, BaseModel) and (shorthand := scalar_shorthand_of(type(value))) is not None:
            raw = shorthand.folded(raw)
        if isinstance(value, RootModel):
            pending.append((value.root, raw, path))
        elif isinstance(value, BaseModel) and isinstance(raw, Mapping):
            for name, field in type(value).model_fields.items():
                if name not in raw:
                    continue
                field_path = (*path, name)
                declarer = (longest_prefix_value(provenance, field_path) if provenance else None) or owner
                if declarer.kind.endswith("-template") and any(
                    isinstance(item, InstanceOnly) for item in spine_metadata(field)
                ):
                    rendered = ""
                    for part in field_path:
                        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
                    inherited = f" (inherited from {declarer.display})" if declarer != owner else ""
                    raise ConfigError(
                        located(location, f"{owner.display}{rendered}: may only be declared on an instance{inherited}"),
                        entity_kind=owner.kind,
                        entity_name=owner.name,
                        hint=hint,
                    )
                pending.append((getattr(value, name), raw[name], field_path))
        elif isinstance(value, Mapping) and isinstance(raw, Mapping):
            pending.extend((item, raw[key], (*path, key)) for key, item in value.items() if key in raw)
        elif isinstance(value, (list, tuple)) and isinstance(raw, (list, tuple)):
            pending.extend(
                (item, source, (*path, index)) for index, (item, source) in enumerate(zip(value, raw, strict=False))
            )
