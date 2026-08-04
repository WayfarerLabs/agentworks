"""Tests for ``agw resource migrate`` (the ``agentworks.migrate`` package)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.migrate import execute_plan, plan_migration

MAXIMAL_RESOURCES = """\
# npm secret comment
[secrets.npm-token]
description = "npm registry token"  # inline comment survives
backend_mappings.env-var = "NPM_TOKEN"

[vm_templates.default]
cpus = 4

[vm_templates.dev]
inherits = ["default"]
cpus = 8

[vm_templates.dev.env]
HTTP_PROXY = "http://proxy:3128"

[workspace_templates.proj]
repo = "https://github.com/org/proj.git"
tmuxinator = false

[agent_templates.default]
shell = "bash"

[session_templates.claude]
command = "claude"
description = "Claude session"

[session_templates.claude.env]
CLAUDE_LOG_LEVEL = "info"

[git_credentials.github]
type = "github"
description = "gh access"

[azure]
subscription_id = "0000"
resource_group = "agw"
region = "eastus"

[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000

[secret_backends.env-var]

[admin.config]
shell = "zsh"

[admin.env]
EDITOR = "nvim"

[named_console]
tmux_layout = "tiled"

[apt_sources.my-repo]
description = "internal repo"
key_url = "https://apt.example.com/key.gpg"
key_path = "/etc/apt/keyrings/my-repo.gpg"
source = "deb [arch={arch}] https://apt.example.com/debian bookworm main"
source_file = "my-repo.list"

[apt_packages.my-tool]
description = "my tool"
apt = ["my-tool"]

[system_install_commands.my-sys]
description = "sys tool"
command = "echo sys"
test_exec = "my-sys"

[user_install_commands.my-user]
description = "user tool"
command = "echo user"
test_exec = "my-user"
"""


def _write_config(tmp_path: Path, resources: str = MAXIMAL_RESOURCES, *, prefix: str = "") -> Path:
    """``prefix`` lands before the first table header -- the only place
    a TOP-LEVEL assignment shape (``secrets = {...}``) can live."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""\
{prefix}# operator identity comment stays
[operator]
ssh_public_key = "{pub.as_posix()}"
ssh_private_key = "{priv.as_posix()}"

[paths]
backups = "{(tmp_path / "backups").as_posix()}"

{resources}
[defaults]
"""
    )
    return cfg


def _plan(cfg: Path, selectors: list[str], **kwargs: object):
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    return config, plan_migration(config, registry, selectors, **kwargs)  # type: ignore[arg-type]


def _loaded_docs(path: Path) -> list[dict]:  # type: ignore[type-arg]
    return [d for d in yaml.safe_load_all(path.read_text()) if d is not None]


# ---------------------------------------------------------------------------
# Golden migration
# ---------------------------------------------------------------------------


def test_full_migration_golden(tmp_path: Path) -> None:
    """The maximal config migrates wholesale (--all): every kind lands
    in YAML, the TOML keeps only config sections (comments preserved),
    the secret_backends residue is dropped, and verification passes."""
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, [], all_resources=True)

    kinds = {(u.kind, u.name) for u in plan.units}
    assert kinds == {
        ("secret", "npm-token"),
        ("vm-template", "default"),
        ("vm-template", "dev"),
        ("workspace-template", "proj"),
        ("agent-template", "default"),
        ("session-template", "claude"),
        ("git-credential", "github"),
        ("vm-site", "azure"),
        ("vm-site", "proxmox"),
        ("admin-template", "default"),
        ("named-console-template", "default"),
        ("apt-source", "my-repo"),
        ("apt-package", "my-tool"),
        ("system-install-command", "my-sys"),
        ("user-install-command", "my-user"),
    }

    result = execute_plan(plan, config)
    assert result.verified_rows > 0
    assert result.dropped_secret_backends

    after = cfg.read_text()
    # Surviving config sections and their comments are untouched.
    assert "# operator identity comment stays" in after
    assert "[operator]" in after
    assert "[defaults]" in after
    assert "[secret_backends.env-var]" not in after
    # Migrated sections are commented out (default mode) with markers.
    assert "# migrated to resources/secrets.yaml" in after
    assert "# [secrets.npm-token]" in after
    assert "\n[secrets.npm-token]" not in after

    # The rewritten config still loads and the registry is equivalent
    # (execute_plan verified this; double-check the reload works).
    reloaded = load_config(cfg, warn_issues=False)
    build_registry(reloaded)
    # The emitted manifests spell the tagged capability shape, so they
    # load warning-free: no deprecated-shape aggregate.
    from agentworks.manifests import load_manifests

    emitted = load_manifests(tmp_path / "resources")
    assert emitted.deprecation_issues == ()
    assert not emitted.issues

    # Per-kind layout: one file per kind with the plural-s convention.
    resources = tmp_path / "resources"
    assert (resources / "secrets.yaml").exists()
    assert (resources / "vm-templates.yaml").exists()
    docs = _loaded_docs(resources / "vm-templates.yaml")
    assert [d["metadata"]["name"] for d in docs] == ["default", "dev"]
    # Non-contiguous env section folded into the one document.
    assert docs[1]["spec"]["env"] == {"HTTP_PROXY": "http://proxy:3128"}


def test_vm_site_sections_migrate_flat_to_nested(tmp_path: Path) -> None:
    """The legacy flat [azure] / [proxmox] sections emit as vm-site
    manifests with the platform-owned keys folded into the tagged
    spec.platform table, the whole section comments out, and the
    post-run registry-equivalence verification passes."""
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["vm-site"])

    assert {(u.kind, u.name) for u in plan.units} == {
        ("vm-site", "azure"),
        ("vm-site", "proxmox"),
    }
    result = execute_plan(plan, config)
    assert result.verified_rows > 0

    docs = _loaded_docs(tmp_path / "resources" / "vm-sites.yaml")
    assert [d["metadata"]["name"] for d in docs] == ["azure", "proxmox"]
    azure, proxmox = docs
    # The migrated SITE keeps the section name "azure"; the platform
    # underneath is the renamed azure-vm capability, emitted as the
    # tagged table (name selects the capability, other keys are config).
    assert azure["spec"] == {
        "platform": {
            "name": "azure-vm",
            "subscription_id": "0000",
            "resource_group": "agw",
            "region": "eastus",
        },
    }
    assert proxmox["spec"]["platform"]["name"] == "proxmox"
    assert proxmox["spec"]["platform"]["template_vmid"] == 9000

    after = cfg.read_text()
    assert "# migrated to resources/vm-sites.yaml" in after
    assert "# [azure]" in after
    assert "\n[azure]" not in after

    # The rewritten config reloads and the sites resolve as manifests.
    reloaded = load_config(cfg, warn_issues=False, warn_deprecations=False)
    registry = build_registry(reloaded)
    assert registry.lookup("vm-site", "azure").platform == "azure-vm"


def test_vm_site_selector_by_name(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["vm-site/azure"])
    assert [(u.kind, u.name) for u in plan.units] == [("vm-site", "azure")]
    execute_plan(plan, config)
    after = cfg.read_text()
    assert "# [azure]" in after
    assert "[proxmox]" in after  # unselected sibling untouched


def test_vm_site_description_refused_before_write(tmp_path: Path) -> None:
    """The flat legacy sections never supported description (the TOML
    loader silently drops it), so it must NOT ride into metadata: the
    pre-rows carry no description and verification would fail after
    writing. It falls into platform_config and refuses pre-write."""
    resources = MAXIMAL_RESOURCES.replace('region = "eastus"', 'region = "eastus"\ndescription = "our sub"')
    cfg = _write_config(tmp_path, resources)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    with pytest.raises(ConfigError, match="cannot migrate vm-site/azure"):
        plan_migration(config, registry, ["vm-site/azure"])


def test_vm_site_stray_key_refused_before_write(tmp_path: Path) -> None:
    """A stray key the TOML loader silently drops would fail manifest
    validation after emission; the migrator refuses pre-write in the
    operator's TOML vocabulary instead."""
    resources = MAXIMAL_RESOURCES.replace('region = "eastus"', 'region = "eastus"\nstray_key = "x"')
    cfg = _write_config(tmp_path, resources)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    with pytest.raises(ConfigError, match="cannot migrate vm-site/azure"):
        plan_migration(config, registry, ["vm-site/azure"])


def test_git_credential_type_becomes_provider(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["git-credential/github"])
    execute_plan(plan, config)
    (doc,) = _loaded_docs(tmp_path / "resources" / "git-credentials.yaml")
    assert doc["spec"] == {"provider": {"name": "github"}}
    assert doc["metadata"]["description"] == "gh access"


def test_git_credential_org_nests_in_emission(tmp_path: Path) -> None:
    """The migrator emits the tagged YAML shape (provider-owned org AND
    token folded into the spec.provider table); the run's own
    registry-equivalence verification proves the divergence from the
    flat TOML is shape-only."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[git_credentials.ado]
type = "azdo"
org = "my-org"
token = "git-token-ado"
description = "AZDO access"
""",
    )
    config, plan = _plan(cfg, ["git-credential/ado"])
    execute_plan(plan, config)  # verification passes -> rows equivalent
    (doc,) = _loaded_docs(tmp_path / "resources" / "git-credentials.yaml")
    assert doc["spec"] == {
        "provider": {"name": "azdo", "org": "my-org", "token": "git-token-ado"},
    }
    assert doc["metadata"]["description"] == "AZDO access"


