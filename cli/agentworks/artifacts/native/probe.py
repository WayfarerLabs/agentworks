"""Read-only native launch checks, before any artifact publication or teardown."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.artifacts import publication
from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.frontmatter import parse_metadata
from agentworks.artifacts.native.common import MAX_CODEX_PERSONA_BYTES
from agentworks.artifacts.native.layout import NATIVE_USER_DIRECTORIES
from agentworks.capabilities.harness_integration.setup import (
    UserSetupInvocation,
    VMSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.errors import AgentworksError, ExternalError, StateError
from agentworks.execution.requests import Bytes, CaptureOutput, PythonProgram
from agentworks.harness_setup.outcomes import Redactor
from agentworks.package_sources import DEFAULT_CAPTURE_LIMITS
from agentworks.sources import SourceRefError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.capabilities.harness_integration.setup import SetupInvocation
    from agentworks.execution.access import VMAccess


class NativeArtifactConflictError(StateError):
    """Native names that setup can defer or sessions can leave unhandled."""

    def __init__(self, message: str, identities: tuple[str, ...], *, hint: str) -> None:
        super().__init__(message, hint=hint)
        self.identities = identities


_MAX_CATALOG_ENTRIES = 512


# Executed in the same login and interactive environment as the native workload. Only
# selected bounded artifact frontmatter and discovery facts return for local identity
# checks. Settings bodies and artifact instruction bodies are not returned.
_PROBE = (
    resources.files("agentworks.artifacts").joinpath("publication_plan.py").read_text(encoding="utf-8")
    + f"\nMAX_CATALOG_ENTRIES = {_MAX_CATALOG_ENTRIES}\n"
    + r"""
import fnmatch, hashlib, json, os, pathlib, shutil, stat, subprocess, sys, tomllib
from collections import Counter, namedtuple
request = json.load(sys.stdin)
tool = request['tool']
vm_only = request['vm_only']
home = os.environ.get('HOME', '')
variable, default = request['native_directory']
native_home = request.get('native_root') or os.environ.get(variable) or home + '/' + default
problems = []
problem_paths = []
skill_names = {name[6:] for name in request['identities'] if name.startswith('skill:')}
agent_names = {name[6:] for name in request['identities'] if name.startswith('agent:')}
previous = {path: Placement(*row) for path, row in request.get('previous', {}).items()}
desired = {path: Placement(*row) for path, row in request.get('desired', {}).items()}
held = held_placements(previous, desired, frozenset(request.get('retain', ())))
def reserved_package(name):
    return tool == 'claude' and (name.casefold() in ('synced', 'anthropic-skills')
                                 or name == '.trash' or name.casefold().startswith('anthropic-skills:'))

prior_skill_roots = {pathlib.Path(path).parent.parent for path in previous
                     if path.endswith('/SKILL.md') and not reserved_package(pathlib.Path(path).parent.name)}
prior_agent_roots = {pathlib.Path(path).parent for path, item in previous.items()
                     if (item.native_identity or '').startswith('agent:')}
if not vm_only and request['home'] is not None and home != request['home']:
    problems.append('home-mismatch')
if not vm_only and (not native_home.startswith('/') or any(p in ('', '.', '..') for p in native_home.split('/')[1:])):
    problems.append('invalid-native-home')
executable = shutil.which(tool)
if request['workspace_only'] or vm_only or (previous and not request['paths']):
    pass
elif executable is None:
    problems.append('missing-command')
machine_root = {'claude': pathlib.Path('/etc/claude-code/.claude'),
                'codex': pathlib.Path('/etc/codex')}.get(tool)
Location = namedtuple('Location', 'scope root anchor skills agents')
def location(scope, root, anchor):
    skills = anchor / '.agents' / 'skills' if tool == 'codex' and scope != 'machine' else root / 'skills'
    agents = None if tool == 'codex' and root == machine_root else root / 'agents'
    return Location(scope, root, anchor, skills, agents)

workspace = request['workspace']
locations = []
if request['check_policy']:
    if not request['workspace_only'] and not vm_only:
        locations.append(location('user', pathlib.Path(native_home), pathlib.Path(home)))
    if workspace and not vm_only:
        project = pathlib.Path(workspace)
        locations.append(location('workspace', project / default, project))
    if machine_root is not None:
        locations.append(location('machine', machine_root, pathlib.Path('/')))
