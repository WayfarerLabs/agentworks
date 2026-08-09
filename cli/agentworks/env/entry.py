"""``EnvEntry``: one env var declaration, either a plaintext value or a secret reference."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, ClassVar, Final

from pydantic import AfterValidator, ConfigDict

from agentworks.schema import (
    AgwModel,
    AgwRootModel,
    ScalarShorthand,
    SecretRef,
    StructuralUnion,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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


class PlaintextEnvEntry(AgwModel):
    """An env var whose exported value is written as plaintext."""

    model_config = ConfigDict(title="plaintext")

    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")

    value: str
    """The plaintext value to export."""


class SecretEnvEntry(AgwModel):
    """An env var whose exported value comes from a declared secret."""

    model_config = ConfigDict(title="secret")

    secret: Annotated[str, SecretRef(usage="an env var's value")]
    """The name of a declared secret whose value is exported."""


class EnvEntry(AgwRootModel[Annotated[PlaintextEnvEntry | SecretEnvEntry, StructuralUnion()]]):
    """One env var declaration, as a structural plaintext-or-secret union.

    ``PlaintextEnvEntry`` and ``SecretEnvEntry`` are distinct closed arms.
    Their required and allowed fields select the arm for validation,
    reference extraction, and owner-default filling; no cross-field
    validator carries that choice.

    Three spellings, one type: an operator writes ``FOO: a value`` or
    ``FOO: {value: a value}`` for plaintext, and
    ``FOO: {secret: my-secret}`` for the other. The shorthand declared
    below makes the first spelling the plaintext arm's short form. The KEY is
    the env var's name and lives in the table that holds this entry; it is
    deliberately not a field here, because two places to say one thing is
    two places that can disagree (nothing ever enforced that a table's key
    matched its entry's).
    """

    scalar_shorthand: ClassVar = PlaintextEnvEntry.scalar_shorthand
    """``FOO: a value`` is the plaintext form, and it is the shape
    operators write for all but a handful of entries.

    One declaration, three derivations: the fold, the emitted schema's
    string arm, and the "string or table" every human surface renders.
    Both of the first two were written by hand here once, which is how the
    third came to be missing: ``describe-kind`` documented the table form
    alone for a field whose emitted schema had offered both since it was
    written."""

    @property
    def value(self) -> str | None:
        """The plaintext value, or ``None`` for the secret arm."""
        return self.root.value if isinstance(self.root, PlaintextEnvEntry) else None

    @property
    def secret(self) -> str | None:
        """The secret name, or ``None`` for the plaintext arm."""
        return self.root.secret if isinstance(self.root, SecretEnvEntry) else None

    def referenced_resources(
        self,
        key: str,
        source: tuple[str, str],
        declared_by: tuple[str, str] | None = None,
    ) -> list[SecretReference]:
        """Adapt the structurally extracted edge to its owning Resource.

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

        Which arm carries an edge is read from the same ``SecretRef`` marker
        and structural selector as pre-validation extraction. This method
        adds only the owning Resource facts the schema layer cannot know.

        The import of ``SecretReference`` is ``TYPE_CHECKING``-only to keep
        ``EnvEntry`` framework-ignorant at runtime; constructed lazily inside
        the method.
        """
        from agentworks.resources.reference import SecretReference
        from agentworks.schema import extract_references
        from agentworks.secrets.kinds import SECRET_KIND_NAME

        return [
            SecretReference(
                name=reference.name,
                kind=SECRET_KIND_NAME,
                usage=f"the {key} env var",
                source=source,
                declared_by=declared_by,
            )
            for reference in extract_references(type(self), self.model_dump())
            if reference.kind == SECRET_KIND_NAME
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