def test_session_template_flat_fields_nest_under_harness_integration_config(
    tmp_path: Path,
) -> None:
    """The migrator emits the tagged YAML shape: flat command fields
    fold into the spec.harness_integration table on the 'shell' integration (mirroring
    the git-credential fold); env stays kind-owned at the spec top
    level, and the run's registry-equivalence verification proves the
    hoist and the emission land on the identical value."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[session_templates.claude]
command = "claude"
restart_command = "claude --resume"
required_commands = ["claude"]
description = "Claude session"

[session_templates.claude.env]
CLAUDE_LOG_LEVEL = "info"
""",
    )
    config, plan = _plan(cfg, ["session-template/claude"])
    execute_plan(plan, config)  # verification passes -> rows equivalent
    (doc,) = _loaded_docs(tmp_path / "resources" / "session-templates.yaml")
    assert doc["metadata"]["description"] == "Claude session"
    assert doc["spec"] == {
        "harness_integration": {
            "name": "shell",
            "command": "claude",
            "resume_command": "claude --resume",
            "required_commands": ["claude"],
        },
        "env": {"CLAUDE_LOG_LEVEL": "info"},
    }


def test_session_template_declared_pair_passes_through(tmp_path: Path) -> None:
    """A TOML template already spelling the nested pair migrates to the
    tagged table (the legacy harness + harness_config pair folds together, env stays
    top-level)."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[session_templates.htop]
harness = "shell"

[session_templates.htop.harness_config]
command = "htop"
required_commands = ["htop"]
""",
    )
    config, plan = _plan(cfg, ["session-template/htop"])
    execute_plan(plan, config)
    (doc,) = _loaded_docs(tmp_path / "resources" / "session-templates.yaml")
    assert doc["spec"] == {
        "harness_integration": {"name": "shell", "command": "htop", "required_commands": ["htop"]},
    }


