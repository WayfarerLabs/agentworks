"""The vm-template and agent-template spec models.

The two inheriting kinds that carry non-capability reference edges, so
this file also pins what FR17 depends on and cannot regress: `None` keeps
meaning "inherit" field for field, and no marker on either row fills an
absent value from a template.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.env import EnvEntry
from agentworks.errors import ConfigError, StateError
from agentworks.schema import SecretRef, marker_of
from agentworks.vms.template import VMTemplate

from ._specs import WHERE, decode, decode_issues, rejection

# -- vm-template --------------------------------------------------------------

_VM_FULL = {
    "inherits": ["base"],
    "cpus": 4,
    "memory": 8,
    "disk": 50,
    "swap": 0,
    "apt": ["jq"],
    "apt_packages": ["tools"],
    "snap": ["helm"],
    "system_install_commands": ["docker"],
    "env": {"EDITOR": "nvim"},
    "tailscale_auth_key": "ts-key",
}


def test_every_vm_template_field_round_trips() -> None:
    row = decode("vm-template", "big", dict(_VM_FULL))

    assert row == VMTemplate(
        name="big",
        declared_at=WHERE,
        **{**_VM_FULL, "env": {"EDITOR": EnvEntry({"value": "nvim"})}},  # type: ignore[arg-type]
    )


def test_an_unset_vm_template_field_stays_none_so_it_can_inherit() -> None:
    row = decode("vm-template", "big", {})

    assert (row.cpus, row.memory, row.disk, row.swap) == (None, None, None, None)
    assert (row.apt, row.apt_packages, row.snap, row.system_install_commands) == (None, None, None, None)
    assert row.tailscale_auth_key is None


def test_a_quoted_integer_is_no_longer_coerced() -> None:
    """The decoder spelled ``int(spec["cpus"])``, so ``cpus: "4"`` loaded
    and ``cpus: true`` loaded as 1. Strict mode says what they are."""
    assert rejection("vm-template", "big", {"cpus": "4"}) == "res.yaml:7: vm-template/big.cpus: must be an integer"
    assert rejection("vm-template", "big", {"cpus": True}) == "res.yaml:7: vm-template/big.cpus: must be an integer"


def test_a_non_string_auth_key_is_refused() -> None:
    assert rejection("vm-template", "big", {"tailscale_auth_key": 7}) == (
        "res.yaml:7: vm-template/big.tailscale_auth_key: must be a string"
    )


def test_a_non_conforming_auth_key_is_warned_about_and_kept() -> None:
    (issue,) = decode_issues("vm-template", "big", {"tailscale_auth_key": "GITHUB_TOKEN"})

    assert issue.startswith("res.yaml:7: vm-template/big: secret name 'GITHUB_TOKEN' for the Tailscale auth key")
    assert decode("vm-template", "big", {"tailscale_auth_key": "GITHUB_TOKEN"}).tailscale_auth_key == "GITHUB_TOKEN"


# -- agent-template -----------------------------------------------------------


def test_every_agent_template_field_round_trips() -> None:
    spec = {
        "inherits": ["base"],
        "shell": "zsh",
        "git_credentials": ["gh"],
        "user_install_commands": ["ripgrep"],
        "dotfiles_source": "github:me/dotfiles",
        "dotfiles_destination": "~/dots",
        "dotfiles_install_cmd": "./setup.sh",
        "mise_activate": True,
        "mise_packages": ["node@22"],
        "mise_lockfile": "github:me/dotfiles",
        "mise_allow_unlocked": True,
        "mise_install_before": "30d",
        "mise_prune_on_reinit": False,
        "claude_marketplaces": ["me/market"],
        "claude_plugins": ["thing@me/market"],
    }

    row = decode("agent-template", "claude", dict(spec))

    assert row == AgentTemplate(name="claude", declared_at=WHERE, **spec)  # type: ignore[arg-type]


def test_an_unset_agent_template_field_stays_none_so_it_can_inherit() -> None:
    row = decode("agent-template", "claude", {})

    assert (row.shell, row.git_credentials, row.mise_activate, row.mise_packages) == (None, None, None, None)


def test_the_mise_check_runs_against_the_resolved_default_but_stores_none() -> None:
    """The decoder validated with ``"7d"`` and STORED ``None``, and the
    asymmetry is load-bearing: the stored ``None`` is what lets a child
    inherit its parent's value."""
    assert decode("agent-template", "claude", {}).mise_install_before is None


