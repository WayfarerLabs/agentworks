"""Exercise retirement forecasts with the real guest probe and guarded publication."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.native import probe
from agentworks.artifacts.publication import publish_artifacts
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.errors import StateError
from agentworks.execution.files import Files
from agentworks.harness_setup.model import SetupRecord
from agentworks.package_sources import DEFAULT_CAPTURE_LIMITS
from tests.native_setup_fixtures import LocalFixtureAccount

if TYPE_CHECKING:
    from agentworks.artifacts.application import ArtifactPublication
    from agentworks.db import VMRow

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution")


def _account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> LocalFixtureAccount:
    account = LocalFixtureAccount(tmp_path / "native")
    source = probe._PROBE
    for path in ("/etc/codex", "/etc/claude-code/.claude", "/etc/claude-code/managed-settings.json"):
        source = source.replace(repr(path), repr(str(account.root) + path))
    monkeypatch.setattr(probe, "_PROBE", source)
    binary = account.root / "bin" / tool
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 97\n")
    binary.chmod(0o700)
    return account


def _preflight(
    account: LocalFixtureAccount, tool: str, files: tuple[ArtifactFile, ...], previous: tuple[OwnedArtifactFile, ...]
) -> None:
    prior = SetupRecord(
        component="agent", integration=tool, destination_id="a" * 64, declaration={}, artifact_files=previous
    )
    access = account.access()
    call = UserSetupInvocation(
        vm=cast("VMRow", object()),
        access=access,
        files=access.files(),
        prior=prior,
        checkpoint=lambda _: None,
        username="fixture",
        home=str(account.home),
    )
    probe.probe_native(access, tool=tool, environment={}, home=call.home, files=files, setup=call)


def _skill(catalog: Path, name: str, origin: str, supporting: str = "") -> tuple[ArtifactFile, ...]:
    root = catalog / name
    members = [("SKILL.md", f"---\nname: {name}\n---\nReview.\n".encode())]
    if supporting:
        members.append((supporting, b"Supporting instructions.\n"))
    return tuple(
        ArtifactFile(str(root / member), data, (origin * 64,), native_identity=f"skill:{name}", package_root=str(root))
        for member, data in members
    )


@pytest.mark.parametrize("scenario", ["marker-removed", "nested-support-removed", "unrecorded-empty-directory"])
def test_codex_catalog_retry_credits_empty_recorded_directories(tmp_path, monkeypatch, scenario):
    account = _account(tmp_path, monkeypatch, "codex")
    catalog = account.home / ".agents/skills"
    supporting = "guide.md" if scenario == "marker-removed" else "nested/guide.md"
    old = _skill(catalog, "review", "a", supporting)
    wanted = _skill(catalog, "vm-review", "a", supporting)
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = [()]

    def apply(files: tuple[ArtifactFile, ...]) -> ArtifactPublication:
        return publish_artifacts(
            account.files(), files, checkpoints[-1], checkpoints.append, roots=(str(account.home),)
        )

    apply(old)
    unrelated = 509 if scenario == "marker-removed" else 508
    for index in range(unrelated):
        (catalog / f"other-{index}").touch()
    assert sum(1 for _ in catalog.rglob("*")) == 512
    _preflight(account, "codex", wanted, checkpoints[-1])

    remove = Files.remove_if
    prune = Files.prune_empty_parents

    def lose_unlink_reply(self, path, **kwargs):
        remove(self, path, **kwargs)
        if path == old[0].path:
            raise InterruptedError("fixture lost the marker unlink reply")

    def lose_prune_request(self, path, **kwargs):
        if path == old[1].path:
            raise InterruptedError("fixture stopped before pruning the support directory")
        return prune(self, path, **kwargs)

    with monkeypatch.context() as interrupted:
        if scenario == "marker-removed":
            interrupted.setattr(Files, "remove_if", lose_unlink_reply)
        else:
            interrupted.setattr(Files, "prune_empty_parents", lose_prune_request)
        with pytest.raises(InterruptedError):
            apply(wanted)

    abandoned = catalog / "review"
    if scenario == "marker-removed":
        assert [file.path for file in checkpoints[-1]] == [old[0].path]
        assert not list(abandoned.iterdir())
    else:
        assert Path(old[0].path).is_file()
        assert not list((abandoned / "nested").iterdir())
    if scenario == "unrecorded-empty-directory":
        (abandoned / "nested").rename(abandoned / "unrecorded")
        with pytest.raises(StateError):
            _preflight(account, "codex", wanted, checkpoints[-1])
        assert (abandoned / "unrecorded").is_dir()
        return

    _preflight(account, "codex", wanted, checkpoints[-1])
    final = apply(wanted)
    assert not final.skipped and {file.path for file in final.files} == {file.path for file in wanted}
    assert not abandoned.exists()
    assert sum(1 for _ in catalog.rglob("*")) == 512
    _preflight(account, "codex", wanted, final.files)


@pytest.mark.parametrize(
    ("tool", "scenario"),
    [
        ("grok", "paired-hidden"),
        ("claude", "withdrawn-hidden"),
        ("grok", "rootless"),
        ("claude", "clean-withdrawal"),
        ("grok", "untouched"),
    ],
)
def test_direct_marker_catalog_allowance_checks_disappearing_packages(tmp_path, monkeypatch, tool, scenario):
    account = _account(tmp_path, monkeypatch, tool)
    catalog = account.home / f".{tool}/skills"
    first = _skill(catalog, "review", "a")
    second = _skill(catalog, "other", "b")
    previous = publish_artifacts(
        account.files(), (*first, *second), (), lambda _: None, roots=(str(account.home),)
    ).files
    for index in range(509):
        (catalog / f"unrelated-{index}").touch()
    hidden = tuple(catalog / "other" / f"operator-{index}" for index in range(3))
    if scenario.endswith("-hidden") or scenario == "untouched":
        for member in hidden:
            member.write_bytes(b"Operator bytes.")
    if scenario == "rootless":
        previous = tuple(
            file.model_copy(update={"package_root": None}) if file.path == second[0].path else file for file in previous
        )
    replacement = _skill(catalog, "vm-review", "a")
    addition = _skill(catalog, "vm-other", "b" if scenario == "paired-hidden" else "c")
    wanted = (*replacement, *addition, *(second if scenario == "untouched" else ()))

    with pytest.raises(StateError):
        _preflight(account, tool, wanted, ())
    if scenario in {"paired-hidden", "withdrawn-hidden", "rootless"}:
        with pytest.raises(StateError):
            _preflight(account, tool, wanted, previous)
        assert all(Path(file.path).is_file() for file in (*first, *second))
        assert all(not Path(file.path).exists() for file in (*replacement, *addition))
        if scenario.endswith("-hidden"):
            assert all(path.read_bytes() == b"Operator bytes." for path in hidden)
        return

    _preflight(account, tool, wanted, previous)
    final = publish_artifacts(account.files(), wanted, previous, lambda _: None, roots=(str(account.home),))
    assert not final.skipped and {file.path for file in final.files} == {file.path for file in wanted}
    assert not Path(first[0].path).exists()
    if scenario == "untouched":
        assert all(path.read_bytes() == b"Operator bytes." for path in hidden)
    _preflight(account, tool, wanted, final.files)


def _persona(catalog: Path, name: str, origin: str, size: int, tool: str) -> ArtifactFile:
    if tool == "codex":
        before = f'name = "{name}"\ndeveloper_instructions = "'
        after = '"\n'
        data = (before + "x" * (size - len(before) - len(after)) + after).encode()
        assert len(data) == size
        suffix = ".toml"
    else:
        before = f"name: {name}\ndescription: "
        header = before + "x" * (size - 1 - len(before)) + "\n"
        assert len(header.encode()) == size
        data = f"---\n{header}---\nReview.\n".encode()
        suffix = ".md"
    return ArtifactFile(str(catalog / (name + suffix)), data, (origin * 64,), native_identity=f"agent:{name}")


def _fill_persona_bytes(catalog: Path, remaining: int, tool: str) -> None:
    index = 0
    while remaining:
        size = min(30_000, remaining)
        extra = _persona(catalog, f"unrelated-{index}", "e", size, tool)
        Path(extra.path).write_bytes(extra.data)
        remaining -= size
        index += 1


@pytest.mark.parametrize("tool", ["grok", "codex"])
@pytest.mark.parametrize("fits", [False, True])
def test_catalog_allowance_keeps_bytes_at_a_conflicting_destination(tmp_path, monkeypatch, tool, fits):
    account = _account(tmp_path, monkeypatch, tool)
    catalog = account.home / f".{tool}/agents"
    old = (_persona(catalog, "review", "a", 160, tool), _persona(catalog, "other", "b", 100, tool))
    previous = publish_artifacts(account.files(), old, (), lambda _: None, roots=(str(account.home),)).files
    foreign = _persona(catalog, "vm-other", "c", 115, tool)
    Path(foreign.path).write_bytes(foreign.data)
    limit = 1_048_576 if tool == "grok" else 1_024
    if tool == "codex":
        monkeypatch.setattr(probe, "DEFAULT_CAPTURE_LIMITS", replace(DEFAULT_CAPTURE_LIMITS, total_bytes=limit))
    _fill_persona_bytes(catalog, limit - 1 - 160 - 100 - 115, tool)
    replacement = _persona(catalog, "vm-review", "a", 158 if fits else 163, tool)
    wanted = (replacement, _persona(catalog, "vm-other", "b", 103, tool))

    with pytest.raises(StateError):
        _preflight(account, tool, wanted, ())
    if not fits:
        with pytest.raises(StateError):
            _preflight(account, tool, wanted, previous)
        assert all(Path(file.path).read_bytes() == file.data for file in old)
        assert not Path(replacement.path).exists()
    else:
        _preflight(account, tool, wanted, previous)
        final = publish_artifacts(account.files(), wanted, previous, lambda _: None, roots=(str(account.home),))
        assert final.skipped and Path(old[1].path).read_bytes() == old[1].data
        assert not Path(old[0].path).exists() and Path(replacement.path).read_bytes() == replacement.data
        _preflight(account, tool, wanted, final.files)
    assert Path(foreign.path).read_bytes() == foreign.data


def test_owned_persona_can_shrink_in_place_without_replacement_credit(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch, "claude")
    catalog = account.home / ".claude/agents"
    old = (_persona(catalog, "review", "a", 30_000, "claude"),)
    previous = publish_artifacts(account.files(), old, (), lambda _: None, roots=(str(account.home),)).files
    _fill_persona_bytes(catalog, 990_000, "claude")
    wanted = (
        _persona(catalog, "review", "a", 500, "claude"),
        _persona(catalog, "new-one", "b", 25_000, "claude"),
        _persona(catalog, "new-two", "c", 25_000, "claude"),
    )

    _preflight(account, "claude", wanted, previous)
    final = publish_artifacts(account.files(), wanted, previous, lambda _: None, roots=(str(account.home),))
    assert not final.skipped and Path(old[0].path).read_bytes() == wanted[0].data
    _preflight(account, "claude", wanted, final.files)
