from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/generate-agentworks-package.py"
SPEC = importlib.util.spec_from_file_location("agentworks_package_generator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


@pytest.fixture
def clean_package_root(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "packaging/agentworks").mkdir(parents=True)
    shutil.copy2(ROOT / generator.CANONICAL_BODY, root / generator.CANONICAL_BODY)
    shutil.copy2(ROOT / generator.METADATA_FILE, root / generator.METADATA_FILE)
    (root / "README.md").write_text(
        "# Fixture\n\n<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->\n<!-- END GENERATED AGENTWORKS ASSISTANCE -->\n"
    )
    return root


def _frontmatter(skill: bytes) -> tuple[dict[str, object], bytes]:
    assert skill.startswith(b"---\n")
    raw, body = skill[4:].split(b"---\n\n", maxsplit=1)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed, body


def test_clean_generation_is_deterministic_and_check_is_read_only(clean_package_root: Path) -> None:
    root = clean_package_root
    changed = generator.generate(root, check=False)
    assert changed == generator.GENERATED_OUTPUTS
    first = {path: (root / path).read_bytes() for path in generator.GENERATED_OUTPUTS}
    mtimes = {path: (root / path).stat().st_mtime_ns for path in generator.GENERATED_OUTPUTS}

    assert generator.generate(root, check=True) == ()
    assert generator.generate(root, check=False) == ()
    assert {path: (root / path).read_bytes() for path in generator.GENERATED_OUTPUTS} == first
    assert {path: (root / path).stat().st_mtime_ns for path in generator.GENERATED_OUTPUTS} == mtimes
    assert not tuple(root.rglob("*.tmp"))


def test_committed_checkout_matches_generated_outputs() -> None:
    assert generator.generate(ROOT, check=True) == ()


def test_check_reports_every_stale_or_missing_path_without_writing(clean_package_root: Path) -> None:
    root = clean_package_root
    generator.generate(root, check=False)
    stale = root / generator.CLAUDE_MANIFEST
    missing = root / generator.CODEX_SKILL
    stale.write_text("stale\n")
    missing.unlink()

    assert generator.generate(root, check=True) == (generator.CLAUDE_MANIFEST, generator.CODEX_SKILL)
    assert stale.read_text() == "stale\n"
    assert not missing.exists()


def test_command_check_exits_nonzero_without_repairing_drift(clean_package_root: Path) -> None:
    root = clean_package_root
    (root / "scripts").mkdir()
    shutil.copy2(SCRIPT, root / "scripts/generate-agentworks-package.py")
    generator.generate(root, check=False)
    stale = root / generator.CODEX_MANIFEST
    stale.write_text("stale\n")

    result = subprocess.run(
        [sys.executable, "scripts/generate-agentworks-package.py", "--check"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "plugins/codex/agentworks/.codex-plugin/plugin.json" in result.stdout
    assert stale.read_text() == "stale\n"


def test_unexpected_generated_file_is_rejected(clean_package_root: Path) -> None:
    root = clean_package_root
    generator.generate(root, check=False)
    extra = root / generator.CODEX_ROOT / "commands/install.md"
    extra.parent.mkdir(parents=True)
    extra.write_text("not owned\n")

    with pytest.raises(generator.GenerationError, match="commands/install.md"):
        generator.generate(root, check=False)


@pytest.mark.parametrize("run_length", [3, 4, 7])
def test_readme_fence_is_minimally_collision_proof(clean_package_root: Path, run_length: int) -> None:
    root = clean_package_root
    body = f"# Body\n\n{'`' * run_length}\ninside\n{'`' * run_length}\n".encode()
    (root / generator.CANONICAL_BODY).write_bytes(body)

    generator.generate(root, check=False)
    readme = (root / "README.md").read_bytes()
    start = readme.index(generator.README_BEGIN) + len(generator.README_BEGIN) + 2
    end = readme.index(generator.README_END)
    projection = readme[start:end]
    opener, projected_body = projection.split(b"\n", maxsplit=1)
    fence = opener.removesuffix(b"markdown")

    assert opener == fence + b"markdown"
    assert fence == b"`" * (run_length + 1)
    assert projected_body == body + fence + b"\n\n"


@pytest.mark.parametrize(
    "readme",
    [
        "# Missing\n",
        "<!-- END GENERATED AGENTWORKS ASSISTANCE -->\n<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->\n",
        "<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->\n"
        "<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->\n"
        "<!-- END GENERATED AGENTWORKS ASSISTANCE -->\n",
    ],
)
def test_readme_requires_one_ordered_marker_pair(clean_package_root: Path, readme: str) -> None:
    (clean_package_root / "README.md").write_text(readme)
    with pytest.raises(generator.GenerationError, match="README"):
        generator.render_outputs(clean_package_root)


def test_skills_share_exact_body_and_skill_description_owner(clean_package_root: Path) -> None:
    root = clean_package_root
    generator.generate(root, check=False)
    metadata = json.loads((root / generator.METADATA_FILE).read_text())
    body = (root / generator.CANONICAL_BODY).read_bytes()

    claude_frontmatter, claude_body = _frontmatter((root / generator.CLAUDE_SKILL).read_bytes())
    codex_frontmatter, codex_body = _frontmatter((root / generator.CODEX_SKILL).read_bytes())

    assert claude_body == codex_body == body
    assert claude_frontmatter == codex_frontmatter
    assert claude_frontmatter["description"] == metadata["skillDescription"]
    assert claude_frontmatter["metadata"] == {
        "agentworks-package-version": "1.0.0",
        "agentworks-min-cli-version": "0.14.0",
    }


def test_metadata_schema_rejects_unknown_fields(clean_package_root: Path) -> None:
    path = clean_package_root / generator.METADATA_FILE
    metadata = json.loads(path.read_text())
    metadata["anotherDescription"] = "A drifting description"
    path.write_text(json.dumps(metadata))

    with pytest.raises(generator.GenerationError, match="unknown anotherDescription"):
        generator.load_metadata(path)


def test_atomic_write_preserves_destination_when_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "artifact.json"
    destination.write_bytes(b"original\n")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError(f"refused {source} -> {target}")

    monkeypatch.setattr(generator.os, "replace", fail_replace)
    with pytest.raises(OSError, match="refused"):
        generator._atomic_write(destination, b"replacement\n")

    assert destination.read_bytes() == b"original\n"
    assert tuple(tmp_path.iterdir()) == (destination,)


def _fingerprint(blobs: dict[Path, bytes]) -> str:
    digest = hashlib.sha256()
    for path in generator.PLUGIN_OUTPUTS:
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update(blobs[path])
        digest.update(b"\0")
    return digest.hexdigest()


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True)


def _missing_base_policy(*, github_actions: bool) -> str:
    return "fail" if github_actions else "skip"


def _is_strict_version_bump(*, current: str, previous: str) -> bool:
    current_parts = tuple(int(part) for part in current.split("."))
    previous_parts = tuple(int(part) for part in previous.split("."))
    return current_parts > previous_parts


def test_missing_fingerprint_base_can_skip_only_outside_github_actions() -> None:
    assert _missing_base_policy(github_actions=False) == "skip"
    assert _missing_base_policy(github_actions=True) == "fail"


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [
        ("1.0.1", "1.0.0", True),
        ("1.1.0", "1.0.9", True),
        ("2.0.0", "1.99.99", True),
        ("1.0.0", "1.0.0", False),
        ("0.9.9", "1.0.0", False),
    ],
)
def test_package_version_bump_is_strictly_increasing(current: str, previous: str, expected: bool) -> None:
    assert _is_strict_version_bump(current=current, previous=previous) is expected


def test_changed_package_fingerprint_requires_package_version_bump() -> None:
    configured_base = os.environ.get("AGENTWORKS_PACKAGE_BASE_REF", "origin/main")
    base_check = _git("rev-parse", "--verify", configured_base, check=False)
    if base_check.returncode != 0:
        if _missing_base_policy(github_actions=os.environ.get("GITHUB_ACTIONS") == "true") == "fail":
            pytest.fail(f"authoritative package base ref is unavailable: {configured_base}")
        pytest.skip(f"package base ref is unavailable: {configured_base}")
    merge_base = _git("merge-base", configured_base, "HEAD").stdout.decode().strip()
    old_metadata_result = _git("show", f"{merge_base}:{generator.METADATA_FILE}", check=False)
    current_metadata = json.loads((ROOT / generator.METADATA_FILE).read_text())
    if old_metadata_result.returncode != 0:
        assert current_metadata["packageVersion"] == "1.0.0"
        return

    old_metadata = json.loads(old_metadata_result.stdout)
    old_blobs: dict[Path, bytes] = {}
    for path in generator.PLUGIN_OUTPUTS:
        result = _git("show", f"{merge_base}:{path}", check=False)
        assert result.returncode == 0, f"baseline package inventory is incomplete: {path}"
        old_blobs[path] = result.stdout
    current_blobs = {path: (ROOT / path).read_bytes() for path in generator.PLUGIN_OUTPUTS}

    if _fingerprint(current_blobs) != _fingerprint(old_blobs):
        assert _is_strict_version_bump(
            current=current_metadata["packageVersion"], previous=old_metadata["packageVersion"]
        ), "generated package content changed without a metadata.json packageVersion bump"
