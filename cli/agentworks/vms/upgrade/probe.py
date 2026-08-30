"""Read-only guest probes for the Debian upgrade preflight."""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from agentworks.errors import StateError

from .preflight import PreflightIssue, UpgradePreflight

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.transports import Transport

_SUPPORTED_ARCHITECTURES = frozenset({"amd64", "arm64"})
_MINIMUM_BOOT_FREE = 512 * 1024 * 1024
_MINIMUM_ROOT_FREE = 5 * 1024 * 1024 * 1024
_MINIMUM_CACHE_FREE = 2 * 1024 * 1024 * 1024
_DEBIAN_HOSTS = ("deb.debian.org", "security.debian.org", "ftp.debian.org")


def probe_upgrade_preflight(
    target: Transport,
    *,
    database_release: str,
    live_release: str,
    source_suites: Sequence[str],
    minimum_openssh_version: str,
    blocker_hooks: Sequence[str],
    non_quiescent_sessions: Sequence[str],
) -> UpgradePreflight:
    """Collect one complete, read-only planning snapshot."""
    architecture = _stdout(target, "dpkg --print-architecture")
    kernel = _stdout(target, "uname -r")
    dpkg_audit = _lines(target, "dpkg --audit")
    held = _lines(target, "apt-mark showhold")
    kernel_metapackage = _kernel_metapackage(target, architecture)
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
    simulation_result = target.run("LC_ALL=C apt-get -s full-upgrade", check=False)
    simulation = (simulation_result.stdout or "").strip()
    removals = tuple(sorted(set(re.findall(r"^Remv\s+(\S+)", simulation, flags=re.MULTILINE))))
    boot_free = _free_bytes(target, "/boot", missing_ok=True)
    root_free = _free_bytes(target, "/")
    cache_free = _free_bytes(target, "/var/cache/apt/archives")
    assert root_free is not None and cache_free is not None
    blockers, blocker_issues = _evaluate_blockers(target, blocker_hooks)
    extra_issues = blocker_issues
    if not simulation_result.ok:
        extra_issues += (PreflightIssue.APT_SIMULATION_FAILED,)
    apt_timer_states = {
        name: (
            _stdout(target, f"systemctl is-enabled {name}", check=False) or "unknown",
            _stdout(target, f"systemctl is-active {name}", check=False) or "unknown",
        )
        for name in ("apt-daily.timer", "apt-daily-upgrade.timer")
    }

    return UpgradePreflight(
        database_release=database_release,
        live_release=live_release,
        architecture=architecture,
        kernel=kernel,
        dpkg_audit=dpkg_audit,
        held_packages=held,
        kernel_metapackage=kernel_metapackage,
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
        boot_free_bytes=boot_free,
        boot_required_bytes=_MINIMUM_BOOT_FREE,
        root_free_bytes=root_free,
        root_required_bytes=_MINIMUM_ROOT_FREE,
        cache_free_bytes=cache_free,
        cache_required_bytes=_MINIMUM_CACHE_FREE,
        apt_timer_states=apt_timer_states,
        extra_issues=extra_issues,
    )


def supported_architectures() -> frozenset[str]:
    return _SUPPORTED_ARCHITECTURES


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
    command = (
        "command -v fuser >/dev/null 2>&1 || exit 69; "
        "fuser /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend "
        "/var/cache/apt/archives/lock /var/lib/apt/lists/lock 2>/dev/null"
    )
    result = target.run(command, sudo=True, check=False)
    if result.returncode == 69:
        return "cannot inspect native package locks because fuser is unavailable"
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
    output = _stdout(target, command)
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
  matches=$(apt-cache madison "$package" | awk -F '|' -v wanted="$version" '
    {
      candidate=$2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", candidate)
      if (candidate == wanted) print $3
    }
  ') || exit 70
  if [ -z "$matches" ]; then
    printf 'OBSOLETE:%s\n' "$package"
  elif ! printf '%s\n' "$matches" | grep -Eq 'deb\.debian\.org|security\.debian\.org|ftp\.debian\.org'; then
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
    for line in content.splitlines():
        active = line.strip()
        if not active or active.startswith("#"):
            continue
        if re.match(r"deb(?:-src)?\s", active, flags=re.IGNORECASE):
            if not any(host in active for host in _DEBIAN_HOSTS):
                return True
        elif active.lower().startswith("uris:"):
            uris = active.partition(":")[2].split()
            if any(not any(host in uri for host in _DEBIAN_HOSTS) for uri in uris):
                return True
    return False


def _mentions_other_debian_suite(content: str, source_suites: Sequence[str]) -> bool:
    if not any(host in content for host in _DEBIAN_HOSTS):
        return False
    allowed = set(source_suites)
    found: set[str] = set()
    for line in content.splitlines():
        active = line.strip()
        if not active or active.startswith("#"):
            continue
        legacy = re.match(r"deb(?:-src)?\s+(?:\[[^]]+\]\s+)?\S+\s+(\S+)", active, flags=re.IGNORECASE)
        if legacy is not None:
            found.add(legacy.group(1))
        elif active.lower().startswith("suites:"):
            found.update(active.partition(":")[2].split())
    return bool(found - allowed)


def _evaluate_blockers(
    target: Transport,
    hooks: Sequence[str],
) -> tuple[tuple[str, ...], tuple[PreflightIssue, ...]]:
    blockers: list[str] = []
    extra: list[PreflightIssue] = []
    for hook in hooks:
        if hook == "rabbitmq-server-installed":
            if _package_installed(target, "rabbitmq-server"):
                blockers.append(hook)
        elif hook == "mariadb-unsafe-shutdown":
            if _package_installed(target, "mariadb-server"):
                inactive = _command_ok(target, "systemctl is-active --quiet mariadb", sudo=True, invert=True)
                clean_log = _command_ok(
                    target,
                    "journalctl -u mariadb -n 300 --no-pager -o cat 2>/dev/null | "
                    "grep -E 'Shutdown complete|Starting MariaDB|Started MariaDB' | "
                    "tail -1 | grep -Fq 'Shutdown complete'",
                    sudo=True,
                )
                if not inactive or not clean_log:
                    blockers.append(hook)
        else:
            extra.append(PreflightIssue.RELEASE_BLOCKER)
            blockers.append(f"unknown policy blocker hook: {hook}")
    return tuple(blockers), tuple(extra)


def _package_installed(target: Transport, package: str) -> bool:
    return _command_ok(
        target,
        f"dpkg-query -W -f='${{db:Status-Status}}' {shlex.quote(package)} 2>/dev/null | grep -qx installed",
    )


def _free_bytes(target: Transport, path: str, *, missing_ok: bool = False) -> int | None:
    result = target.run(f"df -B1 --output=avail {shlex.quote(path)} | tail -1", check=False)
    if not result.ok:
        if missing_ok:
            return None
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


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
