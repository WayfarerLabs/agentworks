"""The per-kind model assembly shared by emission and the renderer.

The point of this module having its own tests is the SHARING: emission and
the sample / field-reference renderer must describe the same document, and
the only way to guarantee that is for both to read one model. These pin
that the model is what both need, including the capability splice.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from agentworks.errors import ValidationError
from agentworks.manifests.spec_model import (
    class_name,
    declarable_kinds,
    hosted_capability,
    row_model,
    spec_model,
)
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import iter_field_docs


def test_declarable_kinds_is_the_registry_category_and_nothing_else() -> None:
    declarable = {name for name, handler in KIND_REGISTRY.items() if handler.category == "declarable"}
    assert set(declarable_kinds()) == declarable
    assert list(declarable_kinds()) == sorted(declarable)


def test_a_kind_hosting_no_capability_is_its_own_row() -> None:
    assert spec_model("secret") is row_model("secret")


def test_a_hosting_kind_gets_the_union_spliced_onto_its_naming_field() -> None:
    """The row carries a ``CapabilityBlock`` (``name`` plus extra keys the
    row does not own), which describes nothing an operator can act on. The
    spliced model carries the union, so the walk both surfaces read reaches
    every platform's own fields."""
    docs = {doc.path: doc for doc in iter_field_docs(spec_model("vm-site"))}
    arms = docs[("platform", "root")].union_arms

    assert {arm.tag for arm in arms} >= {"lima", "wsl2"}
    # Not merely the block's own `name`, which is all the unspliced row has.
    assert ("platform", "name") not in docs


def test_the_splice_keeps_an_optional_capability_block_optional() -> None:
    """``session-template``'s harness_integration is nullable, and
    ``harness_integration: null`` loads. A splice that dropped the null arm
    would describe a document the loader accepts as invalid."""
    docs = {doc.path: doc for doc in iter_field_docs(spec_model("session-template"))}

    assert docs[("harness_integration",)].required is False
    assert docs[("harness_integration",)].default is None


def test_the_plugin_platforms_are_in_the_union_in_a_FRESH_interpreter() -> None:
    """Live from the registry, plugins included, is what both surfaces
    promise, and neither builds a registry. Before the seating step
    ``spec_model`` calls, `agw resource schema vm-site` emitted lima and
    wsl2 while three platform plugins shipped in-tree.

    **This runs in a subprocess, and it has to.** Seating is an import side
    effect of ``agentworks.plugins``, so the state it guards against
    (nothing seated yet) cannot be restored once anything in the
    interpreter has imported that package. Three sibling test modules
    import it at module scope for ``Plugin`` / ``seated_plugin``, so
    collection seats the whole process before any assertion runs: the
    in-process version of this test passed with the seating step neutered
    to a no-op, which is the same defect this step found in emission, one
    layer down. No fixture can fix that, because there is no pre-import
    state to return to. A fresh interpreter is the only place the
    assertion means anything, so please do not simplify it back.

    Enablement is deliberately not consulted: a plugin's implementations
    seat whether or not config opts in, and a disabled platform is still a
    platform an operator may be reading about.
    """
    result = subprocess.run(  # noqa: S603  (our own interpreter, a literal script)
        [sys.executable, "-c", _FRESH_SEATING_CHECK],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


#: The whole check, as a script: two surfaces, one interpreter that has not
#: imported ``agentworks.plugins``. Spelled as a literal rather than
#: assembled, so what runs in the subprocess is what a reader reads.
_FRESH_SEATING_CHECK = """
import sys

from agentworks.manifests.spec_model import spec_model
from agentworks.manifests.emit import document_schema
from agentworks.schema import iter_field_docs

# The premise: nothing on the import path to either surface has seated a
# plugin. If this fails, the rest of the script proves nothing.
assert "agentworks.plugins" not in sys.modules, "the plugins package was already imported"

expected = {"azure-vm", "aws-ec2", "proxmox"}

docs = {doc.path: doc for doc in iter_field_docs(spec_model("vm-site"))}
tags = {arm.tag for arm in docs[("platform", "root")].union_arms}
assert expected <= tags, f"spec_model is missing {sorted(expected - tags)} (got {sorted(tags)})"

mapping = set(document_schema("vm-site")["$defs"]["VmPlatformConfig"]["discriminator"]["mapping"])
assert expected <= mapping, f"emitted schema is missing {sorted(expected - mapping)}"
"""


def test_hosted_capability_answers_for_the_tagged_kinds_only() -> None:
    assert hosted_capability("vm-site") is not None
    assert hosted_capability("vm-site").kind == "vm-platform"  # type: ignore[union-attr]
    # A kind that hosts nothing, and a kind whose capability is selected by
    # a map key (secret-backend under `backend_mappings`), which has no
    # discriminator and so no union to splice.
    assert hosted_capability("vm-template") is None
    assert hosted_capability("secret") is None


def test_a_capability_kind_has_no_row_to_describe() -> None:
    with pytest.raises(ValidationError, match="declares no spec model"):
        row_model("vm-platform")


def test_class_name_matches_what_pydantic_keys_defs_by() -> None:
    assert class_name("vm-site") == "VmSite"
    assert class_name("secret") == "Secret"
