#!/usr/bin/env python3
"""Launch the real CLI with two fixed, process-local facet acceptance fixtures."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.capabilities.descriptor import Facet
    from agentworks.capabilities.harness_integration.setup import SetupGap, SetupReadiness


def _install_fixtures() -> None:
    from agentworks.capabilities.harness_integration.base import HarnessIntegration
    from agentworks.capabilities.harness_integration.setup import SetupGap
    from agentworks.capabilities.harness_integration.shell import ShellConfig, ShellIntegration
    from agentworks.plugins import SYSTEM_PLUGINS, Plugin, register_plugin

    class ReadinessConfig(ShellConfig):
        prerequisite: Literal["none", "required", "recommended"] = "none"

    class ReadinessIntegration(ShellIntegration):
        name = "facet-test-readiness"
        description = "Local actual-user readiness acceptance fixture"
        prose = None
        config_model = ReadinessConfig

        @classmethod
        def config_for(cls, facet: Facet | None = None) -> type[BaseModel] | None:
            return super().config_for(facet) if facet in ("vm", "user", "workspace") else ReadinessConfig

        @property
        def config(self) -> ReadinessConfig:
            return self._config_as(ReadinessConfig)

        def check_setup(self, evidence: SetupReadiness) -> tuple[SetupGap, ...]:
            severity = self.config.prerequisite
            if severity == "none":
                return ()
            user = evidence.user
            return () if user.current else (SetupGap(user, severity, "Fixture requires actual-user setup"),)

    class UnsupportedIntegration(ReadinessIntegration):
        name = "facet-test-unsupported"
        description = "Local unsupported user-facet acceptance fixture"
        user_init = HarnessIntegration.user_init

    plugin = Plugin(
        name="facet-test-fixtures",
        capabilities={"harness-integration": (ReadinessIntegration, UnsupportedIntegration)},
    )
    register_plugin(plugin)
    # Publication still requires explicit [plugins].system opt-in in the supplied HOME.
    SYSTEM_PLUGINS[plugin.name] = plugin


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home", required=True, type=Path, help="Existing absolute isolated test HOME with prepared config"
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="Ordinary agw arguments after --")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("facet fixture CLI requires a POSIX host and a prepared .venv/bin/python")
    import pwd

    home = args.home
    if not home.is_absolute() or not home.is_dir():
        parser.error("--home must name an existing absolute isolated test HOME")
    if home.resolve() == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve():
        parser.error("--home must differ from the current account's home directory")
    if not (home / ".config" / "agentworks" / "config.toml").is_file():
        parser.error("--home must already contain .config/agentworks/config.toml")

    override = os.environ.get("AGW_CLI_DIR")
    root = Path(override).resolve() if override else Path(sys.prefix).resolve().parent
    interpreter = root / ".venv" / "bin" / "python"
    if not (root / "pyproject.toml").is_file() or not (root / "agentworks").is_dir() or not interpreter.is_file():
        parser.error("select a prepared agentworks CLI tree with AGW_CLI_DIR or invoke its .venv/bin/python")
    if not sys.flags.isolated or Path(sys.prefix).resolve() != (root / ".venv").resolve():
        # Preserve the venv launcher path: resolving its symlink would select the base interpreter.
        os.execve(
            str(interpreter),
            [str(interpreter), "-I", str(Path(__file__).resolve()), *sys.argv[1:]],
            dict(os.environ),
        )

    import agentworks

    if not Path(agentworks.__file__).resolve().is_relative_to(root / "agentworks"):
        parser.error("selected interpreter imports agentworks outside the selected CLI tree")

    # Transport credentials and PATH belong to the test session. Only HOME is selected here.
    os.environ["HOME"] = str(home)
    _install_fixtures()
    from agentworks.cli import main as cli_main

    arguments = args.arguments
    sys.argv = ["agw", *(arguments[1:] if arguments[:1] == ["--"] else arguments)]
    cli_main()


if __name__ == "__main__":
    main()
