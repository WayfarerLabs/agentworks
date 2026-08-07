"""The editor association: what a written manifest points at, and whether
what it points at actually checks it.

FR9's second half. Both writers the plan names stamp the modeline (``agw
resource sample --write`` and ``agw resource migrate``), and the end-to-end
test here is the automated counterpart of the manual check documented in
``docs/guides/resources.md``: it resolves the modeline the way a
schema-aware editor would (relative to the file), loads the schema it
finds, and validates the file's own documents against it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from agentworks.config import load_config
from agentworks.manifests.emit import ENVELOPE_SCHEMA_FILENAME, SCHEMA_DIRNAME
from agentworks.manifests.samples import write_sample
from agentworks.manifests.spec_model import declarable_kinds
from agentworks.migrate import execute_plan, plan_migration

if TYPE_CHECKING:
    from pathlib import Path

MODELINE_PREFIX = "# yaml-language-server: $schema="

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


# -- agw resource sample --write -------------------------------------------


@pytest.mark.parametrize("kind", declarable_kinds())
def test_a_written_sample_is_checked_by_the_schema_it_points_at(tmp_path: Path, kind: str) -> None:
    """The whole feature in one assertion per kind: write a sample, follow
    its modeline the way an editor would, and check the sample's own
    documents against what is found there."""
    resources = tmp_path / "resources"
    manifest, appended = write_sample(resources, f"{kind}.yaml", kind)
    assert not appended

    schema = _schema_an_editor_would_load(manifest)
    validator = Draft202012Validator(schema)
    for document in _uncommented_documents(manifest):
        assert [error.message for error in validator.iter_errors(document)] == [], (kind, document)


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


def test_an_append_leaves_the_existing_files_first_line_alone(tmp_path: Path) -> None:
    """A modeline has to be the first line, so stamping an existing file
    means inserting at line 1 and shifting every line number an operator
    (and every stored ``declared_at``) already knows. Creation only."""
    resources = tmp_path / "resources"
    manifest, _ = write_sample(resources, "mixed.yaml", "secret")
    before = manifest.read_text()

    _, appended = write_sample(resources, "mixed.yaml", "vm-template")
    assert appended
    after = manifest.read_text()
    assert after.startswith(before)
    assert after.count(MODELINE_PREFIX) == 1


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


# -- agw resource migrate --------------------------------------------------

_TOML = """\
[secrets.npm-token]
description = "npm registry token"
backend_mappings.env-var = "NPM_TOKEN"

[vm_templates.dev]
cpus = 8
"""


def _config(tmp_path: Path) -> Path:
    pub, priv = tmp_path / "id.pub", tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""\
[operator]
ssh_public_key = "{pub.as_posix()}"
ssh_private_key = "{priv.as_posix()}"

[paths]
backups = "{(tmp_path / "backups").as_posix()}"

{_TOML}
"""
    )
    return cfg


def _migrate(tmp_path: Path, layout: str = "per-kind") -> Path:
    cfg = _config(tmp_path)
    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, [], all_resources=True, layout=layout)
    execute_plan(plan, config)
    return tmp_path / "resources"


def test_a_migrated_per_kind_file_is_checked_by_its_own_schema(tmp_path: Path) -> None:
    resources = _migrate(tmp_path)
    manifest = resources / "secrets.yaml"
    assert manifest.read_text().splitlines()[0] == f"{MODELINE_PREFIX}{SCHEMA_DIRNAME}/secret.schema.json"

    validator = Draft202012Validator(_schema_an_editor_would_load(manifest))
    for document in yaml.safe_load_all(manifest.read_text()):
        assert [error.message for error in validator.iter_errors(document)] == [], document


def test_a_migrated_single_layout_file_points_at_the_any_kind_schema(tmp_path: Path) -> None:
    resources = _migrate(tmp_path, layout="single")
    manifest = resources / "resources.yaml"
    assert manifest.read_text().splitlines()[0] == f"{MODELINE_PREFIX}{SCHEMA_DIRNAME}/{ENVELOPE_SCHEMA_FILENAME}"

    validator = Draft202012Validator(_schema_an_editor_would_load(manifest))
    for document in yaml.safe_load_all(manifest.read_text()):
        assert [error.message for error in validator.iter_errors(document)] == [], document


def test_migrating_leaves_the_schemas_beside_the_manifests(tmp_path: Path) -> None:
    resources = _migrate(tmp_path)
    assert (resources / SCHEMA_DIRNAME / ENVELOPE_SCHEMA_FILENAME).is_file()


def test_migrating_into_an_existing_file_leaves_its_first_line_alone(tmp_path: Path) -> None:
    """The append path, which is what a second migration run hits."""
    resources = tmp_path / "resources"
    resources.mkdir(parents=True)
    existing = resources / "secrets.yaml"
    existing.write_text("# hand-written header\n")

    _migrate(tmp_path)
    lines = existing.read_text().splitlines()
    assert lines[0] == "# hand-written header"
    assert MODELINE_PREFIX not in existing.read_text()


def test_the_dry_run_shows_the_modeline_it_would_write(tmp_path: Path) -> None:
    """What a dry run prints has to be what lands, header included."""
    from agentworks.migrate.render import render_dry_run

    cfg = _config(tmp_path)
    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, [], all_resources=True)
    lines = render_dry_run(plan, full=True)
    assert f"{MODELINE_PREFIX}{SCHEMA_DIRNAME}/secret.schema.json" in lines
