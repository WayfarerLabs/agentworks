"""``agw resource sample``: the CLI contract, and the rendered output.

The CLI-contract tests here predate the renderer (kind selection, ``--all``,
the capability-kind refusal, ``--write``'s create-then-append behavior) and
are carried through the swap unchanged: what the command DOES did not
change, only where its text comes from.

What is new is what replaced the bundled-file pins. The old suite proved a
curated corpus was honest by stripping one ``#`` per line and loading it;
these prove the same property of GENERATED text, which is a stronger claim,
because a curated file could be fixed by hand and a renderer cannot.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.manifests.decode import KIND_SECTIONS
from agentworks.manifests.emit import MODELINE_PREFIX
from agentworks.manifests.loader import load_manifests
from agentworks.manifests.samples import _SEPARATOR, sample_text, write_sample
from agentworks.manifests.spec_model import declarable_kinds
from tests.manifests.conftest import activate, uncomment


def _capability_kinds() -> list[str]:
    """The code-backed kinds that carry no manifest, straight from the
    registry so this list can't drift from the kind inventory."""
    from agentworks.resources import KIND_REGISTRY

    return [name for name, handler in KIND_REGISTRY.items() if handler.category == "capability"]


def _registered_kinds() -> list[str]:
    """Every kind the registry holds, whatever its category: the set the
    two partitions above have to add up to."""
    from agentworks.resources import KIND_REGISTRY

    return list(KIND_REGISTRY)


# --- what gets sampled ------------------------------------------------


def test_every_declarable_kind_renders() -> None:
    # secret-backend is a capability descriptor post-collapse
    # (2026-07-07): in KIND_SECTIONS as a retired section name, not
    # declarable, no sample. That sampling it reports the capability-kind
    # error rather than crashing is one case of
    # `test_capability_kinds_report_no_sample`, which sweeps the category.
    assert "secret-backend" not in declarable_kinds()
    assert set(declarable_kinds()) | {"secret-backend"} == set(KIND_SECTIONS)
    for kind in declarable_kinds():
        assert sample_text(kind).strip()


def test_every_registered_kind_is_declarable_or_a_capability() -> None:
    """A kind is sampleable exactly when a document of it can exist, which
    is the registry's own per-kind ``category``.

    The assertion that used to stand here was that the two sets are
    DISJOINT, which no registry contents could ever violate: both are
    ``KIND_REGISTRY`` filtered on complementary values of one field
    (``spec_model.declarable_kinds``, and ``_capability_kinds`` above), so
    an overlap is unrepresentable and the test could only ever pass.

    Coverage is the property that can actually break, and it breaks
    loudly. ``agw resource kinds`` lists straight from the registry
    (``cli/commands/resource.py:155``), so a kind whose category is
    neither value is offered to the operator; ``resource sample`` then
    misses the capability-kind refusal it falls through
    (``manifests/samples.py:238``) and answers "unknown kind" about a kind
    the CLI just listed, and no schema is emitted for it either.
    """
    assert set(declarable_kinds()) | set(_capability_kinds()) == set(_registered_kinds())


def test_capability_kinds_report_no_sample() -> None:
    """Every capability kind `resource kinds` lists is a valid argument
    value, so it reaches the service layer instead of dying as a raw parse
    error (issue #276). The service layer rejects it with a typed domain
    error that names the kind, offers the declarable set, and (new with the
    renderer) points at the surface that DOES document a capability.

    Looped over the registry's capability category, which
    ``test_kind_models.py::test_the_registry_has_kinds_of_both_categories``
    keeps non-empty. One refusal serves every one of them, so the report
    worth having is each kind that stopped getting it.
    """
    for kind in _capability_kinds():
        with pytest.raises(ValidationError) as excinfo:
            sample_text(kind)
        err = excinfo.value
        assert kind in str(err)
        assert "capability kind" in str(err), kind
        assert "no sample manifest" in str(err), kind
        assert err.hint is not None, kind
        assert f"describe-kind {kind}" in err.hint
        for declarable in declarable_kinds():
            assert declarable in err.hint, (kind, declarable)


def test_all_kinds_concatenation_and_unknown_kind() -> None:
    everything = sample_text(all_kinds=True)
    for kind in declarable_kinds():
        # Every sample opens with a semantic kind header.
        assert re.search(rf"^## kind: {re.escape(kind)}(?=[:\s])", everything, re.MULTILINE)
    with pytest.raises(ValidationError, match="unknown kind"):
        sample_text("nope")


