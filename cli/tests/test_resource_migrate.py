"""Tests for ``agw resource migrate`` (the ``agentworks.migrate`` package)."""

from __future__ import annotations

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


def _write_config(
    tmp_path: Path, resources: str = MAXIMAL_RESOURCES, *, prefix: str = ""
) -> Path:
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
    """The maximal config migrates wholesale: every kind lands in YAML,
    the TOML keeps only config sections (comments preserved), the
    secret_backends residue is dropped, and verification passes."""
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, [])

    kinds = {(u.kind, u.name) for u in plan.units}
    assert kinds == {
        ("secret", "npm-token"),
        ("vm-template", "default"),
        ("vm-template", "dev"),
        ("workspace-template", "proj"),
        ("agent-template", "default"),
        ("session-template", "claude"),
        ("git-credential", "github"),
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

    # Per-kind layout: one file per kind with the plural-s convention.
    resources = tmp_path / "resources"
    assert (resources / "secrets.yaml").exists()
    assert (resources / "vm-templates.yaml").exists()
    docs = _loaded_docs(resources / "vm-templates.yaml")
    assert [d["metadata"]["name"] for d in docs] == ["default", "dev"]
    # Non-contiguous env section folded into the one document.
    assert docs[1]["spec"]["env"] == {"HTTP_PROXY": "http://proxy:3128"}


def test_git_credential_type_becomes_provider(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["git-credential/github"])
    execute_plan(plan, config)
    (doc,) = _loaded_docs(tmp_path / "resources" / "git-credentials.yaml")
    assert doc["spec"] == {"provider": "github"}
    assert doc["metadata"]["description"] == "gh access"


def test_singletons_emit_default_documents(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["admin-template", "named-console-template"])
    execute_plan(plan, config)
    (admin,) = _loaded_docs(tmp_path / "resources" / "admin-templates.yaml")
    assert admin["metadata"]["name"] == "default"
    assert admin["spec"]["shell"] == "zsh"
    assert admin["spec"]["env"] == {"EDITOR": "nvim"}
    (console,) = _loaded_docs(
        tmp_path / "resources" / "named-console-templates.yaml"
    )
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
    with pytest.raises(ValidationError, match="no TOML-declared secret"):
        _plan(cfg, ["secret/nope"])


def test_kind_selector_with_no_toml_rows_errors(tmp_path: Path) -> None:
    """Explicit kind selector after that kind is fully migrated: error,
    not silence -- the operator named something specific."""
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, ["secret"])
    execute_plan(plan, config)
    with pytest.raises(ValidationError, match="no TOML-declared resources"):
        _plan(cfg, ["secret"])


def test_secret_backend_selector_gets_tailored_error(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    with pytest.raises(ValidationError, match="no manifest successor"):
        _plan(cfg, ["secret-backend"])


def test_bare_run_with_nothing_left_is_nothing_to_do(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config, plan = _plan(cfg, [])
    execute_plan(plan, config)
    _config2, plan2 = _plan(cfg, [])
    assert plan2.nothing_to_do


def test_bare_run_with_only_secret_backends_offers_drop(tmp_path: Path) -> None:
    """The [secret_backends.*] residue is droppable even when there are
    no resources left to migrate."""
    cfg = _write_config(tmp_path, resources="[secret_backends.env-var]\n")
    config, plan = _plan(cfg, [])
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
    config, plan = _plan(cfg, [], layout="single")
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
    cfg = _write_config(
        tmp_path,
        resources="""\
[vm_templates."weird/name"]
cpus = 2
""",
    )
    with pytest.raises(ConfigError, match="not filename-safe"):
        _plan(cfg, [], layout="per-resource")


def test_unknown_layout_and_toml_mode_error(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    with pytest.raises(ValidationError, match="unknown layout"):
        _plan(cfg, [], layout="flat")
    with pytest.raises(ValidationError, match="unknown --toml mode"):
        _plan(cfg, [], toml_mode="erase")


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
        _plan(cfg, [])
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


def test_slash_names_are_individually_addressable(tmp_path: Path) -> None:
    """First-slash split leaves the full name as the remainder, so a
    name containing `/` selects fine (per-kind layout carries it)."""
    cfg = _write_config(
        tmp_path,
        resources="""\
[vm_templates."we/ird"]
cpus = 2
""",
    )
    config, plan = _plan(cfg, ["vm-template/we/ird"])
    assert [(u.kind, u.name) for u in plan.units] == [("vm-template", "we/ird")]
    execute_plan(plan, config)
    (doc,) = _loaded_docs(tmp_path / "resources" / "vm-templates.yaml")
    assert doc["metadata"]["name"] == "we/ird"


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
    config, plan = _plan(cfg, [])
    result = execute_plan(plan, config)
    assert result.backup_path.parent == tmp_path / "backups"
    assert result.backup_path.read_text() == original


def test_backup_taken_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup must exist before the first manifest byte is written:
    force the very first write step to fail and assert the backup is
    already on disk with the original content."""
    import agentworks.migrate.execute as execute_mod

    cfg = _write_config(tmp_path)
    original = cfg.read_text()

    def boom(*args: object, **kwargs: object) -> list:  # type: ignore[type-arg]
        raise OSError("simulated write failure")

    monkeypatch.setattr(execute_mod, "_ensure_parents", boom)
    config, plan = _plan(cfg, [])
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
    _config, plan = _plan(cfg, [])
    text = "\n".join(render_preview(plan))
    for unit in plan.units:
        assert f"{unit.kind}/{unit.name} -> " in text
    assert "[secret_backends.*] sections will be dropped" in text


def test_dry_run_is_plan_only(tmp_path: Path) -> None:
    """Planning writes nothing; the dry-run path is plan + print."""
    from agentworks.migrate.render import render_dry_run

    cfg = _write_config(tmp_path)
    original = cfg.read_text()
    _config, plan = _plan(cfg, [])
    lines = render_dry_run(plan)
    assert any("config.toml changes" in line for line in lines)
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


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]):
    from typer.testing import CliRunner

    from agentworks.cli import app

    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "config.toml")
    return CliRunner().invoke(app, args)


def test_cli_migrate_bare_nothing_to_do_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, resources="")
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "Nothing to migrate" in result.stdout


def test_cli_migrate_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path)
    original = cfg.read_text()
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "Dry run: nothing was written." in result.stdout
    assert cfg.read_text() == original
    assert not (tmp_path / "resources").exists()


def test_cli_migrate_yes_executes_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "verified: registry unchanged" in result.stdout
    assert (tmp_path / "resources" / "secrets.yaml").exists()


def test_cli_migrate_explicit_selector_miss_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, resources="")
    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "secret", "--yes"])
    assert result.exit_code != 0


def test_cli_sample_stdout_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_verification_mismatch_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    monkeypatch.setattr(
        execute_mod, "first_difference", lambda pre, post: "forced difference"
    )

    config, plan = _plan(cfg, [], layout="per-resource")
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
