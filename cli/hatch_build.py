"""Build hook for the curated repository documents consumed by the guide."""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_CURATED_SOURCES = {
    "README.md": "agentworks/_guide_sources/README.md",
    "docs/manifesto.md": "agentworks/_guide_sources/docs/manifesto.md",
}


class CustomBuildHook(BuildHookInterface):
    """Vendor canonical guide sources into every distributable CLI artifact."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        project_root = Path(self.root).resolve()
        repository_root = project_root.parent
        if not (
            (repository_root / ".git").exists()
            and (project_root / "pyproject.toml").is_file()
            and (project_root / "agentworks").is_dir()
        ):
            if all((project_root / destination).is_file() for destination in _CURATED_SOURCES.values()):
                return
            raise RuntimeError("canonical repository guide sources are unavailable for the Agentworks CLI build")

        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise RuntimeError("the Hatch build target supplied an invalid force-include mapping")
        for source, destination in _CURATED_SOURCES.items():
            canonical = repository_root / source
            if not canonical.is_file():
                raise RuntimeError(f"canonical guide source {source!r} is unavailable for the Agentworks CLI build")
            force_include[str(canonical)] = destination
