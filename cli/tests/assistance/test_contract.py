from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
BODY_PATH = ROOT / "packaging/agentworks/assistance.md"
BODY = BODY_PATH.read_text()
NORMALIZED_BODY = " ".join(BODY.split())
METADATA = json.loads((ROOT / "packaging/agentworks/metadata.json").read_text())


def _skill_body(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text()
    frontmatter, body = text.removeprefix("---\n").split("---\n\n", maxsplit=1)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed, body


def test_canonical_body_is_thin_table_free_bootstrap() -> None:
    headings = re.findall(r"^#{1,6} .+$", BODY, flags=re.MULTILINE)
    assert headings == [
        "# Agentworks CLI bootstrap",
        "## Install and hand off",
    ]
    assert not re.search(r"^\s*\|.*\|\s*$", BODY, flags=re.MULTILINE)
    assert "Agentworks assistant agent" in NORMALIZED_BODY
    assert "not an Agentworks-managed agent" in NORMALIZED_BODY
    assert "Python 3.12 or newer" in NORMALIZED_BODY
    assert "`agentworks-cli`" in NORMALIZED_BODY
    assert "only to make a compatible `agentworks-cli` available" in NORMALIZED_BODY
    assert "obey the returned guide context" in NORMALIZED_BODY


def test_bootstrap_starts_with_version_check_and_contains_no_authorization_teaching() -> None:
    assert BODY.index("## Install and hand off") < BODY.index("Run `agw version`")
    for removed in (
        "authorization",
        "permission",
        "approval",
        "sandbox",
        "harness posture",
        "privilege",
        "secret",
        "SSH",
        "danger-full-access",
        "bypassPermissions",
    ):
        assert removed.casefold() not in BODY.casefold()


def test_installation_is_exact_verified_and_hands_off_to_the_guide() -> None:
    assert "exact compatible stable version at least 0.14.0" in NORMALIZED_BODY
    assert "latest compatible non-prerelease" in NORMALIZED_BODY
    assert "https://pypi.org/pypi/agentworks-cli/json" in NORMALIZED_BODY
    assert "`uv tool install --upgrade 'agentworks-cli==VERSION'`" in NORMALIZED_BODY
    assert "require the selected exact version" in NORMALIZED_BODY
    assert "require version 0.14.0 or newer" in NORMALIZED_BODY
    assert NORMALIZED_BODY.count("`agw guide --agent`") == 2


def test_compatible_no_update_path_retains_and_verifies_the_installed_version() -> None:
    assert "retain it and skip installation" in NORMALIZED_BODY
    assert "After installation or update" in NORMALIZED_BODY
    assert "require the selected exact version" in NORMALIZED_BODY
    assert "For a retained installation, require version 0.14.0 or newer" in NORMALIZED_BODY


def test_no_compatible_stable_release_stops_before_installation_or_guide() -> None:
    assert (
        "If no exact compatible stable version at least 0.14.0 is available, explain that no compatible "
        "stable release is available. Make no installation or update attempt, do not run `agw guide "
        "--agent`, and ask me to retry after the release is published. Do not use a pre-release, a lower "
        "version, or an unpinned latest version."
    ) in NORMALIZED_BODY


def test_bootstrap_does_not_offer_source_review_or_ongoing_teaching() -> None:
    for removed in (
        "Source review",
        "Focused read-only review",
        "Full read-only review",
        "model usage",
        "protected policy root",
        "AGENTS.md",
        "CLAUDE.md",
        "concept-",
        "https://github.com/WayfarerLabs/agentworks/tree/",
    ):
        assert removed not in BODY
    assert "repository source" not in NORMALIZED_BODY


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
        assert " ".join(body.split()).count("`agw guide --agent`") == 2
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
    assert codex_manifest["interface"] == {
        "displayName": "Agentworks",
        "shortDescription": "Bootstrap Agentworks CLI",
        "longDescription": "Install or update the Agentworks CLI, verify it, and open its agent guide.",
        "developerName": "Wayfarer Labs",
        "category": "Productivity",
        "capabilities": ["CLI bootstrap"],
        "defaultPrompt": ["Install or update Agentworks and open its guide."],
    }
    assert codex_manifest["skills"] == "./skills/"
    assert codex_manifest["version"] == claude_manifest["version"] == "1.0.0"


def test_readme_projection_is_exact_and_human_installation_remains_below_it() -> None:
    readme = (ROOT / "README.md").read_text()
    begin = readme.index("<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->")
    end = readme.index("<!-- END GENERATED AGENTWORKS ASSISTANCE -->")
    projection = readme[begin:end]
    opening = re.search(r"\n(`{3,})markdown\n", projection)
    assert opening is not None
    fence = opening.group(1)
    projected_body = projection[opening.end() :]
    assert projected_body == BODY + fence + "\n\n"
    assert begin < readme.index("# Agentworks CLI bootstrap") < end
    assert end < readme.index("Install from PyPI:")


def test_permanent_installation_docs_use_https_and_do_not_claim_installation_is_authority() -> None:
    docs = (ROOT / "docs/agentworks-assistance-packages.md").read_text()
    assert "claude plugin marketplace add https://github.com/WayfarerLabs/agentworks.git" in docs
    assert "claude plugin install agentworks@agentworks" in docs
    assert "codex plugin marketplace add WayfarerLabs/agentworks" in docs
    assert "codex plugin add agentworks@agentworks" in docs
    normalized_docs = " ".join(docs.split())
    assert "grants no workstation or Agentworks permission" in normalized_docs
    assert "0.14.0 or newer" in normalized_docs
    assert "does not offer or perform repository source inspection" in normalized_docs
    assert "guide owns all ongoing Agentworks teaching" in normalized_docs
    assert "adds no authorization, security-setting, or harness-posture teaching" in normalized_docs
    assert "If no compatible stable release is available" in docs
    assert "does not install or update the CLI or invoke the guide" in normalized_docs
    assert "retry after the release is published" in normalized_docs
