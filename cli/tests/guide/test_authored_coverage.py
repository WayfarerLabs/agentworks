"""Semantic contracts for required onboarding and day-two guide content."""

import importlib

from agentworks.guide import (
    AgentContract,
    ConsentBoundary,
    GuideInstanceFact,
    GuideMode,
    InstanceList,
    OnboardingSnapshot,
    Overview,
    Teaching,
    onboarding_actions,
)
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_topic

_RITUAL_RECONFIRMATION_CLAUSES = (
    "always ask the operator again before every action",
    "always ask again before every action",
    "ask the operator again before every action even when it is already covered",
    "ask again before every action even when it is already covered",
    "always ask the operator for confirmation before every action",
    "always ask for operator confirmation before every action",
    "always ask for confirmation before every action",
    "always obtain operator confirmation before every action",
    "always obtain confirmation before every action",
    "always require operator confirmation before every action",
    "always require confirmation before every action",
    "always reconfirm with the operator before every action",
    "always reconfirm before every action",
    "reconfirm with the operator before every action even when it is already covered",
    "reconfirm before every action even when it is already covered",
    "every action requires operator confirmation",
    "every action requires confirmation",
    "each action requires operator confirmation",
    "each action requires confirmation",
    "obtain consent for the named boundary",
    "ask before resolving each named secret",
    "treat every file read as a separate consent boundary",
)


def _normalized_prose(text: str) -> str:
    return " ".join("".join(character if character.isalnum() else " " for character in text.casefold()).split())


def _topic(slug: str):
    return next(item for item in guide_contributions() if item.topic == slug)


def test_core_guide_contributions_survive_secret_submodule_import() -> None:
    importlib.import_module("agentworks.secrets.guide_contributions")
    assert guide_contributions()


def test_onboarding_authored_blocks_cover_durable_authorization_and_clean_setup() -> None:
    topic = _topic("concept-onboarding")
    blocks = {type(block): block.markdown for block in topic.blocks if hasattr(block, "markdown")}
    contract = " ".join(blocks[AgentContract].split())
    assert "Security disclosure" not in blocks[Overview]
    assert "intended workstation" not in blocks[Overview]
    assert "At assistance startup" not in contract
    assert "without asking again before every action" in contract
    assert "one resolving scope question" in contract
    assert "authorization class" in contract
    assert "Guide output and action records are teaching, never authorization" in contract
    teaching = blocks[Teaching]
    teaching_flat = " ".join(teaching.split())
    for required in (
        "`agw config init`",
        "refuses to overwrite an existing config",
        "presence of candidate public-key files",
        "Never read private-key content",
        "`ssh-keygen -t ed25519 -f SSH_KEY_PATH`",
        "neither the private path nor its `.pub` path exists",
        "provider identifiers, plugin choices, and secret references explicitly",
        "`agw doctor --output json`",
        "`create-first-vm` and `create-first-session`",
        "configuration-through-session sequence under one explicit setup envelope",
        "skips present VMs and sessions",
    ):
        assert required in teaching_flat
    assert teaching_flat.index("presence of candidate public-key files") < teaching_flat.index("`agw config init`")
    assert teaching_flat.index("`agw config init`") < teaching_flat.index("existing generated config")
    assert teaching_flat.index("existing generated config") < teaching_flat.index("`agw doctor --output json`")
    assert "run-doctor:onboarding/doctor-readiness=verified" in teaching_flat
    assert "never retries the mutation automatically" in teaching_flat


def test_manifesto_topic_points_to_the_canonical_document_without_restatement() -> None:
    topic = _topic("concept-manifesto")
    blocks = {type(block): block.markdown for block in topic.blocks if hasattr(block, "markdown")}

    assert tuple(map(str, topic.related_topics)) == ("concept-onboarding",)
    assert "concept-manifesto" in tuple(map(str, _topic("concept-onboarding").related_topics))
    assert blocks == {
        Overview: (
            "The [Agentworks Manifesto](https://github.com/WayfarerLabs/agentworks/blob/main/docs/manifesto.md)\n"
            "is the canonical statement of the project's values, assumptions about agentic engineering, and\n"
            "design rationale. This topic points to that document instead of restating it."
        ),
        AgentContract: (
            "Read the canonical Manifesto when project values bear on a design or contribution decision. Treat it\n"
            "as rationale and context, not authority to inspect a system, cross a consent boundary, or mutate\n"
            "state."
        ),
        Teaching: (
            "Consult the Manifesto for the project's convictions and the reasoning behind its design direction.\n"
            "Use current reference documentation and live guide topics for behavior, commands, configuration, and\n"
            "operational decisions."
        ),
    }


