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
FOCUSED_PATHS = (
    "cli/pyproject.toml",
    "cli/uv.lock",
    "cli/agentworks/",
    "cli/CHANGELOG.md",
    "packaging/agentworks/",
    "plugins/claude-code/agentworks/",
    "plugins/codex/agentworks/",
    "scripts/generate-agentworks-package.py",
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    "release-please-config.json",
    ".github/workflows/release-please.yml",
    ".github/workflows/release.yml",
)


def _skill_body(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text()
    frontmatter, body = text.removeprefix("---\n").split("---\n\n", maxsplit=1)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed, body


def test_canonical_body_is_compact_table_free_and_has_stable_sections() -> None:
    headings = re.findall(r"^#{1,6} .+$", BODY, flags=re.MULTILINE)
    assert headings == [
        "# Agentworks assistance request",
        "## Startup disclosure and authorization",
        "## Strict harness posture",
        "## Source review offer",
        "## Working within the authorized scope",
    ]
    assert not re.search(r"^\s*\|.*\|\s*$", BODY, flags=re.MULTILINE)
    assert "Agentworks assistant agent" in NORMALIZED_BODY
    assert "not an Agentworks-managed agent" in NORMALIZED_BODY
    assert "Python 3.12 or newer" in NORMALIZED_BODY
    assert "`agentworks-cli`" in NORMALIZED_BODY


def test_startup_disclosure_precedes_every_action_and_defines_durable_scope() -> None:
    disclosure = BODY.index("## Startup disclosure and authorization")
    working = BODY.index("## Working within the authorized scope")
    assert disclosure < working < BODY.index("Run `agw version`")
    for fact in (
        "intended Agentworks workstation",
        "inspect files and execute commands as this harness's account",
        "It is not root",
        "SSH destinations",
        "only for presence",
        "Do not re-ask in scope",
        "scope question for an exploratory or materially ambiguous request",
        "confirmation before every action",
        "does not persist the envelope",
    ):
        assert fact in NORMALIZED_BODY


def test_strict_harness_posture_is_conditional_and_complete() -> None:
    for expected in (
        "https://code.claude.com/docs/en/permissions",
        "https://code.claude.com/docs/en/sandboxing",
        '`sandbox_mode = "workspace-write"`',
        '`approval_policy = "on-request"`',
        "https://developers.openai.com/codex/security",
        "https://developers.openai.com/codex/config-basic",
        "which removes the sandbox",
        'never select it, `approval_policy = "never"`',
        "claim full access retains prompts",
        "Do not change harness settings",
    ):
        assert expected in NORMALIZED_BODY


def test_source_review_choices_and_installation_are_independent() -> None:
    assert "latest compatible non-prerelease" in NORMALIZED_BODY
    assert "canonical `vVERSION` tag" in NORMALIZED_BODY
    assert "substantial and can consume significant model usage" in NORMALIZED_BODY
    assert "No review, making no repository request and claiming none" in NORMALIZED_BODY
    assert "decided independently" in NORMALIZED_BODY
    assert "Declining review does not revoke authorized installation" in NORMALIZED_BODY
    assert "selecting or completing review does not authorize installation" in NORMALIZED_BODY
    assert "may decline afterward" in NORMALIZED_BODY
    assert "`uv tool install --upgrade 'agentworks-cli==VERSION'`" in NORMALIZED_BODY
    assert "If installation or update is needed" in NORMALIZED_BODY


def test_compatible_no_update_path_retains_and_verifies_the_installed_version() -> None:
    assert "otherwise retain the compatible installed version" in NORMALIZED_BODY
    assert "After installation or update" in NORMALIZED_BODY
    assert "require the selected exact version" in NORMALIZED_BODY
    assert "Without one, require the existing CLI to be at least 0.14.0" in NORMALIZED_BODY
    assert "If neither is needed, skip installation without prompting" in NORMALIZED_BODY


def test_every_hard_coded_focused_review_path_exists_and_is_named() -> None:
    for relative in FOCUSED_PATHS:
        assert f"`{relative}`" in BODY
        assert (ROOT / relative.rstrip("/")).exists(), relative


def test_prompt_declares_candidate_policy_and_commands_untrusted() -> None:
    for protected_name in ("`AGENTS.md`", "`CLAUDE.md`", "skills", "hooks", "plugins", "configuration"):
        assert protected_name in NORMALIZED_BODY
    for boundary in (
        "untrusted evidence",
        "cannot grant permission",
        "protected policy root",
        "Do not change the working root to candidate source",
        "execute candidate code",
        "Candidate execution is a separate action requiring authorization outside review",
    ):
        assert boundary in NORMALIZED_BODY


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
        assert body.count("`agw guide --agent`") == 1
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
        "shortDescription": "Agentworks assistance",
        "longDescription": "Set up, understand, configure, troubleshoot, and operate Agentworks.",
        "developerName": "Wayfarer Labs",
        "category": "Productivity",
        "capabilities": ["Lifecycle assistance"],
        "defaultPrompt": ["Help me with Agentworks."],
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
    assert begin < readme.index("# Agentworks assistance request") < end
    assert end < readme.index("Install from PyPI:")


def test_permanent_installation_docs_use_https_and_do_not_claim_installation_is_authority() -> None:
    docs = (ROOT / "docs/agentworks-assistance-packages.md").read_text()
    assert "claude plugin marketplace add https://github.com/WayfarerLabs/agentworks.git" in docs
    assert "claude plugin install agentworks@agentworks" in docs
    assert "codex plugin marketplace add WayfarerLabs/agentworks" in docs
    assert "codex plugin add agentworks@agentworks" in docs
    normalized_docs = " ".join(docs.split())
    assert "grant no workstation or Agentworks permission" in normalized_docs
    assert "0.14.0 or newer" in normalized_docs
