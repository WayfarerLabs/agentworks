"""Read-only native launch checks, before any artifact publication or teardown."""

from __future__ import annotations

import json
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.artifacts.application import ArtifactFile
from agentworks.artifacts.frontmatter import parse_metadata
from agentworks.artifacts.native.common import MAX_CODEX_PERSONA_BYTES
from agentworks.errors import ExternalError, StateError
from agentworks.native_files import require_python3
from agentworks.package_sources import CaptureLimits
from agentworks.sources import SourceRefError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.transports import Transport

# Executed in the same login-shell environment as the native workload. Only selected
# bounded artifact frontmatter and discovery facts return for local identity checks.
# Native help output, settings bodies and artifact instruction bodies are not returned.
_PROBE = r"""
import fnmatch, json, os, pathlib, re, shutil, subprocess, sys, tomllib
request = json.load(sys.stdin)
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
    help_text = help_result.stdout
    if tool == 'claude':
        # Claude documents the file carrier with an optional suffix in its help.
        help_text = help_text.replace('--append-system-prompt[-file]',
                                      '--append-system-prompt --append-system-prompt-file')
    if help_result.returncode or any(
        not re.search(r'(?<![\w-])' + re.escape(flag) + r'(?![\w-])', help_text)
        for flag in request['flags']
    ):
        problems.append('unsupported-native-cli')
roots = [] if request['workspace_only'] else [pathlib.Path(native_home)]
workspace = request['workspace']
if workspace:
    roots.append(pathlib.Path(workspace) / default)
if not request['check_policy']:
    roots = []

def check_discovery_path(path, anchor):
    # Do not inspect native metadata through links beneath the actual owning root.
    relative = path.relative_to(anchor)
    if anchor.is_symlink():
        raise ValueError()
    for component in relative.parts:
        anchor = anchor / component
        if anchor.is_symlink():
            raise ValueError()

safe_roots = []
for root in roots:
    try:
        user_root = root == pathlib.Path(native_home)
        anchor = pathlib.Path(home) if user_root else pathlib.Path(workspace)
        # Session configuration may select an explicit native home outside HOME;
        # outer user publication applies its own stricter containment rule.
        root_anchor = pathlib.Path(root.anchor) if user_root and not root.is_relative_to(anchor) else anchor
        check_discovery_path(root, root_anchor)
        if tool == 'codex' and any(':' not in name for name in skill_names):
            check_discovery_path(anchor / '.agents' / 'skills', anchor)
        safe_roots.append(root)
    except Exception:
        problems.append('unreadable-native-inventory')
roots = safe_roots
inventory = []
known_entries = request['entries']
registered_agents = {}
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
            for name, agent in document.get('agents', {}).items():
                selected_path = ''
                if isinstance(agent, dict) and isinstance(agent.get('config_file'), str):
                    selected_path = os.path.normpath(os.path.join(str(path.parent), agent['config_file']))
                    registered_agents.setdefault(selected_path, []).append(name)
                if name in agent_names and known_entries.get(selected_path) != 'agent:' + name:
                    inventory.append({'kind': 'agent', 'path': str(path), 'name': name})
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
try:
    candidates = []
    scanned = 0
    existing_entries = set()
    discovery_roots = []
    for root in roots:
        skill_root = (root.parent / '.agents' / 'skills') if tool == 'codex' else root / 'skills'
        if tool == 'codex' and root == pathlib.Path(native_home):
            skill_root = pathlib.Path(home) / '.agents' / 'skills'
        if any(':' not in name for name in skill_names):
            discovery_roots.append(('skill', skill_root))
        if any(':' not in name for name in skill_names) and skill_root.exists():
            if skill_root.is_symlink():
                raise ValueError()
            for directory in skill_root.iterdir():
                existing_entries.add(str(directory))
                scanned += 1
                if scanned > 512 or directory.is_symlink() or (tool == 'codex' and directory.name == 'SKILL.md'):
                    raise ValueError()
                if not directory.is_dir():
                    continue
                marker = directory / 'SKILL.md'
                if marker.is_symlink() or (marker.exists() and not marker.is_file()):
                    raise ValueError()
                if tool == 'codex' or not marker.exists():
                    # Codex recursively discovers skills even beneath another entrypoint.
                    # Cleanup may also leave ordinary unowned files, which are not skills.
                    pending = [directory]
                    while pending:
                        for leftover in pending.pop().iterdir():
                            existing_entries.add(str(leftover))
                            scanned += 1
                            if (scanned > 512 or leftover.is_symlink() or
                                    (leftover.name == 'SKILL.md' and leftover != marker)):
                                raise ValueError()
                            if leftover.is_dir():
                                pending.append(leftover)
                            elif not leftover.is_file():
                                raise ValueError()
                if not marker.exists():
                    continue
                candidates.append(('skill', marker, directory.name))
                if len(candidates) > 512:
                    raise ValueError()
        agent_root = root / 'agents'
        if agent_names:
            discovery_roots.append(('agent', agent_root))
        if agent_names and agent_root.exists():
            if agent_root.is_symlink():
                raise ValueError()
            for path in agent_root.iterdir():
                existing_entries.add(str(path))
                scanned += 1
                if scanned > 512 or path.is_symlink() or path.is_dir():
                    raise ValueError()
                if path.suffix == ('.toml' if tool == 'codex' else '.md') and path.is_file():
                    candidates.append(('agent', path, path.stem))
                if len(candidates) > 512:
                    raise ValueError()
    proposed = {}
    proposed_entries = set()
    if tool == 'codex':
        # Count every future package member and implied directory, as the recursive scan will.
        for spelling in dict.fromkeys((*request['paths'], *request['proposed'])):
            path = pathlib.Path(spelling)
            for kind, root in discovery_roots:
                if kind != 'skill' or path == root or not path.is_relative_to(root):
                    continue
                for member in (path, *path.parents):
                    if member == root:
                        break
                    if member.name == 'SKILL.md' and (member != path or path.parent.parent != root):
                        raise ValueError()
                    proposed_entries.add(str(member))
    for spelling, size in request['proposed'].items():
        path = pathlib.Path(spelling)
        for kind, root in discovery_roots:
            selected = (path.name == 'SKILL.md' and path.parent.parent == root) if kind == 'skill' else (
                path.parent == root and path.suffix == ('.toml' if tool == 'codex' else '.md'))
            if selected:
                limit = request['codex_persona_bytes'] if tool == 'codex' and kind == 'agent' else 32768
                if size < 0 or size > limit:
                    raise ValueError()
                proposed[spelling] = (kind, size)
                proposed_entries.add(str(path.parent) if kind == 'skill' else spelling)
    if scanned + len(proposed_entries - existing_entries) > 512:
        raise ValueError()
    total = 0
    codex_total = 0
    observed_sizes = {}
    for kind, path, fallback in candidates:
        limit = request['codex_persona_bytes'] if tool == 'codex' and kind == 'agent' else 32768
        if tool == 'codex' and kind == 'agent':
            limit = min(limit, request['inventory_bytes'] - codex_total)
        with path.open('rb') as source:
            raw = source.read(limit + 1)
        if tool == 'codex' and kind == 'agent':
            if len(raw) > limit:
                raise ValueError()
            codex_total += len(raw)
            observed_sizes[str(path)] = (kind, len(raw))
            name = tomllib.loads(raw.decode()).get('name')
            if name is None:
                aliases = registered_agents.get(str(path), [])
                name = next((alias for alias in aliases if alias in agent_names), next(iter(aliases), None))
            if not isinstance(name, str):
                raise ValueError()
            inventory.append({'kind': kind, 'path': str(path), 'name': name})
        else:
            lines = raw.splitlines(keepends=True)
            frontmatter = ''
            if lines and lines[0].strip() == b'---':
                end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == b'---'), None)
                if end is None:
                    raise ValueError()
                frontmatter = b''.join(lines[1:end]).decode('utf-8')
            total += len(frontmatter.encode())
            observed_sizes[str(path)] = (kind, len(frontmatter.encode()))
            if total > 1048576:
                raise ValueError()
            inventory.append({'kind': kind, 'path': str(path), 'fallback_name': fallback, 'frontmatter': frontmatter})
    future = {**observed_sizes, **proposed}
    if sum(size for kind, size in future.values() if tool == 'codex' and kind == 'agent') > request['inventory_bytes']:
        raise ValueError()
    if sum(size for kind, size in future.values() if tool != 'codex' or kind != 'agent') > 1048576:
        raise ValueError()
except Exception:
    problems.append('unreadable-native-inventory')
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
print('AGW_ARTIFACT_PROBE=' + json.dumps({
    'home': home, 'native_home': native_home, 'problems': sorted(set(problems)), 'inventory': inventory
}))
"""


