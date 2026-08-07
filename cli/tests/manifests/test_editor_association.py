"""The editor association: what a written manifest points at, and whether
what it points at actually checks it.

FR9's second half. ``agw resource sample --write`` is the writer that
stamps the modeline, and the end-to-end test here is the automated
counterpart of the manual check documented in
``docs/guides/resources.md``: it resolves the modeline the way a
schema-aware editor would (relative to the file), loads the schema it
finds, and validates the file's own documents against it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import yaml
from jsonschema import Draft202012Validator

from agentworks.manifests.emit import ENVELOPE_SCHEMA_FILENAME, MODELINE_PREFIX, SCHEMA_DIRNAME
from agentworks.manifests.samples import write_sample
from agentworks.manifests.spec_model import declarable_kinds

if TYPE_CHECKING:
    from pathlib import Path

# This file used to import ``agentworks.plugins`` for its side effect,
# because an emitted schema describes the capabilities THIS HOST has
# registered and a sample that named a plugin's capability would otherwise
# be checked against a schema that had never heard of it. The seating is
# the emitter's own responsibility now
# (``plugins.registration.seat_installed_plugins``, called by the shared
# spec-model assembly), so the import is gone rather than left as a
# precondition a test has to remember.


def _schema_an_editor_would_load(manifest: Path) -> dict[str, Any]:
    """The schema the file's modeline names, resolved the way an editor
    resolves it: relative to the manifest, not to any working directory."""
    first = manifest.read_text().splitlines()[0]
    assert first.startswith(MODELINE_PREFIX), first
    referenced = manifest.parent / first.removeprefix(MODELINE_PREFIX)
    assert referenced.is_file(), f"{manifest} points at {referenced}, which does not exist"
    return json.loads(referenced.read_text())  # type: ignore[no-any-return]


def _uncommented_documents(manifest: Path) -> list[dict[str, Any]]:
    """The file's documents as an operator activates them: one leading
    ``#`` stripped from each line of the sample body."""
    lines = manifest.read_text().splitlines()[1:]  # past the modeline
    body = "\n".join(line[1:] if line.startswith("#") else line for line in lines)
    return [doc for doc in yaml.safe_load_all(body) if isinstance(doc, dict)]


def test_a_written_sample_is_checked_by_the_schema_it_points_at(tmp_path: Path) -> None:
    """The whole feature, over every declarable kind: write a sample,
    follow its modeline the way an editor would, and check the sample's
    own documents against what is found there.

    One pass reporting every offending ``(kind, document, message)``
    rather than one case per kind. What breaks this is emission or the
    renderer, and both are shared: a widening that regresses puts a red
    underline on every kind at once, and reading that as one list beats
    reading it as thirteen tracebacks.
    """
    resources = tmp_path / "resources"
    red: list[str] = []
    for kind in declarable_kinds():
        manifest, outcome = write_sample(resources, f"{kind}.yaml", kind)
        assert outcome == "created", (kind, outcome)

        validator = Draft202012Validator(_schema_an_editor_would_load(manifest))
        for document in _uncommented_documents(manifest):
            name = document.get("metadata", {}).get("name")
            red.extend(f"{kind}/{name}: {error.message}" for error in validator.iter_errors(document))
    assert not red, "\n".join(red)


def test_a_single_kind_file_points_at_that_kinds_schema(tmp_path: Path) -> None:
    """A one-kind file gets its own schema rather than the any-kind one:
    an editor that ignores the OpenAPI-style discriminator localizes an
    error far better against one document shape than against a
    thirteen-arm oneOf."""
    resources = tmp_path / "resources"
    manifest, _ = write_sample(resources, "nested/secrets.yaml", "secret")
    first = manifest.read_text().splitlines()[0]
    assert first == f"{MODELINE_PREFIX}../{SCHEMA_DIRNAME}/secret.schema.json"


