"""Passive current-user native locations, shared by setup's independent roles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.native.common import defer
from agentworks.errors import ConfigError, ConnectivityError, ExternalError, StateError
from agentworks.execution.files import guest_path
from agentworks.execution.requests import Bytes, PythonProgram

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
    from agentworks.execution.access import VMAccess

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
print('\nAGW_NATIVE_USER_LAYOUT=' + json.dumps({
    'root': root, 'mcp_path': path,
    'physical_root': os.path.realpath(root), 'physical_home': os.path.realpath(home),
}))
"""

_PHYSICAL_PATHS = r"""
import json, os, sys
print(json.dumps([os.path.realpath(path) for path in json.load(sys.stdin)]))
"""


class NativeRootNotBeneathHomeError(ConfigError):
    """A native tool home whose physical path is not beneath the account's physical HOME: outside it, or HOME itself.

    Each account's tool homes stay inside its own HOME so that the account's
    lock covers them: the native CLIs' writes are not compare-and-swaps, so a
    root two accounts shared would need a lock of its own.
    """


def resolve_user_layout(
    tool: NativeLayoutTool, access: VMAccess, *, home: str, environment: Mapping[str, str]
) -> NativeUserLayout:
    """Validate HOME and the native-home export from the actual login shell, and confine the root to HOME.

    The observation is a Python program run after the account's login and
    interactive startup, as the native workload's environment is built, with
    its request on stdin. The root is compared by its physical path, links
    followed, against the physical HOME, and one not beneath it raises
    :class:`NativeRootNotBeneathHomeError`. The layout keeps the root's observed
    spelling, which ownership claims record.
    """
    variable, default = NATIVE_USER_DIRECTORIES[tool]
    request = json.dumps({"tool": tool, "home": home, "variable": variable, "default": default}).encode()
    result = access.run(
        PythonProgram(
            _USER_LAYOUT,
            login=True,
            interactive=True,
            env=dict(environment),
            stdin=Bytes(request),
            deadline=30,
        )
    )
    status = result.require_completed(access.vm_name)
    if status:
        raise ExternalError(f"Native user configuration location could not be resolved (exit {status})")
    lines = [
        line.removeprefix("AGW_NATIVE_USER_LAYOUT=")
        for line in result.stdout.decode("utf-8", "replace").splitlines()
        if line.startswith("AGW_NATIVE_USER_LAYOUT=")
    ]
    keys = ("root", "mcp_path", "physical_root", "physical_home")
    try:
        observed = json.loads(lines[0]) if len(lines) == 1 else None
        if not isinstance(observed, dict) or not all(isinstance(observed.get(key), str) for key in keys):
            raise ValueError
        layout = NativeUserLayout(guest_path(observed["root"]), guest_path(observed["mcp_path"]))
        physical_root, physical_home = guest_path(observed["physical_root"]), guest_path(observed["physical_home"])
    except (ValueError, RecursionError, StateError):
        raise ConfigError("Native user configuration locations must be absolute normalized paths") from None
    if not _inside(physical_root, physical_home):
        resolved = "" if physical_root == layout.root else f" (resolving to {physical_root!r})"
        where = "is HOME itself" if physical_root == physical_home.rstrip("/") else "is outside HOME"
        raise NativeRootNotBeneathHomeError(
            f"Native {tool} home {layout.root!r}{resolved} {where} ({physical_home!r}); "
            "it must be a directory beneath it",
            hint=(
                f"Point {variable} at a directory beneath HOME, or replace the link that leads outside it, "
                "and retry the owning setup."
            ),
        )
    return layout


def recorded_roots_outside_home(access: VMAccess, *, home: str, roots: Iterable[str]) -> frozenset[str]:
    """The recorded native roots whose physical path on the guest lies outside the physical ``home``.

    An earlier release recorded ownership under whatever root the login
    selected; reconciling one outside HOME would write where this account's
    lock does not reach. Links are followed and a missing tail is taken as
    spelled, so a root already removed is judged by the path it names.
    """
    roots = tuple(dict.fromkeys(roots))
    if not roots:
        return frozenset()
    result = access.run(PythonProgram(_PHYSICAL_PATHS, stdin=Bytes(json.dumps([home, *roots]).encode()), deadline=30))
    status = result.require_completed(access.vm_name)
    if status:
        raise ExternalError(f"Recorded native configuration homes could not be resolved (exit {status})")
    try:
        observed = json.loads(result.stdout)
        if not isinstance(observed, list) or len(observed) != len(roots) + 1:
            raise ValueError
        physical_home, *physical = (guest_path(path) for path in observed)
    except (ValueError, RecursionError, StateError):
        raise ExternalError("Recorded native configuration homes resolved to unreadable paths") from None
    return frozenset(root for root, path in zip(roots, physical, strict=True) if not _inside(path, physical_home))


def _inside(path: str, home: str) -> bool:
    """Whether ``path`` lies beneath ``home``, on a component boundary."""
    return path.startswith(home.rstrip("/") + "/")


def user_layout(tool: NativeLayoutTool, invocation: UserSetupInvocation) -> NativeUserLayout | None:
    """Report unknown or unsupported current locations without replacing them with defaults."""
    try:
        return resolve_user_layout(
            tool, invocation.access, home=invocation.home, environment=invocation.request_environment()
        )
    except NativeRootNotBeneathHomeError as error:
        invocation.report_incomplete(f"{error}. {error.hint}")
        return None
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
