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

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from agentworks.install_commands import SystemInstallCommandEntry, UserInstallCommandEntry
from agentworks.manifests.emit import document_schema

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


def test_test_exec_schema_describes_name_and_executable_path_semantics() -> None:
    description = SystemInstallCommandEntry.model_json_schema()["properties"]["test_exec"]["description"]

    assert "containing '/' is a path checked with 'test -x'" in description
    assert "bare name is resolved on PATH in the target user's login shell" in description


# -- What an operator reads when it is wrong ----------------------------------


def test_a_missing_command_says_it_is_required() -> None:
    """Over both kinds, unlike its neighbours: what it pins is that the
    diagnostic names the kind the operator wrote, which is the one fact
    the shared spec does not supply. One loop, because the expectation is
    that kind's own name and both come from one shared refusal."""
    misread = [
        (kind, got)
        for kind in _KINDS
        if (got := rejection(kind, "example", {})) != f"res.yaml:7: {kind}/example.command: is required"
    ]
    assert not misread


def test_multiple_tests_round_trip_for_both_kinds() -> None:
    spec = {"command": "true", "test_exec": "example", "test_dir": "~/example"}

    rows = [decode(kind, "example", spec) for kind in _KINDS]

    assert [(row.test_exec, row.test_dir) for row in rows] == [
        ("example", "~/example"),
        ("example", "~/example"),
    ]


def test_emitted_schemas_accept_multiple_tests_for_both_kinds() -> None:
    for kind in _KINDS:
        document = {
            "apiVersion": "agentworks/v1",
            "kind": kind,
            "metadata": {"name": "example"},
            "spec": {
                "command": "true",
                "test_exec": "example",
                "test_file": "~/example",
                "test_dir": "~/example.d",
            },
        }

        assert list(Draft202012Validator(document_schema(kind)).iter_errors(document)) == []


def test_an_empty_test_is_legal_beside_a_non_empty_test() -> None:
    spec = {"command": "true", "test_exec": "", "test_file": "~/example"}

    row = decode(_KIND, "example", spec)

    assert (row.test_exec, row.test_file) == ("", "~/example")


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