def test_an_all_kinds_file_points_at_the_any_kind_schema(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    manifest, _ = write_sample(resources, "everything.yaml", all_kinds=True)
    first = manifest.read_text().splitlines()[0]
    assert first == f"{MODELINE_PREFIX}{SCHEMA_DIRNAME}/{ENVELOPE_SCHEMA_FILENAME}"


def test_an_append_never_inserts_a_modeline(tmp_path: Path) -> None:
    """A modeline has to be the first line, so stamping a file that has
    none means inserting at line 1 and shifting every line number an
    operator (and every stored ``declared_at``) already knows. Insertion
    is creation-only, and a file the operator wrote by hand keeps its own
    first line."""
    resources = tmp_path / "resources"
    resources.mkdir(parents=True)
    manifest = resources / "hand-written.yaml"
    manifest.write_text("# my own notes\napiVersion: agentworks/v1\n")
    before = manifest.read_text()

    _, outcome = write_sample(resources, "hand-written.yaml", "secret")

    assert outcome == "appended"
    after = manifest.read_text()
    assert after.startswith(before)
    assert MODELINE_PREFIX not in after


def test_appending_a_second_kind_restamps_the_modeline(tmp_path: Path) -> None:
    """A single-kind file's modeline stops being true the moment a second
    kind lands in it: the editor validates the new document against the
    first kind's shape, red-underlining configuration the loader accepts,
    which is worse than no association at all.

    Restamping costs no line numbers, because it REPLACES a line that is
    already there rather than inserting one, so the reason insertion is
    creation-only does not apply.
    """
    resources = tmp_path / "resources"
    manifest, _ = write_sample(resources, "mixed.yaml", "secret")
    body_before = manifest.read_text().split("\n", 1)[1]

    _, outcome = write_sample(resources, "mixed.yaml", "vm-template")

    assert outcome == "appended"
    first, body_after = manifest.read_text().split("\n", 1)
    assert first == f"{MODELINE_PREFIX}{SCHEMA_DIRNAME}/{ENVELOPE_SCHEMA_FILENAME}"
    assert body_after.startswith(body_before), "the body is appended to, never rewritten"
    assert manifest.read_text().count(MODELINE_PREFIX) == 1
    assert (resources / SCHEMA_DIRNAME / ENVELOPE_SCHEMA_FILENAME).is_file()


def test_appending_the_same_kind_leaves_the_modeline_alone(tmp_path: Path) -> None:
    """The single-kind schema localizes an error far better than the
    thirteen-arm envelope, so a file that is still about one kind keeps
    it. Restamping is for a file that stopped being single-kind."""
    resources = tmp_path / "resources"
    manifest, _ = write_sample(resources, "secrets.yaml", "secret")
    before = manifest.read_text().splitlines()[0]

    write_sample(resources, "secrets.yaml", "secret")

    assert manifest.read_text().splitlines()[0] == before


def test_writing_a_sample_writes_the_schemas_it_refers_to(tmp_path: Path) -> None:
    """A modeline pointing at a file that is not there is a red banner in
    the operator's editor: strictly worse than no modeline. So the
    promise and the thing it promises are written together."""
    resources = tmp_path / "resources"
    write_sample(resources, "secrets.yaml", "secret")
    written = {path.name for path in (resources / SCHEMA_DIRNAME).iterdir()}
    assert ENVELOPE_SCHEMA_FILENAME in written
    assert {f"{kind}.schema.json" for kind in declarable_kinds()} <= written


def test_a_stamped_sample_is_still_inert_through_the_loader(tmp_path: Path) -> None:
    """The modeline is a comment and the schemas live in a dot-directory
    the manifest walk prunes, so a written sample still declares
    nothing."""
    from agentworks.manifests.loader import load_manifests

    resources = tmp_path / "resources"
    write_sample(resources, "everything.yaml", all_kinds=True)
    manifests = load_manifests(resources)
    assert not manifests.entries
    assert not manifests.issues