def test_session_template_nested_restart_command_migrates_to_resume_command(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        resources="""\
[session_templates.shell]
harness_integration = "shell"

[session_templates.shell.harness_integration_config]
command = "tool"
restart_command = "tool --resume"
""",
    )
    config, plan = _plan(cfg, ["session-template/shell"])
    execute_plan(plan, config)
    (doc,) = _loaded_docs(tmp_path / "resources" / "session-templates.yaml")
    assert doc["spec"]["harness_integration"] == {
        "name": "shell",
        "command": "tool",
        "resume_command": "tool --resume",
    }


def test_session_template_mixed_resume_spellings_fail_without_writes(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        resources="""\
[session_templates.shell]
harness_integration = "shell"

[session_templates.shell.harness_integration_config]
resume_command = "new"
restart_command = "old"
""",
    )
    original = cfg.read_text()

    with pytest.raises(ConfigError, match="resume_command and restart_command cannot be combined"):
        _plan(cfg, ["session-template/shell"])

    assert cfg.read_text() == original
    assert not (tmp_path / "resources").exists()


def test_singletons_emit_default_documents(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["admin-template", "named-console-template"])
    execute_plan(plan, config)
    (admin,) = _loaded_docs(tmp_path / "resources" / "admin-templates.yaml")
    assert admin["metadata"]["name"] == "default"
    assert admin["spec"]["shell"] == "zsh"
    assert admin["spec"]["env"] == {"EDITOR": "nvim"}
    (console,) = _loaded_docs(tmp_path / "resources" / "named-console-templates.yaml")
    assert console["spec"] == {"tmux_layout": "tiled"}


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


