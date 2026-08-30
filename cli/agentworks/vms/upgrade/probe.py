"""Read-only guest probes for the Debian upgrade preflight."""

from __future__ import annotations

import re
import shlex
import uuid
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit

from agentworks.errors import StateError

from .preflight import APT_TIMER_UNITS, PreflightIssue, UpgradePreflight
from .scripts import render_debian_sources

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentworks.transports import Transport

_SUPPORTED_ARCHITECTURES = frozenset({"amd64", "arm64"})
_MINIMUM_BOOT_FREE = 512 * 1024 * 1024
_MINIMUM_ROOT_FREE = 5 * 1024 * 1024 * 1024
_MINIMUM_VAR_FREE = 2 * 1024 * 1024 * 1024
_MINIMUM_CACHE_FREE = 2 * 1024 * 1024 * 1024
_DEBIAN_HOSTS = frozenset({"deb.debian.org", "security.debian.org", "ftp.debian.org"})
_DEBIAN_URI_SCHEMES = frozenset({"http", "https"})
NATIVE_PACKAGE_LOCK_COMMAND = r"""
if command -v fuser >/dev/null 2>&1; then
  fuser /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend /var/cache/apt/archives/lock /var/lib/apt/lists/lock 2>/dev/null
elif command -v lslocks >/dev/null 2>&1; then
  owners=$(lslocks --noheadings --output PATH,PID 2>/dev/null | awk '
    $1 == "/var/lib/dpkg/lock" ||
    $1 == "/var/lib/dpkg/lock-frontend" ||
    $1 == "/var/cache/apt/archives/lock" ||
    $1 == "/var/lib/apt/lists/lock" { print $2 }
  ') || exit 70
  [ -z "$owners" ] || { printf '%s\n' "$owners"; exit 0; }
  exit 1
else
  exit 69
fi
""".strip()


class _SourceEntry(NamedTuple):
    uris: tuple[str, ...]
    suites: tuple[str, ...]
    has_binary: bool


