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


def test_a_kind_hosting_no_capability_or_mapping_is_its_own_row() -> None:
    assert spec_model("apt-package") is row_model("apt-package")


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


#: One literal script per surface that seats for itself, each run in its own
#: interpreter that has not imported ``agentworks.plugins``. Literal rather
#: than assembled, so what runs in the subprocess is what a reader reads,
#: and each repeats the premise assertion because each is alone.
#:
#: EVERY ``seat_installed_plugins`` call site in the source needs an entry
#: here. ``reference.py``'s two had none while the suite stayed green,
#: because ``test_reference.py`` drives fixture models rather than the live
#: registry: deleting both lazy imports left `explain` quietly
#: dropping every plugin arm with nothing failing anywhere.
_SPEC_MODEL_CHECK = """
import sys

from agentworks.manifests.spec_model import spec_model
from agentworks.schema import iter_field_docs

assert "agentworks.plugins" not in sys.modules, "the plugins package was already imported"
expected = {"azure-vm", "aws-ec2", "gcp-gce", "proxmox"}

docs = {doc.path: doc for doc in iter_field_docs(spec_model("vm-site"))}
tags = {arm.tag for arm in docs[("platform", "root")].union_arms}
assert expected <= tags, f"spec_model is missing {sorted(expected - tags)} (got {sorted(tags)})"
"""

_EMITTED_SCHEMA_CHECK = """
import sys

from agentworks.manifests.emit import document_schema

assert "agentworks.plugins" not in sys.modules, "the plugins package was already imported"
expected = {"azure-vm", "aws-ec2", "gcp-gce", "proxmox"}

mapping = set(document_schema("vm-site")["$defs"]["VmPlatformConfig"]["discriminator"]["mapping"])
assert expected <= mapping, f"emitted schema is missing {sorted(expected - mapping)}"
"""

#: `agw resource explain vm-platform`: the capability-kind index,
#: which reaches the descriptor table without going through spec_model.
_DESCRIBE_KIND_INDEX_CHECK = """
import sys

from agentworks.manifests.reference import reference_for

assert "agentworks.plugins" not in sys.modules, "the plugins package was already imported"
expected = {"azure-vm", "aws-ec2", "gcp-gce", "proxmox"}

listed = {alt.name for alt in reference_for("vm-platform").alternatives}
assert expected <= listed, f"explain is missing {sorted(expected - listed)} (got {sorted(listed)})"
"""

#: `agw resource explain vm-platform/<name>`: one implementation,
#: resolved against the kind's registry. A plugin's implementation is one
#: this host HAS, so it is describable whether or not config opted in.
_DESCRIBE_KIND_IMPL_CHECK = """
import sys

from agentworks.manifests.reference import reference_for

assert "agentworks.plugins" not in sys.modules, "the plugins package was already imported"

for name in ("aws-ec2", "azure-vm", "gcp-gce", "proxmox"):
    assert reference_for(f"vm-platform/{name}").implementation == name
"""

_FRESH_SEATING_CHECKS = (
    _SPEC_MODEL_CHECK,
    _EMITTED_SCHEMA_CHECK,
    _DESCRIBE_KIND_INDEX_CHECK,
    _DESCRIBE_KIND_IMPL_CHECK,
)
_FRESH_SEATING_IDS = ("spec-model", "emitted-schema", "explain-index", "explain-impl")


@pytest.mark.parametrize("script", _FRESH_SEATING_CHECKS, ids=_FRESH_SEATING_IDS)
def test_the_plugin_platforms_are_present_in_a_FRESH_interpreter(script: str) -> None:
    """Live from the registry, plugins included, is what every surface
    below promises, and none of them builds a registry. Before the seating
    step ``spec_model`` calls, `agw resource schema vm-site` emitted lima
    and wsl2 while three platform plugins shipped in-tree.

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

    **One interpreter per surface, and that is load-bearing too.** Seating
    is process-wide, so the FIRST surface a script touches seats for every
    surface after it: checking them in sequence would prove the first one
    seats and prove nothing at all about the rest. Adding the
    ``explain`` checks to the end of the spec-model script passed
    with both of ``reference.py``'s seating calls deleted, which is exactly
    the hole they were being added to close. A surface belongs in its own
    script, or it is not being tested.

    Enablement is deliberately not consulted: a plugin's implementations
    seat whether or not config opts in, and a disabled platform is still a
    platform an operator may be reading about.
    """
    result = subprocess.run(  # noqa: S603  (our own interpreter, a literal script)
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_hosted_capability_answers_for_the_tagged_kinds_only() -> None:
    assert hosted_capability("vm-site") is not None
    assert hosted_capability("vm-site").kind == "vm-platform"  # type: ignore[union-attr]
    assert hosted_capability("secret-source") is not None
    assert hosted_capability("secret-source").kind == "secret-backend"  # type: ignore[union-attr]
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
