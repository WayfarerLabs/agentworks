"""Owning reinit budgets current and replacement native catalogs."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentworks.artifacts import publication
from agentworks.artifacts.application import ArtifactApplication, ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.declarations import ArtifactDefinitions, ArtifactsConfig, RuleArtifactSpec, SkillArtifactSpec
from agentworks.artifacts.model import ArtifactInputs, ArtifactOrigin, ArtifactType
from agentworks.artifacts.native import probe
from agentworks.artifacts.native.common import prepare_session_delivery
from agentworks.artifacts.session_paths import session_publication_roots
from agentworks.artifacts.state import write_capture
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, VMSetupInvocation
from agentworks.errors import StateError
from agentworks.execution.files import Files
from agentworks.harness_setup import dispatch
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.lifecycle import prepare_vm_setup
from agentworks.harness_setup.model import NativeSetupState, SetupRecord
from agentworks.harness_setup.outcomes import SetupFailure
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.origin import Origin
from agentworks.package_sources import DEFAULT_CAPTURE_LIMITS
from agentworks.plugins.codex import harness_integration as codex
from agentworks.plugins.codex.artifacts import session_artifacts
from agentworks.plugins.codex.artifacts import vm_artifacts as render_vm_artifacts
from agentworks.plugins.grok import artifacts as grok
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from agentworks.schema import CapabilityConfig
from agentworks.vms.admin import AdminConfig
from agentworks.vms.templates import ResolvedVMTemplate
from tests.artifacts.test_catalog_retirement import _account, _persona
from tests.artifacts.test_native_delivery import artifact, context, owned
from tests.conftest import registry_with_shell
from tests.native_setup_fixtures import LocalFixtureAccount, invocation

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution")


@pytest.mark.parametrize(
    ("count", "scenario"),
    [
        (86, "retry"),
        (170, "near-limit"),
        (3, "modified"),
        (3, "unowned"),
        (3, "legacy"),
        (170, "actual-over-limit"),
        (170, "future-over-limit"),
    ],
)
def test_vm_reinit_replaces_a_same_declaration_codex_catalog(db, monkeypatch, tmp_path, count, scenario):
    target = LocalFixtureAccount(tmp_path / "native")
    machine = target.root / "etc/codex"
    catalog = machine / "skills"
    monkeypatch.setattr(probe, "_PROBE", probe._PROBE.replace("'/etc/codex'", repr(str(machine))))
    monkeypatch.setattr(dispatch, "ROOT_FILE_DIRECTORIES", (str(machine),))
    monkeypatch.setattr(publication, "ROOT_FILE_DIRECTORIES", (str(machine),))

    def vm_artifacts(inputs: ArtifactInputs) -> ArtifactApplication:
        application = render_vm_artifacts(inputs)
        files = []
        for file in application.files:
            assert file.package_root is not None
            files.append(
                replace(
                    file,
                    path=file.path.replace("/etc/codex", str(machine), 1),
                    package_root=str(machine) + file.package_root.removeprefix("/etc/codex"),
                )
            )
        return replace(application, files=tuple(files))

    monkeypatch.setattr(codex, "vm_artifacts", vm_artifacts)
    rules = {f"rule-{index}": RuleArtifactSpec(text=f"Read rule {index}.") for index in range(count)}
    registry = registry_with_shell()
    registry.add("harness-integration", "codex", HarnessIntegrationEntry(name="codex"), Origin.built_in(source="test"))
    registry.finalize(probe_host_readiness=False)
    vm = db.insert_vm("vm", "local", "vm")
    declaration = CapabilityConfig()

    def prepared() -> SetupInputs:
        return prepare_vm_setup(
            db,
            registry,
            name=vm.name,
            template=ResolvedVMTemplate(
                "default",
                artifacts=ArtifactsConfig(declare=ArtifactDefinitions(rules=rules)),
                harness_integrations={"codex": declaration},
            ),
            admin=AdminConfig(),
        )[0]

    baseline = prepared()
    captured = baseline.artifact_snapshot
    assert captured is not None
    access = target.access()
    call = VMSetupInvocation(vm=vm, access=access, files=access.files(), prior=None, checkpoint=lambda _: None)
    expected = vm_artifacts(ArtifactInputs(local=captured.inputs)).files
    records = []
    for file in expected:
        assert file.package_root is not None
        name = Path(file.package_root).name
        address = ["vm", "vm", "vm", "rule", name.removeprefix("agentworks-rule-")]
        old_name = name + "-" + hashlib.sha256(json.dumps(address).encode()).hexdigest()[:10]
        package = catalog / old_name
        package.mkdir(mode=0o755, parents=True, exist_ok=True)
        path = package / Path(file.path).name
        data = file.data.replace(f"name: {name}\n".encode(), f"name: {old_name}\n".encode())
        path.write_bytes(data)
        path.chmod(0o644)
        records.append(
            OwnedArtifactFile(
                path=str(path),
                sha256=hashlib.sha256(data).hexdigest(),
                origins=file.origins,
                native_identity=f"skill:{old_name}",
                package_root=None if scenario == "legacy" and address[-1] == "rule-0" else str(package),
            )
        )
    prior = SetupRecord(
        component="vm",
        integration="codex",
        destination_id=dispatch.destination_id(vm, access),
        declaration=baseline.declaration("codex", declaration),
        complete=True,
        artifact_inputs=tuple(item.identity for item in captured.inputs.items()),
        artifact_files=tuple(records),
    )
    write_native_setup(db, "vm", vm.name, NativeSetupState(records=(prior,)), operation="fixture")
    write_capture(db, "vm", vm.name, "vm", captured, operation="fixture")
    assert len(list(catalog.rglob("*"))) == 3 * count

    if scenario == "near-limit":
        integration = codex.CodexIntegration.for_setup(owner_kind="vm", owner_name=vm.name, facet="vm", config={})
        application = integration.vm_init(replace(call, artifacts=ArtifactInputs(local=captured.inputs), prior=prior))
        assert application.files == expected and not application.deferred
        return

    if scenario in {"modified", "unowned", "legacy"}:
        for index in range(510 - 3 * count):
            (catalog / f"unrelated-{index}").touch()
    first_package = Path(records[0].path).parent
    if scenario == "modified":
        (first_package / "guidance.md").write_text("Operator edit.")
    elif scenario == "unowned":
        (first_package / "operator.md").write_text("Operator file.")
    elif scenario == "actual-over-limit":
        for index in range(3):
            (catalog / f"unowned-{index}").touch()
    elif scenario == "future-over-limit":
        rules["additional"] = RuleArtifactSpec(text="One extra rule.")

    failures: list[SetupFailure] = []

    def reinit() -> SetupRecord:
        return dispatch.run_setup(db, registry, prepared(), call, failures=failures, operation="vm-reinit").records[0]

    if scenario in {"modified", "unowned", "legacy"}:
        after = reinit()
        assert not failures and after.complete == (scenario != "modified")
        if scenario == "modified":
            assert after.skipped and (first_package / "guidance.md").read_text() == "Operator edit."
            assert len(list(catalog.rglob("*"))) == 510
        elif scenario == "unowned":
            assert not after.skipped and (first_package / "operator.md").read_text() == "Operator file."
            assert len(list(catalog.rglob("*"))) == 512
        else:
            assert len(list(catalog.rglob("*"))) == 511
        unchanged = Mock(side_effect=AssertionError("unchanged reinit must preserve the catalog"))
        monkeypatch.setattr(Files, "write_if", unchanged)
        monkeypatch.setattr(Files, "remove_if", unchanged)
        assert reinit().artifact_files == after.artifact_files
        unchanged.assert_not_called()
        return

    if scenario != "retry":
        before = {path: path.read_bytes() for path in catalog.rglob("*") if path.is_file()}
        refuse_write = Mock(side_effect=AssertionError("over-limit setup must leave the catalog intact"))
        monkeypatch.setattr(Files, "write_if", refuse_write)
        monkeypatch.setattr(Files, "remove_if", refuse_write)
        after = reinit()
        assert len(failures) == 1 and not after.complete
        assert after.artifact_files == prior.artifact_files
        assert {path: path.read_bytes() for path in catalog.rglob("*") if path.is_file()} == before
        refuse_write.assert_not_called()
        return

    def interrupt_checkpoint(db, kind, name, state, **kwargs):
        if len(state.records[0].artifact_files) < len(prior.artifact_files):
            raise StateError("fixture lost the first retirement checkpoint")
        return write_native_setup(db, kind, name, state, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(dispatch, "write_native_setup", interrupt_checkpoint)
        with pytest.raises(StateError):
            reinit()
    saved = read_native_setup(db, "vm", vm.name).records[0]
    assert not saved.complete and saved.artifact_files == prior.artifact_files
    assert len(list(catalog.rglob("*"))) == 3 * count - 1

    after = reinit()
    assert not failures and after.complete and not after.skipped
    assert after.declaration == prior.declaration and after.artifact_inputs == prior.artifact_inputs
    assert {file.path for file in after.artifact_files} == {file.path for file in expected}
    assert len(list(catalog.rglob("*"))) == 3 * count
    assert all(Path(file.path).read_bytes() == file.data for file in expected)
    no_mutation = Mock(side_effect=AssertionError("converged reinit must leave the catalog intact"))
    monkeypatch.setattr(Files, "write_if", no_mutation)
    monkeypatch.setattr(Files, "remove_if", no_mutation)
    assert reinit().artifact_files == after.artifact_files
    no_mutation.assert_not_called()


@pytest.mark.parametrize(
    ("tool", "count", "padding", "codex_budget"),
    [
        pytest.param("claude", 257, 0, None, id="claude-entries"),
        pytest.param("grok", 257, 0, None, id="grok-entries"),
        pytest.param("claude", 20, 30_000, None, id="claude-metadata-bytes"),
        pytest.param("codex", 2, 36_000, 128_000, id="codex-persona-bytes"),
    ],
)
def test_user_probe_budgets_selectively_renamed_personas(tmp_path, monkeypatch, tool, count, padding, codex_budget):
    target = LocalFixtureAccount(tmp_path / "native")
    native = target.home / f".{tool}"
    agents = native / "agents"
    agents.mkdir(parents=True)
    binary = target.root / "bin" / tool
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 97\n")
    binary.chmod(0o700)
    source = probe._PROBE
    for relative in ("etc/claude-code/.claude", "etc/claude-code/managed-settings.json", "etc/codex"):
        source = source.replace(repr("/" + relative), repr(str(target.root / relative)))
    monkeypatch.setattr(probe, "_PROBE", source)
    if codex_budget is not None:
        limits = replace(DEFAULT_CAPTURE_LIMITS, total_bytes=codex_budget)
        monkeypatch.setattr(probe, "DEFAULT_CAPTURE_LIMITS", limits)

    def persona(name: str, origins: tuple[str, ...]) -> ArtifactFile:
        description = "x" * padding
        if tool == "codex":
            text = f'name = "{name}"\ndeveloper_instructions = "{description}"\n'
            suffix = ".toml"
        else:
            text = f"---\nname: {name}\ndescription: Review {description}\n---\nReview the changes.\n"
            suffix = ".md"
        return ArtifactFile(str(agents / (name + suffix)), text.encode(), origins, native_identity=f"agent:{name}")

    previous, desired = [], []
    for index in range(count):
        name = f"review-{index}"
        origins = (hashlib.sha256(name.encode()).hexdigest(),)
        original = persona(f"previous-{index}", origins)
        path = Path(original.path)
        path.write_bytes(original.data)
        path.chmod(0o600)
        previous.append(
            OwnedArtifactFile(
                path=original.path,
                sha256=hashlib.sha256(original.data).hexdigest(),
                origins=origins,
                native_identity=original.native_identity,
            )
        )
        desired.append(persona(name, origins))

    record = SetupRecord(
        component="agent", integration=tool, destination_id="a" * 64, declaration={}, artifact_files=tuple(previous)
    )
    call = invocation(target, [], prior=record)

    def check(setup: UserSetupInvocation | None = None) -> str:
        return probe.probe_native(call.access, tool=tool, environment={}, home=call.home, files=desired, setup=setup)

    assert check(call) == str(native)
    with pytest.raises(StateError):
        check()
    held = frozenset(origin for file in previous for origin in file.origins)
    assert check(replace(call, retain_artifact_origins=held)) == str(native)
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = []
    retained = publication.publish_artifacts(
        target.files(), desired, tuple(previous), checkpoints.append, roots=(str(target.home),), retain_origins=held
    )
    assert not checkpoints and retained.files == tuple(previous) and len(retained.skipped) == 2 * count
    assert check(replace(call, retain_artifact_origins=held)) == str(native)
    desired.extend(persona(f"additional-{index}", ("f" * 64,)) for index in range(count))
    with pytest.raises(StateError):
        check(call)


def test_grok_skill_upgrade_budgets_distinct_physical_members(tmp_path):
    account = LocalFixtureAccount(tmp_path / "native")
    binary = account.root / "bin/grok"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 97\n")
    binary.chmod(0o700)
    native = account.home / ".grok"
    source = native / "skills/review"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: review\ndescription: Review fixture\n---\nReview.\n")
    for index in range(2048):
        (source / f"note-{index}").touch()
    for path in source.iterdir():
        path.chmod(0o600)
    captured = capture_artifacts(
        [("team", ArtifactBundle(name="team", skills={"review": SkillArtifactSpec(source=str(source))}))],
        ArtifactOrigin("vm", "vm", "fixture"),
    )
    old = grok.outer_artifacts(ArtifactInputs(local=captured), str(native)).files
    assert len(old) == 2049
    prior = SetupRecord(
        component="agent",
        integration="grok",
        destination_id="a" * 64,
        declaration={},
        artifact_files=tuple(owned(file) for file in old),
    )
    inputs = ArtifactInputs(deferred={captured.owner: captured})
    call = replace(invocation(account, []), artifacts=inputs, prior=prior)
    integration = GrokBuildIntegration.for_setup(owner_kind="agent", owner_name="fixture", facet="user", config={})
    expected = grok.outer_artifacts(inputs, str(native)).files
    receiver = native / "skills/vm-review"
    assert not receiver.exists() and integration.user_init(call).files == expected
    assert {Path(file.path).parent for file in expected} == {receiver}

    # Matching pre-existing receiving members also need fingerprints. The old
    # package's files are visited again while looking for newly exposed children.
    receiver.mkdir()
    marker = receiver / "SKILL.md"
    marker.write_bytes(next(file.data for file in expected if file.path == str(marker)))
    for index in range(2046):
        (receiver / f"note-{index}").touch()
    assert integration.user_init(call).files == expected  # 2,049 old + 2,047 receiving files.
    extra_receiver = receiver / "note-2046"
    extra_receiver.touch()
    with pytest.raises(StateError):
        integration.user_init(call)
    extra_receiver.unlink()
    (source / "foreign-note").write_bytes(b"Retain me.")
    with pytest.raises(StateError):
        integration.user_init(call)
    assert (source / "foreign-note").read_bytes() == b"Retain me." and (source / "SKILL.md").is_file()


def test_codex_session_with_workspace_config_home_preserves_both_skill_catalogs(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch, "codex")
    workspace = account.root / "workspace"
    native = workspace / ".codex"
    native.mkdir(parents=True)
    user_skills = account.home / ".agents/skills"
    project_skills = workspace / ".agents/skills"
    for directory, count in ((user_skills, 256), (project_skills, 253)):
        directory.mkdir(parents=True)
        for index in range(count):
            (directory / str(index)).touch()
    call = replace(
        context(artifact(ArtifactType.RULE)),
        home=str(account.home),
        directory=str(account.home / "private/session/run"),
        environment={"CODEX_HOME": str(native)},
    )

    def prepare() -> ArtifactApplication:
        plan, _ = prepare_session_delivery(
            account.access(),
            tool="codex",
            integration="codex",
            context=call,
            workspace=str(workspace),
            render=lambda prepared: session_artifacts(
                prepared, extra_args=[], enabled_workarounds=["session-home-skills"]
            ),
        )
        return plan.application

    plan = prepare()
    assert len(plan.files) == 2
    roots = session_publication_roots(
        plan.files, home=call.home, session_uuid=call.session_uuid, run_id=call.run_id, directory=call.directory
    )
    saved = publication.publish_artifacts(account.files(), plan.files, (), lambda _: None, roots=roots)
    assert not saved.skipped and len(list(user_skills.rglob("*"))) == 259
    assert prepare() == plan
    (project_skills / "extra").touch()
    with pytest.raises(StateError):
        prepare()


def test_codex_workspace_config_alias_counts_persona_metadata_once(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch, "codex")
    workspace = account.root / "workspace"
    native = workspace / ".codex"
    agents = native / "agents"
    agents.mkdir(parents=True)
    persona = _persona(agents, "review", "a", 500, "codex")
    Path(persona.path).write_bytes(persona.data)
    monkeypatch.setattr(probe, "DEFAULT_CAPTURE_LIMITS", replace(DEFAULT_CAPTURE_LIMITS, total_bytes=500))

    def check() -> str:
        return probe.probe_native(
            account.access(),
            tool="codex",
            environment={"CODEX_HOME": str(native)},
            home=str(account.home),
            workspace=str(workspace),
            files=(owned(persona),),
        )

    assert check() == str(native)
    Path(persona.path).write_bytes(persona.data + b"\n")
    with pytest.raises(StateError):
        check()
