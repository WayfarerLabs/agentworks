"""The workspace-template spec model, and with it the shared env table.

This is the smallest env-bearing kind, so the env work rides it: the two
spellings an operator writes per key, the key's own naming rule, and the
two load-time hygiene warnings that no model validator could carry
(a validator has no channel but an exception, and neither warning is
fatal).
"""

from __future__ import annotations

import pytest

from agentworks.env import EnvEntry
from agentworks.workspaces.template import WorkspaceTemplate

from ._specs import WHERE, decode, decode_issues, rejection

_FULL = {
    "inherits": ["base"],
    "repo": "git@example.test:me/thing.git",
    "tmuxinator": False,
    "git_user_name": "Bot",
    "git_user_email": "bot@example.test",
    "env": {"EDITOR": "nvim", "TOKEN": {"secret": "my-token"}},
}


def test_every_field_round_trips() -> None:
    row = decode("workspace-template", "web", dict(_FULL), description="the web workspace")

    assert row == WorkspaceTemplate(
        name="web",
        description="the web workspace",
        declared_at=WHERE,
        inherits=["base"],
        repo="git@example.test:me/thing.git",
        tmuxinator=False,
        git_user_name="Bot",
        git_user_email="bot@example.test",
        env={"EDITOR": EnvEntry({"value": "nvim"}), "TOKEN": EnvEntry({"secret": "my-token"})},
    )


def test_an_unset_field_stays_none_so_it_can_inherit() -> None:
    """``None`` means "not set here" and is what the merge reads; a
    default applied at this layer would make every child override its
    parent."""
    row = decode("workspace-template", "web", {})

    assert (row.repo, row.tmuxinator, row.git_user_name, row.git_user_email) == (None, None, None, None)
    assert (row.inherits, row.env) == ([], {})


# -- What an operator reads when it is wrong ----------------------------------


def test_an_unknown_key_names_the_fields_that_are_valid() -> None:
    assert rejection("workspace-template", "web", {"git_user_emial": "bot@example.test"}) == (
        "res.yaml:7: workspace-template/web.git_user_emial: unknown field; expected one of: "
        "env, git_user_email, git_user_name, inherits, repo, tmuxinator"
    )


def test_an_env_key_that_is_not_a_variable_name_is_refused() -> None:
    """The message names the offending KEY rather than making an operator
    match a regex against their own table, and it carries no ``[key]``
    marker: pydantic's way of saying "the failure is in the key" is not
    something the operator wrote."""
    assert rejection("workspace-template", "web", {"env": {"1BAD": "x"}}) == (
        "res.yaml:7: workspace-template/web.env.1BAD: invalid env var name '1BAD' "
        "(must match /^[A-Za-z_][A-Za-z0-9_]*$/)"
    )


def test_an_env_entry_that_is_neither_shape_is_refused() -> None:
    assert rejection("workspace-template", "web", {"env": {"PORT": 8080}}) == (
        "res.yaml:7: workspace-template/web.env.PORT: must be a table"
    )


def test_an_env_entry_with_neither_source_field_is_refused_as_one_problem() -> None:
    assert rejection("workspace-template", "web", {"env": {"TOKEN": {}}}) == (
        "res.yaml:7: workspace-template/web.env.TOKEN: must match exactly one table shape; "
        "required fields by alternative: value or secret"
    )


def test_an_env_entry_with_both_source_fields_is_refused_as_one_problem() -> None:
    assert rejection(
        "workspace-template",
        "web",
        {"env": {"TOKEN": {"value": "plain", "secret": "token"}}},
    ) == (
        "res.yaml:7: workspace-template/web.env.TOKEN: must match exactly one table shape; "
        "required fields by alternative: value or secret"
    )


def test_an_env_entry_with_an_unknown_inner_key_is_refused() -> None:
    assert rejection("workspace-template", "web", {"env": {"TOKEN": {"secrit": "x"}}}) == (
        "res.yaml:7: workspace-template/web.env.TOKEN.secrit: unknown field; expected one of: secret, value"
    )


@pytest.mark.parametrize("field", ["value", "secret"])
def test_a_malformed_selected_env_arm_keeps_only_its_real_error(field: str) -> None:
    assert rejection("workspace-template", "web", {"env": {"TOKEN": {field: 8}}}) == (
        f"res.yaml:7: workspace-template/web.env.TOKEN.{field}: must be a string"
    )


def test_a_bare_string_where_the_inherits_list_belongs_is_refused() -> None:
    assert rejection("workspace-template", "web", {"inherits": "base"}) == (
        "res.yaml:7: workspace-template/web.inherits: must be a list"
    )


# -- The advisories, derived rather than enumerated ---------------------------


def test_an_agentworks_prefixed_key_is_warned_about() -> None:
    issues = decode_issues("workspace-template", "web", {"env": {"AGENTWORKS_WORKSPACE": "x"}})

    assert issues == [
        "res.yaml:7: workspace-template/web.env sets agentworks-managed identity variable "
        "'AGENTWORKS_WORKSPACE'; identity values win at the runtime prelude, so your value "
        "will be ignored at command time. Remove the entry."
    ]


def test_a_newline_in_a_plaintext_value_is_warned_about() -> None:
    """ADR 0014: it would corrupt the SSH ``-o SetEnv=KEY=VALUE`` argument
    shape."""
    issues = decode_issues("workspace-template", "web", {"env": {"NOTE": "one\ntwo"}})

    assert issues == [
        "res.yaml:7: workspace-template/web.env.NOTE: value contains a newline; "
        "SSH SetEnv cannot transport it cleanly. Strip the newline at the source."
    ]


def test_a_non_conforming_secret_name_is_warned_about() -> None:
    """Found through the ``SecretRef`` marker on ``EnvEntry.secret``, not
    by a call at each site that happens to name a secret."""
    (issue,) = decode_issues("workspace-template", "web", {"env": {"TOKEN": {"secret": "Bad_Name!"}}})

    assert issue.startswith("res.yaml:7: workspace-template/web: secret name 'Bad_Name!' for an env var's value")


def test_a_clean_document_earns_no_advisories() -> None:
    """Non-vacuity for the three above: an advisory pass that returned
    nothing would satisfy none of them, but one that returned everything
    would satisfy all three."""
    assert decode_issues("workspace-template", "web", dict(_FULL)) == []
