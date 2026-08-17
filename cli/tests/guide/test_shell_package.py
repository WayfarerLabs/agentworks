from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


def _build(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True, capture_output=True, text=True)


def test_wheel_and_source_distribution_vendor_the_same_canonical_readme(tmp_path: Path) -> None:
    project = Path(__file__).parents[2]
    repository = project.parent
    environment = {**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache")}
    direct_dist = tmp_path / "direct-dist"
    _build(["uv", "build", "--wheel", "--sdist", "--out-dir", str(direct_dist)], project, environment)

    wheel = next(direct_dist.glob("*.whl"))
    source_distribution = next(direct_dist.glob("*.tar.gz"))
    expected_readme = (repository / "README.md").read_bytes()
    with zipfile.ZipFile(wheel) as archive:
        packaged = set(archive.namelist())
        assert archive.read("agentworks/_guide_sources/README.md") == expected_readme
        assert "agentworks/guide/guide-content/core-model.md" in packaged
        assert "agentworks/plugins/apt/guide-content/apt.md" in packaged
        assert "agentworks/plugins/install_command/guide-content/install-commands.md" in packaged
        assert not any(name.endswith("guide-content/.markdownlint.jsonc") for name in packaged)

    extracted = tmp_path / "source"
    with tarfile.open(source_distribution) as archive:
        archive.extractall(extracted, filter="data")
    source_root = next(extracted.iterdir())
    assert (source_root / "agentworks" / "_guide_sources" / "README.md").read_bytes() == expected_readme

    rebuilt_dist = tmp_path / "rebuilt-dist"
    _build(["uv", "build", "--wheel", "--out-dir", str(rebuilt_dist)], source_root, environment)
    rebuilt_wheel = next(rebuilt_dist.glob("*.whl"))
    with zipfile.ZipFile(rebuilt_wheel) as archive:
        assert archive.read("agentworks/_guide_sources/README.md") == expected_readme

    wheel_environment = tmp_path / "wheel-environment"
    _build(
        ["uv", "venv", "--python", sys.executable, "--system-site-packages", str(wheel_environment)],
        tmp_path,
        environment,
    )
    python = wheel_environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _build(["uv", "pip", "install", "--python", str(python), str(wheel)], tmp_path, environment)
    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            (
                "from agentworks.guide import GuideMode, render_guide; "
                "result = render_guide(('concept-core-model',), GuideMode.HUMAN); "
                "assert result.exit_code == 0; "
                "assert result.markdown.count('raw.githubusercontent.com') == 2"
            ),
        ],
        cwd=tmp_path,
        env={**environment, "PYTHONPATH": ""},
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0
