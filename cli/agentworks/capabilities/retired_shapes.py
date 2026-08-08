"""Retired config shapes, refused BY NAME with the exact rewrite.

RELEASE-SCOPED, like ``manifests/decode.py``'s retired sibling shape and
the guide both of them name. This module exists to carry operators across
one break and is meant to be DELETED once 0.14 is far enough back that a
generic "unknown field" is a good enough answer. Nothing else depends on
it: remove the module, the two call sites in
:mod:`agentworks.capabilities.config`, and the
:attr:`~agentworks.capabilities.base.Capability.retired_shape`
declarations, and the framework is unchanged.

**The defect it is the receipt for.** Three platforms used to express a
mode CHOICE through the presence or absence of an optional block: azure's
``service_principal``, aws's ``credentials``, lima's ``vm_host``. Writing
the block picked one mechanism, omitting it picked another, and no
document could tell "I chose the omitted one" from "I never configured
this". Each is now a required tagged union, so both choices are written
down.

That makes this a total break: EVERY existing manifest for those three
platforms fails, including the ones that are wrong in no way except that
they predate the union. The ABSENT case is the one that needs the most
help, because the operator's document does not contain the offending
text: they wrote nothing, and an error about a field they never typed
reads as "you deleted something" unless it says otherwise. So the absent
case is a first-class message here, not an afterthought on the present
one.
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
    'See "Authentication and placement are declared, not inferred" in docs/guides/upgrading-to-0.14.md.'
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
    used to select another, replaced by a required tagged union.

    One declaration per platform, stating what the operator's old document
    says and what it has to say now. The rewrite is rendered from the
    document rather than from a template, so the error shows an operator
    their own resource in the shape it now needs.
    """

    retired_field: str
    """The key that used to carry the choice (``service_principal``)."""

    union_field: str
    """The required union that replaced it (``auth``)."""

    present_mode: str
    """The mode an operator who WROTE ``retired_field`` meant."""

    absent_mode: str
    """The mode an operator who OMITTED it meant."""

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
        """The exact replacement for a document that wrote NOTHING.

        The arms with no fields of their own are on purpose: what the
        operator has to add is one line saying which mechanism they were
        relying on all along.
        """
        return f"{self.union_field}: {{mode: {self.absent_mode}}}"


def retired_shape_error(
    shape: RetiredPresenceShape | None,
    config: object,
    owner: RefOwner,
    location: SourceLocation | None = None,
) -> None:
    """Refuse a config written in ``shape``'s retired spelling, naming the
    exact rewrite; return silently for anything else.

    Runs BEFORE model validation, for the reason
    ``decode._reject_legacy_shape`` runs before it: the model layer
    answers the retired document with two problems it has no reason to
    connect (an unknown ``service_principal`` key and a missing ``auth``),
    and connecting them is the whole job here.

    ``location`` frames the message the way every other manifest error is
    framed, through :func:`~agentworks.schema.located`, and it is the same
    location the validation path two lines down is given. Running earlier
    in the pipeline is not a reason for an error to arrive without a file
    and a line: these two carry an operator across a break that reaches
    every azure, aws, and lima manifest, so they are the LAST errors that
    should make someone grep for which document they are about. Defaulted
    for the construct-time caller, which is validating a config that came
    from code and has no declaration site.

    A config that already carries the union field is left entirely alone,
    even when the retired field sits beside it. That document is a
    half-applied migration rather than a pre-migration one, and the model
    layer's unknown-key error against the stray field is already the
    precise answer; printing a rewrite would tell the operator to write
    something they have already written.

    **An explicit ``null`` selects the ABSENT mode, so the branch is
    taken on the VALUE and not on key membership.** All three retired
    fields were optional, so a written ``null`` did exactly what omitting
    the key did: ``service_principal: null`` and ``credentials: null``
    selected the ambient chain, and ``vm_host: null`` ran ``limactl``
    locally. Branching on membership answers the operator whose site was
    LOCAL with ``placement: {mode: ssh, host: ...}``, which is not a
    rewrite of their site but the opposite of it.

    That is worth calling out rather than folding into the membership
    test, because it is the very conflation this break exists to end: a
    written ``null`` and a missing key meant one thing, one surface
    disagreed, and every union below is the fix. Advice that reproduces
    the conflation while announcing the cure is worse than no advice.

    It gets its own message rather than sharing the absent one, because
    the two documents need different edits. The absent message says
    nothing was deleted and one line is added, and for a document that
    WROTE ``vm_host: null`` both halves are false: that line has to go,
    and leaving it while adding the union yields
    ``vm_host: unknown field`` on the next load. Three spellings of the
    old choice, three edits, three messages.
    """
    if shape is None or not isinstance(config, Mapping) or shape.union_field in config:
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
                f"{owner.display}: '{shape.union_field}' is required and this resource does not declare it. "
                f"'{shape.retired_field}: null' selected '{shape.absent_mode}', exactly as omitting the key "
                f"did, and that is the conflation the required '{shape.union_field}' ends; delete the null "
                f"line and write the choice instead: {shape.absent_rewrite}",
            ),
            entity_kind=owner.kind,
            entity_name=owner.name,
            hint=RETIRED_SHAPE_HINT,
        )
    raise ConfigError(
        located(
            location,
            f"{owner.display}: '{shape.union_field}' is required and this resource does not declare it. "
            f"Omitting '{shape.retired_field}' used to mean '{shape.absent_mode}'; that choice is written "
            f"down now rather than inferred from what is missing, so nothing was deleted from your document "
            f"and one line is added to it: {shape.absent_rewrite}",
        ),
        entity_kind=owner.kind,
        entity_name=owner.name,
        hint=RETIRED_SHAPE_HINT,
    )