def probe_upgrade_preflight(
    target: Transport,
    *,
    database_release: str,
    live_release: str,
    source_suites: Sequence[str],
    target_suites: Sequence[str],
    guest_kernel_required: bool,
    minimum_openssh_version: str,
    blocker_probe: Callable[[Transport], tuple[str, ...]],
    non_quiescent_sessions: Sequence[str],
) -> UpgradePreflight:
    """Collect one complete, read-only planning snapshot."""
    architecture = _stdout(target, "dpkg --print-architecture")
    kernel = _stdout(target, "uname -r")
    dpkg_audit = _lines(target, "dpkg --audit")
    held = _lines(target, "apt-mark showhold")
    kernel_metapackage = _kernel_metapackage(target, architecture) if guest_kernel_required else None
    openssh_ok = _command_ok(
        target,
        "dpkg-query -W -f='${Version}' openssh-server >/dev/null 2>&1 && "
        f"dpkg --compare-versions \"$(dpkg-query -W -f='${{Version}}' openssh-server)\" ge "
        f"{shlex.quote(minimum_openssh_version)}",
    )
    package_owner = _package_manager_owner(target)
    modified_conffiles = _modified_conffiles(target)
    source_files = _source_files(target)
    third_party = tuple(sorted(path for path, content in source_files.items() if _is_third_party(content)))
    non_debian_packages, obsolete_packages = _package_origin_inventory(target)
    mixed_suites = tuple(
        sorted(path for path, content in source_files.items() if _mentions_other_debian_suite(content, source_suites))
    )
    apt_pins = _lines(
        target,
        "{ test ! -f /etc/apt/preferences || printf '%s\\n' /etc/apt/preferences; } && "
        "find /etc/apt/preferences.d -maxdepth 1 -type f -print 2>/dev/null",
    )
    removals, apt_download, installed_growth, simulation_ok = _simulate_target_upgrade(target, target_suites)
    root_filesystem, _root_total, root_free = _filesystem_stats(target, "/")
    var_filesystem, _var_total, var_free = _filesystem_stats(target, "/var")
    cache_filesystem, _cache_total, cache_free = _filesystem_stats(target, "/var/cache/apt/archives")
    boot_filesystem, boot_total, boot_free = _filesystem_stats(target, "/boot")
    space_requirements = _filesystem_requirements(
        root_filesystem,
        var_filesystem,
        cache_filesystem,
        boot_filesystem=boot_filesystem,
        apt_download_bytes=apt_download,
        installed_growth_bytes=installed_growth,
    )
    blockers = blocker_probe(target)
    extra_issues: tuple[PreflightIssue, ...] = ()
    if not simulation_ok:
        extra_issues += (PreflightIssue.APT_SIMULATION_FAILED,)
    apt_timer_states = {
        name: (
            _stdout(target, f"systemctl is-enabled {name}", check=False) or "unknown",
            _stdout(target, f"systemctl is-active {name}", check=False) or "unknown",
        )
        for name in APT_TIMER_UNITS
    }

    return UpgradePreflight(
        database_release=database_release,
        live_release=live_release,
        architecture=architecture,
        kernel=kernel,
        dpkg_audit=dpkg_audit,
        held_packages=held,
        kernel_metapackage=kernel_metapackage,
        guest_kernel_required=guest_kernel_required,
        openssh_minimum_satisfied=openssh_ok,
        package_manager_owner=package_owner,
        non_quiescent_sessions=tuple(non_quiescent_sessions),
        modified_conffiles=modified_conffiles,
        release_blockers=blockers,
        apt_pins=apt_pins,
        mixed_suites=mixed_suites,
        third_party_sources=third_party,
        non_debian_packages=non_debian_packages,
        obsolete_packages=obsolete_packages,
        removals=removals,
        apt_download_bytes=apt_download,
        apt_installed_growth_bytes=installed_growth,
        boot_filesystem=boot_filesystem,
        boot_total_bytes=boot_total,
        boot_free_bytes=boot_free,
        boot_required_bytes=space_requirements[boot_filesystem],
        root_filesystem=root_filesystem,
        root_free_bytes=root_free,
        root_required_bytes=space_requirements[root_filesystem],
        var_filesystem=var_filesystem,
        var_free_bytes=var_free,
        var_required_bytes=space_requirements[var_filesystem],
        cache_filesystem=cache_filesystem,
        cache_free_bytes=cache_free,
        cache_required_bytes=space_requirements[cache_filesystem],
        apt_timer_states=apt_timer_states,
        extra_issues=extra_issues,
    )


def supported_architectures() -> frozenset[str]:
    return _SUPPORTED_ARCHITECTURES


def target_source_hygiene_issues(
    target: Transport,
    target_suites: Sequence[str],
) -> tuple[str, ...]:
    """Return enabled source files that are not clean target-release Debian sources."""
    files = _source_files(target)
    expected = set(target_suites)
    covered: set[str] = set()
    issues: set[str] = set()
    for path, content in files.items():
        for entry in _enabled_source_entries(path, content):
            debian_uris = tuple(uri for uri in entry.uris if _is_debian_uri(uri))
            if len(debian_uris) != len(entry.uris):
                issues.add(path)
            if debian_uris:
                if entry.has_binary:
                    covered.update(expected.intersection(entry.suites))
                if set(entry.suites) - expected:
                    issues.add(path)
    issues.update(f"<missing-target-suite:{suite}>" for suite in sorted(expected - covered))
    return tuple(sorted(issues))


