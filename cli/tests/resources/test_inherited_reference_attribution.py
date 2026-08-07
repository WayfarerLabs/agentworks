"""An inherited edge names the template that DECLARED it, not the one
that publishes it.

The consequence of FR17's producing half that has to be paid for
separately: a child now publishes the runtime needs of its merged
declaration, so ``ResourceReference.source`` on such an edge is the child
even when the name was written by an ancestor. Every message that answers
"who wants this?" therefore reads ``declarer`` instead: the miss-policy
error, the auto-declared row's origin and description, and describe's
"Referenced by:" line.

**Every test here publishes the CHILD FIRST**, because that is the
ordering that exposes it. With the parent first, its own edge reaches the
target before the child's and the answer comes out right by accident, so
a test that did not control the order would pass over broken code.
Publishing order is otherwise arbitrary (it follows manifest discovery),
which is the second half of the problem: blame that depends on it moves
when a file is renamed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.errors import ConfigError
from agentworks.resources import Origin, Registry
from agentworks.resources.render import format_reference_entry
from agentworks.vms.template import VMTemplate


def _origin(name: str) -> Origin:
    return Origin.operator_declared(file=Path(f"{name}.yaml"), line=1)


def _child_first(parent: VMTemplate, child: VMTemplate) -> Registry:
    """A registry publishing ``child`` before ``parent``, finalized."""
    registry = Registry.empty()
    registry.add("vm-template", child.name, child, _origin(child.name))
    registry.add("vm-template", parent.name, parent, _origin(parent.name))
    registry.finalize()
    return registry


def test_a_miss_on_an_inherited_name_blames_the_template_that_wrote_it() -> None:
    """The blocking case: the operator is told which file to open, and
    ``kid.yaml`` has no ``apt_packages`` in it at all."""
    parent = VMTemplate(name="base", apt_packages=["nope"])
    child = VMTemplate(name="kid", inherits=["base"])
    with pytest.raises(ConfigError) as exc:
        _child_first(parent, child)
    assert "vm-template 'base' references unknown apt-package 'nope'" in str(exc.value)


def test_an_auto_declared_row_records_the_declaring_template_as_its_origin() -> None:
    from agentworks.env.entry import EnvEntry

    parent = VMTemplate(name="base", env={"BASE": EnvEntry(secret="base-secret")})
    child = VMTemplate(name="kid", inherits=["base"])
    registry = _child_first(parent, child)

    row = registry.lookup("secret", "base-secret")
    assert row.origin.source == ("vm-template", "base")
    assert row.description == "(auto) the BASE env var for vm-template/base"


def test_two_descendants_of_one_declarer_do_not_read_as_two_declarations() -> None:
    """``(and N more)`` counts DECLARERS, so a name written once and
    inherited twice is one declaration, not three."""
    from agentworks.env.entry import EnvEntry

    parent = VMTemplate(name="base", env={"BASE": EnvEntry(secret="base-secret")})
    registry = Registry.empty()
    for name in ("kid-a", "kid-b"):
        registry.add("vm-template", name, VMTemplate(name=name, inherits=["base"]), _origin(name))
    registry.add("vm-template", "base", parent, _origin("base"))
    registry.finalize()

    assert registry.lookup("secret", "base-secret").description == "(auto) the BASE env var for vm-template/base"


def test_describe_says_an_inherited_reference_was_inherited() -> None:
    """ "Referenced by: vm-template/kid" is true and, on its own, sends the
    operator to the wrong file."""
    from agentworks.env.entry import EnvEntry

    parent = VMTemplate(name="base", env={"BASE": EnvEntry(secret="base-secret")})
    child = VMTemplate(name="kid", inherits=["base"])
    registry = _child_first(parent, child)

    lines = [format_reference_entry(entry) for entry in registry.graph.dependents_of("secret", "base-secret")]
    assert "vm-template/kid: the BASE env var (inherited from vm-template/base)" in lines
    # The parent's own edge is not dressed up as inherited.
    assert "vm-template/base: the BASE env var" in lines


def test_a_row_that_declared_the_name_itself_is_blamed_for_it() -> None:
    """The override case, so the fix cannot be "always blame an ancestor":
    ``kid`` wrote this one, and a child-first publish must still say so."""
    parent = VMTemplate(name="base")
    child = VMTemplate(name="kid", inherits=["base"], apt_packages=["nope"])
    with pytest.raises(ConfigError) as exc:
        _child_first(parent, child)
    assert "vm-template 'kid' references unknown apt-package 'nope'" in str(exc.value)
