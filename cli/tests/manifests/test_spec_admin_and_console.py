"""The admin-template and named-console-template spec models.

admin-template is the kind that proves FR15's shape before step 2.6 gets
there: it does not inherit, so every optional field carries a CONCRETE
default and nothing downstream has to supply a fallback.
named-console-template is the kind that proves the choice-set inversion,
where the ``Literal`` is the authored list and the runtime tuple is
derived from it.
"""

from __future__ import annotations

from agentworks.schema import iter_field_docs
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT, VALID_TMUX_LAYOUTS
from agentworks.sessions.template import NamedConsoleConfig
from agentworks.vms.admin import AdminConfig

from ._specs import WHERE, decode, rejection

# -- named-console-template ---------------------------------------------------


def test_the_console_layout_round_trips() -> None:
    assert decode("named-console-template", "default", {"tmux_layout": "tiled"}) == NamedConsoleConfig(
        name="default", tmux_layout="tiled", declared_at=WHERE
    )


def test_the_console_layout_defaults_to_the_agentworks_one() -> None:
    assert decode("named-console-template", "default", {}).tmux_layout == AW_SESSION_VERTICAL_LAYOUT


def test_an_unknown_layout_lists_the_ones_that_exist() -> None:
    message = rejection("named-console-template", "default", {"tmux_layout": "diagonal"})

    assert message.startswith("res.yaml:7: named-console-template/default.tmux_layout: must be one of: ")
    for layout in VALID_TMUX_LAYOUTS:
        assert layout in message


def test_the_layouts_reach_the_explain_surface_as_choices() -> None:
    """What the inversion buys: a validator would have left ``choices``
    empty, so ``agw resource explain`` could not list the layouts (FR10)
    and the emitted schema could not enumerate them."""
    (field,) = [doc for doc in iter_field_docs(NamedConsoleConfig) if doc.path == ("tmux_layout",)]

    assert field.choices == VALID_TMUX_LAYOUTS


# -- admin-template -----------------------------------------------------------


def test_an_empty_admin_spec_is_fully_defaulted() -> None:
    """FR15 in miniature: an admin-template is not part of a chain, so
    there is nothing for ``None`` to mean and nothing downstream has to
    guess."""
    row = decode("admin-template", "default", {})

    assert row == AdminConfig(name="default", declared_at=WHERE)
    assert (row.username, row.shell, row.dotfiles_destination) == ("agentworks", "bash", "~/.dotfiles")
    assert (row.mise_activate, row.mise_install_before, row.git_force_safe_directory) == (True, "7d", True)


def test_every_admin_field_round_trips() -> None:
    spec = {
        "username": "ops",
        "shell": "zsh",
        "git_credentials": ["gh"],
        "user_install_commands": ["ripgrep"],
        "dotfiles_source": "github:me/dotfiles",
        "dotfiles_destination": "~/dots",
        "dotfiles_install_cmd": "./setup.sh",
        "mise_activate": False,
        "mise_packages": ["node@22"],
        "mise_lockfile": "github:me/dotfiles",
        "mise_allow_unlocked": True,
        "mise_install_before": "2026-01-01",
        "mise_prune_on_reinit": False,
        "git_force_safe_directory": False,
        "claude_marketplaces": ["me/market"],
        "claude_plugins": ["thing@me/market"],
        "env": {"EDITOR": "nvim"},
    }

    row = decode("admin-template", "default", dict(spec))

    assert {key: getattr(row, key) for key in spec if key != "env"} == {
        key: value for key, value in spec.items() if key != "env"
    }
    assert row.env["EDITOR"].value == "nvim"


def test_a_bad_mise_package_says_what_the_syntax_is() -> None:
    assert rejection("admin-template", "default", {"mise_packages": ["node"]}) == (
        "res.yaml:7: admin-template/default: mise_packages entries must use non-empty name@version syntax"
    )


def test_a_bad_mise_install_before_says_what_it_accepts() -> None:
    assert rejection("admin-template", "default", {"mise_install_before": "soon"}) == (
        "res.yaml:7: admin-template/default: mise_install_before must be a positive duration "
        "such as '7d' or an ISO date"
    )


def test_a_bad_mise_lockfile_carries_the_source_refs_own_message() -> None:
    assert "mise_lockfile is invalid: " in rejection("admin-template", "default", {"mise_lockfile": "::nope"})


def test_an_unknown_admin_key_names_the_fields_that_are_valid() -> None:
    message = rejection("admin-template", "default", {"user_name": "ops"})

    assert message.startswith("res.yaml:7: admin-template/default.user_name: unknown field; expected one of: ")
    assert "username" in message
