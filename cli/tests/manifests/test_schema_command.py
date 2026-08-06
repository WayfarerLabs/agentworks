"""``agw resource schema``: what it prints, what it writes, how it refuses.

End-to-end through the real CLI entry point rather than against the
service functions (``tests/manifests/test_emit.py`` covers those), because
what this file is about is the surface: a bare invocation having one
obvious answer, ``--write`` landing where the modeline says it will, and a
bad invocation producing a clean ``Error:`` line rather than a traceback.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator

from agentworks.manifests.emit import ENVELOPE_SCHEMA_FILENAME, SCHEMA_DIRNAME, emittable_kinds
from agentworks.manifests.loader import RESOURCES_DIRNAME

if TYPE_CHECKING:
    from pathlib import Path


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Run the command through the real entry point; return the exit code.

    Typer's standalone mode exits on the way out whether the command
    succeeded or not, so the code is what a caller reads, not the return.
    """
    from agentworks import cli as cli_mod

    monkeypatch.setattr("sys.argv", ["agentworks", "resource", "schema", *argv])
    monkeypatch.setenv("AGW_DEBUG", "")
    with pytest.raises(SystemExit) as exc:
        cli_mod.main()
    return 1 if exc.value.code is None else int(exc.value.code)


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal config directory, which is all ``--write`` needs: it
    loads settings only, so it works against a config that still fails
    resource validation.

    ``CONFIG_PATH`` as well as ``CONFIG_DIR``: the loader re-imports the
    former by name, and the two are independent module attributes rather
    than one derived from the other.
    """
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "config.toml")
    (tmp_path / "id.pub").write_text("ssh-ed25519 AAAA...")
    (tmp_path / "id").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    (tmp_path / "config.toml").write_text(
        f"""\
[operator]
ssh_public_key = "{(tmp_path / "id.pub").as_posix()}"
ssh_private_key = "{(tmp_path / "id").as_posix()}"
"""
    )
    return tmp_path


def test_a_bare_invocation_prints_the_any_kind_schema(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unlike `resource sample`, a bare invocation is not an error: a
    manifest file can hold any kind, so "the schema for a manifest" has
    exactly one right answer, and it is the file the modeline points at
    most often."""
    assert _run(monkeypatch) == 0
    schema = json.loads(capsys.readouterr().out)
    Draft202012Validator.check_schema(schema)
    assert schema["discriminator"]["propertyName"] == "kind"


def test_a_kind_prints_that_kinds_schema(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(monkeypatch, "vm-template") == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["properties"]["kind"]["const"] == "vm-template"


def test_write_lands_where_the_modeline_says(
    configured: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The destination is fixed rather than the operator's to choose,
    because the modeline stamped into manifests refers to it by that
    relative path."""
    assert _run(monkeypatch, "--write") == 0
    schema_dir = configured / RESOURCES_DIRNAME / SCHEMA_DIRNAME
    written = {path.name for path in schema_dir.iterdir()}
    assert written == {ENVELOPE_SCHEMA_FILENAME, *(f"{kind}.schema.json" for kind in emittable_kinds())}
    assert str(schema_dir) in capsys.readouterr().out


def test_write_with_a_kind_is_a_clean_refusal(
    configured: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partial set would leave some manifest's modeline pointing at a
    file that is not there, so the kind is refused rather than
    ignored."""
    assert _run(monkeypatch, "secret", "--write") == 1
    err = capsys.readouterr().err
    assert "writes the whole schema set" in err
    assert "Traceback" not in err
    assert not (configured / RESOURCES_DIRNAME / SCHEMA_DIRNAME).exists()


@pytest.mark.parametrize(
    ("argument", "expected"),
    [("nope", "unknown kind"), ("vm-platform", "capability kind")],
)
def test_a_bad_kind_is_a_clean_domain_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argument: str,
    expected: str,
) -> None:
    """Issue #276's contract, which is why the kind argument is a plain
    string and not a click.Choice: anything an operator types reaches the
    service layer and comes back as one `Error:` line."""
    assert _run(monkeypatch, argument) == 1
    err = capsys.readouterr().err
    assert expected in err
    assert "Traceback" not in err