def _simulate_target_upgrade(
    target: Transport,
    target_suites: Sequence[str],
) -> tuple[tuple[str, ...], int, int, bool]:
    """Simulate both target-release upgrade stages against isolated APT state."""
    scratch = f"/var/tmp/agentworks-apt-plan-{uuid.uuid4().hex}"
    sources = f"{scratch}/debian.sources"
    apt_config = f"{scratch}/apt.conf"
    apt_config_parts = f"{scratch}/apt.conf.d"
    q_scratch = shlex.quote(scratch)
    q_sources = shlex.quote(sources)
    q_apt_config = shlex.quote(apt_config)
    source_document = render_debian_sources(target_suites)
    config_document = f'Dir::Etc::main "/dev/null";\nDir::Etc::parts "{apt_config_parts}";\n'
    removals: set[str] = set()
    downloads: list[int] = []
    growth: list[int] = []
    try:
        target.run(
            f"install -d -m 0700 {q_scratch} {q_scratch}/lists/partial "
            f"{q_scratch}/archives/partial {q_scratch}/apt.conf.d",
            sudo=True,
        )
        target.run(
            f"install -m 0600 /var/lib/dpkg/status {q_scratch}/status && "
            f"if test -f /var/lib/apt/extended_states; then "
            f"install -m 0600 /var/lib/apt/extended_states {q_scratch}/extended_states; "
            f"else install -m 0600 /dev/null {q_scratch}/extended_states; fi",
            sudo=True,
        )
        writer = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())"
        target.run(
            f"python3 -c {shlex.quote(writer)} {q_sources}",
            sudo=True,
            input_text=source_document,
            tty=False,
        )
        target.run(
            f"python3 -c {shlex.quote(writer)} {q_apt_config}",
            sudo=True,
            input_text=config_document,
            tty=False,
        )
        options = " ".join(
            (
                f"-o Dir::Etc::sourcelist={q_sources}",
                "-o Dir::Etc::sourceparts=-",
                f"-o Dir::State::Lists={q_scratch}/lists",
                f"-o Dir::Cache::Archives={q_scratch}/archives",
                f"-o Dir::State::status={q_scratch}/status",
                f"-o Dir::State::extended_states={q_scratch}/extended_states",
                "-o Dir::Etc::Preferences=/dev/null",
                "-o Dir::Etc::PreferencesParts=-",
                "-o APT::Get::List-Cleanup=0",
            )
        )
        update = target.run(
            f"APT_CONFIG={q_apt_config} LC_ALL=C apt-get {options} update",
            sudo=True,
            check=False,
        )
        results = (
            target.run(
                f"APT_CONFIG={q_apt_config} LC_ALL=C apt-get {options} -s upgrade --without-new-pkgs",
                sudo=True,
                check=False,
            ),
            target.run(
                f"APT_CONFIG={q_apt_config} LC_ALL=C apt-get {options} -s full-upgrade",
                sudo=True,
                check=False,
            ),
        )
        estimates = (
            target.run(
                f"APT_CONFIG={q_apt_config} LC_ALL=C apt-get {options} "
                "--print-uris --yes --download-only upgrade --without-new-pkgs",
                sudo=True,
                check=False,
            ),
            target.run(
                f"APT_CONFIG={q_apt_config} LC_ALL=C apt-get {options} --print-uris --yes --download-only full-upgrade",
                sudo=True,
                check=False,
            ),
        )
        for result in results:
            removals.update(re.findall(r"^Remv\s+(\S+)", result.stdout or "", flags=re.MULTILINE))
        for result in estimates:
            sizes = _apt_plan_sizes(result.stdout or "")
            if sizes is not None:
                download_bytes, growth_bytes = sizes
                downloads.append(download_bytes)
                growth.append(growth_bytes)
        sizes_complete = len(downloads) == len(results)
        return (
            tuple(sorted(removals)),
            max(downloads, default=0),
            max(growth, default=0),
            update.ok
            and all(result.ok for result in results)
            and all(result.ok for result in estimates)
            and sizes_complete,
        )
    finally:
        target.run(f"rm -rf {q_scratch}", sudo=True, check=False)


def _apt_plan_sizes(output: str) -> tuple[int, int] | None:
    if re.search(r"\b0 upgraded, 0 newly installed, 0 to remove\b", output):
        return 0, 0
    download = re.search(r"Need to get\s+([0-9.]+\s*[kMGT]?B)", output)
    additional = re.search(r"After this operation,\s+([0-9.]+\s*[kMGT]?B).*additional", output)
    freed = re.search(r"After this operation,\s+[0-9.]+\s*[kMGT]?B.*freed", output)
    if download is None or (additional is None and freed is None):
        return None
    return (
        _apt_quantity_bytes(download.group(1)),
        0 if additional is None else _apt_quantity_bytes(additional.group(1)),
    )


def _apt_quantity_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9.]+)\s*([kMGT]?B)", value)
    if match is None:
        raise ValueError(f"invalid apt size: {value}")
    multipliers = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
    return int(float(match.group(1)) * multipliers[match.group(2)])