def test_kind_selector_scopes_to_kind(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    _config, plan = _plan(cfg, ["vm-template"])
    assert {(u.kind, u.name) for u in plan.units} == {
        ("vm-template", "default"),
        ("vm-template", "dev"),
    }


def test_overlapping_selectors_union(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    _config, plan = _plan(cfg, ["vm-template", "vm-template/dev"])
    names = [u.name for u in plan.units if u.kind == "vm-template"]
    assert names == ["default", "dev"]  # each exactly once, declaration order


def test_unknown_kind_selector_errors(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    with pytest.raises(ValidationError, match="unknown kind"):
        _plan(cfg, ["vm-templates"])


def test_explicit_selector_matching_nothing_errors(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    with pytest.raises(ValidationError, match="no migratable secret"):
        _plan(cfg, ["secret/nope"])


def test_kind_selector_with_no_toml_rows_errors(tmp_path: Path) -> None:
    """Explicit kind selector after that kind is fully migrated: error,
    not silence -- the operator named something specific."""
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["secret"])
    execute_plan(plan, config)
    with pytest.raises(ValidationError, match="no migratable resources"):
        _plan(cfg, ["secret"])


def test_secret_backend_selector_gets_tailored_error(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    with pytest.raises(ValidationError, match="no manifest successor"):
        _plan(cfg, ["secret-backend"])


def test_bare_invocation_is_an_error(tmp_path: Path) -> None:
    """No selectors and no --all: error, never an accidental
    whole-config migration (maintainer ruling, 2026-07-05)."""
    cfg = _write_config(tmp_path)
    with pytest.raises(ValidationError, match="indicate resources to migrate"):
        _plan(cfg, [])


def test_selectors_and_all_are_mutually_exclusive(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    with pytest.raises(ValidationError, match="not both"):
        _plan(cfg, ["secret"], all_resources=True)


def test_all_run_with_nothing_left_is_nothing_to_do(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, [], all_resources=True)
    execute_plan(plan, config)
    _config2, plan2 = _plan(cfg, [], all_resources=True)
    assert plan2.nothing_to_do


def test_all_run_with_only_secret_backends_offers_drop(tmp_path: Path) -> None:
    """The [secret_backends.*] residue is droppable even when there are
    no resources left to migrate."""
    cfg = _write_config(tmp_path, resources="[secret_backends.env-var]\n")
    config, plan = _plan(cfg, [], all_resources=True)
    assert not plan.units
    assert plan.drops_secret_backends
    assert not plan.nothing_to_do
    execute_plan(plan, config)
    assert "[secret_backends" not in cfg.read_text()


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------


def test_single_layout_one_file(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, [], all_resources=True, layout="single")
    execute_plan(plan, config)
    target = tmp_path / "resources" / "resources.yaml"
    assert target.exists()
    assert len(_loaded_docs(target)) == len(plan.units)


def test_per_resource_layout_kind_directories(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["vm-template"], layout="per-resource")
    execute_plan(plan, config)
    assert (tmp_path / "resources" / "vm-template" / "default.yaml").exists()
    assert (tmp_path / "resources" / "vm-template" / "dev.yaml").exists()


def test_per_resource_layout_refuses_unsafe_names(tmp_path: Path) -> None:
    """'/' is rejected at load, but spaces (and other shell-hostile
    characters) survive name pass-through and are refused by the
    per-resource layout specifically."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[vm_templates."weird name"]
cpus = 2
""",
    )
    with pytest.raises(ConfigError, match="not filename-safe"):
        _plan(cfg, [], all_resources=True, layout="per-resource")


def test_unknown_layout_and_toml_mode_error(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    with pytest.raises(ValidationError, match="unknown layout"):
        _plan(cfg, [], all_resources=True, layout="flat")
    with pytest.raises(ValidationError, match="unknown --toml mode"):
        _plan(cfg, [], all_resources=True, toml_mode="erase")


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def test_append_to_existing_file(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    existing = resources / "secrets.yaml"
    hand_written = (
        "apiVersion: agentworks/v1\n"
        "kind: secret\n"
        "metadata:\n"
        "  name: hand-written\n"
        "  description: already here\n"
        "spec: {}\n"
    )
    existing.write_text(hand_written)

    config, plan = _plan(cfg, ["secret/npm-token"])
    result = execute_plan(plan, config)
    assert result.appended == [existing]
    text = existing.read_text()
    assert text.startswith(hand_written)  # never rewritten
    docs = _loaded_docs(existing)
    assert [d["metadata"]["name"] for d in docs] == ["hand-written", "npm-token"]


def test_append_newline_guard(tmp_path: Path) -> None:
    """A file lacking a trailing newline gets one before the --- separator."""
    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    existing = resources / "secrets.yaml"
    existing.write_text(
        "apiVersion: agentworks/v1\n"
        "kind: secret\n"
        "metadata:\n"
        "  name: hand-written\n"
        "  description: already here\n"
        "spec: {}"  # no trailing newline
    )
    config, plan = _plan(cfg, ["secret/npm-token"])
    execute_plan(plan, config)
    assert "spec: {}\n---\n" in existing.read_text()
    assert len(_loaded_docs(existing)) == 2


# ---------------------------------------------------------------------------
# TOML edit modes
# ---------------------------------------------------------------------------


def test_comment_mode_preserves_operator_comments(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["secret/npm-token"])
    execute_plan(plan, config)
    after = cfg.read_text()
    assert "# npm secret comment" in after
    assert '# description = "npm registry token"  # inline comment survives' in after


def test_comment_mode_non_contiguous_unit(tmp_path: Path) -> None:
    """[session_templates.claude] and its later .env section are one
    unit: both are commented out, each where it sits."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[session_templates.claude]
command = "claude"
description = "Claude session"

[secrets.keeper]
description = "stays"

[session_templates.claude.env]
CLAUDE_LOG_LEVEL = "info"
""",
    )
    config, plan = _plan(cfg, ["session-template/claude"])
    execute_plan(plan, config)
    after = cfg.read_text()
    assert "# [session_templates.claude]" in after
    assert "# [session_templates.claude.env]" in after
    assert "\n[secrets.keeper]" in after  # untouched neighbor between the halves
    (doc,) = _loaded_docs(tmp_path / "resources" / "session-templates.yaml")
    assert doc["spec"]["env"] == {"CLAUDE_LOG_LEVEL": "info"}


def test_partial_occurrence_comment_keeps_siblings(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        resources="""\
[secrets.migrate-me]
description = "goes"

[secrets.keeper]
description = "stays"
""",
    )
    config, plan = _plan(cfg, ["secret/migrate-me"])
    execute_plan(plan, config)
    after = cfg.read_text()
    assert "# [secrets.migrate-me]" in after
    assert "\n[secrets.keeper]" in after
    # No stray bare [secrets] header appears.
    assert "\n[secrets]\n" not in after


def test_delete_mode_removes_sections(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["secret/npm-token"], toml_mode="delete")
    execute_plan(plan, config)
    after = cfg.read_text()
    assert "npm-token" not in after
    assert "# migrated to" not in after


def test_dotted_key_declaration_refused_with_location(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        resources="""\
[secrets]
npm-token = { description = "inline shape" }
""",
    )
    with pytest.raises(ConfigError, match="dotted key or inline table") as exc:
        _plan(cfg, ["secret/npm-token"])
    assert "config.toml:" in str(exc.value)


def test_top_level_assignment_shape_refused_on_bare_run(tmp_path: Path) -> None:
    """A resource declared via a top-level assignment (`secrets = {...}`)
    loads into the registry but has no faithful comment-out rendering.
    It must be discovered and REFUSED -- silently skipping it would
    report a complete migration that left rows behind."""
    cfg = _write_config(
        tmp_path,
        resources="",
        prefix='secrets = { npm-token = { description = "assignment shape" } }\n',
    )
    with pytest.raises(ConfigError, match="standard TOML tables") as exc:
        _plan(cfg, [], all_resources=True)
    assert "config.toml:" in str(exc.value)
    # And the explicit selector reaches the same refusal, not a
    # misleading "no TOML-declared secret".
    with pytest.raises(ConfigError, match="standard TOML tables"):
        _plan(cfg, ["secret/npm-token"])


def test_singleton_assignment_shape_refused(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        resources="",
        prefix='admin = { config = { shell = "zsh" } }\n',
    )
    with pytest.raises(ConfigError, match="standard TOML tables"):
        _plan(cfg, ["admin-template"])


def test_slash_names_are_rejected_at_load(tmp_path: Path) -> None:
    """'/' is banned in resource names at Registry.add (maintainer
    ruling, 2026-07-05), so a slash-named resource never reaches the
    migrate tool -- the KIND/NAME selector grammar is unambiguous."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[vm_templates."we/ird"]
cpus = 2
""",
    )
    with pytest.raises(ConfigError, match="contains '/'"):
        _plan(cfg, [], all_resources=True)


def test_per_resource_comment_markers_name_every_file(tmp_path: Path) -> None:
    """A whole contiguous run replaced under per-resource layout gets
    one marker line per distinct target file, not just the first."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[vm_templates.default]
cpus = 4

[vm_templates.dev]
cpus = 8
""",
    )
    config, plan = _plan(cfg, ["vm-template"], layout="per-resource")
    execute_plan(plan, config)
    after = cfg.read_text()
    assert "# migrated to resources/vm-template/default.yaml" in after
    assert "# migrated to resources/vm-template/dev.yaml" in after


# ---------------------------------------------------------------------------
# Safety: backup, dry-run, verification, rollback
# ---------------------------------------------------------------------------


def test_backup_holds_the_original(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    original = cfg.read_text()
    config, plan = _plan(cfg, [], all_resources=True)
    result = execute_plan(plan, config)
    assert result.backup_path.parent == tmp_path / "backups"
    assert result.backup_path.read_text() == original


def test_backup_taken_before_any_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backup must exist before the first manifest byte is written:
    force the very first write step to fail and assert the backup is
    already on disk with the original content."""
    import agentworks.migrate.execute as execute_mod

    cfg = _write_config(tmp_path)
    original = cfg.read_text()

    def boom(*args: object, **kwargs: object) -> list:  # type: ignore[type-arg]
        raise OSError("simulated write failure")

    monkeypatch.setattr(execute_mod, "_ensure_parents", boom)
    config, plan = _plan(cfg, [], all_resources=True)
    with pytest.raises(OSError, match="simulated"):
        execute_plan(plan, config)
    backups = sorted((tmp_path / "backups").glob("config-*.toml"))
    assert backups, "backup must be taken before any write"
    assert backups[0].read_text() == original
    assert cfg.read_text() == original


def test_backup_stamps_do_not_collide(tmp_path: Path) -> None:
    """Two runs inside one second keep both backups."""
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["secret"])
    execute_plan(plan, config)
    config2, plan2 = _plan(cfg, ["vm-template"])
    execute_plan(plan2, config2)
    backups = list((tmp_path / "backups").glob("config-*.toml"))
    assert len(backups) == 2


def test_preview_lists_every_resource_and_the_drop_note(tmp_path: Path) -> None:
    from agentworks.migrate.render import render_preview

    cfg = _write_config(tmp_path)
    _config, plan = _plan(cfg, [], all_resources=True)
    text = "\n".join(render_preview(plan))
    for unit in plan.units:
        assert f"{unit.kind}/{unit.name} -> " in text
    assert "[secret_backends.*] sections will be dropped" in text


def test_dry_run_is_plan_only_and_summary_by_default(tmp_path: Path) -> None:
    """Planning writes nothing, and the dry-run default is the summary
    (maintainer ruling, 2026-07-05: whole-config content dumps are
    unusable as a first answer); --full opts into documents + diff."""
    from agentworks.migrate.render import render_dry_run

    cfg = _write_config(tmp_path)
    original = cfg.read_text()
    _config, plan = _plan(cfg, [], all_resources=True)
    summary = render_dry_run(plan)
    assert not any("Config.toml changes" in line for line in summary)
    assert any("Pass --full" in line for line in summary)
    assert any("secret/npm-token -> " in line for line in summary)
    detailed = render_dry_run(plan, full=True)
    assert any("Config.toml changes" in line for line in detailed)
    assert any("apiVersion: agentworks/v1" in line for line in detailed)
    assert cfg.read_text() == original
    assert not (tmp_path / "resources").exists()
    assert not (tmp_path / "backups").exists()


def test_partial_migration_verifies(tmp_path: Path) -> None:
    """One kind moved, the rest still TOML: rows changed publishers, so
    this pins the keyed (not ordered) comparison."""
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["secret"])
    result = execute_plan(plan, config)
    assert result.verified_rows > 0


def test_old_yaml_session_selector_migrates_with_comments_and_digest_guard(tmp_path: Path) -> None:
    """A selector can target a YAML-only old form; migration round-trips
    its comments and verification confirms canonical YAML resolves alike."""
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        """\
# file comment
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: htop
spec:
  # selector comment
  harness: shell
  harness_config:
    command: htop
"""
    )

    config, plan = _plan(cfg, ["session-template/htop"])
    assert [(unit.kind, unit.name, unit.source) for unit in plan.units] == [("session-template", "htop", "yaml")]
    assert not plan.writes
    assert len(plan.yaml_rewrites) == 1

    result = execute_plan(plan, config)
    rewritten = manifest.read_text()
    assert result.replaced == [manifest]
    assert "# file comment" in rewritten
    assert "# selector comment" in rewritten
    assert "harness_integration:" in rewritten
    assert "harness_config:" not in rewritten
    registry = build_registry(load_config(cfg, warn_issues=False))
    assert registry.lookup("session-template", "htop").harness_integration == "shell"


def test_yaml_selector_rewrite_preserves_markers_and_all_comment_attachment_kinds(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        """---
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: htop
spec:
  harness: shell # selector value comment
  harness_config: # config key comment
    # nested map comment
    command: htop # nested value comment
...
"""
    )
    config, plan = _plan(cfg, ["session-template/htop"])
    execute_plan(plan, config)

    rewritten = manifest.read_text()
    assert rewritten.startswith("---\n")
    assert rewritten.endswith("...\n")
    assert "harness_integration: shell # selector value comment" not in rewritten
    assert "selector value comment" in rewritten
    assert "config key comment" in rewritten
    assert "nested map comment" in rewritten
    assert "command: htop # nested value comment" in rewritten


def test_yaml_selector_rewrite_preserves_preamble_before_explicit_start_marker(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        """# preamble
--- # opening marker
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: htop
spec:
  harness: shell
... # closing marker
# trailing comment
"""
    )
    config, plan = _plan(cfg, ["session-template/htop"])
    execute_plan(plan, config)

    rewritten = manifest.read_text()
    assert rewritten.startswith("# preamble\n--- # opening marker\n")
    assert rewritten.endswith("... # closing marker\n# trailing comment\n")
    assert "harness_integration:" in rewritten


def test_yaml_rewrite_does_not_duplicate_an_implicit_first_document(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        """apiVersion: agentworks/v1
kind: session-template
metadata:
  name: first
spec:
  harness: shell
---
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: second
spec:
  harness: shell
"""
    )
    config, plan = _plan(cfg, [], all_resources=True)
    execute_plan(plan, config)
    documents = _loaded_docs(manifest)
    assert [document["metadata"]["name"] for document in documents] == ["first", "second"]


def test_yaml_rewrite_preserves_heterogeneous_per_document_markers(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        """apiVersion: agentworks/v1
kind: session-template
metadata:
  name: first
spec:
  harness: shell
--- # second start
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: second
spec:
  harness: shell
... # second end
"""
    )
    config, plan = _plan(cfg, [], all_resources=True)
    execute_plan(plan, config)
    rewritten = manifest.read_text()
    assert not rewritten.startswith("---")
    assert "--- # second start" in rewritten
    assert rewritten.endswith("... # second end\n")


def test_interrupt_after_mutation_rolls_back_then_reraises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path)
    original = cfg.read_text()
    config, plan = _plan(cfg, ["secret"])

    import agentworks.migrate.execute as execute_mod

    monkeypatch.setattr(execute_mod, "_verify", lambda plan: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        execute_plan(plan, config)
    assert cfg.read_text() == original
    assert not (tmp_path / "resources" / "secrets.yaml").exists()


def test_yaml_rewrite_preserves_existing_file_mode(tmp_path: Path) -> None:
    import stat

    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: htop\nspec:\n  harness: shell\n"
    )
    manifest.chmod(0o640)
    config, plan = _plan(cfg, ["session-template/htop"])
    execute_plan(plan, config)
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o640


def test_existing_manifest_metadata_failure_happens_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    original = "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: htop\nspec:\n  harness: shell\n"
    manifest.write_text(original)
    config, plan = _plan(cfg, ["session-template/htop"])

    real_chmod = os.chmod

    def fail_temp_chmod(path: object, mode: object, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".sessions.yaml."):
            raise OSError("chmod failed")
        real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", fail_temp_chmod)
    with pytest.raises(OSError, match="chmod failed"):
        execute_plan(plan, config)
    assert manifest.read_text() == original


@pytest.mark.parametrize("mutation", ["created", "appended", "yaml", "config"])
def test_post_replace_interrupt_rolls_back_pre_registered_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    if mutation == "yaml":
        cfg = _write_config(tmp_path, resources="")
        target = tmp_path / "resources" / "sessions.yaml"
        target.parent.mkdir()
        original = (
            "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: htop\nspec:\n  harness: shell\n"
        )
        target.write_text(original)
        config, plan = _plan(cfg, ["session-template/htop"])
    else:
        cfg = _write_config(tmp_path)
        target = tmp_path / "resources" / "secrets.yaml"
        original = "# existing\n"
        if mutation == "appended":
            target.parent.mkdir()
            target.write_text(original)
        config, plan = _plan(cfg, ["secret"])

    real_replace = os.replace

    def interrupt_after_target_replace(source: object, destination: object) -> None:
        real_replace(source, destination)
        if (mutation != "config" or Path(destination) == cfg) and (mutation == "config" or Path(destination) == target):
            raise KeyboardInterrupt()

    monkeypatch.setattr(os, "replace", interrupt_after_target_replace)
    with pytest.raises(KeyboardInterrupt):
        execute_plan(plan, config)
    if mutation == "created":
        assert not target.exists()
    elif mutation == "config":
        assert "[secrets.npm-token]" in cfg.read_text()
    else:
        assert target.read_text() == original


def test_toml_append_and_yaml_selector_rewrite_same_target_are_coalesced(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        resources="""\
[session_templates.toml-template]
command = "toml-command"
""",
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "session-templates.yaml"
    manifest.write_text(
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: yaml-template\nspec:\n  harness: shell\n"
    )
    config, plan = _plan(cfg, [], all_resources=True)
    assert not plan.writes
    assert [rewrite.path for rewrite in plan.yaml_rewrites] == [manifest]
    execute_plan(plan, config)
    documents = _loaded_docs(manifest)
    assert [document["metadata"]["name"] for document in documents] == ["yaml-template", "toml-template"]
    assert documents[0]["spec"]["harness_integration"]["name"] == "shell"
    assert documents[1]["spec"]["harness_integration"]["name"] == "shell"


def test_yaml_migration_order_matches_manifest_loader_files_then_directories(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    nested = resources / "a"
    nested.mkdir(parents=True)
    (resources / "z.yaml").write_text(
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: root\nspec:\n  harness: shell\n"
    )
    (nested / "first.yaml").write_text(
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: nested\nspec:\n  harness: shell\n"
    )
    _config, plan = _plan(cfg, [], all_resources=True)
    assert [(unit.name, unit.section) for unit in plan.units] == [
        ("root", str(resources / "z.yaml")),
        ("nested", str(nested / "first.yaml")),
    ]


def test_yaml_only_preview_lists_real_replacement_paths_not_config_targets(tmp_path: Path) -> None:
    from agentworks.migrate.render import render_preview

    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: htop\nspec:\n  harness: shell\n"
    )
    _config, plan = _plan(cfg, ["session-template/htop"])
    preview = "\n".join(render_preview(plan))
    assert f"rewrite {manifest}: session-template/htop" in preview
    assert "from config.toml" not in preview
    assert " -> ?" not in preview


def test_preview_rewrite_lines_always_have_a_canonicalizing_header(tmp_path: Path) -> None:
    """Rendering groups from the rewrite plan itself, even if a caller builds a
    plan whose informational unit list omits the corresponding YAML units."""
    from agentworks.migrate.render import render_preview

    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: htop\nspec:\n  harness: shell\n"
    )
    _config, plan = _plan(cfg, ["session-template/htop"])
    plan.units = []

    preview = render_preview(plan)

    assert preview[0] == "Canonicalizing 1 YAML session-template selector(s):"
    assert preview[1] == f"  rewrite {manifest}: session-template/htop"


def test_yaml_only_migration_does_not_replace_config_toml(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "sessions.yaml").write_text(
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: htop\nspec:\n  harness: shell\n"
    )
    before = cfg.stat()
    config, plan = _plan(cfg, ["session-template/htop"])
    result = execute_plan(plan, config)
    after = cfg.stat()
    assert not result.config_rewritten
    assert before.st_ino == after.st_ino
    assert before.st_mtime_ns == after.st_mtime_ns


@pytest.mark.parametrize("existing", [False, True])
def test_atomic_manifest_writes_leave_no_partial_artifact_when_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    cfg = _write_config(tmp_path)
    target = tmp_path / "resources" / "secrets.yaml"
    original = "# pre-existing\n"
    if existing:
        target.parent.mkdir()
        target.write_text(original)
    config, plan = _plan(cfg, ["secret"])

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("injected replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replacement failure"):
        execute_plan(plan, config)
    if existing:
        assert target.read_text() == original
    else:
        assert not target.exists()


def test_failed_later_yaml_replacement_rolls_back_only_completed_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    first = resources / "a.yaml"
    second = resources / "b.yaml"
    original = "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: {name}\nspec:\n  harness: shell\n"
    first.write_text(original.format(name="first"))
    second.write_text(original.format(name="second"))
    config, plan = _plan(cfg, [], all_resources=True)

    real_replace = os.replace
    calls = 0

    def fail_second_replace(source: object, destination: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second YAML replacement failed")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="second YAML replacement failed"):
        execute_plan(plan, config)
    assert first.read_text() == original.format(name="first")
    assert second.read_text() == original.format(name="second")


def test_config_digest_mismatch_refuses_before_writing(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["secret"])
    cfg.write_text(cfg.read_text() + "# concurrent edit\n")

    with pytest.raises(StateError, match="cannot rewrite config.toml: it changed after migration planning"):
        execute_plan(plan, config)
    assert "# concurrent edit" in cfg.read_text()
    assert not (tmp_path / "resources").exists()


def test_yaml_recovery_copy_restores_only_the_run_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """YAML rewrites snapshot the original on disk, and rollback uses that
    copy only while the target still has this run's planned digest."""
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    original = (
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: htop\nspec:\n"
        "  harness: shell\n  harness_config:\n    command: htop\n"
    )
    manifest.write_text(original)
    config, plan = _plan(cfg, ["session-template/htop"])

    import agentworks.migrate.execute as execute_mod

    monkeypatch.setattr(execute_mod, "first_difference", lambda pre, post: "forced difference")
    with pytest.raises(StateError, match="migration verification failed"):
        execute_plan(plan, config)

    snapshots = list((tmp_path / "backups").glob("config-*.toml.resources/resources/sessions.yaml"))
    assert len(snapshots) == 1
    assert snapshots[0].read_text() == original
    assert manifest.read_text() == original


def test_yaml_rollback_preserves_a_post_write_concurrent_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path, resources="")
    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = resources / "sessions.yaml"
    manifest.write_text(
        "apiVersion: agentworks/v1\nkind: session-template\nmetadata:\n  name: htop\nspec:\n  harness: shell\n"
    )
    config, plan = _plan(cfg, ["session-template/htop"])

    import agentworks.migrate.execute as execute_mod

    def concurrent_edit(pre: object, post: object) -> str:
        manifest.write_text("# operator edit after migration write\n")
        return "forced difference"

    monkeypatch.setattr(execute_mod, "first_difference", concurrent_edit)
    with pytest.raises(StateError, match="rollback is incomplete") as exc:
        execute_plan(plan, config)
    assert manifest.read_text() == "# operator edit after migration write\n"
    assert str(manifest) in (exc.value.hint or "")
    assert "expected digest" in (exc.value.hint or "")
    assert "observed" in (exc.value.hint or "")
    assert "recover manually" in (exc.value.hint or "")


@pytest.mark.parametrize("existing", [False, True])
def test_rollback_does_not_delete_or_truncate_concurrently_edited_toml_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    if existing:
        resources.mkdir()
        target = resources / "secrets.yaml"
        target.write_text("# existing manifest\n")
    config, plan = _plan(cfg, ["secret"])
    target = plan.writes[0].path

    import agentworks.migrate.execute as execute_mod

    def concurrent_edit(pre: object, post: object) -> str:
        target.write_text("# concurrent operator edit\n")
        return "forced difference"

    monkeypatch.setattr(execute_mod, "first_difference", concurrent_edit)
    with pytest.raises(StateError, match="rollback is incomplete") as exc:
        execute_plan(plan, config)
    assert target.read_text() == "# concurrent operator edit\n"
    assert str(target) in (exc.value.hint or "")


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]):
    from typer.testing import CliRunner

    from agentworks.cli import app

    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "config.toml")
    return CliRunner().invoke(app, args)


