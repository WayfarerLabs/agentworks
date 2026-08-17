"""Build hook for the one repository-root document consumed by the guide."""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_PACKAGE_DESTINATION = "agentworks/_guide_sources/README.md"


class CustomBuildHook(BuildHookInterface):
    """Vendor the canonical root README into every distributable CLI artifact."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        project_root = Path(self.root).resolve()
        vendored = project_root / _PACKAGE_DESTINATION
        if vendored.is_file():
            return

        repository_root = project_root.parent
        canonical = repository_root / "README.md"
        if not (
            (repository_root / ".git").exists()
            and canonical.is_file()
            and (project_root / "pyproject.toml").is_file()
            and (project_root / "agentworks").is_dir()
        ):
            raise RuntimeError("the canonical repository README is unavailable for the Agentworks CLI build")

        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise RuntimeError("the Hatch build target supplied an invalid force-include mapping")
        force_include[str(canonical)] = _PACKAGE_DESTINATION
