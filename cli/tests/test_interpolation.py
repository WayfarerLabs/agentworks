"""Behavioral checks for namespaced runtime interpolation and literal data."""

from __future__ import annotations

import pytest

from agentworks.env import ResourceContext
from agentworks.errors import ValidationError
from agentworks.interpolation import context_values, render_template, runtime_values


def test_context_uses_linux_identity_and_optional_resource_facts() -> None:
    ctx = ResourceContext(
        vm_name="box",
        platform="lima",
        site="local",
        user="agt-actual-user",
        agent_name="friendly-name",
        workspace_name="project",
        workspace_dir="/srv/a project's files",
        session_name="work",
        session_kind="agent",
    )
    assert runtime_values(ctx) == {
        "user_name": "agt-actual-user",
        "user_home_dir": "/home/agt-actual-user",
        "vm_name": "box",
        "agent_name": "friendly-name",
        "workspace_name": "project",
        "workspace_dir": "/srv/a project's files",
        "session_name": "work",
    }
    assert context_values(vm_name="box", user="operator") == {
        "vm_name": "box",
        "user_name": "operator",
        "user_home_dir": "/home/operator",
    }


def test_adjacent_references_are_rendered_once() -> None:
    values = context_values(vm_name="box", user="agent", workspace_dir="${agw:session_name}")
    assert render_template("${agw:workspace_dir}/${agw:user_name}${agw:vm_name}", values, field="env.PATH") == (
        "${agw:session_name}/agentbox"
    )


@pytest.mark.parametrize(
    "text",
    [
        "$HOME ${HOME} ${PATH:-/bin} $$ $${HOME} $(command)",
        "docker ps --format '{{.Names}}'",
        "{{range .items}}{{.name}}{{end}}",
        '{"nested":{"value":1}}',
        "${other:value} ${agw_other:value}",
    ],
)
def test_unreserved_syntax_is_literal(text: str) -> None:
    assert render_template(text, {}, field="command") == text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("$${agw:user_home_dir}", "${agw:user_home_dir}"),
        ("$${agw:unknown}", "${agw:unknown}"),
        ("$${agw:", "${agw:"),
        ("literal $${agw:user_name}, actual ${agw:user_name}", "literal ${agw:user_name}, actual account"),
    ],
)
def test_reserved_escape_does_not_expand_escaped_content(text: str, expected: str) -> None:
    assert render_template(text, context_values(vm_name="box", user="account"), field="env.LITERAL") == expected


@pytest.mark.parametrize(
    "text",
    [
        "${agw:",
        "${agw:user_name",
        "${agw:}",
        "${agw: user_name}",
        "${agw:user_name }",
        "${agw:user-name}",
        "${agw:1name}",
        "${agw:ünicode}",
        "${agw:${agw:user_name}}",
        "${agw:unknown}",
        "${agw:session_name}",
    ],
)
def test_invalid_or_unavailable_context_has_a_field_aware_error(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        render_template(text, context_values(vm_name="box", user="account"), field="env.CONFIG")
    assert caught.value.entity_kind == "template"
    assert caught.value.entity_name == "env.CONFIG"


def test_available_empty_value_is_not_treated_as_missing() -> None:
    assert render_template("before${agw:workspace_dir}after", {"workspace_dir": ""}, field="env.EMPTY") == "beforeafter"


def test_error_does_not_echo_surrounding_input_or_replacements() -> None:
    sensitive = "opaque-credential-value"
    with pytest.raises(ValidationError) as caught:
        render_template(sensitive + "${agw:unknown}", {"user_name": sensitive}, field="env.TOKEN")
    assert sensitive not in repr((caught.value.args, vars(caught.value)))


def test_legacy_tokens_require_explicit_consumer_opt_in() -> None:
    text = "{{session_name}} {{workspace_name}}"
    values = {"session_name": "work", "workspace_name": "project"}
    assert render_template(text, values, field="env.PLAIN") == text
    assert (
        render_template(
            text,
            values,
            field="command",
            legacy_placeholders={"{{session_name}}": "session_name", "{{workspace_name}}": "workspace_name"},
        )
        == "work project"
    )


def test_legacy_and_namespaced_replacements_share_one_pass() -> None:
    session = "${agw:workspace_name}{{workspace_name}}"
    workspace = "{{session_name}}${agw:session_name}"
    assert (
        render_template(
            "{{session_name}}:${agw:workspace_name}:{{workspace_name}}:${agw:session_name}",
            {"session_name": session, "workspace_name": workspace},
            field="command",
            legacy_placeholders={"{{session_name}}": "session_name", "{{workspace_name}}": "workspace_name"},
        )
        == f"{session}:{workspace}:{workspace}:{session}"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("${{session_name}}", "$work"),
        ("$${{session_name}}", "$$work"),
        ("{{{session_name}}}", "{work}"),
        ("$${agw:session_name} {{session_name}}", "${agw:session_name} work"),
        ("{{unknown}} {{range .items}}{{.name}}{{end}}", "{{unknown}} {{range .items}}{{.name}}{{end}}"),
    ],
)
def test_legacy_tokens_preserve_surrounding_dollars_and_unrelated_syntax(text: str, expected: str) -> None:
    assert (
        render_template(
            text,
            {"session_name": "work"},
            field="command",
            legacy_placeholders={"{{session_name}}": "session_name"},
        )
        == expected
    )


@pytest.mark.parametrize("text", ["{{session_name}}", "{{session_name}} ${agw:unknown}", "${agw:bad-name}"])
def test_legacy_rendering_retains_field_aware_validation(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        render_template(
            text,
            {},
            field="session=work.resume_command",
            legacy_placeholders={"{{session_name}}": "session_name"},
        )
    assert caught.value.entity_kind == "template"
    assert caught.value.entity_name == "session=work.resume_command"