def test_cli_migrate_bare_invocation_errors_with_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--yes"])
    assert result.exit_code != 0
    # The error surfaces through the CLI entry's renderer; under
    # CliRunner it is the raw exception.
    assert "indicate resources to migrate" in str(result.exception)


def test_cli_migrate_all_nothing_to_do_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, resources="")
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--all", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert (
        "Nothing to migrate: no migratable TOML-declared resources or legacy YAML session-template selectors remain."
        in result.stdout
    )


def test_cli_migrate_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path)
    original = cfg.read_text()
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--all", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "Dry run: nothing was written." in result.stdout
    assert "Pass --full" in result.stdout
    assert "apiVersion" not in result.stdout  # summary by default
    assert cfg.read_text() == original
    assert not (tmp_path / "resources").exists()


def test_cli_migrate_dry_run_full_includes_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    result = _cli(
        tmp_path,
        monkeypatch,
        ["resource", "migrate", "--all", "--dry-run", "--full"],
    )
    assert result.exit_code == 0, result.stdout
    assert "apiVersion: agentworks/v1" in result.stdout
    assert "Config.toml changes" in result.stdout


def test_cli_migrate_full_requires_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--all", "--full", "--yes"])
    assert result.exit_code != 0


def test_cli_migrate_yes_executes_and_verifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--all", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "verified: registry unchanged" in result.stdout
    assert (tmp_path / "resources" / "secrets.yaml").exists()


