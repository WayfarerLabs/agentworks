"""Tests for ``declared_at: SourceLocation`` attachment in ``load_config``.

Every operator-declared Resource carries a ``declared_at`` pointing at the
opening line of its TOML section header. For Resources composed from multiple
sub-sections (e.g., ``[vm_templates.x]`` plus ``[vm_templates.x.env]``),
``declared_at`` points at the earliest contributing header. Singletons that
the operator omits entirely (no ``[admin.*]``, no ``[named_console]``) still
appear in ``Config`` with a sentinel ``SourceLocation(file=<config_path>,
line=0)`` so downstream framework code never has to face a missing field.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import load_config


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id_ed25519.pub"
    priv = tmp_path / "id_ed25519"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    return pub, priv


def _write_config(tmp_path: Path, body: str, ssh_keys: tuple[Path, Path]) -> Path:
    pub, priv = ssh_keys
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub.as_posix()}"
            ssh_private_key = "{priv.as_posix()}"

            """
        )
        + dedent(body)
    )
    return config_file


def test_vm_template_declared_at_points_at_root_header(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [vm_templates.azure-prod]
        cpus = 4

        [vm_templates.azure-prod.env]
        FOO = "bar"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    tmpl = cfg.vm_templates["azure-prod"]
    assert tmpl.declared_at.file == config_file
    assert tmpl.declared_at.line == 5  # [vm_templates.azure-prod]


def test_vm_template_declared_at_uses_subsection_when_only_env_present(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """The implicit-parent case per the revised FRD R2: writing only
    ``[vm_templates.x.env]`` produces a valid ``vm_templates.x`` Resource
    whose ``declared_at`` points at the env header line.
    """
    config_file = _write_config(
        tmp_path,
        """\
        [vm_templates.only-env.env]
        FOO = "bar"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    tmpl = cfg.vm_templates["only-env"]
    assert tmpl.declared_at.file == config_file
    assert tmpl.declared_at.line == 5


def test_admin_config_declared_at_points_at_admin_subtree(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [admin.config]
        shell = "zsh"

        [admin.env]
        FOO = "bar"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    assert cfg.admin.declared_at.file == config_file
    assert cfg.admin.declared_at.line == 5  # earliest under [admin.*]


def test_admin_config_synthesized_when_section_omitted(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """A config with no ``[admin.*]`` sections still yields a valid
    ``AdminConfig`` with sentinel ``declared_at = SourceLocation(file, 0)``.
    """
    config_file = _write_config(tmp_path, "", ssh_keys)

    cfg = load_config(config_file, warn_issues=False)
    assert cfg.admin.declared_at.file == config_file
    assert cfg.admin.declared_at.line == 0


def test_named_console_declared_at(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [named_console]
        tmux_layout = "tiled"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    assert cfg.named_console.declared_at.file == config_file
    assert cfg.named_console.declared_at.line == 5


def test_named_console_synthesized_when_omitted(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(tmp_path, "", ssh_keys)

    cfg = load_config(config_file, warn_issues=False)
    assert cfg.named_console.declared_at.file == config_file
    assert cfg.named_console.declared_at.line == 0


def test_git_credential_declared_at(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [git_credentials.github-prod]
        type = "github"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    cred = cfg.git_credentials["github-prod"]
    assert cred.declared_at.file == config_file
    assert cred.declared_at.line == 5


def test_secret_decl_declared_at(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [secrets.anthropic-api-key]
        description = "Anthropic API key"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    decl = cfg.secrets["anthropic-api-key"]
    assert decl.declared_at.file == config_file
    assert decl.declared_at.line == 5


def test_secret_backend_config_declared_at(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [secret_backends.env-var]
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    backend = cfg.secret_backends["env-var"]
    assert backend.declared_at.file == config_file
    assert backend.declared_at.line == 5


def test_secret_config_declared_at(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    assert cfg.secret_config_data.declared_at.file == config_file
    assert cfg.secret_config_data.declared_at.line == 5


def test_secret_config_synthesized_when_omitted(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(tmp_path, "", ssh_keys)

    cfg = load_config(config_file, warn_issues=False)
    assert cfg.secret_config_data.declared_at.file == config_file
    assert cfg.secret_config_data.declared_at.line == 0


def test_session_template_declared_at(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [session_templates.dev]
        command = "claude"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    tmpl = cfg.session_templates["dev"]
    assert tmpl.declared_at.file == config_file
    assert tmpl.declared_at.line == 5


def test_workspace_template_declared_at(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [workspace_templates.gruntweave]
        repo = "https://example.com/org/repo.git"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    tmpl = cfg.workspace_templates["gruntweave"]
    assert tmpl.declared_at.file == config_file
    assert tmpl.declared_at.line == 5


def test_agent_template_declared_at(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [agent_templates.claude]
        shell = "zsh"
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    tmpl = cfg.agent_templates["claude"]
    assert tmpl.declared_at.file == config_file
    assert tmpl.declared_at.line == 5
