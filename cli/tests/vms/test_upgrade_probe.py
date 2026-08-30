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
    assert all("Dir::State::status=/var/tmp/agentworks-apt-plan-" in command for command in apt_commands)
    assert all("Dir::State::status=/var/lib/dpkg/status" not in command for command in apt_commands)
    assert any("install -m 0600 /var/lib/dpkg/status" in command for command in commands)
    assert commands[-1].startswith("rm -rf /var/tmp/agentworks-apt-plan-")

    source_writes = [kwargs["input_text"] for _command, kwargs in target.calls if "input_text" in kwargs]
    assert len(source_writes) == 1
    source_write = source_writes[0]
    assert isinstance(source_write, str)
    assert "Suites: trixie trixie-updates" in source_write
    assert "Suites: trixie-security" in source_write
    assert "bookworm" not in source_write


def test_target_plan_fails_closed_and_cleans_up_when_indexes_fail() -> None:
    target = _Target(update_returncode=100)

    _removals, _download, _growth, ok = _simulate_target_upgrade(  # type: ignore[arg-type]
        target,
        ("trixie",),
    )

    assert ok is False
    assert target.calls[-1][0].startswith("rm -rf /var/tmp/agentworks-apt-plan-")


def test_shared_filesystem_requirement_aggregates_each_space_component() -> None:
    separate = _filesystem_requirements(
        "root",
        "var",
        "cache",
        apt_download_bytes=3_000_000_000,
        installed_growth_bytes=400_000_000,
    )
    shared = _filesystem_requirements(
        "shared",
        "shared",
        "shared",
        apt_download_bytes=3_000_000_000,
        installed_growth_bytes=400_000_000,
    )

    assert shared == {"shared": sum(separate.values())}
