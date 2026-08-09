"""The reference vocabulary the model layer produces.

``RefRelationship`` and :class:`ConfigReference` live HERE, in the schema
package, rather than beside the graph's ``ResourceReference``, and the
reason is the package's import rule: ``agentworks.schema`` must be a leaf,
importable from a capability module without dragging in the resource
layer. ``agentworks/resources/reference.py`` imports these two and
re-exports them, so every existing consumer spells them where it always
did and the direction runs one way only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RefRelationship(Enum):
    """What the referring Resource MEANS by pointing at the target.

    - ``USES``: a runtime need. The target must resolve for the referrer
      to work.
    - ``INHERITS``: source composition. The target's declaration is
      merged into the referrer's.

    The distinction is the relationship, never the target's kind: today
    a template is pointed at only to inherit from it, but a future
    uses-a-template edge would be misclassified by any filter that reads
    "points at a template" as "inherits from it".

    Defined here, beside the reference records that carry it, rather than
    with the field markers that declare it: it is the reference
    vocabulary's word, and this module is a leaf that the rest of
    ``agentworks/schema/`` imports, never the reverse.
    """

    USES = "uses"
    INHERITS = "inherits"


@dataclass(frozen=True)
class ConfigReference:
    """A resource reference implied by a modeled blob: the record
    ``extract_references`` produces from a model's reference-marked
    fields, and the record a capability's ``dependencies`` returns while
    that hand-rolled surface still exists.

    Sourceless by design: the consuming resource that owns the blob
    attaches itself as the ``source`` when it emits the corresponding
    ``ResourceReference`` (whoever hosts the config that names the
    resource emits the reference).

    ``relationship`` says what the referrer MEANS by the edge; it
    defaults to ``USES``, which is what every producer implies today.
    """

    kind: str
    name: str
    usage: str
    relationship: RefRelationship = RefRelationship.USES
