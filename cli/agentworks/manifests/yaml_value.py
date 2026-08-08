"""How a Python value renders on a YAML surface. One answer, three
surfaces.

The generated sample writes a value into a document, the field reference
prints one in a parenthetical, and the tree derives the value a sample
would write. All three are answering the same question, "what would an
operator type for this", and before this module they answered it three
ways: the sample dumped YAML, the reference printed Python ``repr`` (so a
boolean default read ``True`` where a document carries ``true``, and a
string default arrived wearing quotes), and the tree converted enum
members by hand in the one branch that happened to know about them, which
left an enum DEFAULT to reach pyyaml and raise ``RepresenterError`` out of
``agw resource sample``.

**Total, like the walkers it presents.** A value with no YAML
representation renders as its string form rather than raising: these
surfaces exist to teach, and a plugin whose config declares an exotic
default should get an imperfect line in its sample, not a traceback in
place of the whole document.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Final

import yaml
from pydantic import BaseModel

#: Wide enough that pyyaml never folds a scalar. A folded line would not
#: carry the leading `#` that makes a rendered sample inert, and an apt
#: source stanza is long enough to hit any realistic limit.
_NO_FOLDING: Final = 1 << 30

#: What pyyaml's safe dumper represents natively, and therefore what
#: :func:`_wire` may hand it untouched.
_REPRESENTABLE: Final = (str, bool, int, float, date, datetime)


def render_value(value: object) -> str:
    """``value`` as ONE line of YAML: what an operator would type for it.

    Flow style, so a list or a table sits on its key's own line
    (``apt: [zsh, ripgrep]``) rather than opening a block. That is how the
    hand-written samples wrote a short collection, it is what an operator
    edits in place, and it keeps one field to one line, which is what lets
    the same rendering serve a document line and a parenthetical.
    """
    dumped = yaml.safe_dump(_wire(value), default_flow_style=True, sort_keys=False, width=_NO_FOLDING)
    return dumped.strip().removesuffix("...").strip()


def _wire(value: object) -> object:
    """``value`` as a YAML document carries it, at any depth.

    An enum member becomes its value: a model's ``Literal`` choices are
    already the wire form and its enum members are not, which is the
    asymmetry that made "convert it where we notice" a bug waiting for
    the first enum-typed field with a default.

    A set becomes a sorted list. Sorted because a rendered sample has to
    be stable across runs or the tests that pin it are worthless, and by
    the rendering rather than by the value so that a mixed-type set
    orders at all.

    A model instance becomes the mapping a document writes for it, which
    is what a defaulted union arm is on this surface: ``{mode: local}``,
    not a Python ``repr``.
    """
    if isinstance(value, BaseModel):
        return _wire(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return _wire(value.value)
    if isinstance(value, Mapping):
        return {_wire(key): _wire(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((_wire(item) for item in value), key=repr)
    if isinstance(value, list | tuple):
        return [_wire(item) for item in value]
    if value is None or isinstance(value, _REPRESENTABLE):
        return value
    return str(value)