def test_cli_migrate_explicit_selector_miss_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, resources="")
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "secret", "--yes"])
    assert result.exit_code != 0


def test_cli_sample_stdout_and_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, resources="")
    result = _cli(tmp_path, monkeypatch, ["resource", "sample", "secret"])
    assert result.exit_code == 0, result.stdout
    assert "kind: secret" in result.stdout

    result2 = _cli(
        tmp_path,
        monkeypatch,
        ["resource", "sample", "secret", "--write", "secrets.yaml"],
    )
    assert result2.exit_code == 0, result2.stdout
    assert (tmp_path / "resources" / "secrets.yaml").exists()


def test_verification_mismatch_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path)
    original = cfg.read_text()
    resources = tmp_path / "resources"
    resources.mkdir()
    existing = resources / "secrets.yaml"
    hand_written = (
        "apiVersion: agentworks/v1\n"
        "kind: secret\n"
        "metadata:\n"
        "  name: hand-written\n"
        "  description: already here\n"
        "spec: {}\n"
    )
    existing.write_text(hand_written)

    import agentworks.migrate.execute as execute_mod

    monkeypatch.setattr(execute_mod, "first_difference", lambda pre, post: "forced difference")

    config, plan = _plan(cfg, [], all_resources=True, layout="per-resource")
    # Also append into the existing per-kind file to exercise truncation:
    # switch one write target to the existing file by planning a second
    # per-kind run for the secret.
    config_b, plan_b = _plan(cfg, ["secret/npm-token"])  # per-kind -> appends

    with pytest.raises(StateError, match="migration verification failed"):
        execute_plan(plan_b, config_b)
    assert cfg.read_text() == original  # TOML restored
    assert existing.read_text() == hand_written  # append truncated

    with pytest.raises(StateError, match="migration verification failed"):
        execute_plan(plan, config)
    assert cfg.read_text() == original
    assert not (resources / "vm-template").exists()  # created dirs removed
    assert existing.read_text() == hand_written  # untouched by rollback


def test_git_credential_stray_key_fails_at_plan_time(tmp_path: Path) -> None:
    """The emission sweep nests every non-kind-owned flat key into
    provider_config, including stray keys the TOML loader silently
    ignores. The manifest loader validates blobs strictly, so planning
    validates the emitted blob up front: the run fails BEFORE anything
    is written, in TOML vocabulary, instead of failing verification
    after the write and citing a rolled-back file."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[git_credentials.ado]
type = "azdo"
org = "my-org"
bogus = "stray"
""",
    )
    with pytest.raises(ConfigError, match="cannot migrate git-credential/ado") as exc:
        _plan(cfg, ["git-credential/ado"])
    assert "unknown azdo provider field" in str(exc.value)
    assert "Remove them from config.toml" in (exc.value.hint or "")
    assert not (tmp_path / "resources").exists()  # nothing written
