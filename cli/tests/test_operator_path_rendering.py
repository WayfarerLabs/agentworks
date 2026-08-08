"""Every host path an operator reads is spelled the same way.

One invariant over N sites, for the same reason
``tests/manifests/test_error_framing.py`` is one invariant over N refusal
sites: the spelling of a path is a property of the operator surface as a
whole, not a fact about any single message. Per-site assertions have now
failed twice. First for the nine manifest refusal sites, then for this
family, where ``agw doctor`` printed an absolute config path two lines
above a home-relative manifest path while the whole suite stayed green.

**Why the suite could not see it.** ``tmp_path`` is never under ``$HOME``,
so ``format_host_path`` hits its no-common-prefix fallback and returns the
absolute path: the correct rendering and a hand-rolled ``f"{path}"`` are
byte-identical, and an assertion written against either passes for both.
Every test here therefore puts the whole config tree UNDERNEATH a patched
home, which is the shape a real operator has and the only shape in which
the two renderings differ.

**Why each test also asserts what it saw.** A "no absolute path leaked"
check passes trivially against output that names no path at all, so a
surface that stops rendering a path (a refactor, a skipped branch, a
degraded-mode early return) would silently take its own guard with it.
Each test below pins the specific rows or lines it expects to find before
it checks how they are spelled.
"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

# A manifest the loader refuses, so the Configuration group renders a
# `[FAIL] Manifest:` row whose framing is already correct. It is the
# control in this experiment: it and the Config file row name files in
# the same directory, so any difference in their spelling is the defect.
_BAD_MANIFEST = dedent("""\
    apiVersion: agentworks/v1
    kind: vm-template
    metadata:
      name: default
    spec:
      memory_gib: 8
    """)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A populated ``$HOME`` with the config tree inside it.

    ``HOME`` is set as well as ``Path.home`` patched, and the two are not
    redundant: ``Path.home()`` is what ``format_host_path`` consults,
    while ``Path.expanduser()`` (which turns the ``~/.ssh/id_ed25519`` an
    operator writes in config.toml into a real path) reads the
    environment. Patching only one leaves half the flow pointed at the
    developer's real home.

    ``CONFIG_DIR`` / ``CONFIG_PATH`` / ``LOG_DIR`` are module-level
    constants evaluated at import, long before any patching here, so they
    are re-pointed explicitly rather than derived from the patched home.
    """
    import agentworks.config
    import agentworks.config.validation
    import agentworks.ssh

    root = tmp_path / "home"
    config_dir = root / ".config" / "agentworks"
    resources = config_dir / "resources"
    resources.mkdir(parents=True)
    ssh_dir = root / ".ssh"
    ssh_dir.mkdir()

    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA...")
    (ssh_dir / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    (config_dir / "config.toml").write_text(
        dedent("""\
        [operator]
        ssh_public_key = "~/.ssh/id_ed25519.pub"
        ssh_private_key = "~/.ssh/id_ed25519"
        """)
    )
    (resources / "vm-templates.yaml").write_text(_BAD_MANIFEST)

    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    monkeypatch.setattr(agentworks.config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(agentworks.config, "CONFIG_PATH", config_dir / "config.toml")
    monkeypatch.setattr(agentworks.config.validation, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(agentworks.config.validation, "CONFIG_PATH", config_dir / "config.toml")
    monkeypatch.setattr(agentworks.ssh, "LOG_DIR", config_dir / "logs")
    yield root


def _assert_home_relative(text: str, home: Path, *, label: str) -> None:
    """``text`` names paths, and names all of them home-relative."""
    assert "~/" in text, f"{label}: rendered no path at all, so it cannot witness the invariant: {text!r}"
    assert str(home) not in text, f"{label}: an absolute path under $HOME leaked into {text!r}"


# -- The doctor screen ---------------------------------------------------------


def test_doctor_spells_every_path_on_one_screen_the_same_way(home: Path) -> None:
    """The reported bug, as an assertion over the whole report.

    Doctor is the surface where the inconsistency was visible, because it
    is the one place that renders the config path, the SSH key paths and
    a manifest error together in a single group.
    """
    from agentworks.doctor import run_checks

    report = run_checks()
    rows = [c for g in report.groups for c in g.checks]
    rendered = {c.name: " ".join(filter(None, (c.message, c.hint))) for c in rows}

    # Members first: these rows are the subjects of the experiment, and a
    # report missing any of them proves nothing about spelling.
    for name in ("Config file", "SSH public key", "SSH private key", "Manifest"):
        assert name in rendered, f"doctor rendered no {name!r} row; the guard has lost its subject"

    for name, text in rendered.items():
        assert str(home) not in text, f"doctor row {name!r} shows an absolute path under $HOME: {text!r}"

    # And the rows that DO name a file all agree on how to spell it.
    for name in ("Config file", "SSH public key", "SSH private key", "Manifest"):
        assert "~/" in rendered[name], f"doctor row {name!r} named no path: {rendered[name]!r}"


# -- The commands that write the files whose errors doctor reports -------------

#: ``(label, argv, expected substring)``. The substring pins the line that
#: is supposed to carry a path, so a command that stops emitting it fails
#: here rather than passing an emptily-satisfied absence check.
_PATH_PRINTING_COMMANDS = [
    ("config init", ["config", "init"], "Config already exists:"),
    ("resource sample --write", ["resource", "sample", "vm-template", "--write", "s.yaml"], "sample to"),
    ("resource schema --write", ["resource", "schema", "--write"], "schemas to"),
    ("completion install", ["completion", "install", "--shell", "bash"], "Installed to"),
]


@pytest.mark.parametrize(
    ("label", "argv", "expected"),
    _PATH_PRINTING_COMMANDS,
    ids=[label for label, _, _ in _PATH_PRINTING_COMMANDS],
)
def test_operator_commands_name_files_home_relative(label: str, argv: list[str], expected: str, home: Path) -> None:
    """The success-path writes, which is where this family hid longest.

    These commands create the very files whose errors doctor reports, so
    an operator saw the absolute spelling in the write confirmation and
    the home-relative one in an error moments later.
    """
    from typer.testing import CliRunner

    from agentworks.cli import app

    result = CliRunner().invoke(app, argv)

    assert expected in result.stdout, f"{label}: expected a line containing {expected!r}, got {result.stdout!r}"
    _assert_home_relative(result.stdout, home, label=label)


# -- The failure branches, which the happy-path fixture cannot reach ----------
#
# These are separate tests rather than more rows above because the fixture
# above deliberately builds a WORKING config, and a working config never
# takes a not-found branch. Writing the revert check for this module caught
# exactly that: reverting doctor's missing-config row left every test green,
# because no test had a missing config. A guard for a branch nothing
# exercises is not a guard.


def test_doctor_names_a_missing_config_file_home_relative(home: Path) -> None:
    """The first row an operator with no config.toml ever sees."""
    from agentworks.config import CONFIG_PATH
    from agentworks.doctor import run_checks

    CONFIG_PATH.unlink()

    report = run_checks()
    rows = {c.name: c.message or "" for g in report.groups for c in g.checks}

    assert "Config file" in rows, "doctor rendered no Config file row"
    assert "not found" in rows["Config file"], rows["Config file"]
    _assert_home_relative(rows["Config file"], home, label="doctor missing-config row")


@pytest.mark.skipif(
    getattr(os, "getuid", lambda: 1)() == 0,
    reason="root bypasses file mode, so the not-readable branch cannot be reached",
)
def test_doctor_names_an_unreadable_ssh_key_home_relative(home: Path) -> None:
    """``loaders_core`` checks that the key EXISTS, not that it is
    readable, so this branch is doctor's alone to report.
    """
    from agentworks.doctor import run_checks

    (home / ".ssh" / "id_ed25519.pub").chmod(0o000)

    report = run_checks()
    rows = {c.name: c.message or "" for g in report.groups for c in g.checks}

    assert "SSH public key" in rows, "doctor rendered no SSH public key row"
    assert "not readable" in rows["SSH public key"], rows["SSH public key"]
    _assert_home_relative(rows["SSH public key"], home, label="doctor unreadable-key row")


def test_the_pre_cli_config_load_failures_name_the_file_home_relative(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``load_config`` refuses before any command runs and prints straight
    to stderr, so these two lines bypass the whole output layer and are
    the first thing a broken install shows.
    """
    from agentworks.config import CONFIG_PATH, load_config

    CONFIG_PATH.unlink()
    with pytest.raises(SystemExit):
        load_config()
    missing = capsys.readouterr().err
    assert "Configuration file not found" in missing, missing
    _assert_home_relative(missing, home, label="load_config missing file")

    CONFIG_PATH.write_text("this is not = = valid toml\n")
    with pytest.raises(SystemExit):
        load_config()
    invalid = capsys.readouterr().err
    assert "invalid config file" in invalid, invalid
    _assert_home_relative(invalid, home, label="load_config invalid TOML")


def test_config_edit_names_the_missing_file_home_relative(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app
    from agentworks.config import CONFIG_PATH

    monkeypatch.setenv("EDITOR", "true")
    CONFIG_PATH.unlink()

    result = CliRunner().invoke(app, ["config", "edit"])

    assert result.exit_code != 0
    assert "config file not found at" in result.output, result.output
    _assert_home_relative(result.output, home, label="config edit missing file")


def test_a_config_error_names_the_setting_the_way_the_operator_wrote_it(home: Path) -> None:
    """``operator.ssh_public_key`` is loaded through ``expanduser``, so the
    absolute path it reports back is not the text the operator typed. The
    home-relative rendering is also the one that matches their config.toml.
    """
    from agentworks.config import load_config
    from agentworks.errors import ConfigError

    (home / ".ssh" / "id_ed25519.pub").unlink()

    with pytest.raises(ConfigError) as caught:
        load_config(warn_issues=False)

    message = str(caught.value)
    assert "operator.ssh_public_key does not exist" in message, message
    _assert_home_relative(message, home, label="ssh_public_key config error")
