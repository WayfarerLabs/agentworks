"""Retired capability-config shapes, refused with the exact rewrite.

RELEASE-SCOPED, like ``manifests/decode.py``'s retired sibling shape and
the guide both of them name. This module exists to carry operators across
one break and is meant to be DELETED once 0.14 is far enough back that a
generic "unknown field" is a good enough answer. Nothing else depends on
it: remove the module, the two call sites in
:mod:`agentworks.capabilities.config`, and the
:attr:`~agentworks.capabilities.base.Capability.retired_shape` declarations,
and the framework is unchanged.

**The defect it is the receipt for.** Three platforms used to express a
mode CHOICE through the presence or absence of an optional block: azure's
``service_principal``, aws's ``credentials``, lima's ``vm_host``. The
defect was never that absence selected a mechanism; it was that there
was no way to DECLARE the choice at all, so a document could not tell "I
chose the omitted one" from "I never configured this", and a misspelled
key silently selected the wrong mechanism. Each is a tagged union now,
with the mode the old absence selected as its ordinary declared default
(operator ruling, reversing an earlier required-with-no-default
revision; the union sites carry the reasoning).

The default is what keeps those breaks narrow: a manifest that omitted an
old block still loads, landing on the same mechanism it always used. Only
a document that WROTE the retired field crosses the break. Every changed
written shape gets its exact rewrite here rather than a bare model error.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.schema import located

if TYPE_CHECKING:
    from agentworks.schema import RefOwner
    from agentworks.source_location import SourceLocation

RETIRED_SHAPE_HINT = (
    "Apply the rewrite above; `agw resource describe-kind <kind>` documents the field, "
    "and `agw resource sample <kind>` prints it as a document to edit. "
    'See "Authentication and placement are one tagged field now" in docs/guides/upgrading-to-0.14.md.'
)
"""Where an operator goes to make a retired presence-shape load again.