def test_bare_sample_requires_kind_or_all() -> None:
    """Dumping every kind is an explicit opt-in (the CLI's standing
    `--all` shape), and mixing a kind with --all is an error."""
    with pytest.raises(ValidationError, match="indicate a kind"):
        sample_text()
    with pytest.raises(ValidationError, match="not both"):
        sample_text("secret", all_kinds=True)


# --- the rendered text is inert, and true -----------------------------


def test_samples_are_fully_commented() -> None:
    """Every non-blank line starts with ``#``: written samples are inert."""
    for kind in declarable_kinds():
        for line in sample_text(kind).splitlines():
            assert not line or line.startswith("#"), (kind, line)


def test_a_field_that_folds_a_scalar_names_both_spellings_and_shows_the_block() -> None:
    """The decision this surface makes for a field that takes either a
    scalar or a table, and the reason for it.

    The type line names both spellings, because that is the fact. What
    gets a BLOCK is the table form: a skeleton exists to show structure an
    operator cannot guess, and ``FOO: a value`` has none to show, while
    ``{secret: <name>}`` is two keys nobody guesses. So the block is
    rendered and the scalar rides on the line above it, where an operator
    who wants the short form reads it and writes one line instead of
    three.
    """
    lines = sample_text("vm-template").splitlines()
    at = lines.index("#  # env:")

    assert lines[at - 1] == "#  # (table of string or table, optional)"
    assert lines[at + 1 : at + 3] == [
        "#    # one entry, as an example:",
        "#    # One of: plaintext, secret. Shown here: plaintext.",
    ]
    assert "#    # <key>:" in lines
    assert "#      # value: <string>" in lines
    assert "#      # secret: <string>" not in lines


def test_git_token_sample_shows_the_stored_union_and_its_scalar_spelling() -> None:
    """The one-arm union is not collapsed into a plain secret field on
    the generated operator surface."""
    lines = sample_text("git-credential").splitlines()
    at = lines.index("#    # token:")

    assert lines[at - 2] == "#    # (string or table, optional)"
    assert lines[at - 1] == "#    # One of: stored. Shown here: stored."
    assert "#      # mode: stored" in lines
    assert "#      # secret: <string>" in lines


def test_commented_samples_are_inert_through_the_loader(tmp_path: Path) -> None:
    """As rendered, a written sample declares nothing."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "all.yaml").write_text(sample_text(all_kinds=True))
    manifests = load_manifests(resources)
    assert not manifests.entries
    assert not manifests.issues


def test_uncommented_samples_load_through_the_real_loader(tmp_path: Path) -> None:
    """The teaching surface must be true: stripping one ``#`` per line
    yields documents the real loader accepts.

    This is what makes "only required fields are live lines" a property
    rather than a preference. A renderer that emitted an optional field as
    a document line would have to invent a value for it, and this test is
    where an invented value fails.
    """
    resources = tmp_path / "resources"
    resources.mkdir()
    for kind in declarable_kinds():
        (resources / f"{kind}.yaml").write_text(uncomment(sample_text(kind)))
    manifests = load_manifests(resources)

    assert {entry.kind for entry in manifests.entries} == set(declarable_kinds())
    assert not manifests.issues, manifests.issues


def test_the_whole_set_uncomments_as_one_file(tmp_path: Path) -> None:
    """``--all`` is a real multi-document file, not a pile of documents
    YAML reads as one: the renderer separates them with a commented
    ``---``, which the same one-``#`` strip turns into a separator. The
    per-file corpus this replaces never proved that."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "everything.yaml").write_text(uncomment(sample_text(all_kinds=True)))
    manifests = load_manifests(resources)

    assert {entry.kind for entry in manifests.entries} == set(declarable_kinds())
    assert not manifests.issues, manifests.issues


# That the whole uncommented set also BUILDS a full registry (so every
# reference a rendered document makes resolves at finalize, and every
# capability config block validates against its own model) is asserted in
# ``test_emit.py::test_emitted_schemas_accept_every_document_the_full_load
# _path_accepts``, which renders this same corpus with the same plugins
# enabled and then validates it against the emitted schemas. A second copy
# of the corpus-and-build stood here; every mutation it caught (a
# truncated capability union, an optional field rendered as a live line, a
# dropped metadata block) reddens that test too, and only that test also
# proves what the schemas make of the result.


# --- --write ----------------------------------------------------------


