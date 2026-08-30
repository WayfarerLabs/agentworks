from __future__ import annotations

from dataclasses import dataclass

from agentworks.vms.upgrade.probe import _filesystem_requirements, _simulate_target_upgrade


@dataclass(frozen=True)
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _Target:
    def __init__(self, *, update_returncode: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.update_returncode = update_returncode

    def run(self, command: str, **kwargs: object) -> _Result:
        self.calls.append((command, kwargs))
        if " apt-get " in f" {command} " and command.endswith(" update"):
            return _Result(self.update_returncode)
        if "upgrade --without-new-pkgs" in command:
            return _Result(
                stdout=(
                    "Inst changed\nRemv minimal-only [1]\nNeed to get 10 MB of archives.\n"
                    "After this operation, 20 MB of additional disk space will be used.\n"
                )
            )
        if command.endswith("full-upgrade"):
            return _Result(
                stdout=(
                    "Remv full-only [1]\nRemv minimal-only [1]\nNeed to get 30 MB of archives.\n"
                    "After this operation, 40 MB of additional disk space will be used.\n"
                )
            )
        return _Result()


def test_target_plan_uses_isolated_sources_and_both_upgrade_stages() -> None:
    target = _Target()

    removals, download, growth, ok = _simulate_target_upgrade(
        target,  # type: ignore[arg-type]
        ("trixie", "trixie-updates", "trixie-security"),
    )

    assert ok is True
    assert removals == ("full-only", "minimal-only")
    assert download == 30_000_000
    assert growth == 40_000_000
    commands = [command for command, _kwargs in target.calls]
    apt_commands = [command for command in commands if "apt-get" in command]
    assert len(apt_commands) == 5
    assert any(command.endswith(" update") for command in apt_commands)
    assert any("upgrade --without-new-pkgs" in command for command in apt_commands)
    assert any(command.endswith(" -s full-upgrade") for command in apt_commands)
    assert sum("--print-uris" in command for command in apt_commands) == 2
    assert all("Dir::Etc::sourcelist=/var/tmp/agentworks-apt-plan-" in command for command in apt_commands)
    assert all("APT_CONFIG=/var/tmp/agentworks-apt-plan-" in command for command in apt_commands)
    assert all("/apt.conf LC_ALL=C apt-get" in command for command in apt_commands)
    assert all("Dir::Etc::main=" not in command for command in apt_commands)
    assert all("Dir::Etc::parts=" not in command for command in apt_commands)
    assert all("Dir::State::status=/var/tmp/agentworks-apt-plan-" in command for command in apt_commands)
    assert all("Dir::State::status=/var/lib/dpkg/status" not in command for command in apt_commands)
    assert any("install -m 0600 /var/lib/dpkg/status" in command for command in commands)
    assert commands[-1].startswith("rm -rf /var/tmp/agentworks-apt-plan-")

    writes = {command: kwargs["input_text"] for command, kwargs in target.calls if "input_text" in kwargs}
    assert len(writes) == 2
    source_write = next(value for command, value in writes.items() if command.endswith("/debian.sources"))
    assert isinstance(source_write, str)
    assert "Suites: trixie trixie-updates" in source_write
    assert "Suites: trixie-security" in source_write
    assert "bookworm" not in source_write
    config_write = next(value for command, value in writes.items() if command.endswith("/apt.conf"))
    assert isinstance(config_write, str)
    assert 'Dir::Etc::main "/dev/null";' in config_write
    assert 'Dir::Etc::parts "/var/tmp/agentworks-apt-plan-' in config_write
    assert config_write.endswith('/apt.conf.d";\n')


def test_target_plan_fails_closed_and_cleans_up_when_indexes_fail() -> None:
    target = _Target(update_returncode=100)

    _removals, _download, _growth, ok = _simulate_target_upgrade(  # type: ignore[arg-type]
        target,  # type: ignore[arg-type]
        ("trixie",),
    )

    assert ok is False
    assert target.calls[-1][0].startswith("rm -rf /var/tmp/agentworks-apt-plan-")


def test_installed_growth_is_charged_to_each_distinct_root_or_var_filesystem() -> None:
    without_growth = _filesystem_requirements(
        "root",
        "var",
        "cache",
        boot_filesystem=None,
        apt_download_bytes=3_000_000_000,
        installed_growth_bytes=0,
    )
    with_growth = _filesystem_requirements(
        "root",
        "var",
        "cache",
        boot_filesystem=None,
        apt_download_bytes=3_000_000_000,
        installed_growth_bytes=400_000_000,
    )
    shared_without_growth = _filesystem_requirements(
        "shared",
        "shared",
        "shared",
        boot_filesystem=None,
        apt_download_bytes=3_000_000_000,
        installed_growth_bytes=0,
    )
    shared_with_growth = _filesystem_requirements(
        "shared",
        "shared",
        "shared",
        boot_filesystem=None,
        apt_download_bytes=3_000_000_000,
        installed_growth_bytes=400_000_000,
    )

    assert with_growth["root"] - without_growth["root"] == 400_000_000
    assert with_growth["var"] - without_growth["var"] == 400_000_000
    assert shared_with_growth["shared"] - shared_without_growth["shared"] == 400_000_000


def test_boot_floor_is_aggregated_when_boot_shares_root() -> None:
    without_boot = _filesystem_requirements(
        "root",
        "var",
        "cache",
        boot_filesystem=None,
        apt_download_bytes=0,
        installed_growth_bytes=0,
    )
    shared_boot = _filesystem_requirements(
        "root",
        "var",
        "cache",
        boot_filesystem="root",
        apt_download_bytes=0,
        installed_growth_bytes=0,
    )
    separate_boot = _filesystem_requirements(
        "root",
        "var",
        "cache",
        boot_filesystem="boot",
        apt_download_bytes=0,
        installed_growth_bytes=0,
    )

    assert shared_boot["root"] > without_boot["root"]
    assert separate_boot["root"] == without_boot["root"]
    assert separate_boot["boot"] == shared_boot["root"] - without_boot["root"]
