from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
BODY_PATH = ROOT / "packaging/agentworks/assistance.md"
BODY = BODY_PATH.read_text()
METADATA = json.loads((ROOT / "packaging/agentworks/metadata.json").read_text())


def _skill_body(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text()
    frontmatter, body = text.removeprefix("---\n").split("---\n\n", maxsplit=1)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed, body


def test_generator_has_no_network_or_process_boundary() -> None:
    imports = {
        node.names[0].name.split(".")[0]
        for node in ast.walk(ast.parse((ROOT / "scripts/generate-agentworks-package.py").read_text()))
        if isinstance(node, ast.Import)
    } | {
        node.module.split(".")[0]
        for node in ast.walk(ast.parse((ROOT / "scripts/generate-agentworks-package.py").read_text()))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports.isdisjoint({"subprocess", "socket", "urllib", "http", "requests"})


def test_generated_packages_are_inert_and_share_the_canonical_handoff() -> None:
    skills = (
        ROOT / "plugins/claude-code/agentworks/skills/agentworks/SKILL.md",
        ROOT / "plugins/codex/agentworks/skills/agentworks/SKILL.md",
    )
    for skill in skills:
        frontmatter, body = _skill_body(skill)
        assert body == BODY
        assert frontmatter["description"] == METADATA["skillDescription"]
        assert "allowed-tools" not in frontmatter
    for root in (
        ROOT / "plugins/claude-code/agentworks",
        ROOT / "plugins/codex/agentworks",
    ):
        relative_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        assert relative_files in (
            {".claude-plugin/plugin.json", "skills/agentworks/SKILL.md"},
            {".codex-plugin/plugin.json", "skills/agentworks/SKILL.md"},
        )
        assert not relative_files & {"hooks.json", ".mcp.json", ".app.json"}


def test_catalogs_and_manifests_have_neutral_identity_and_complete_codex_policy() -> None:
    claude_catalog = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    codex_catalog = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    claude_manifest = json.loads((ROOT / "plugins/claude-code/agentworks/.claude-plugin/plugin.json").read_text())
    codex_manifest = json.loads((ROOT / "plugins/codex/agentworks/.codex-plugin/plugin.json").read_text())

    assert claude_catalog["name"] == claude_manifest["name"] == "agentworks"
    assert claude_catalog["description"]
    assert claude_catalog["plugins"] == [
        {
            "name": "agentworks",
            "description": METADATA["description"],
            "source": "./plugins/claude-code/agentworks",
        }
    ]
    assert codex_catalog == {
        "name": "agentworks",
        "interface": {"displayName": "Agentworks"},
        "plugins": [
            {
                "name": "agentworks",
                "source": {"source": "local", "path": "./plugins/codex/agentworks"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }
    interface = codex_manifest["interface"]
    assert interface["displayName"] == METADATA["displayName"]
    assert interface["developerName"] == METADATA["publisher"]["name"]
    assert {key: interface[key] for key in METADATA["interface"]} == METADATA["interface"]
    assert codex_manifest["skills"] == "./skills/"
    assert codex_manifest["version"] == claude_manifest["version"] == "1.0.1"


def test_readme_projection_is_exact() -> None:
    readme = (ROOT / "README.md").read_text()
    begin = readme.index("<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->")
    end = readme.index("<!-- END GENERATED AGENTWORKS ASSISTANCE -->")
    projection = readme[begin:end]
    opening = re.search(r"\n(`{3,})markdown\n", projection)
    assert opening is not None
    fence = opening.group(1)
    projected_body = projection[opening.end() :]
    assert projected_body == BODY + fence + "\n\n"
