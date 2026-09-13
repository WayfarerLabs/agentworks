"""Read-only native launch checks, before any artifact publication or teardown."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, cast

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.transports import Transport

# Executed in the same login-shell environment as the native workload. Only selected
# non-secret discovery facts escape; native help output and settings bodies do not.
_PROBE = r"""
import json, os, pathlib, shutil, subprocess, sys, tomllib
request = json.loads(sys.argv[1])
tool = request['tool']
home = os.environ.get('HOME', '')
variable, default = {'claude': ('CLAUDE_CONFIG_DIR', '.claude'),
                     'codex': ('CODEX_HOME', '.codex'), 'grok': ('GROK_HOME', '.grok')}[tool]
native_home = os.environ.get(variable) or home + '/' + default
problems = []
if request['home'] is not None and home != request['home']:
    problems.append('home-mismatch')
if not native_home.startswith('/') or any(p in ('', '.', '..') for p in native_home.split('/')[1:]):
    problems.append('invalid-native-home')
executable = shutil.which(tool)
if request['workspace_only']:
    pass
elif executable is None:
    problems.append('missing-command')
elif request['flags']:
    help_result = subprocess.run([executable, '--help'], text=True, capture_output=True, timeout=20)
    if help_result.returncode or any(flag not in help_result.stdout for flag in request['flags']):
        problems.append('unsupported-native-cli')
roots = [] if request['workspace_only'] else [pathlib.Path(native_home)]
workspace = request['workspace']
if workspace:
    roots.append(pathlib.Path(workspace) / default)
paths = []
for root in roots:
    paths.extend([root / 'settings.json', root / 'settings.local.json'] if tool == 'claude' else [root / 'config.toml'])
if tool == 'claude' and not request['workspace_only']:
    paths.append(pathlib.Path('/etc/claude-code/managed-settings.json'))
for path in paths:
    try:
        raw = path.read_bytes()
        document = json.loads(raw) if path.suffix == '.json' else tomllib.loads(raw.decode())
        if not isinstance(document, dict):
            raise ValueError()
        if tool == 'claude':
            if document.get('claudeMdExcludes') or document.get('ignorePatterns'):
                problems.append('native-discovery-exclusions')
            if any(value is False for value in document.get('enabledPlugins', {}).values()):
                if request['session_plugin']:
                    problems.append('native-plugin-policy')
        elif tool == 'codex':
            skills = document.get('skills', {})
            if skills.get('include_instructions') is False or any(
                item.get('enabled') is False for item in skills.get('config', [])
            ):
                problems.append('native-skill-policy')
            if document.get('features', {}).get('multi_agent') is False:
                problems.append('native-agent-policy')
        else:
            if any(value is False for value in document.get('agents', {}).get('enabled', {}).values()):
                problems.append('native-agent-policy')
            if any(value is False for value in document.get('skills', {}).get('enabled', {}).values()):
                problems.append('native-skill-policy')
    except FileNotFoundError:
        pass
    except Exception:
        problems.append('unreadable-native-policy')
if tool == 'grok' and workspace and request['paths']:
    result = subprocess.run(['git', '-C', workspace, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if result.returncode == 0:
        for path in request['paths']:
            ignored = subprocess.run(['git', '-C', workspace, 'check-ignore', '--no-index', '--', path],
                                     capture_output=True)
            if ignored.returncode == 0:
                problems.append('native-discovery-exclusions')
            elif ignored.returncode != 1:
                problems.append('unreadable-native-policy')
print('AGW_ARTIFACT_PROBE=' + json.dumps({'home': home, 'native_home': native_home, 'problems': sorted(set(problems))}))
"""


def probe_native(
    runner: Transport,
    *,
    tool: str,
    environment: Mapping[str, str],
    home: str | None = None,
    workspace: str = "",
    paths: tuple[str, ...] = (),
    flags: tuple[str, ...] = (),
    session_plugin: bool = False,
    workspace_only: bool = False,
) -> str:
    """Check the actual native home and relevant native policy without starting a model."""
    request = {
        "tool": tool,
        "home": home,
        "workspace": workspace,
        "paths": paths,
        "flags": flags,
        "session_plugin": session_plugin,
        "workspace_only": workspace_only,
    }
    command = shlex.join(["python3", "-c", _PROBE, json.dumps(request)])
    result = runner.run(f'"$SHELL" -lic {shlex.quote(command)}', env=dict(environment), check=False, timeout=30)
    try:
        lines = [
            line.removeprefix("AGW_ARTIFACT_PROBE=")
            for line in result.stdout.splitlines()
            if line.startswith("AGW_ARTIFACT_PROBE=")
        ]
        observed = json.loads(lines[-1])
        root = observed["native_home"]
        problems = observed["problems"]
        if result.returncode or not isinstance(root, str) or not root.startswith("/") or not isinstance(problems, list):
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise StateError(f"could not verify {tool} artifact discovery on the launch target") from None
    if problems:
        raise StateError(
            f"{tool} artifact delivery is blocked by native discovery or configuration",
            hint="Inspect the native home, discovery exclusions, disabled artifacts, and installed CLI version.",
        )
    return cast("str", root)