def test_a_bad_mise_package_says_what_the_syntax_is() -> None:
    assert rejection("agent-template", "claude", {"mise_packages": ["node"]}) == (
        "res.yaml:7: agent-template/claude: mise_packages entries must use non-empty name@version syntax"
    )


def test_a_key_that_never_did_anything_is_now_refused() -> None:
    """The clearest single argument for FR12's flip: both keys were in
    this kind's accepted key set and are NOT fields of the row, so an
    operator who wrote either got no warning and no effect.

    Both in one loop, and "both" is the argument: they land on the one
    ``extra="forbid"`` refusal, so re-admitting either is what a failure
    naming the key would be reporting.
    """
    accepted = [
        (field, got)
        for field, value in (("username", "ops"), ("git_force_safe_directory", True))
        if not (got := rejection("agent-template", "claude", {field: value})).startswith(
            f"res.yaml:7: agent-template/claude.{field}: unknown field; expected one of: "
        )
    ]
    assert not accepted


# -- FR17: markers on an inheriting row do not fill anything ------------------


def test_the_auth_key_marker_declares_no_default_template() -> None:
    """The trap this step most needs to hold: ``AgwModel`` fills any field
    whose marker declares a ``default_template``, so one here would give
    every template the literal default and a child that overrides its
    parent would silently stop doing so."""
    marker = marker_of(VMTemplate.model_fields["tailscale_auth_key"])

    assert isinstance(marker, SecretRef)
    assert marker.default_template is None


def test_an_inheriting_row_refuses_a_default_template_at_import() -> None:
    """Proven non-vacuous by declaring one: the check fires when the class
    is created, so the author who adds it reads the failure rather than
    this test."""
    from agentworks.declared_resource import DeclaredResource

    with pytest.raises(StateError, match="composes along an `inherits` chain"):

        class _Bad(DeclaredResource):
            """A row that inherits and templates a default."""

            inherits: list[str] = []
            token: Annotated[str, SecretRef(usage="a token", default_template="token-{owner_name}")] | None = None


def test_a_non_inheriting_row_is_left_alone() -> None:
    """Non-vacuity for the check above: it is keyed on the ``inherits``
    field, so a row without one is not touched."""
    from agentworks.declared_resource import DeclaredResource

    class _Fine(DeclaredResource):
        """A row that does not inherit."""

        token: Annotated[str, SecretRef(usage="a token", default_template="token-{owner_name}")] | None = None

    assert "inherits" not in _Fine.model_fields


def test_an_empty_auth_key_is_refused_rather_than_overriding_the_default() -> None:
    """``None`` means inherit and the merge overrides on ``is not None``,
    so an empty string is a VALUE: it would replace the resolved default
    with the name of no secret at all, auto-declare a secret called ``''``,
    and send ``vm create`` to resolve it instead of the deployment
    default. The advisory does not fire for it either, so nothing else
    would have caught it."""
    assert rejection("vm-template", "big", {"tailscale_auth_key": ""}) == (
        "res.yaml:7: vm-template/big.tailscale_auth_key: must not be empty"
    )


def test_an_empty_auth_key_never_reaches_the_resolved_template(tmp_path: Path) -> None:
    """The end-to-end half, because the field is only half the mechanism:
    what made the empty string dangerous is the merge, three modules
    away."""
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from tests.conftest import ManifestDoc, write_manifests

    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test")
    (tmp_path / "k").write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[operator]\nssh_public_key = "{(tmp_path / "k.pub").as_posix()}"\n'
        f'ssh_private_key = "{(tmp_path / "k").as_posix()}"\n'
    )
    write_manifests(tmp_path, ManifestDoc("vm-template", "big", {"tailscale_auth_key": ""}))

    with pytest.raises(ConfigError, match="tailscale_auth_key: must not be empty"):
        build_registry(load_config(cfg, warn_issues=False))
