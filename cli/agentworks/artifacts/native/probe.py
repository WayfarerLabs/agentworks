"""Read-only native launch checks, before any artifact publication or teardown."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.transports import Transport

# Executed in the same login-shell environment as the native workload. Only selected
# non-secret discovery facts escape; native help output and settings bodies do not.
_PROBE = r"""
import fnmatch, json, os, pathlib, re, shutil, subprocess, sys, tomllib
request = json.loads(sys.argv[1])
tool = request['tool']
home = os.environ.get('HOME', '')
variable, default = {'claude': ('CLAUDE_CONFIG_DIR', '.claude'),
                     'codex': ('CODEX_HOME', '.codex'), 'grok': ('GROK_HOME', '.grok')}[tool]
native_home = os.environ.get(variable) or home + '/' + default
problems = []
skill_names = {name[6:] for name in request['identities'] if name.startswith('skill:')}
agent_names = {name[6:] for name in request['identities'] if name.startswith('agent:')}
if request['home'] is not None and home != request['home']:
    problems.append('home-mismatch')
if not native_home.startswith('/') or any(p in ('', '.', '..') for p in native_home.split('/')[1:]):
    problems.append('invalid-native-home')
executable = shutil.which(tool)
if request['workspace_only']:
    pass
elif executable is None:
    problems.append('missing-command')
else:
    version = subprocess.run([executable, '--version'], text=True, capture_output=True, timeout=20)
    match = re.search(r'\b(\d+)\.(\d+)\.(\d+)\b', version.stdout)
    minimum = {'claude': (2, 1, 265), 'codex': (0, 153, 4), 'grok': (1, 0, 10)}[tool]
    if version.returncode or match is None or tuple(map(int, match.groups())) < minimum:
        problems.append('unsupported-native-version')
if executable and request['flags']:
    help_result = subprocess.run([executable, '--help'], text=True, capture_output=True, timeout=20)
    if help_result.returncode or any(flag not in help_result.stdout for flag in request['flags']):
        problems.append('unsupported-native-cli')
roots = [] if request['workspace_only'] else [pathlib.Path(native_home)]
workspace = request['workspace']
if workspace:
    roots.append(pathlib.Path(workspace) / default)
if not request['check_policy']:
    roots = []
paths = []
for root in roots:
    paths.extend([root / 'settings.json', root / 'settings.local.json'] if tool == 'claude' else [root / 'config.toml'])
if tool == 'claude' and not request['workspace_only'] and request['check_policy']:
    paths.append(pathlib.Path('/etc/claude-code/managed-settings.json'))
for path in paths:
    try:
        raw = path.read_bytes()
        document = json.loads(raw) if path.suffix == '.json' else tomllib.loads(raw.decode())
        if not isinstance(document, dict):
            raise ValueError()
        if tool == 'claude':
            rule_paths = [p for p in request['paths'] if '/rules/' in p]
            for pattern in document.get('claudeMdExcludes', []):
                if any(character in pattern for character in ('{', '(', '!')) and rule_paths:
                    problems.append('unsupported-discovery-pattern')
                if any(fnmatch.fnmatchcase(p, os.path.expanduser(pattern)) for p in rule_paths):
                    problems.append('native-discovery-exclusions')
            if request['session_plugin'] and any(
                value is False and name.split('@')[0] == 'agentworks-artifacts'
                for name, value in document.get('enabledPlugins', {}).items()
            ):
                problems.append('native-plugin-policy')
        elif tool == 'codex':
            skills = document.get('skills', {})
            if skill_names and skills.get('include_instructions') is False:
                problems.append('native-skill-policy')
            for item in skills.get('config', []):
                if item.get('enabled') is not False:
                    continue
                selected = item.get('name') in skill_names or any(
                    p == item.get('path') or p.startswith(str(item.get('path')) + '/') for p in request['paths']
                )
                if selected:
                    problems.append('native-skill-policy')
            if agent_names and document.get('features', {}).get('multi_agent') is False:
                problems.append('native-agent-policy')
        elif path.parent == pathlib.Path(native_home):
            subagents = document.get('subagents', {})
            if agent_names and (subagents.get('enabled') is False or any(
                subagents.get('toggle', {}).get(name) is False for name in agent_names
            )):
                problems.append('native-agent-policy')
            if skill_names.intersection(document.get('skills', {}).get('disabled', [])):
                problems.append('native-skill-policy')
    except FileNotFoundError:
        pass
    except Exception:
        problems.append('unreadable-native-policy')
if tool == 'grok' and workspace and request['paths']:
    result = subprocess.run(['git', '-C', workspace, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if result.returncode == 0:
        for path in request['paths']:
            if not path.startswith(workspace.rstrip('/') + '/'):
                continue
            ignored = subprocess.run(['git', '-C', workspace, 'check-ignore', '--no-index', '--', path],
                                     capture_output=True)
            if ignored.returncode == 0:
                problems.append('native-discovery-exclusions')
            elif ignored.returncode != 1:
                problems.append('unreadable-native-policy')
print('AGW_ARTIFACT_PROBE=' + json.dumps({'home': home, 'native_home': native_home, 'problems': sorted(set(problems))}))
"""


_PROBLEM_MESSAGES = {
    "home-mismatch": "the login shell changed the actual user's HOME",
    "invalid-native-home": "the native home is not an absolute normalized directory",
    "missing-command": "the native command is unavailable in the login environment",
    "unsupported-native-version": "the native version is older than the supported artifact baseline",
    "unsupported-native-cli": "the native CLI lacks a required artifact carrier flag",
    "native-discovery-exclusions": "native discovery excludes an artifact file",
    "unsupported-discovery-pattern": "the adapter cannot evaluate an extended native exclusion pattern",
    "native-plugin-policy": "native policy disables the generated artifact plugin",
    "native-skill-policy": "native policy disables a supplied skill or its discovery instructions",
    "native-agent-policy": "native policy disables a supplied agent persona",
    "unreadable-native-policy": "native discovery configuration cannot be read or parsed",
}


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
    identities: tuple[str, ...] = (),
    check_policy: bool = True,
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
        "identities": identities,
        "check_policy": check_policy,
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
        if any(not isinstance(problem, str) or problem not in _PROBLEM_MESSAGES for problem in problems):
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise StateError(f"could not verify {tool} artifact discovery on the launch target") from None
    if problems:
        raise StateError(
            f"{tool} artifact delivery: " + "; ".join(_PROBLEM_MESSAGES[problem] for problem in problems),
            hint="Inspect the native home, discovery exclusions, disabled artifacts, and installed CLI version.",
        )
    return root