Attached to every error below, all of which PRINT a rewrite, which is what
"the rewrite above" refers to. Release-scoped with the guide it names, and
retired with this module; the errors keep their inline rewrite, which is
the half that stands on its own.
"""


@dataclass(frozen=True, kw_only=True)
class RetiredPresenceShape:
    """A field whose PRESENCE used to select one mode and whose ABSENCE
    used to select another, replaced by a tagged union that defaults to
    the absent mode.

    One declaration per platform, stating what the operator's old document
    says and what it has to say now. The rewrite is rendered from the
    document rather than from a template, so the error shows an operator
    their own resource in the shape it now needs.
    """

    retired_field: str
    """The key that used to carry the choice (``service_principal``)."""

    union_field: str
    """The union that replaced it (``auth``)."""

    present_mode: str
    """The mode an operator who WROTE ``retired_field`` meant."""

    absent_mode: str
    """The mode an operator who OMITTED it meant, and the union's
    declared default now, so an omitting document loads unchanged."""

    scalar_field: str | None = None
    """Where a retired SCALAR's value goes inside the new arm.

    Set only for lima, whose ``vm_host: me@box`` was a bare string rather
    than a table, so its value moves into a named field (``host``) instead
    of its keys being folded in. ``None`` means the retired field was a
    table and its keys fold as they are.
    """

    def rewrite_for(self, value: object) -> str:
        """The exact replacement for a document that WROTE the retired
        field, values elided as ``...`` exactly as the sibling-shape
        rewrite elides them.

        The keys come from what the operator actually wrote, so a
        service principal with no ``secret`` does not get told to add
        one.

        A value with NO keys to transcribe (a bare ``service_principal:
        "oops"``, or an empty table) gets a bare ``...`` standing for the
        arm's own fields. Without it the rewrite reads
        ``auth: {mode: service-principal}``, a document the model rejects
        for the three keys that arm requires: advice that fails when
        applied verbatim, which is the defect class this whole break is
        being carried across. The marker is honest for every declaration
        below, because a retired field only ever carried a mode choice by
        carrying fields, and
        ``tests/capabilities/test_retired_shapes.py`` holds the present
        arm to having some.
        """
        if self.scalar_field is not None:
            return f"{self.union_field}: {{mode: {self.present_mode}, {self.scalar_field}: ...}}"
        keys = list(value) if isinstance(value, Mapping) else []
        elided = [f"{key}: ..." for key in keys] or ["..."]
        inner = ", ".join([f"mode: {self.present_mode}", *elided])
        return f"{self.union_field}: {{{inner}}}"

    @property
    def absent_rewrite(self) -> str:
        """The line a document that wrote ``retired_field: null`` writes
        in the null line's place: the mechanism it was relying on, made
        explicit. One line with no arm fields on purpose, because the
        absent-mode arms have none.
        """
        return f"{self.union_field}: {{mode: {self.absent_mode}}}"


type RetiredShape = RetiredPresenceShape


def retired_shape_error(
    shape: RetiredShape | None,
    config: object,
    owner: RefOwner,
    location: SourceLocation | None = None,
) -> None:
    """Refuse a config matching ``shape``, naming the exact rewrite;
    return silently for anything else.

    A config that wrote nothing is not refused at all: the union's
    declared default resolves it to the same mechanism omission always
    selected, so an omitting document was never broken and there is no
    absent case here.

    Runs BEFORE model validation, for the reason
    ``decode._reject_legacy_shape`` runs before it: the model layer
    answers the retired document with a bare unknown-key error that says
    nothing about where the choice the key carried has gone, and saying
    so is the whole job here.

    ``location`` frames the message the way every other manifest error is
    framed, through :func:`~agentworks.schema.located`, and it is the same
    location the validation path two lines down is given. Running earlier
    in the pipeline is not a reason for an error to arrive without a file
    and a line: these errors carry an operator across a break, so they
    are the LAST errors that should make someone grep for which document
    they are about. Defaulted for the construct-time caller, which is
    validating a config that came from code and has no declaration site.

    For ``RetiredPresenceShape``, a config that already carries the union field is left entirely alone,
    even when the retired field sits beside it. That document is a
    half-applied migration rather than a pre-migration one, and the model
    layer's unknown-key error against the stray field is already the
    precise answer; printing a rewrite would tell the operator to write
    something they have already written.

    **For a retired presence shape, explicit ``null`` names the ABSENT mode, so the branch is taken
    on the VALUE and not on key membership.** All three retired fields
    were optional, so a written ``null`` did exactly what omitting the
    key did: ``service_principal: null`` and ``credentials: null``
    selected the ambient chain, and ``vm_host: null`` ran ``limactl``
    locally. Branching on membership would answer the operator whose site
    was LOCAL with ``placement: {mode: ssh, host: ...}``, which is not a
    rewrite of their site but the opposite of it.

    The null document still errors, because a retired field is still a
    retired field, and it gets its own message because it needs its own
    edit: the null line has to GO, and the message says what the null was
    doing while it names the line to write. It resolves to the same arm
    the default now supplies, so strictly the write is belt beside
    braces, but advice that leaves the choice implicit while retiring
    the old implicit spelling would be a poor cure.
    """
    if shape is None or not isinstance(config, Mapping):
        return
    if shape.union_field in config:
        return
    written = config.get(shape.retired_field)
    if written is not None:
        raise ConfigError(
            located(
                location,
                f"{owner.display}: '{shape.retired_field}' is no longer a supported field; the choice it "
                f"used to carry by being present is written explicitly now: "
                f"{shape.rewrite_for(written)}",
            ),
            entity_kind=owner.kind,
            entity_name=owner.name,
            hint=RETIRED_SHAPE_HINT,
        )
    if shape.retired_field in config:
        raise ConfigError(
            located(
                location,
                f"{owner.display}: '{shape.retired_field}: null' is a retired spelling. It selected "
                f"'{shape.absent_mode}', exactly as omitting the key did, and ending that conflation is why "
                f"the field is gone; delete the null line and write the choice instead: "
                f"{shape.absent_rewrite}",
            ),
            entity_kind=owner.kind,
            entity_name=owner.name,
            hint=RETIRED_SHAPE_HINT,
        )