# A prior placement in an applicable catalog selects its kind across the same
# discovery roots as a desired placement, even if the last declaration is withdrawn.
scan_skills = (any(':' not in name for name in skill_names) or
               not prior_skill_roots.isdisjoint(loc.skills for loc in locations))
scan_agents = bool(agent_names) or not prior_agent_roots.isdisjoint(loc.agents for loc in locations)

def check_discovery_path(path, anchor):
    # Do not inspect native metadata through links beneath the actual owning root.
    relative = path.relative_to(anchor)
    if anchor.is_symlink():
        raise ValueError()
    for component in relative.parts:
        anchor = anchor / component
        if anchor.is_symlink():
            raise ValueError()

unsafe_location = False
for loc in locations:
    try:
        # A session may select a user configuration directory outside HOME. Its
        # Codex skills still belong to HOME, even if its config is the project's.
        anchor = loc.anchor
        if loc.scope == 'user' and not loc.root.is_relative_to(anchor):
            anchor = pathlib.Path('/')
        check_discovery_path(loc.root, anchor)
        if tool == 'codex' and loc.scope != 'machine' and scan_skills:
            check_discovery_path(loc.skills, loc.anchor)
    except Exception:
        problems.append('unreadable-native-inventory')
        problem_paths.append(str(loc.root))
        unsafe_location = True
# Every logical anchor must be safe before reading any aliased physical catalog.
if unsafe_location:
    locations = []
discovery_roots = {}
for loc in locations:
    if scan_skills:
        discovery_roots['skill', loc.skills] = None
    if scan_agents and loc.agents is not None:
        discovery_roots['agent', loc.agents] = None
inventory = []
known_entries = request['entries']
registered_agents = {}
paths = []
for loc in locations:
    root = loc.root
    if root == machine_root:
        continue
    paths.extend([root / 'settings.json', root / 'settings.local.json'] if tool == 'claude' else [root / 'config.toml'])
if tool == 'claude' and request['check_policy']:
    paths.append(pathlib.Path('/etc/claude-code/managed-settings.json'))
for path in dict.fromkeys(paths):
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
                value is False and name.split('@')[0] in request['plugin_names']
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
                    inventory.append({'kind': 'agent', 'path': str(path), 'name': name, 'registered': True})
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
            if agent_names and (document.get('agents', {}).get('enabled') is False or
                                document.get('features', {}).get('multi_agent') is False):
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
        problem_paths.append(str(path))

def planned_entries(paths, markers):
    # Direct-marker adapters hide descendants only while a marker will be present.
    result = set()
    for spelling in paths:
        path = pathlib.Path(spelling)
        for kind, root in discovery_roots:
            if not path.is_relative_to(root) or path == root:
                continue
            if kind == 'agent':
                if path.parent != root:
                    return None
                result.add(spelling)
                continue
            package = root / path.relative_to(root).parts[0]
            result.add(str(package))
            if tool == 'codex' or str(package / 'SKILL.md') not in markers:
                for member in (path, *path.parents):
                    if member == root:
                        break
                    if member.name == 'SKILL.md' and (member != path or member.parent.parent != root):
                        return None
                    result.add(str(member))
    return result

def fits(entries, sizes):
    metadata = personas = 0
    for kind, size in sizes.values():
        if tool == 'codex' and kind == 'agent':
            personas += size
        else:
            metadata += size
    return len(entries) <= MAX_CATALOG_ENTRIES and metadata <= 1048576 and personas <= request['inventory_bytes']