def _kernel_metapackage(target: Transport, architecture: str) -> str | None:
    candidates = {
        "amd64": ("linux-image-amd64", "linux-image-cloud-amd64"),
        "arm64": ("linux-image-arm64", "linux-image-cloud-arm64"),
    }.get(architecture, ())
    for package in candidates:
        if _command_ok(target, f"dpkg-query -W -f='${{db:Status-Status}}' {shlex.quote(package)} | grep -qx installed"):
            return package
    return None


def _package_manager_owner(target: Transport) -> str | None:
    result = target.run(NATIVE_PACKAGE_LOCK_COMMAND, sudo=True, check=False)
    if result.returncode == 69:
        return "cannot inspect native package locks because neither fuser nor lslocks is available"
    if result.returncode not in {0, 1}:
        return "cannot prove native package lock ownership"
    owners = " ".join((result.stdout or "").split())
    return owners or None


def _modified_conffiles(target: Transport) -> tuple[str, ...]:
    command = r"""
dpkg-query -W -f='${Conffiles}\n' | awk '$1 ~ /^\// && $2 ~ /^[0-9a-f]{32}$/ {print $1, $2}' |
while read -r path expected; do
  [ -e "$path" ] || continue
  actual=$(md5sum "$path" 2>/dev/null | awk '{print $1}')
  [ "$actual" = "$expected" ] || printf '%s\n' "$path"
done
""".strip()
    return _lines(target, f"bash -o pipefail -c {shlex.quote(command)}", sudo=True)


def _source_files(target: Transport) -> dict[str, str]:
    command = r"""
set -e
for path in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$path" ] || continue
  printf 'AGW-SOURCE:%s\n' "$path"
  cat "$path"
done
""".strip()
    output = _stdout(target, command, sudo=True)
    files: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("AGW-SOURCE:"):
            current = line.removeprefix("AGW-SOURCE:")
            files[current] = []
        elif current is not None:
            files[current].append(line)
    return {path: "\n".join(lines) for path, lines in files.items()}


def _package_origin_inventory(target: Transport) -> tuple[tuple[str, ...], tuple[str, ...]]:
    command = r"""
dpkg-query -W -f='${binary:Package}\t${Version}\n' |
while IFS="$(printf '\t')" read -r package version; do
  debian_origin='(^|[[:space:]])https?://(deb\.debian\.org|security\.debian\.org|ftp\.debian\.org)(:[0-9]+)?(/|[[:space:]]|$)'
  matches=$(apt-cache madison "$package" | awk -F '|' -v wanted="$version" '
    {
      candidate=$2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", candidate)
      if (candidate == wanted) print $3
    }
  ') || exit 70
  if [ -z "$matches" ]; then
    printf 'OBSOLETE:%s\n' "$package"
  elif ! printf '%s\n' "$matches" | grep -Eiq "$debian_origin"; then
    printf 'NONDEBIAN:%s\n' "$package"
  fi
done
""".strip()
    output = _stdout(target, f"bash -o pipefail -c {shlex.quote(command)}")
    non_debian: list[str] = []
    obsolete: list[str] = []
    for line in output.splitlines():
        if line.startswith("NONDEBIAN:"):
            non_debian.append(line.removeprefix("NONDEBIAN:"))
        elif line.startswith("OBSOLETE:"):
            obsolete.append(line.removeprefix("OBSOLETE:"))
        elif line:
            raise StateError("Debian package-origin probe returned invalid output")
    return tuple(sorted(non_debian)), tuple(sorted(obsolete))


def _is_third_party(content: str) -> bool:
    path = "source.sources" if re.search(r"(?im)^\s*types\s*:", content) else "source.list"
    return any(any(not _is_debian_uri(uri) for uri in entry.uris) for entry in _enabled_source_entries(path, content))


def _mentions_other_debian_suite(content: str, source_suites: Sequence[str]) -> bool:
    allowed = set(source_suites)
    path = "source.sources" if re.search(r"(?im)^\s*types\s*:", content) else "source.list"
    return any(
        any(_is_debian_uri(uri) for uri in entry.uris) and bool(set(entry.suites) - allowed)
        for entry in _enabled_source_entries(path, content)
    )


