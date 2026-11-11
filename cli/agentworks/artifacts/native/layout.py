"""Passive current-user native locations, shared by setup's independent roles."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.native.common import defer
from agentworks.errors import ConfigError, ConnectivityError, ExternalError, StateError
from agentworks.native_files import native_path

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
    from agentworks.transports import Transport

type NativeLayoutTool = Literal["claude", "codex", "grok"]
NATIVE_USER_DIRECTORIES = {
    "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
    "codex": ("CODEX_HOME", ".codex"),
    "grok": ("GROK_HOME", ".grok"),
}


@dataclass(frozen=True)
class NativeUserLayout:
    """One login observation, before wrappers bind the selected native root."""

    root: str
    mcp_path: str


_USER_LAYOUT = r"""
import json, os, sys
request = json.load(sys.stdin)
home = os.environ.get('HOME', '')
if home != request['home']:
    raise SystemExit(2)
override = os.environ.get(request['variable'])
root = override or home + '/' + request['default']
path = ((override + '/.claude.json' if override else home + '/.claude.json')
        if request['tool'] == 'claude' else root + '/config.toml')
print('AGW_NATIVE_USER_LAYOUT=' + json.dumps({'root': root, 'mcp_path': path}))
"""


def resolve_user_layout(
    tool: NativeLayoutTool, runner: Transport, *, home: str, environment: Mapping[str, str]
) -> NativeUserLayout:
    """Validate only HOME and the native-home export from the actual login shell."""
    variable, default = NATIVE_USER_DIRECTORIES[tool]
    command = shlex.join(["python3", "-c", _USER_LAYOUT])
    result = runner.run(
        f'"$SHELL" -lic {shlex.quote(command)}',
        env=dict(environment),
        input_data=json.dumps({"tool": tool, "home": home, "variable": variable, "default": default}),
        tty=False,
        check=False,
        timeout=30,
    )
    if not result.ok:
        raise ExternalError("Native user configuration location could not be resolved")
    lines = [
        line.removeprefix("AGW_NATIVE_USER_LAYOUT=")
        for line in result.stdout.splitlines()
        if line.startswith("AGW_NATIVE_USER_LAYOUT=")
    ]
    try:
        observed = json.loads(lines[0]) if len(lines) == 1 else None
        if not isinstance(observed, dict) or not all(
            isinstance(observed.get(key), str) for key in ("root", "mcp_path")
        ):
            raise ValueError
        return NativeUserLayout(native_path(observed["root"]), native_path(observed["mcp_path"]))
    except (ValueError, RecursionError, StateError):
        raise ConfigError("Native user configuration locations must be absolute normalized paths") from None


def user_layout(tool: NativeLayoutTool, invocation: UserSetupInvocation) -> NativeUserLayout | None:
    """Report unknown current locations without replacing them with defaults."""
    try:
        return resolve_user_layout(tool, invocation.runner, home=invocation.home, environment=invocation.environment)
    except (ConfigError, ConnectivityError, ExternalError):
        invocation.report_incomplete(_UNAVAILABLE)
        return None


_UNAVAILABLE = (
    "Native user configuration locations could not be resolved safely; repair the login environment and retry."
)


def unavailable_user_artifacts(invocation: UserSetupInvocation, *, retiring: bool) -> ArtifactApplication:
    """Preserve existing file effects when no current discovery authority is known."""
    if invocation.prior and invocation.prior.artifact_files:
        # Failing the facet keeps core from interpreting an empty plan as file retirement.
        raise ExternalError(_UNAVAILABLE)
    return ArtifactApplication() if retiring else defer(invocation.artifacts, "session", _UNAVAILABLE)