def publication_projection(previous, desired, held, retiring_markers):
    # Only an owning setup can spend a physical removal or a decrease in metadata.
    # Fingerprint bounded declared/recorded members; foreign descendants need only a directory entry.
    paths = dict.fromkeys(path for path in (*desired, *previous) if path not in held)
    inspected = set()
    available = request['inventory_bytes']

    def charge(spelling):
        inspected.add(spelling)
        if len(inspected) > request['proof_members']:
            raise ValueError()

    observed = {}
    invalid_sections = {}
    for spelling in paths:
        path = pathlib.Path(spelling)
        item = desired.get(spelling) or previous[spelling]
        if item.section or (spelling in previous and previous[spelling].section):
            if any(path.is_relative_to(root) for kind, root in discovery_roots):
                raise ValueError()
            # A malformed section can skip only its own destination. It cannot
            # create a whole-file conflict for other members of its origin.
            invalid_sections[spelling] = ''
            observed[spelling] = None
            continue
        if any(path.is_relative_to(root) for root in opaque_packages):
            raise ValueError()
        check_discovery_path(path, pathlib.Path('/'))
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            observed[spelling] = None
            continue
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > available:
                raise ValueError()
            charge(spelling)
            available -= info.st_size
            raw = source.read(info.st_size + 1)
            if len(raw) != info.st_size:
                raise ValueError()
            observed[spelling] = (hashlib.sha256(raw).hexdigest(), stat.S_IMODE(info.st_mode))
    plan = plan_files(previous, desired, observed, held, invalid_sections)
    retired = set(plan.retire)
    physical = existing_entries.copy()
    for spelling in retiring_markers & retired:
        package = pathlib.Path(spelling).parent
        pending = [package]
        while pending:
            for path in pending.pop().iterdir():
                charge(str(path))
                if str(path) in retired:
                    continue
                if path.is_symlink() or path.name == 'SKILL.md':
                    raise ValueError()
                if path.is_dir():
                    pending.append(path)
                elif not path.is_file():
                    raise ValueError()
                physical.add(str(path))
    physical.difference_update(retired)
    directories = set()
    package_roots = set()
    for spelling in retired:
        package = previous[spelling].package_root
        if package is not None:
            package_roots.add(package)
            directories.update(parent for parent in pathlib.Path(spelling).parents if parent.is_relative_to(package))
    # Only recorded cleanup boundaries authorize pruning, even when a file
    # disappeared before its removal checkpoint.
    markers = {path for path, (kind, size) in observed_sizes.items() if kind == 'skill' and path not in retired}
    children = Counter(pathlib.Path(path).parent for path in physical | markers)
    for path in sorted(directories, key=lambda directory: len(directory.parts), reverse=True):
        if str(path) not in physical or children[path] or path.is_mount():
            continue
        # A permission failure can leave an empty package root after successful
        # retirement. Below that boundary it aborts publication before the marker retires.
        if str(path) in package_roots and (os.statvfs(path).f_flag & os.ST_RDONLY or (not vm_only and (
            not os.access(path.parent, os.W_OK | os.X_OK) or path.parent.stat().st_mode & stat.S_ISVTX
        ))):
            continue
        physical.remove(str(path))
        children[path.parent] -= 1
    sizes = {path: size for path, size in observed_sizes.items() if path not in retired}
    for spelling in plan.publish:
        if spelling in proposed:
            sizes[spelling] = proposed[spelling]
    markers.update(path for path in plan.publish if path in proposed and proposed[path][0] == 'skill')
    additions = planned_entries(plan.publish, markers)
    if additions is None:
        return False
    physical.update(additions)
    if tool != 'codex':
        opaque = {pathlib.Path(marker).parent for marker in markers}
        physical = {path for path in physical if opaque.isdisjoint(pathlib.Path(path).parents)}
    return fits(physical, sizes)

try:
    candidates = []
    existing_entries = set()
    opaque_packages = set()
    inspecting = None
    for kind, catalog in discovery_roots:
        inspecting = catalog
        if catalog.is_symlink():
            raise ValueError()
        if not catalog.exists():
            continue
        if kind == 'skill':
            for directory in catalog.iterdir():
                # Keep direct-name casing aligned with _is_reserved_claude_skill on the host.
                if reserved_package(directory.name):
                    opaque_packages.add(directory)
                    # These names do not expose ordinary native skills. Do not inspect
                    # account sync, retired skills, or other reserved namespace contents.
                    continue
                inspecting = directory
                existing_entries.add(str(directory))
                if (len(existing_entries) > MAX_CATALOG_ENTRIES or directory.is_symlink() or
                        (tool == 'codex' and directory.name == 'SKILL.md')):
                    raise ValueError()
                if not directory.is_dir():
                    continue
                marker = directory / 'SKILL.md'
                inspecting = marker
                if marker.is_symlink() or (marker.exists() and not marker.is_file()):
                    raise ValueError()
                if tool == 'codex' or not marker.exists():
                    # Codex recursively discovers skills even beneath another entrypoint.
                    # Cleanup may also leave ordinary unowned files, which are not skills.
                    pending = [directory]
                    while pending:
                        for leftover in pending.pop().iterdir():
                            inspecting = leftover
                            existing_entries.add(str(leftover))
                            if (len(existing_entries) > MAX_CATALOG_ENTRIES or leftover.is_symlink() or
                                    (leftover.name == 'SKILL.md' and leftover != marker)):
                                raise ValueError()
                            if leftover.is_dir():
                                pending.append(leftover)
                            elif not leftover.is_file():
                                raise ValueError()
                if not marker.exists():
                    continue
                candidates.append(('skill', marker, directory.name))
                if len(candidates) > MAX_CATALOG_ENTRIES:
                    raise ValueError()
        else:
            for path in catalog.iterdir():
                inspecting = path
                existing_entries.add(str(path))
                if len(existing_entries) > MAX_CATALOG_ENTRIES or path.is_symlink() or path.is_dir():
                    raise ValueError()
                if path.suffix == ('.toml' if tool == 'codex' else '.md') and path.is_file():
                    candidates.append(('agent', path, path.stem))
                if len(candidates) > MAX_CATALOG_ENTRIES:
                    raise ValueError()
    proposed = {}
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
    total = 0
    codex_total = 0
    observed_sizes = {}
    for kind, path, fallback in candidates:
        inspecting = path
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
    future = observed_sizes.copy()
    for spelling, (kind, size) in proposed.items():
        future[spelling] = (kind, max(size, observed_sizes.get(spelling, (kind, 0))[1]))
    # With earlier ownership, a held marker can stay absent even while a
    # separately attributable support file is writable. Only existing markers
    # hide descendants in the upper bound. The exact plan can prove new ones.
    additions = planned_entries(request['paths'], observed_sizes if previous else future)
    retiring_markers = {path for path in observed_sizes if tool != 'codex' and path.endswith('/SKILL.md')
                        and path in previous and path not in desired and path not in held}
    if retiring_markers or additions is None or not fits(existing_entries | additions, future):
        if not previous or not publication_projection(previous, desired, held, retiring_markers):
            raise ValueError()
