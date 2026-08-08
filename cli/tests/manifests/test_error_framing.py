"""Every manifest error frames its path the same way, whichever layer
refused.

The guard here is deliberately ONE assertion over N refusal sites rather
than a per-site expectation. Framing is an invariant of the manifest
error surface, not a fact about any one message, and the per-site form
has already failed once: three sites (the envelope's ``_err``, the
retired-shape refusal via the old ``Document.where``, and the duplicate
check) hand-rolled ``f"{location.file}:{location.line}"`` for as long as
they existed, each with tests passing.

Those tests passed because they framed the path the same way the code
did, and because a `tmp_path` fixture is never under `$HOME`: with no
home to be relative to, ``format_host_path`` falls back to the absolute
path and the correct and incorrect renderings are byte-identical. The
defect was only visible to an operator, whose resources directory IS
under their home. So every test in this module puts the resources
directory under a patched home; that is the whole reason it can see what
the rest of the suite could not.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests

# -- Fixtures: one manifest per refusing layer --------------------------------

_SECRET = """
apiVersion: agentworks/v1
kind: secret
metadata:
  name: {name}
  description: d
spec: {{}}
"""


def _secret_spec(body: str) -> str:
    return f"""
    apiVersion: agentworks/v1
    kind: secret
    metadata:
      name: s
      description: d
    spec:
    {body}
    """


#: ``(id, {relative path: file text})``, one per layer that can refuse a
#: manifest. Each entry names the module and function that raises, so a
#: reader can check the list against the code rather than trusting it.
REFUSALS: list[tuple[str, dict[str, str]]] = [
    # envelope.py::_err -- unknown top-level key
    ("envelope-unknown-key", {"a.yaml": "apiVersion: agentworks/v1\nkind: secret\nbogus: 1\n"}),
    # envelope.py::_err -- wrong apiVersion
    ("envelope-api-version", {"a.yaml": "apiVersion: agentworks/v0\nkind: secret\n"}),
    # envelope.py::_err -- unknown kind
    ("envelope-unknown-kind", {"a.yaml": "apiVersion: agentworks/v1\nkind: nonesuch\n"}),
    # loader.py::load_manifests -- duplicate (kind, name) across files
    ("duplicate-across-files", {"a.yaml": _SECRET.format(name="dup"), "b.yaml": _SECRET.format(name="dup")}),
    # loader.py::_iter_documents -- unparseable YAML, with a problem mark
    ("invalid-yaml", {"a.yaml": "apiVersion: agentworks/v1\n  bad: indent\n"}),
    # decode.py::_reject_spec_metadata -- envelope field written in spec
    ("spec-carries-metadata", {"a.yaml": _secret_spec("  name: x")}),
    # decode.py -> schema/errors.py::config_error_from -- the pydantic bridge
    ("unknown-spec-field", {"a.yaml": _secret_spec("  bogus: 1")}),
    # decode.py::_check_declared_description -- required description empty
    (
        "empty-description",
        {
            "a.yaml": """
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: s
              description: ""
            spec: {}
            """
        },
    ),
    # decode.py::_reject_legacy_shape -- the retired sibling-pair shape
    (
        "retired-sibling-shape",
        {
            "a.yaml": """
            apiVersion: agentworks/v1
            kind: git-credential
            metadata:
              name: gc
              description: d
            spec:
              provider: github
              provider_config:
                owner: me
            """
        },
    ),
]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A patched ``$HOME`` with the resources directory inside it, which
    is the shape an operator actually has and the shape that makes a
    hand-rolled absolute path distinguishable from a framed one.
    """
    root = tmp_path / "home"
    (root / ".config" / "agentworks" / "resources").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    return root


def _resources(home: Path, files: dict[str, str]) -> Path:
    root = home / ".config" / "agentworks" / "resources"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text))
    return root


# -- The invariant -------------------------------------------------------------


