from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path


def test_installed_wheel_contains_every_authored_guide_block(tmp_path: Path) -> None:
    project = Path(__file__).parents[2]
    environment = {**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache")}
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next((tmp_path / "dist").glob("*.whl"))
    authored = {
        path.relative_to(project).as_posix() for path in (project / "agentworks").glob("**/guide-content/**/*.md")
    }
    assert authored
    expected_manifests = {
        "agentworks/plugins/apt/manifests/apt-packages.yaml",
        "agentworks/plugins/apt/manifests/apt-sources.yaml",
        "agentworks/plugins/install_command/manifests/install-commands.yaml",
    }
    with zipfile.ZipFile(wheel) as archive:
        packaged = set(archive.namelist())
        assert authored <= packaged
        assert expected_manifests <= packaged
        assert all(archive.read(path).strip() for path in expected_manifests)
        assert not any(name.endswith("guide-content/.markdownlint.jsonc") for name in packaged)
        assert archive.read("agentworks/CHANGELOG.md") == (project / "CHANGELOG.md").read_bytes()

    wheel_environment = tmp_path / "wheel-environment"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, "--system-site-packages", str(wheel_environment)],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    python = wheel_environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import agentworks; "
                "from agentworks.guide.service import build_authored_catalog; "
                "catalog = build_authored_catalog(strict=True); "
                "names = set(catalog.names()); "
                "assert {'plugin/apt/overview', 'plugin/install-command/overview'} <= names; "
                "assert not any(issue.error.source in {'system-plugin:apt', 'system-plugin:install-command'} "
                "for issue in catalog.issues); "
                "blocks = [block for topic in catalog.topics for block in topic.blocks "
                "if hasattr(block, 'markdown')]; "
                "assert blocks and all(block.markdown.strip() for block in blocks); "
                "print(agentworks.__file__)"
            ),
        ],
        cwd=tmp_path,
        env={**environment, "PYTHONPATH": ""},
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(wheel_environment) in probe.stdout