except Exception as error:
    problems.append('unreadable-native-inventory')
    failed_path = getattr(error, 'filename', None) or inspecting
    if failed_path is not None:
        problem_paths.append(str(failed_path))
if tool == 'codex' and request['check_policy']:
    for spelling in request['paths']:
        path = pathlib.Path(spelling)
        if path.name != 'AGENTS.md':
            continue
        try:
            override = path.with_name('AGENTS.override.md')
            check_discovery_path(override, path.parent)
            if override.exists() and override.read_bytes().strip():
                problems.append('native-guidance-shadowed')
        except Exception:
            problems.append('unreadable-native-policy')
if tool == 'grok' and workspace and request['paths']:
    result = subprocess.run(['git', '-C', workspace, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if result.returncode == 0:
        for path in request['paths']:
            if not any(path.startswith(workspace.rstrip('/') + suffix)
                       for suffix in ('/.grok/rules/', '/.claude/rules/')):
                continue
            ignored = subprocess.run(['git', '-C', workspace, 'check-ignore', '--no-index', '--', path],
                                     capture_output=True)
            if ignored.returncode == 0:
                problems.append('native-discovery-exclusions')
            elif ignored.returncode != 1:
                problems.append('unreadable-native-policy')
print('\nAGW_ARTIFACT_PROBE=' + json.dumps({
    'home': home, 'native_home': str(machine_root) if vm_only else native_home,
    'problems': sorted(set(problems)), 'inventory': inventory, 'problem_paths': list(dict.fromkeys(problem_paths))
}))
"""
)


_PROBLEM_MESSAGES = {
    "home-mismatch": "the login shell changed the actual user's HOME",
    "invalid-native-home": "the native home is not an absolute normalized directory",
    "missing-command": "command {tool!r} was not found in the target login shell; install it for this user",
    "native-discovery-exclusions": "a configured discovery exclusion matches an artifact file",
    "unsupported-discovery-pattern": "the adapter cannot evaluate an extended native exclusion pattern",
    "native-plugin-policy": "native configuration restricts the generated artifact plugin",
    "native-guidance-shadowed": "AGENTS.override.md now suppresses an applied AGENTS.md; reinitialize its owning facet",
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


def _is_reserved_claude_skill(identity: str | None) -> bool:
    """Direct-name counterpart to _PROBE; plugin-qualified identities keep their namespace."""
    return (
        identity is not None
        and identity.startswith("skill:")
        and identity[6:].casefold() in ("synced", "anthropic-skills")
    )


def _setup_evidence(
    setup: SetupInvocation | None, files: Sequence[ArtifactFile | OwnedArtifactFile]
) -> dict[str, object]:
    if setup is None or setup.prior is None or not setup.prior.artifact_files:
        return {}
    roots: tuple[str, ...]
    vm = isinstance(setup, VMSetupInvocation)
    if isinstance(setup, WorkspaceSetupInvocation):
        roots, group = (setup.root,), setup.linux_group
    elif isinstance(setup, UserSetupInvocation):
        roots, group = (setup.home,), ""
    else:
        roots, group = publication.ROOT_FILE_DIRECTORIES, ""
    previous = publication.publication_placements(setup.prior.artifact_files, roots=roots, group=group, root=vm)
    desired = publication.publication_placements(files, roots=roots, group=group, root=vm)
    return {
        "previous": previous,
        "desired": desired,
        "retain": sorted(setup.retain_artifact_origins),
        "proof_members": DEFAULT_CAPTURE_LIMITS.members,
    }


# Bound the complete response, including JSON-escaped frontmatter, catalog paths,
# matching Codex registrations and login-shell noise.
_PROBE_OUTPUT_LIMIT = 16 * 1024 * 1024


def probe_native(
    access: VMAccess,
    *,
    tool: str,
    environment: Mapping[str, str],
    redactor: Redactor = Redactor(),
    home: str | None = None,
    native_root: str | None = None,
    workspace: str = "",
    files: Sequence[ArtifactFile | OwnedArtifactFile] = (),
    session_plugin: bool = False,
    workspace_only: bool = False,
    vm_only: bool = False,
    check_policy: bool = True,
    setup: SetupInvocation | None = None,
) -> str:
    """Check proposed delivery in its native scope and scan applicable native policy.

    The probe is a Python program run by the system ``python3`` after the
    account's login and interactive startup, so it sees the environment the
    native workload gets. The request travels on its stdin, so a large
    package needs no argument. An owning setup can supply its prior ownership
    as evidence for budgeting replacements. Launch checks have no such authority.
    """
    if tool == "claude":
        # Old placements under Claude-owned names must not select native inventory.
        files = tuple(
            file
            for file in files
            if not isinstance(file, OwnedArtifactFile) or not _is_reserved_claude_skill(file.native_identity)
        )
    entries = {file.path: file.native_identity for file in files if file.native_identity}
    identities = tuple(entries.values())
    reserved = tuple(
        dict.fromkeys(identity for identity in identities if tool == "claude" and _is_reserved_claude_skill(identity))
    )
    if reserved:
        raise NativeArtifactConflictError(
            redactor.sanitize(f"Claude Code reserves these supplied native skill names: {', '.join(reserved)}"),
            reserved,
            hint="Rename the supplied skills, or use namespaced session plugin delivery.",
        )
    request = {
        "tool": tool,
        "codex_persona_bytes": MAX_CODEX_PERSONA_BYTES,
        "inventory_bytes": DEFAULT_CAPTURE_LIMITS.total_bytes,
        "home": home,
        "native_directory": NATIVE_USER_DIRECTORIES[tool],
        "native_root": native_root,
        "workspace": workspace,
        "paths": tuple(file.path for file in files),
        "entries": entries,
        "proposed": _proposed_sizes(files, tool),
        **_setup_evidence(setup, files),
        "session_plugin": session_plugin,
        "plugin_names": tuple(
            dict.fromkeys(identity.split(":")[1] for identity in identities if identity.count(":") > 1)
        ),
        "workspace_only": workspace_only,
        "vm_only": vm_only,
        "identities": identities,
        "check_policy": check_policy,
    }
    try:
        result = access.run(
            PythonProgram(
                _PROBE,
                login=True,
                interactive=True,
                env=dict(environment),
                stdin=Bytes(json.dumps(request).encode()),
                deadline=30,
            ),
            stdout=CaptureOutput(_PROBE_OUTPUT_LIMIT),
        )
        status = result.require_completed(access.vm_name)
    except AgentworksError as error:
        redactor.sanitize_error(error)
        raise
    stdout = result.stdout.decode("utf-8", "replace")
    stderr = result.stderr.decode("utf-8", "replace")
    # Login startup scripts can echo the supplied runtime environment on failure.
    # Redact known values and their shell-quoted forms before publishing diagnostics.
    if status:
        raise ExternalError(redactor.sanitize(f"{tool} artifact discovery probe failed (exit {status}): {stderr}"))
    try:
        lines = [
            line.removeprefix("AGW_ARTIFACT_PROBE=")
            for line in stdout.splitlines()
            if line.startswith("AGW_ARTIFACT_PROBE=")
        ]
        if len(lines) != 1:
            raise ValueError
        observed = json.loads(lines[0])
        root = observed["native_home"]
        problems = observed["problems"]
        if not isinstance(root, str) or not root.startswith("/") or not isinstance(problems, list):
            raise ValueError()
        if any(not isinstance(problem, str) or problem not in _PROBLEM_MESSAGES for problem in problems):
            raise ValueError()
        problem_paths = observed.get("problem_paths", [])
        if (
            not isinstance(problem_paths, list)
            or len(problem_paths) > 512
            or any(not isinstance(path, str) or not path.startswith("/") for path in problem_paths)
        ):
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise ExternalError(
            f"{tool} artifact discovery probe returned an invalid response",
            hint=redactor.sanitize(stderr) or None,
        ) from None
    inventory_problems = _inventory_problems(observed.get("inventory"), entries)
    problems.extend(problem.code for problem in inventory_problems)
    details: list[str] = []
    details.extend(f"Native path: {path!r}" for path in problem_paths)
    for problem in inventory_problems:
        if problem.identity:
            managed = [
                f"{file.path!r} (origins: {', '.join(file.origins)})"
                for file in files
                if file.native_identity == problem.identity or file.path == problem.path
            ]
            details.append(f"Native {problem.identity!r} at {problem.path!r}; managed artifacts: {', '.join(managed)}")
        elif problem.path:
            details.append(f"Native entry: {problem.path!r}")
    if problems:
        message = f"{tool} artifact delivery: " + "; ".join(
            [*(_PROBLEM_MESSAGES[problem].format(tool=tool) for problem in problems), *details]
        )
        message = redactor.sanitize(message)
        paths = [*problem_paths, *(problem.path for problem in inventory_problems if problem.path)]
        hint = redactor.sanitize(
            "Inspect native paths: " + ", ".join(repr(path) for path in dict.fromkeys(paths))
            if paths
            else f"Native configuration directory: {root!r}"
        )
        if all(problem in ("native-artifact-collision", "native-artifact-identity-mismatch") for problem in problems):
            raise NativeArtifactConflictError(
                message, tuple(dict.fromkeys(problem.identity for problem in inventory_problems)), hint=hint
            )
        raise StateError(message, hint=hint)
    return root


@dataclass(frozen=True)
class _InventoryProblem:
    code: str
    identity: str = ""
    path: str = ""


def _inventory_problems(inventory: object, entries: Mapping[str, str]) -> list[_InventoryProblem]:
    """Interpret bounded target metadata without requiring a YAML parser on the guest."""
    current_path = ""
    try:
        # Each requested persona can have one registration in the user config
        # and one in the workspace config, independent of physical catalog entries.
        registration_slots = {identity[6:]: 2 for identity in entries.values() if identity.startswith("agent:")}
        if not isinstance(inventory, list) or len(inventory) > _MAX_CATALOG_ENTRIES + sum(registration_slots.values()):
            raise ValueError
        # Direct entries share a native namespace only within a discovery
        # directory. Codex config aliases select its sibling agents directory.
        selected = set()
        for spelling, identity in entries.items():
            path = PurePosixPath(spelling)
            if identity.startswith("skill:") and path.name == "SKILL.md":
                selected.add((path.parent.parent, identity))
            elif identity.startswith("agent:"):
                selected.add((path.parent, identity))
        total = 0
        physical = 0
        problems: list[_InventoryProblem] = []
        for row in inventory:
            if (
                not isinstance(row, dict)
                or row.get("kind") not in ("skill", "agent")
                or not isinstance(row.get("path"), str)
            ):
                raise ValueError
            current_path = row["path"]
            path = PurePosixPath(current_path)
            registered = row.get("registered", False)
            if type(registered) is not bool or (registered and row["kind"] != "agent"):
                raise ValueError
            name = row.get("name")
            if registered:
                if not isinstance(name, str) or not registration_slots.get(name):
                    raise ValueError
                registration_slots[name] -= 1
            else:
                physical += 1
                if physical > _MAX_CATALOG_ENTRIES:
                    raise ValueError
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
                    problems.append(_InventoryProblem("native-artifact-identity-mismatch", expected, row["path"]))
                continue
            if row["kind"] == "skill":
                directory = path.parent.parent
            elif registered:
                directory = path.parent / "agents"
            else:
                directory = path.parent
            if (directory, native_identity) in selected:
                problems.append(_InventoryProblem("native-artifact-collision", native_identity, row["path"]))
        return problems
    except (ValueError, TypeError, SourceRefError, RecursionError):
        return [_InventoryProblem("unreadable-native-inventory", path=current_path)]
