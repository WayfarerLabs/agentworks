"""The system- and user-install-command spec models.

The two kinds are field-identical and share one authored spec base
(``install_commands._InstallCommandEntry``); each is an empty subclass of
it, so every field, every validator and every message below is authored
once. So the behavior is exercised through ONE kind and the sharing is
pinned structurally by :func:`test_the_two_kinds_declare_one_shared_spec`,
rather than by running each assertion twice: doubling the suite would
catch a divergence between the two subclasses, which is the same and only
thing the structural test catches, and would leave a reader thinking the
two are independently authored.

The one genuinely per-kind fact is that a diagnostic names the kind the
operator wrote, and that keeps its sweep over both.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agentworks.install_commands import SystemInstallCommandEntry, UserInstallCommandEntry

from ._specs import WHERE, decode, rejection

_KINDS = ["system-install-command", "user-install-command"]

#: The kind the behavior below is exercised through. Either would do; the
#: structural test above is what makes that true.
_KIND = "system-install-command"


def test_the_two_kinds_declare_one_shared_spec() -> None:
    """What lets every rule below be checked through one kind.

    Field-for-field identity rather than "both subclass the base", because
    a subclass that ADDS a field is still a subclass, and an added or
    retyped field on one kind is exactly the divergence an operator could
    not predict. Compared as annotation-plus-default per field, so a
    widened type or a changed default fails here too.
    """

    def surface(model: type[BaseModel]) -> dict[str, object]:
        return {name: (field.annotation, repr(field.default)) for name, field in model.model_fields.items()}

    assert SystemInstallCommandEntry.__bases__ == UserInstallCommandEntry.__bases__
    assert surface(SystemInstallCommandEntry) == surface(UserInstallCommandEntry)
    # Separate classes, because the Registry keys rows by type and these are
    # two kinds with two miss policies. Compared by name: mypy reads the
    # identity check itself as the answer.
    assert SystemInstallCommandEntry.__name__ != UserInstallCommandEntry.__name__


def test_every_field_round_trips() -> None:
    spec = {"command": "curl -fsSL https://example.test | sh", "path": ["~/.local/bin"], "test_exec": "example"}

    assert decode(_KIND, "example", dict(spec), description="install example") == SystemInstallCommandEntry(
        name="example", description="install example", declared_at=WHERE, **spec
    )


def test_the_optional_fields_default() -> None:
    row = decode(_KIND, "example", {"command": "true"})

    assert (row.path, row.test_exec, row.test_file, row.test_dir, row.description) == ([], None, None, None, None)


# -- What an operator reads when it is wrong ----------------------------------


@pytest.mark.parametrize("kind", _KINDS)
def test_a_missing_command_says_it_is_required(kind: str) -> None:
    """Swept over both kinds, unlike its neighbours: what it pins is that
    the diagnostic names the kind the operator wrote, which is the one
    fact the shared spec does not supply."""
    assert rejection(kind, "example", {}) == f"res.yaml:7: {kind}/example.command: is required"


def test_two_tests_are_refused_by_name() -> None:
    spec = {"command": "true", "test_exec": "example", "test_dir": "~/example"}

    assert rejection(_KIND, "example", spec) == (
        f"res.yaml:7: {_KIND}/example: at most one of test_exec, test_file, test_dir may be set; "
        f"this one sets test_exec, test_dir"
    )


def test_an_empty_test_says_which_key_to_delete() -> None:
    """``test_exec: ""`` beside a real ``test_file`` used to be legal (the
    empty string normalized away before the count), so this pair is one of
    the loads this phase newly breaks.

    Naming the rule alone is not enough to act on: nothing in "at most one
    of test_exec, test_file, test_dir may be set" says the EMPTY one is
    what newly counts, which makes deleting the meaningful ``test_file``
    exactly as plausible a reading.
    """
    spec = {"command": "true", "test_exec": "", "test_file": "~/example"}

    assert rejection(_KIND, "example", spec) == (
        f"res.yaml:7: {_KIND}/example: at most one of test_exec, test_file, test_dir may be set; "
        f"this one sets test_exec (empty string), test_file. An empty string counts as set, so "
        f"delete test_exec rather than blanking it"
    )


def test_a_bare_test_key_keeps_its_remedy() -> None:
    """``test`` is the mistake operators actually make. As a plain unknown
    key it would name the valid fields but not say which of the three to
    reach for, so it keeps its own steer."""
    assert rejection(_KIND, "example", {"command": "true", "test": "example"}) == (
        f"res.yaml:7: {_KIND}/example: 'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'."
    )


def test_an_unknown_key_names_the_fields_that_are_valid() -> None:
    assert rejection(_KIND, "example", {"command": "true", "cmd": "true"}) == (
        f"res.yaml:7: {_KIND}/example.cmd: unknown field; "
        f"expected one of: command, path, test_dir, test_exec, test_file"
    )


def test_a_non_string_command_is_no_longer_coerced() -> None:
    """The loader spelled ``str(...)`` around this, so ``command: 7``
    installed the string "7". Strict mode says what it is instead."""
    assert rejection(_KIND, "example", {"command": 7}) == f"res.yaml:7: {_KIND}/example.command: must be a string"