def _is_debian_uri(uri: str) -> bool:
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    return parsed.scheme.lower() in _DEBIAN_URI_SCHEMES and parsed.hostname in _DEBIAN_HOSTS


def _enabled_source_entries(path: str, content: str) -> tuple[_SourceEntry, ...]:
    if path.endswith(".sources"):
        return _enabled_deb822_entries(content)
    entries: list[_SourceEntry] = []
    for line in content.splitlines():
        active = line.strip()
        if not active or active.startswith("#"):
            continue
        match = re.match(
            r"(deb|deb-src)\s+(?:\[([^]]+)\]\s+)?(\S+)\s+(\S+)",
            active,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        source_type, options, uri, suite = match.groups()
        if options is not None and re.search(r"(?:^|\s)enabled\s*=\s*(?:no|false|0)(?:\s|$)", options, re.I):
            continue
        entries.append(_SourceEntry((uri,), (suite,), source_type.lower() == "deb"))
    return tuple(entries)


def _enabled_deb822_entries(content: str) -> tuple[_SourceEntry, ...]:
    entries: list[_SourceEntry] = []
    for stanza in re.split(r"\n\s*\n", content):
        fields: dict[str, str] = {}
        current: str | None = None
        for line in stanza.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[:1].isspace() and current is not None:
                fields[current] += " " + line.strip()
                continue
            key, separator, value = line.partition(":")
            if not separator:
                current = None
                continue
            current = key.strip().lower()
            fields[current] = value.strip()
        if fields.get("enabled", "yes").lower() in {"no", "false", "0"}:
            continue
        source_types = {value.lower() for value in fields.get("types", "").split()}
        if not source_types.intersection({"deb", "deb-src"}):
            continue
        uris = tuple(fields.get("uris", "").split())
        suites = tuple(fields.get("suites", "").split())
        if uris and suites:
            entries.append(_SourceEntry(uris, suites, "deb" in source_types))
    return tuple(entries)


def _filesystem_stats(target: Transport, path: str) -> tuple[str, int, int]:
    result = target.run(f"df -B1 --output=source,size,avail {shlex.quote(path)} | tail -1", check=False)
    if not result.ok:
        return f"<unknown:{path}>", 0, 0
    try:
        filesystem, total, free = result.stdout.split()
        return filesystem, int(total), int(free)
    except (TypeError, ValueError):
        return f"<unknown:{path}>", 0, 0


def _filesystem_requirements(
    root_filesystem: str,
    var_filesystem: str,
    cache_filesystem: str,
    *,
    boot_filesystem: str | None,
    apt_download_bytes: int,
    installed_growth_bytes: int,
) -> dict[str, int]:
    requirements: dict[str, int] = {}
    for filesystem, required in (
        (root_filesystem, _MINIMUM_ROOT_FREE),
        (var_filesystem, _MINIMUM_VAR_FREE),
        (cache_filesystem, max(_MINIMUM_CACHE_FREE, apt_download_bytes)),
    ):
        requirements[filesystem] = requirements.get(filesystem, 0) + required
    for filesystem in {root_filesystem, var_filesystem}:
        requirements[filesystem] += installed_growth_bytes
    if boot_filesystem is not None:
        requirements[boot_filesystem] = requirements.get(boot_filesystem, 0) + _MINIMUM_BOOT_FREE
    return requirements


def _lines(target: Transport, command: str, *, sudo: bool = False) -> tuple[str, ...]:
    return tuple(line.strip() for line in _stdout(target, command, sudo=sudo).splitlines() if line.strip())


def _stdout(target: Transport, command: str, *, check: bool = True, sudo: bool = False) -> str:
    result = target.run(command, check=check, sudo=sudo)
    if check and not result.ok:
        raise StateError("Debian upgrade safety probe failed", hint=f"Repair the failed read-only probe: {command}")
    return (result.stdout or "").strip()


def _command_ok(target: Transport, command: str, *, sudo: bool = False, invert: bool = False) -> bool:
    ok = target.run(command, sudo=sudo, check=False).ok
    return not ok if invert else ok