_PROBLEM_MESSAGES = {
    "home-mismatch": "the login shell changed the actual user's HOME",
    "invalid-native-home": "the native home is not an absolute normalized directory",
    "missing-command": "the native command is unavailable in the login environment",
    "unsupported-native-version": "the native version is older than the supported artifact baseline",
    "unsupported-native-cli": "the native CLI lacks a required artifact carrier flag",
    "native-discovery-exclusions": "a configured discovery exclusion matches an artifact file",
    "unsupported-discovery-pattern": "the adapter cannot evaluate an extended native exclusion pattern",
    "native-plugin-policy": "native configuration restricts the generated artifact plugin",
    "native-skill-policy": "native configuration restricts a supplied skill or its discovery instructions",
    "native-agent-policy": "native configuration restricts a supplied agent persona",
    "native-artifact-identity-mismatch": "a supplied native artifact file now declares a different name",
    "native-artifact-collision": "another native skill or persona claims a supplied artifact name",
    "unreadable-native-inventory": "native skill or persona inventory exceeds supported layout or metadata limits",
    "unreadable-native-policy": "native discovery configuration cannot be read or parsed",
}


def _proposed_sizes(files: Sequence[ArtifactFile | OwnedArtifactFile], tool: str) -> dict[str, int]:
    """Budget entrypoints using the same metadata constraints as the next native inventory.

    Only sizes travel to the target. Unsupported proposed metadata is marked for refusal
    if its path belongs to an inspected native root; private session carriers are separate.
    """
    result = {}
    for file in files:
        if not isinstance(file, ArtifactFile) or file.native_identity is None:
            continue
        path = PurePosixPath(file.path)
        kind, _, name = file.native_identity.partition(":")
        if not (
            (kind == "skill" and path.name == "SKILL.md")
            or (kind == "agent" and path.suffix == (".toml" if tool == "codex" else ".md"))
        ):
            continue
        try:
            if tool == "codex" and kind == "agent":
                size = len(file.data)
                if size > MAX_CODEX_PERSONA_BYTES:
                    raise ValueError
                metadata = tomllib.loads(file.data.decode())
            else:
                lines = file.data[:32769].splitlines(keepends=True)
                header = ""
                if lines and lines[0].strip() == b"---":
                    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == b"---"), None)
                    if end is None:
                        raise ValueError
                    header = b"".join(lines[1:end]).decode()
                size = len(header.encode())
                metadata = parse_metadata(header) if header else {}
            actual = metadata.get("name", path.parent.name if kind == "skill" else None)
            if actual != name.rsplit(":", 1)[-1]:
                raise ValueError
        except (ValueError, UnicodeError, SourceRefError, RecursionError):
            size = -1
        result[file.path] = size
    return result