def test_write_sample_creates_and_appends(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    path, outcome = write_sample(resources, "kinds/secret.yaml", "secret")
    assert outcome == "created"
    assert path == resources / "kinds" / "secret.yaml"
    first = path.read_text()

    path2, outcome2 = write_sample(resources, "kinds/secret.yaml", "vm-template")
    assert outcome2 == "appended"
    assert path2 == path
    text = path.read_text()
    # The BODY is appended to, never rewritten. The first line is the
    # modeline, which a second kind restamps (test_editor_association.py).
    assert text.split("\n", 1)[1].startswith(first.split("\n", 1)[1])
    assert "kind: vm-template" in text
    # Still inert after the append.
    manifests = load_manifests(resources)
    assert not manifests.entries


def test_appended_samples_activate_as_separate_documents(tmp_path: Path) -> None:
    """Appending a second kind and activating the result declares BOTH.

    The append emits the commented ``---`` that ``--all`` always emitted,
    so the same one-``#`` strip separates the documents. Without it the
    second document's keys merge into the first and the load dies on a
    duplicate ``apiVersion``, which names neither the missing separator nor
    the command that skipped it.
    """
    resources = tmp_path / "resources"
    write_sample(resources, "repro.yaml", "vm-template")
    write_sample(resources, "repro.yaml", "secret")

    activate(resources / "repro.yaml")
    manifests = load_manifests(resources)

    assert {(entry.kind, entry.name) for entry in manifests.entries} == {
        ("vm-template", "my-vm-template"),
        ("secret", "my-secret"),
    }


def test_appending_to_a_hand_written_manifest_keeps_it_declaring(tmp_path: Path) -> None:
    """The case that costs an operator something they already had.

    Appending to a live hand-written manifest and activating the addition
    used to fold the new document's keys into the resource already in the
    file, so a working resource stopped loading because a DIFFERENT one was
    added next to it.
    """
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "mine.yaml"
    manifest.write_text(
        "apiVersion: agentworks/v1\nkind: vm-template\nmetadata:\n  name: dev\nspec:\n  cpus: 4\n",
        encoding="utf-8",
    )

    write_sample(resources, "mine.yaml", "secret")
    # Inert as written: the hand-written resource is untouched.
    assert [(entry.kind, entry.name) for entry in load_manifests(resources).entries] == [("vm-template", "dev")]

    activate(manifest)
    assert {(entry.kind, entry.name) for entry in load_manifests(resources).entries} == {
        ("vm-template", "dev"),
        ("secret", "my-secret"),
    }


def test_appending_to_a_file_with_no_trailing_newline_still_separates(tmp_path: Path) -> None:
    """``_joined``'s other branch, which nothing reached.

    Every test around this seeds text ending in ``\\n``, so the branch that
    supplies the missing one was unpinned on a helper that had just had a
    bug in it. Without it the separator lands on the end of the operator's
    last line (``  cpus: 4#---``), which is not a document line, does not
    become one when uncommented, and corrupts the line it landed on.
    """
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "mine.yaml"
    manifest.write_text(
        "apiVersion: agentworks/v1\nkind: vm-template\nmetadata:\n  name: dev\nspec:\n  cpus: 4",
        encoding="utf-8",
    )

    _, outcome = write_sample(resources, "mine.yaml", "secret")

    assert outcome == "appended"
    assert "\n  cpus: 4\n\n#---\n" in manifest.read_text()
    activate(manifest)
    assert {(entry.kind, entry.name) for entry in load_manifests(resources).entries} == {
        ("vm-template", "dev"),
        ("secret", "my-secret"),
    }


def test_a_third_append_separates_from_the_second(tmp_path: Path) -> None:
    """Appending is not a two-document special case.

    Two appends were pinned and three were not, which is exactly where a
    separator emitted from the wrong state would show: the third document
    is the first one appended to a file this command itself both created
    and grew.
    """
    resources = tmp_path / "resources"
    write_sample(resources, "many.yaml", "vm-template")
    write_sample(resources, "many.yaml", "secret")
    write_sample(resources, "many.yaml", "apt-package")

    manifest = resources / "many.yaml"
    assert manifest.read_text().count(f"\n{_SEPARATOR}\n") == 2
    activate(manifest)
    assert {(entry.kind, entry.name) for entry in load_manifests(resources).entries} == {
        ("vm-template", "my-vm-template"),
        ("secret", "my-secret"),
        ("apt-package", "my-apt-package"),
    }


def test_an_append_that_dies_at_the_commit_point_leaves_the_manifest_whole(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An append is a read-then-write over a file the OPERATOR authored,
    and this command holds the only copy of the old content between the
    two. A plain write truncates first, so anything that stops it (a
    Ctrl-C, a full disk) used to leave the operator with an empty or
    half-written manifest and nowhere to get the original back from.

    The failure is induced at ``os.replace``, because that is the only
    moment the target is touched at all: everything before it happens to a
    temp file. A write that truncates in place has no such moment, which
    is why this fails against one (nothing raises, and the manifest comes
    back appended-to rather than untouched).
    """
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "mine.yaml"
    original = "apiVersion: agentworks/v1\nkind: vm-template\nmetadata:\n  name: dev\nspec:\n  cpus: 4\n"
    manifest.write_text(original, encoding="utf-8")

    def _interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("agentworks.manifests.samples.os.replace", _interrupted)

    with pytest.raises(KeyboardInterrupt):
        write_sample(resources, "mine.yaml", "secret")

    assert manifest.read_text(encoding="utf-8") == original
    # And no residue: the temp file is dot-prefixed, so a leftover would
    # be invisible to the loader AND to the operator listing the directory.
    assert [path.name for path in resources.iterdir()] == ["mine.yaml"]


def test_an_append_keeps_the_permission_bits_the_operator_chose(tmp_path: Path) -> None:
    """Writing through a temp file means the mode comes from ``mkstemp``
    (0600) unless it is carried over deliberately, so an append could
    silently narrow a manifest the operator had made group-readable.
    Nothing about appending a sample is a reason to change who can read
    the file."""
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "mine.yaml"
    manifest.write_text("apiVersion: agentworks/v1\nkind: secret\n", encoding="utf-8")
    manifest.chmod(0o640)

    write_sample(resources, "mine.yaml", "secret")

    assert stat.S_IMODE(manifest.stat().st_mode) == 0o640


def test_writing_into_a_file_that_exists_and_is_blank_emits_no_separator(tmp_path: Path) -> None:
    """The third outcome, and the reason there are three.

    ``_joined`` deliberately writes no separator over nothing, and an
    append never inserts a modeline, so this file ends up with neither.
    The command used to call this an append and point the operator at a
    ``#---`` that was not written.
    """
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "touched.yaml"
    manifest.write_text("\n  \n", encoding="utf-8")

    _, outcome = write_sample(resources, "touched.yaml", "secret")

    assert outcome == "filled"
    text = manifest.read_text()
    assert _SEPARATOR not in text
    assert MODELINE_PREFIX not in text
    activate(manifest)
    assert [(entry.kind, entry.name) for entry in load_manifests(resources).entries] == [("secret", "my-secret")]


def test_sample_capability_kind_is_a_clean_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end error contract for issue #276: `resource sample
    harness-integration` exits non-zero with a single clean `Error:` line, no
    traceback, and (being a domain error, not an unexpected failure)
    leaves error.log untouched. Regression guard against the raw
    click.Choice traceback that used to escape the top-level handler."""
    from agentworks import cli as cli_mod

    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "resource", "sample", "harness-integration"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "'harness-integration' is a capability kind; it has no sample manifest" in err
    assert "Traceback" not in err
    assert "StopIteration" not in err
    # Domain errors are clean-line, not logged: error.log must not appear.
    assert not (tmp_path / "logs" / "error.log").exists()


def test_write_sample_refuses_escapes_and_suffixes(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    with pytest.raises(ValidationError, match="relative to the resources"):
        write_sample(resources, "/abs/path.yaml")
    with pytest.raises(ValidationError, match="relative to the resources"):
        write_sample(resources, "../escape.yaml")
    with pytest.raises(ValidationError, match=".yaml or .yml"):
        write_sample(resources, "samples.txt")


def test_write_sample_refuses_unloadable_dot_paths(tmp_path: Path) -> None:
    """``--write`` may not write where the loader will never look.

    ``loader._iter_manifest_files`` prunes every dot-prefixed name, file
    and directory alike, so writing there produced a file plus a promise
    ("uncomment to activate") that no amount of uncommenting could keep.

    Three positions for the dot (a leading directory, the file itself, a
    nested directory) against the one production expression that walks
    every part of the path. They fail together or not at all, so they are
    one loop that names the spelling that got through.
    """
    resources = tmp_path / "resources"
    for filename in (".schema/sneaky.yaml", ".hidden.yaml", "nested/.d/x.yaml"):
        with pytest.raises(ValidationError, match="dot-prefixed"):
            write_sample(resources, filename, "secret")
        assert not (resources / filename).exists(), filename
