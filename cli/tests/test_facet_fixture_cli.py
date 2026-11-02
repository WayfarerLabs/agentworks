"""Fresh-process boundary coverage for the opt-in facet acceptance launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from tests.conftest import ManifestDoc, render_manifest, write_cfg


@pytest.mark.skipif(os.name != "posix", reason="fixture launcher selects a POSIX .venv/bin/python")
def test_fixture_publication_schema_and_cli_validation_are_process_local(tmp_path: Path) -> None:
    cli_dir = Path(__file__).parents[1].resolve()
    launcher = cli_dir.parent / "docs" / "testing" / "harnesses" / "facet_fixture_cli.py"
    home = tmp_path / "home"
    config_dir = home / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    settings = '[plugins]\nsystem = ["facet-test-fixtures"]'
    manifest = ManifestDoc(
        "session-template",
        "facet-test-required",
        {"harness_integration": {"name": "facet-test-readiness", "prerequisite": "required"}},
    )
    unsupported = ManifestDoc(
        "agent-template", "facet-test-unsupported", {"harness_integrations": {"facet-test-unsupported": {}}}
    )
    config_path = write_cfg(config_dir, manifest, unsupported, settings=settings)
    environment = {**os.environ, "AGW_CLI_DIR": str(cli_dir), "HOME": str(home), "NO_COLOR": "1"}

    def run(*arguments: str, fixture: bool = True) -> subprocess.CompletedProcess[str]:
        entry = (
            [str(launcher), "--home", str(home), "--"]
            if fixture
            else ["-I", "-c", "from agentworks.cli import main; main()"]
        )
        return subprocess.run(
            [sys.executable, *entry, *arguments],
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    result = run("resource", "list", "--kind", "harness-integration", "--names-only")
    assert result.returncode == 0, result.stderr
    assert {"harness-integration/facet-test-readiness", "harness-integration/facet-test-unsupported"} <= set(
        result.stdout.splitlines()
    )
    result = run("resource", "explain", "harness-integration/facet-test-readiness")
    assert result.returncode == 0, result.stderr

    result = run("resource", "show", "agent-template/facet-test-unsupported", "--output", "json")
    assert result.returncode == 0, result.stderr
    resource = json.loads(result.stdout)["data"]["resource"]
    assert resource["resolution"]["spec"]["harness_integrations"] == {"facet-test-unsupported": {}}

    result = run("resource", "schema", "session-template")
    assert result.returncode == 0, result.stderr
    validator = jsonschema.Draft202012Validator(json.loads(result.stdout))
    document = yaml.safe_load(render_manifest(manifest))
    validator.validate(document)
    document["spec"]["harness_integration"]["prerequisite"] = "invalid"
    assert list(validator.iter_errors(document))

    result = run("resource", "show", "session-template/facet-test-required", "--output", "json")
    assert result.returncode == 0, result.stderr
    resource = json.loads(result.stdout)["data"]["resource"]
    assert resource["resolution"]["spec"]["harness_integration_config"]["prerequisite"] == "required"

    config_dir.joinpath("resources", "resources.yaml").write_text(yaml.safe_dump(document))
    result = run("resource", "show", "session-template/facet-test-required", "--output", "json")
    assert result.returncode == 1

    config_dir.joinpath("resources", "resources.yaml").unlink()
    config_path.write_text(config_path.read_text().replace(settings, ""))
    result = run("resource", "list", "--kind", "harness-integration", "--names-only")
    assert result.returncode == 0, result.stderr
    assert "facet-test-" not in result.stdout
    result = run("resource", "list", "--kind", "harness-integration", "--include-disabled", "--names-only")
    assert result.returncode == 0, result.stderr
    assert "harness-integration/facet-test-readiness" in result.stdout.splitlines()
    result = run(
        "resource", "list", "--kind", "harness-integration", "--include-disabled", "--names-only", fixture=False
    )
    assert result.returncode == 0, result.stderr
    assert "facet-test-" not in result.stdout