def probe_native(
    runner: Transport,
    *,
    tool: str,
    environment: Mapping[str, str],
    home: str | None = None,
    workspace: str = "",
    files: Sequence[ArtifactFile | OwnedArtifactFile] = (),
    flags: tuple[str, ...] = (),
    session_plugin: bool = False,
    workspace_only: bool = False,
    check_policy: bool = True,
) -> str:
    """Check the actual native home and relevant native policy without starting a model."""
    require_python3(runner)
    entries = {file.path: file.native_identity for file in files if file.native_identity}
    identities = tuple(entries.values())
    request = {
        "tool": tool,
        "codex_persona_bytes": MAX_CODEX_PERSONA_BYTES,
        "inventory_bytes": CaptureLimits().total_bytes,
        "home": home,
        "workspace": workspace,
        "paths": tuple(file.path for file in files),
        "entries": entries,
        "proposed": _proposed_sizes(files, tool),
        "flags": flags,
        "session_plugin": session_plugin,
        "workspace_only": workspace_only,
        "identities": identities,
        "check_policy": check_policy,
    }
    command = shlex.join(["python3", "-c", _PROBE])
    result = runner.run(
        f'"$SHELL" -lic {shlex.quote(command)}',
        env=dict(environment),
        input_data=json.dumps(request),
        tty=False,
        check=False,
        timeout=30,
    )
    # Login startup scripts can echo the supplied runtime environment on failure.
    # Redact known values and their shell-quoted forms before publishing diagnostics.
    diagnostic = result.stderr
    redactions = {form for value in environment.values() if value for form in (value, shlex.quote(value))}
    for value in sorted(redactions, key=len, reverse=True):
        diagnostic = diagnostic.replace(value, "[REDACTED]")
    diagnostic = diagnostic.strip()
    if result.returncode:
        raise ExternalError(f"{tool} artifact discovery probe failed (exit {result.returncode}): {diagnostic}")
    try:
        lines = [
            line.removeprefix("AGW_ARTIFACT_PROBE=")
            for line in result.stdout.splitlines()
            if line.startswith("AGW_ARTIFACT_PROBE=")
        ]
        observed = json.loads(lines[-1])
        root = observed["native_home"]
        problems = observed["problems"]
        if not isinstance(root, str) or not root.startswith("/") or not isinstance(problems, list):
            raise ValueError()
        if any(not isinstance(problem, str) or problem not in _PROBLEM_MESSAGES for problem in problems):
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise ExternalError(
            f"{tool} artifact discovery probe returned an invalid response",
            hint=diagnostic or None,
        ) from None
    inventory_problems = _inventory_problems(observed.get("inventory"), identities, entries)
    problems.extend(problem.code for problem in inventory_problems)
    details = []
    for problem in inventory_problems:
        if problem.identity:
            managed = [
                f"{file.path!r} (origins: {', '.join(file.origins)})"
                for file in files
                if file.native_identity == problem.identity or file.path == problem.path
            ]
            details.append(f"Native {problem.identity!r} at {problem.path!r}; managed artifacts: {', '.join(managed)}")
    if problems:
        raise StateError(
            f"{tool} artifact delivery: "
            + "; ".join([*(_PROBLEM_MESSAGES[problem] for problem in problems), *details]),
            hint=(
                "Inspect the native home, relevant restrictions, and installed CLI version. "
                "This preflight cannot verify whether another native configuration layer overrides a restriction."
            ),
        )
    return root