@pytest.mark.parametrize(("label", "files"), REFUSALS, ids=[label for label, _ in REFUSALS])
def test_every_manifest_error_frames_its_path_through_the_shared_helper(
    label: str, files: dict[str, str], home: Path
) -> None:
    """No manifest error shows an operator an absolute path.

    Two halves, and both matter. The message must START with the framed
    ``~/`` form (a message framed by nothing at all would pass a
    "contains no absolute path" check trivially), and it must contain
    the absolute prefix NOWHERE, which catches a second location named
    inline further into the message rather than at the front.
    """
    root = _resources(home, files)

    with pytest.raises(ConfigError) as caught:
        load_manifests(root)
    message = str(caught.value)

    assert message.startswith("~/.config/agentworks/resources/"), (
        f"{label}: refusal is not framed home-relative; an operator sees {message!r}"
    )
    assert str(home) not in message, f"{label}: an absolute path leaked into {message!r}"


def test_the_advisory_channel_frames_the_same_way(home: Path) -> None:
    """``ManifestSet.issues`` is the load-time WARNING channel, framed by
    the same helper. It is not covered by the refusal sweep above because
    it does not raise, and a warning an operator cannot navigate to is
    the same defect as an error they cannot.
    """
    root = _resources(
        home,
        {
            "a.yaml": """
            apiVersion: agentworks/v1
            kind: vm-template
            metadata:
              name: t1
            spec:
              env:
                AGENTWORKS_VM: override
            """
        },
    )

    issues = load_manifests(root).issues

    assert issues, "expected the managed-identity-variable advisory"
    for issue in issues:
        assert issue.startswith("~/.config/agentworks/resources/"), issue
        assert str(home) not in issue, issue


# -- Sentinels: the whole-file location a read failure carries ------------------


def test_an_unreadable_file_names_the_file_and_no_line(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``line == 0`` sentinel, reached from the manifest layer.

    A file that cannot be decoded has no document, so there is no
    declaration line to point at. ``_iter_documents`` says so with
    ``line=0`` rather than inventing a line, and the framing renders the
    file alone: ``~/...yaml: not valid UTF-8``, never ``~/...yaml:0:``,
    because no editor has a line 0.
    """
    root = _resources(home, {"a.yaml": ""})
    (root / "a.yaml").write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(ConfigError) as caught:
        load_manifests(root)

    message = str(caught.value)
    assert message.startswith("~/.config/agentworks/resources/a.yaml: not valid UTF-8")
    assert ":0" not in message


def test_a_yaml_error_without_a_problem_mark_names_the_file_and_no_line(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same sentinel on the other read failure. PyYAML does not
    always attach a ``problem_mark``; when it does not there is no line
    to report, which is the identical "no declaration site" case.
    """
    import yaml

    root = _resources(home, {"a.yaml": "apiVersion: agentworks/v1\n"})
    monkeypatch.setattr(yaml, "compose_all", lambda *a, **k: (_ for _ in ()).throw(yaml.YAMLError("boom")))

    with pytest.raises(ConfigError) as caught:
        load_manifests(root)

    message = str(caught.value)
    assert message == "~/.config/agentworks/resources/a.yaml: invalid YAML: boom"


def test_the_duplicate_error_frames_both_locations(home: Path) -> None:
    """The duplicate check is the one refusal carrying TWO locations: the
    frame points at the second declaration (the one to delete) and the
    first is named inline. The sweep above proves neither is absolute;
    this pins that both are actually present, so a future "fix" that
    drops the inline one to satisfy the sweep is caught.
    """
    root = _resources(home, {"a.yaml": _SECRET.format(name="dup"), "b.yaml": _SECRET.format(name="dup")})

    with pytest.raises(ConfigError) as caught:
        load_manifests(root)

    assert str(caught.value) == (
        '~/.config/agentworks/resources/b.yaml:2: duplicate secret "dup" '
        "(also declared at ~/.config/agentworks/resources/a.yaml:2)"
    )