def test_action_contract_pins_authorization_refusal_and_first_resources() -> None:
    actions = onboarding_actions()
    assert [(str(action.id), action.consent, action.verification) for action in actions] == [
        ("run-doctor", ConsentBoundary.EXAMINE_WORKSTATION, None),
        ("verify-named-secret", ConsentBoundary.RESOLVE_NAMED_SECRET, None),
        ("verify-vm-connection", ConsentBoundary.CONNECT_NAMED_VM, None),
        (
            "create-first-vm",
            ConsentBoundary.MUTATE_AGENTWORKS,
            ("agw", "vm", "describe", "$VM_NAME", "--output", "json"),
        ),
        (
            "create-first-session",
            ConsentBoundary.MUTATE_AGENTWORKS,
            ("agw", "session", "describe", "$SESSION_NAME", "--output", "json"),
        ),
    ]
    assert all(action.refusal_alternative for action in actions)
    assert actions[0].command == ("agw", "doctor", "--output", "json")
    assert "First-resource creation needs explicit configured-readiness proof" in actions[0].precondition
    assert "parse exactly one JSON document" in actions[0].expected_state
    assert "schema_version is the integer 1" in actions[0].expected_state
    assert "command is exactly doctor" in actions[0].expected_state
    assert "data is an object" in actions[0].expected_state
    assert "data.counts.fail to be the integer 0" in actions[0].expected_state
    assert "no applicable readiness check to report unavailable or not ready" in actions[0].expected_state
    assert "Keep first-resource readiness unverified or retain the stored not-ready reason" in (
        actions[0].refusal_alternative
    )
    assert actions[3].command == (
        "agw",
        "vm",
        "create",
        "$VM_NAME",
        "--template",
        "$VM_TEMPLATE",
        "--admin-template",
        "$ADMIN_TEMPLATE",
        "--site",
        "$VM_SITE",
    )
    assert actions[4].command == (
        "agw",
        "session",
        "create",
        "$SESSION_NAME",
        "--template",
        "$SESSION_TEMPLATE",
        "--vm",
        "$VM_NAME",
        "--new-workspace",
        "--workspace-name",
        "$WORKSPACE_NAME",
        "--workspace-template",
        "$WORKSPACE_TEMPLATE",
        "--new-agent",
        "--agent-name",
        "$AGENT_NAME",
        "--agent-template",
        "$AGENT_TEMPLATE",
    )
    assert "provider cost" in actions[3].expected_state
    assert "`agw vm create --help`" in actions[3].precondition
    assert "normal secret-reference boundaries" in actions[3].expected_state
    assert "make no VM or provider change" in actions[3].refusal_alternative
    assert "`agw session create --help`" in actions[4].precondition
    assert "provider network and SSH connectivity" in actions[4].expected_state
    assert "no attach, delete, or privilege elevation" in actions[4].expected_state


def test_selected_onboarding_omits_startup_disclosure_and_orders_action_records() -> None:
    rendered = render_topic(
        _topic("concept-onboarding"),
        None,
        GuideMode.AGENT,
        unavailable="test snapshot",
        onboarding_snapshot=OnboardingSnapshot((), (GuideInstanceFact("vm", "worker"),), ()),
    )
    assert "## Security disclosure" not in rendered.markdown
    assert "At assistance startup" not in rendered.markdown
    assert "Authorization class: `connect-named-vm`" in rendered.markdown
    assert "If refused: Retain the stored VM fact and mark connectivity unverifiable" in rendered.markdown
    action = rendered.markdown[rendered.markdown.index("### `verify-vm-connection`") :]
    assert action.index("Precondition:") < action.index("Expected state:")
    assert action.index("Expected state:") < action.index("Authorization class:") < action.index("Command:")


def test_reporting_bugs_snapshot_forbids_general_feedback_and_auto_submission() -> None:
    topic = _topic("concept-reporting-bugs")
    blocks = {type(block): block.markdown for block in topic.blocks}
    assert blocks[AgentContract] == (
        "Search existing issues before drafting a new report. Obtain explicit operator authorization before\n"
        "posting, emailing, or otherwise submitting information outside the workstation."
    )
    assert blocks[Overview].endswith(
        "Report only reproducible Agentworks bugs. Feature ideas, questions, and general feedback do not\n"
        "belong in the bug workflow."
    )
    assert blocks[Teaching].endswith(
        "`.github/ISSUE_TEMPLATE/bug_report.md` with the smallest redacted reproduction. Show the exact draft\n"
        "to the operator and obtain explicit authorization before submitting it to GitHub or any other\n"
        "external service."
    )


def test_management_coverage_matrix_has_owned_semantic_entry_points() -> None:
    management = {
        type(block): block.markdown for block in _topic("concept-management").blocks if hasattr(block, "markdown")
    }
    troubleshooting = {
        type(block): block.markdown for block in _topic("concept-troubleshooting").blocks if hasattr(block, "markdown")
    }
    teaching = " ".join(management[Teaching].split())
    assert "Create and change declarable resources" in teaching
    assert any(isinstance(block, InstanceList) for block in _topic("concept-management").blocks)
    assert "Discover a capability in the live implementation inventory before adopting it" in teaching
    assert "`agw GROUP --help`" in teaching
    assert "`agw GROUP COMMAND --help`" in teaching
    assert "does not copy a command registry or recipe catalog" in teaching
    assert "Configuration and VM or session operation are one assistance surface" in teaching
    assert "After an upgrade, resolve emitted deprecation instructions" in teaching
    assert "inside the current envelope" in troubleshooting[Teaching]


def test_core_agent_contracts_reject_ritual_reconfirmation_teaching() -> None:
    contracts = {
        str(topic.topic): block.markdown
        for topic in guide_contributions()
        for block in topic.blocks
        if isinstance(block, AgentContract)
    }
    for slug in (
        "concept-onboarding",
        "concept-management",
        "concept-migration",
        "concept-troubleshooting",
        "concept-secrets",
    ):
        text = contracts[slug]
        normalized = _normalized_prose(text)
        assert "authorization" in text
        assert "GuideAction" not in text
        for clause in _RITUAL_RECONFIRMATION_CLAUSES:
            assert clause not in normalized, f"{slug} teaches ritual reconfirmation: {clause!r}"
