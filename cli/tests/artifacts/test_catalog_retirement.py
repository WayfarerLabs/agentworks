"""Exercise retirement forecasts with the real guest probe and guarded publication."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.model import ArtifactInputs, ArtifactOrigin, ArtifactType
from agentworks.artifacts.native import probe
from agentworks.artifacts.publication import publish_artifacts
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.errors import StateError
from agentworks.execution.files import Files
from agentworks.harness_setup.model import SetupRecord
from agentworks.package_sources import DEFAULT_CAPTURE_LIMITS
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from tests.artifacts._fixtures import group, received
from tests.artifacts.test_native_delivery import artifact
from tests.native_setup_fixtures import LocalFixtureAccount, invocation

if TYPE_CHECKING:
    from agentworks.artifacts.application import ArtifactPublication
    from agentworks.artifacts.model import ArtifactInput
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
    account: LocalFixtureAccount,
    tool: str,
    files: tuple[ArtifactFile, ...],
    previous: tuple[OwnedArtifactFile, ...],
    *,
    retain_origins: frozenset[str] = frozenset(),
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
        retain_artifact_origins=retain_origins,
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
        ("claude", "retained"),
        ("grok", "retained"),
    ],
)
def test_direct_marker_catalog_allowance_checks_disappearing_packages(tmp_path, monkeypatch, tool, scenario):
    account = _account(tmp_path, monkeypatch, tool)
    catalog = account.home / f".{tool}/skills"
    supporting = "nested/SKILL.md" if scenario == "clean-withdrawal" else ""
    first = _skill(catalog, "review", "a", supporting)
    second = _skill(catalog, "other", "b")
    previous = publish_artifacts(
        account.files(), (*first, *second), (), lambda _: None, roots=(str(account.home),)
    ).files
    for index in range(509):
        (catalog / f"unrelated-{index}").touch()
    hidden = tuple(catalog / "other" / f"operator-{index}" for index in range(3))
    if scenario.endswith("-hidden") or scenario in {"retained", "untouched"}:
        for member in hidden:
            member.write_bytes(b"Operator bytes.")
    if scenario == "rootless":
        previous = tuple(
            file.model_copy(update={"package_root": None}) if file.path == second[0].path else file for file in previous
        )
    replacement = _skill(catalog, "vm-review", "a", supporting)
    addition = _skill(catalog, "vm-other", "b" if scenario == "paired-hidden" else "c")
    wanted = (*replacement, *addition, *(second if scenario == "untouched" else ()))

    with pytest.raises(StateError):
        _preflight(account, tool, wanted, ())
    if scenario in {"paired-hidden", "withdrawn-hidden"}:
        with pytest.raises(StateError):
            _preflight(account, tool, wanted, previous)
        assert all(Path(file.path).is_file() for file in (*first, *second))
        assert all(not Path(file.path).exists() for file in (*replacement, *addition))
        if scenario.endswith("-hidden"):
            assert all(path.read_bytes() == b"Operator bytes." for path in hidden)
        return

    retain_origins = frozenset(second[0].origins) if scenario == "retained" else frozenset()
    _preflight(account, tool, wanted, previous, retain_origins=retain_origins)
    final = publish_artifacts(
        account.files(), wanted, previous, lambda _: None, roots=(str(account.home),), retain_origins=retain_origins
    )
    if supporting:
        assert Path(replacement[1].path).read_bytes() == first[1].data
    if scenario == "rootless":
        assert (catalog / "other").is_dir() and not list((catalog / "other").iterdir())
        assert len(list(catalog.iterdir())) == 512
    held_paths = {second[0].path} if retain_origins else set()
    assert {item.path for item in final.skipped} == held_paths
    assert {file.path for file in final.files} == {file.path for file in wanted} | held_paths
    assert not Path(first[0].path).exists()
    if scenario in {"retained", "untouched"}:
        assert Path(second[0].path).read_bytes() == second[0].data
        assert all(path.read_bytes() == b"Operator bytes." for path in hidden)
    _preflight(account, tool, wanted, final.files, retain_origins=retain_origins)


@pytest.mark.parametrize("unrelated", [495, 509])
@pytest.mark.parametrize("rename", [False, True], ids=["withdraw", "upgrade"])
def test_direct_marker_retirement_counts_surviving_notes(tmp_path, monkeypatch, unrelated, rename):
    account = _account(tmp_path, monkeypatch, "grok")
    catalog = account.home / ".grok/skills"
    old = _skill(catalog, "review", "a")
    wanted = _skill(catalog, "vm-review" if rename else "independent", "a" if rename else "b")
    previous = publish_artifacts(account.files(), old, (), lambda _: None, roots=(str(account.home),)).files
    for index in range(unrelated):
        (catalog / f"unrelated-{index}").touch()
    notes = [catalog / "review" / f"note-{index}" for index in range(3)]
    for note in notes:
        note.write_bytes(b"Preserved notes.\n")
        note.chmod(0o640)

    if unrelated == 509:
        with pytest.raises(StateError):
            _preflight(account, "grok", wanted, previous)
        assert Path(old[0].path).is_file()
        assert not Path(wanted[0].path).exists()
    else:
        _preflight(account, "grok", wanted, previous)
        final = publish_artifacts(account.files(), wanted, previous, lambda _: None, roots=(str(account.home),))
        assert not final.skipped and not Path(old[0].path).exists()
        _preflight(account, "grok", wanted, final.files)
        checkpoints: list[tuple[OwnedArtifactFile, ...]] = []
        assert (
            publish_artifacts(account.files(), wanted, final.files, checkpoints.append, roots=(str(account.home),))
            == final
        )
        assert not checkpoints
    for note in notes:
        assert note.read_bytes() == b"Preserved notes.\n" and note.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("scenario", ["no-desired", "persona-only", "last-deferred"])
def test_grok_user_reinit_checks_withdrawn_skill_kinds(tmp_path, monkeypatch, scenario):
    account = _account(tmp_path, monkeypatch, "grok")
    catalog = account.home / ".grok/skills"
    integration = GrokBuildIntegration.for_setup(owner_kind="agent", owner_name="fixture", facet="user", config={})
    initial = replace(invocation(account, []), artifacts=received(artifact(ArtifactType.SKILL)))
    old = integration.user_init(initial).files
    previous = publish_artifacts(account.files(), old, (), lambda _: None, roots=(str(account.home),)).files
    prior = SetupRecord(
        component="agent", integration="grok", destination_id="a" * 64, declaration={}, artifact_files=previous
    )
    inputs = ArtifactInputs()
    if scenario == "persona-only":
        inputs = received(artifact(ArtifactType.AGENT, name="independent"))
    elif scenario == "last-deferred":
        inputs = initial.artifacts
        alias = catalog / "external"
        alias.mkdir()
        (alias / "SKILL.md").write_bytes(b"---\nname: review\n---\nExternal skill.\n")
    for index in range(509):
        (catalog / f"unrelated-{index}").touch()
    for index in range(3):
        (catalog / "review" / f"note-{index}").touch()
    reasons: list[str] = []
    with pytest.raises(StateError):
        integration.user_init(replace(initial, prior=prior, artifacts=inputs, report_incomplete=reasons.append))
    assert bool(reasons) == (scenario == "last-deferred")
    assert all(Path(file.path).read_bytes() == file.data for file in old)
    assert not (account.home / ".grok/agents/independent.md").exists()


def test_held_absent_legacy_marker_cannot_hide_independent_new_support(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch, "grok")
    catalog = account.home / ".grok/skills"
    old = tuple(replace(file, native_identity=None, package_root=None) for file in _skill(catalog, "review", "a"))
    previous = publish_artifacts(account.files(), old, (), lambda _: None, roots=(str(account.home),)).files
    Path(old[0].path).unlink()
    for index in range(510):
        (catalog / f"unrelated-{index}").touch()
    wanted = _skill(catalog, "review", "b", supporting="support/notes.txt")
    retained = frozenset(old[0].origins)
    with pytest.raises(StateError):
        _preflight(account, "grok", wanted, previous, retain_origins=retained)
    assert not Path(wanted[1].path).exists()
    (catalog / "unrelated-509").unlink()
    _preflight(account, "grok", wanted, previous, retain_origins=retained)
    final = publish_artifacts(
        account.files(), wanted, previous, lambda _: None, roots=(str(account.home),), retain_origins=retained
    )
    assert [item.path for item in final.skipped] == [old[0].path]
    assert not Path(old[0].path).exists() and Path(wanted[1].path).read_bytes() == wanted[1].data
    assert len(list(catalog.rglob("*"))) == 512
    _preflight(account, "grok", wanted, final.files, retain_origins=retained)


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
    foreign = _persona(catalog, "vm-other", "c", 300, tool)
    Path(foreign.path).write_bytes(foreign.data)
    Path(foreign.path).chmod(0o640)
    limit = 1_048_576 if tool == "grok" else 1_024
    if tool == "codex":
        monkeypatch.setattr(probe, "DEFAULT_CAPTURE_LIMITS", replace(DEFAULT_CAPTURE_LIMITS, total_bytes=limit))
    _fill_persona_bytes(catalog, limit - 1 - 160 - 100 - 300, tool)
    replacement = _persona(catalog, "vm-review", "a", 158 if fits else 163, tool)
    wanted = (replacement, _persona(catalog, "vm-other", "b", 103, tool))

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
    assert Path(foreign.path).stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("changed_mode", [False, True])
def test_catalog_spends_only_verified_owned_persona_shrinkage(tmp_path, monkeypatch, changed_mode):
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

    if changed_mode:
        Path(old[0].path).chmod(0o640)
        with pytest.raises(StateError):
            _preflight(account, "claude", wanted, previous)
        assert Path(old[0].path).read_bytes() == old[0].data and Path(old[0].path).stat().st_mode & 0o777 == 0o640
        assert not any(Path(file.path).exists() for file in wanted[1:])
        return

    _preflight(account, "claude", wanted, previous)
    final = publish_artifacts(account.files(), wanted, previous, lambda _: None, roots=(str(account.home),))
    assert not final.skipped and Path(old[0].path).read_bytes() == wanted[0].data
    _preflight(account, "claude", wanted, final.files)


def _vm_artifact(kind: ArtifactType, name: str) -> ArtifactInput:
    item = artifact(kind, name=name)
    content = replace(item.content, text=f"Instructions for {name}.\n") if kind is ArtifactType.AGENT else item.content
    return replace(item, content=content, origin=ArtifactOrigin("vm", "vm", "fixture", entry=name))


@pytest.mark.parametrize(
    ("kind", "scenario"),
    [
        (ArtifactType.AGENT, "clean-chain"),
        (ArtifactType.SKILL, "clean-chain"),
        (ArtifactType.SKILL, "modified-receiver"),
        (ArtifactType.AGENT, "foreign-tail"),
        (ArtifactType.AGENT, "extra-origin-path"),
    ],
)
def test_codex_catalog_credits_only_proven_retirement_at_the_head_of_a_chain(tmp_path, monkeypatch, kind, scenario):
    account = _account(tmp_path, monkeypatch, "codex")
    skills = account.home / ".agents/skills"
    agents = account.home / ".codex/agents"
    catalog = skills if kind is ArtifactType.SKILL else agents
    owner = group(*(_vm_artifact(kind, name) for name in ("review", "vm-review", "vm-vm-review")))

    def project(inputs: ArtifactInputs) -> tuple[ArtifactFile, ...]:
        return codex.outer_artifacts(inputs, skills_root=str(skills), agents_root=str(agents)).files

    old = project(ArtifactInputs(local=owner))
    changed_path = None
    operator_bytes = b"Operator edit.\n"
    if scenario == "extra-origin-path":
        changed_path = agents / "older-side-file.txt"
        old += (ArtifactFile(str(changed_path), b"Previous instructions.\n", old[0].origins),)
    local = group(artifact(ArtifactType.AGENT, name="independent")) if scenario == "foreign-tail" else None
    wanted = project(ArtifactInputs(local=local, deferred={owner.owner: owner}))
    previous = publish_artifacts(account.files(), old, (), lambda _: None, roots=(str(account.home),)).files
    if scenario == "foreign-tail":
        changed_path = agents / "vm-vm-vm-review.toml"
        operator_bytes = b'name = "vm-vm-vm-review"\ndeveloper_instructions = "Operator edits."\n'
    elif scenario == "modified-receiver":
        changed_path = skills / "vm-review/scripts/check.sh"
    if changed_path is not None:
        changed_path.write_bytes(operator_bytes)
    for index in range(512 - sum(1 for _ in catalog.rglob("*"))):
        (catalog / f"unrelated-{index}").touch()
    assert sum(1 for _ in catalog.rglob("*")) == 512

    if scenario in {"modified-receiver", "extra-origin-path"}:
        with pytest.raises(StateError):
            _preflight(account, "codex", wanted, previous)
        assert changed_path is not None and changed_path.read_bytes() == operator_bytes
        assert Path(old[0].path).is_file()
        assert not Path(wanted[-1].path).exists()
        return

    _preflight(account, "codex", wanted, previous)
    final = publish_artifacts(account.files(), wanted, previous, lambda _: None, roots=(str(account.home),))
    assert not Path(old[0].path).exists()
    if changed_path is None:
        assert not final.skipped
        assert all(Path(file.path).read_bytes() == file.data for file in wanted)
        assert all(bool(Path(file.path).stat().st_mode & 0o111) == file.executable for file in wanted)
    else:
        assert {item.path for item in final.skipped} == {str(changed_path)}
        assert changed_path.read_bytes() == operator_bytes
        assert (agents / "independent.toml").is_file()
    assert sum(1 for _ in catalog.rglob("*")) == 512
    _preflight(account, "codex", wanted, final.files)


def test_claude_catalog_credits_observed_bytes_at_a_verified_overlapping_destination(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch, "claude")
    native = account.home / ".claude"
    catalog = native / "agents"
    owner = group(*(_vm_artifact(ArtifactType.AGENT, name) for name in ("review", "vm-review")))
    old = claude.outer_artifacts(ArtifactInputs(local=owner), str(native)).files
    wanted = claude.outer_artifacts(ArtifactInputs(deferred={owner.owner: owner}), str(native)).files
    previous = publish_artifacts(account.files(), old, (), lambda _: None, roots=(str(account.home),)).files

    def header_bytes(data: bytes) -> int:
        return len(data.split(b"---\n", 2)[1])

    limit = 1_048_576
    _fill_persona_bytes(catalog, limit - sum(header_bytes(file.data) for file in wanted), "claude")
    assert sum(header_bytes(path.read_bytes()) for path in catalog.iterdir()) < limit
    assert len(list(catalog.iterdir())) < 512
    _preflight(account, "claude", old, previous)
    _preflight(account, "claude", wanted, previous)
    final = publish_artifacts(account.files(), wanted, previous, lambda _: None, roots=(str(account.home),))
    assert not final.skipped
    assert sum(header_bytes(path.read_bytes()) for path in catalog.iterdir()) == limit
    _preflight(account, "claude", wanted, final.files)