@dataclass(frozen=True)
class _InventoryProblem:
    code: str
    identity: str = ""
    path: str = ""


def _inventory_problems(
    inventory: object, identities: tuple[str, ...], entries: Mapping[str, str]
) -> list[_InventoryProblem]:
    """Interpret bounded target metadata without requiring a YAML parser on the guest."""
    try:
        if not isinstance(inventory, list) or len(inventory) > 512:
            raise ValueError
        selected = set(identities)
        total = 0
        for row in inventory:
            if (
                not isinstance(row, dict)
                or row.get("kind") not in ("skill", "agent")
                or not isinstance(row.get("path"), str)
            ):
                raise ValueError
            name = row.get("name")
            if name is None:
                header = row.get("frontmatter")
                if not isinstance(header, str) or len(header.encode()) > 32768:
                    raise ValueError
                total += len(header.encode())
                if total > 1048576:
                    raise ValueError
                metadata = parse_metadata(header) if header else {}
                if not isinstance(metadata, dict):
                    raise ValueError
                name = metadata.get("name", row.get("fallback_name") if row["kind"] == "skill" else None)
            if not isinstance(name, str) or not name:
                raise ValueError
            native_identity = f"{row['kind']}:{name}"
            expected = entries.get(row["path"])
            if expected is not None:
                if native_identity != expected:
                    return [_InventoryProblem("native-artifact-identity-mismatch", expected, row["path"])]
                continue
            if native_identity in selected:
                return [_InventoryProblem("native-artifact-collision", native_identity, row["path"])]
        return []
    except (ValueError, TypeError, SourceRefError, RecursionError):
        return [_InventoryProblem("unreadable-native-inventory")]
