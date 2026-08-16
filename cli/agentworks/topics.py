"""``TopicProse``: the authored prose beside a kind or a capability.

Every schema fact an operator reads is DERIVED (from the model, by
``iter_field_docs``). This module carries the one thing that cannot be:
the paragraphs a human wrote about what a kind or a capability is FOR.
It is the whole authored layer, and it is deliberately tiny.

The prose is written in a voice-neutral reference register for
``agw resource describe-kind``. An overview that says "let's start by"
reads wrong when someone is looking up a field.

**Where the summary went.** It already exists: every resource kind
declares ``ResourceKind.description`` (the line
``agw resource kinds`` prints) and every capability implementation
declares ``description`` (the line its registry row carries). Restating
either here would be two authored strings for one fact, which is the drift
this whole effort exists to end. A presenter that wants the summary reads
the description.

**Colocation is the registration.** A kind's prose is a field on its kind
strategy, in the domain package that owns the kind; a capability
implementation's is a ``ClassVar`` on the implementation class; a plugin's
rides the classes it registers. There is no catalog and no registry here,
because the command derives its target inventory from the kind and
capability registries.

Prose is INERT. No placeholders, interpolation, or templating.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from agentworks.errors import StateError


@dataclass(frozen=True)
class TopicProse:
    """What a human wrote about one kind or one capability implementation.

    Carries NO field lists, and that is a rule rather than a habit: every
    field fact reaches both surfaces from the model, so a paragraph
    enumerating fields would be the one part of a rendered page that could
    go stale.
    """

    title: str
    """The display title (``"VM sites"``), used as the heading of the
    field reference. The identifier an operator types (``vm-site``) is not
    this: it is read off the kind."""

    overview: str
    """Markdown prose: what this is, what it is for, and what an operator
    should know before writing one.

    Normalized with :func:`inspect.cleandoc` at construction, so it can be
    written as an indented triple-quoted literal at the declaration site
    without every author remembering to dedent.
    """

    def __post_init__(self) -> None:
        cleaned = inspect.cleandoc(self.overview)
        if not self.title.strip() or not cleaned:
            # Enforced rather than documented: an empty title or overview
            # would render as a blank section, and the contract's answer
            # for "nothing useful to say" is to contribute no prose at
            # all, not to contribute empty prose.
            raise StateError("TopicProse requires a non-empty title and overview")
        object.__setattr__(self, "overview", cleaned)


def summary_of(subject: object) -> str | None:
    """``subject``'s one-line summary: the topic contract's ``summary``.

    The other half of :func:`prose_of`, and the reason this module exports
    a pair rather than one function. A consumer collecting the contract's
    three authored strings needs to know where the third one comes from,
    and "read the ``description`` a kind strategy or a capability class
    declares" is a rule, not an obvious fact. Exported so the rule has one
    implementation instead of being re-derived by each collector.
    """
    summary = getattr(subject, "description", None)
    return summary if isinstance(summary, str) and summary else None


def prose_of(subject: object) -> TopicProse | None:
    """``subject``'s prose, or ``None`` when it declares none.

    Reads the attribute rather than requiring a declaration, because the
    four capability kinds' implementation contracts are not uniform: three
    are ABCs that inherit ``Capability.prose`` (defaulting to ``None``),
    while a secret backend satisfies a Protocol and derives from nothing,
    so there is no base to carry the default for it.

    Optional by design on this side. The topic contract says a participant
    with no useful content contributes nothing, and a plugin author must be
    able to register a capability without writing an essay. Kinds are the
    other side of that: a kind's prose is required by the ``ResourceKind``
    protocol, because every kind the app defines is describable.
    """
    prose = getattr(subject, "prose", None)
    return prose if isinstance(prose, TopicProse) else None
