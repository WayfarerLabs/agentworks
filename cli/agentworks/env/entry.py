"""``EnvEntry``: one env var declaration, either a plaintext value or a secret reference."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import AfterValidator, model_validator

from agentworks.schema import AgwModel, SecretRef

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

    from agentworks.resources.reference import SecretReference

_ENV_KEY_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_env_var_name(value: str) -> str:
    if not _ENV_KEY_RE.match(value):
        raise ValueError(f"invalid env var name {value!r} (must match /{_ENV_KEY_RE.pattern}/)")
    return value


EnvVarName = Annotated[str, AfterValidator(_check_env_var_name)]
"""An env table's KEY: a POSIX-shaped variable name.

A validator rather than ``Field(pattern=...)`` because the bridge reads a
validator's own exception out of the error context, so this reproduces
the message the hand-rolled loader gave verbatim, naming the offending
key rather than making the operator parse a regex."""


class EnvEntry(AgwModel):
    """One env var declaration.

    Exactly one of ``value`` or ``secret`` is set. ``value`` carries a plaintext
    value to export; ``secret`` carries the name of a declared secret
    resolved through the active backends at command time.

    Two spellings, one type: an operator writes ``FOO: a value`` for the
    plaintext form and ``FOO: {secret: my-secret}`` for the other, and the
    before-validator below folds the first into the second. The KEY is the
    env var's name and lives in the table that holds this entry; it is
    deliberately not a field here, because two places to say one thing is
    two places that can disagree (nothing ever enforced that a table's key
    matched its entry's).
    """

    value: str | None = None
    """The plaintext value to export."""

    secret: Annotated[str, SecretRef(usage="an env var's value")] | None = None
    """The name of a declared secret whose value is exported instead."""

    @model_validator(mode="before")
    @classmethod
    def _accept_a_bare_value(cls, data: Any) -> Any:
        """``FOO: a value`` is the plaintext form, and it is the shape
        operators write for all but a handful of entries."""
        return {"value": data} if isinstance(data, str) else data

    @model_validator(mode="after")
    def _exactly_one_source(self) -> EnvEntry:
        if self.value is None and self.secret is None:
            raise ValueError("must set exactly one of value or secret")
        if self.value is not None and self.secret is not None:
            raise ValueError("cannot set both value and secret")
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """The two accepted spellings, in emitted schema.

        The before-validator above is invisible to ``model_json_schema``,
        which would emit the table form alone and let a schema-aware editor
        flag every plaintext entry an operator writes. This is the one
        place in the kind-spec models where a schema fact is written by
        hand rather than derived, and it sits beside the validator that
        implements it so the two are read together; a test validates both
        spellings against the emitted schema.
        """
        return {"anyOf": [{"type": "string"}, handler(core_schema)]}

    def referenced_resources(
        self,
        key: str,
        source: tuple[str, str],
        declared_by: tuple[str, str] | None = None,
    ) -> list[SecretReference]:
        """Emit a ``SecretReference`` for this entry's secret reference,
        or an empty list for plaintext entries.

        Called by the Resource that owns this env entry's table (admin,
        the four template kinds, named_console) through
        :func:`env_references`. ``key`` is the env var name the entry sits
        under, which the owner has and this entry deliberately does not;
        the usage text is derived from it, so a typo'd KEY surfaces in
        diagnostics with the actual variable name. ``source`` is the
        declaring Resource's ``(kind, name)`` identity.

        ``declared_by`` is for an INHERITING owner, whose env table is the
        merged one: it names the template that actually wrote this entry,
        so the edge can be attributed to a file that contains it.

        The import of ``SecretReference`` is ``TYPE_CHECKING``-only to
        keep ``EnvEntry`` framework-ignorant at runtime; constructed
        lazily inside the method.
        """
        if self.secret is None:
            return []
        from agentworks.resources.reference import SecretReference
        from agentworks.secrets.kinds import SECRET_KIND_NAME

        return [
            SecretReference(
                name=self.secret,
                kind=SECRET_KIND_NAME,
                usage=f"the {key} env var",
                source=source,
                declared_by=declared_by,
            )
        ]


EnvTable = dict[EnvVarName, EnvEntry]
"""The shared env-table field type every env-bearing kind declares.

One authored type is what lets the load-time env hygiene checks find
their subject by ANNOTATION rather than by a list of env-bearing kinds,
which is the list the sixth such kind would not be on."""


def env_references(
    env: Mapping[str, EnvEntry] | None,
    source: tuple[str, str],
    declarers: Mapping[str, tuple[str, str]] | None = None,
) -> list[SecretReference]:
    """Aggregate ``EnvEntry.referenced_resources`` across an env table.

    Module-level helper shared by every env-bearing Resource type's
    ``dependencies()`` method so the per-type method body stays
    one line. ``env`` may be ``None`` (``SessionTemplate.env`` is
    optional) or empty, in which case the result is an empty list.

    ``declarers`` maps an env-table KEY to the template that declared it,
    for an inheriting owner passing its MERGED table (FR17). Absent, or
    missing a key, means the owner declared it: the ordinary case, and the
    only case for a kind that does not inherit.

    The env package owns this helper because it aggregates
    ``EnvEntry.referenced_resources``; the template rows that
    live in the domain packages import it from here.
    """
    if not env:
        return []
    out: list[SecretReference] = []
    for key, entry in env.items():
        out.extend(entry.referenced_resources(key, source, (declarers or {}).get(key)))
    return out
