"""The Markdown-only ``agw guide`` command."""

from __future__ import annotations

import os
import re
import sys
from typing import Annotated, Literal

import typer

from agentworks.cli._app import app
from agentworks.guide.agent_mode import select_guide_mode
from agentworks.guide.assessment import VerificationEvidence, VerificationOutcome
from agentworks.guide.contract import ActionId
from agentworks.guide.service import render_guide
from agentworks.guide.view import GuideIdentity

_EVIDENCE_RE = re.compile(
    r"(?P<action>[a-z][a-z0-9-]*):(?P<kind>[a-z][a-z0-9-]*)/(?P<name>[^/:=\s]+)="
    r"(?P<outcome>verified|failed|refused)"
)


def _parse_evidence(values: list[str]) -> tuple[VerificationEvidence, ...]:
    """Parse the replay log completely before constructing any evidence."""
    parsed: list[VerificationEvidence] = []
    for value in values:
        match = _EVIDENCE_RE.fullmatch(value)
        if match is None:
            raise typer.BadParameter(
                "must be ACTION_ID:KIND/NAME=verified|failed|refused",
                param_hint="--evidence",
            ) from None
        parsed.append(
            VerificationEvidence(
                ActionId(match.group("action")),
                GuideIdentity(match.group("kind"), match.group("name")),
                VerificationOutcome(match.group("outcome")),
            )
        )
    return tuple(parsed)


@app.command("guide")
def guide(
    topics: Annotated[
        list[str] | None,
        typer.Argument(help="One or more exact guide topic names."),
    ] = None,
    agent: bool | None = typer.Option(
        None,
        "--agent/--human",
        help="Render for an agent or human, overriding automatic mode selection.",
    ),
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help="Emit one available topic name per line with no formatting.",
    ),
    evidence: Annotated[
        list[str] | None,
        typer.Option(
            "--evidence",
            help="Replay caller-owned verification evidence as ACTION_ID:KIND/NAME=OUTCOME; repeatable.",
        ),
    ] = None,
) -> None:
    """Render authored guidance and safe facts from the current system."""
    explicit: Literal["agent", "human"] | None = None if agent is None else ("agent" if agent else "human")
    mode = select_guide_mode(explicit, os.environ, sys.stdout.isatty())
    verification_evidence = _parse_evidence(evidence or [])
    response = render_guide(
        tuple(topics or ()), mode, names_only=names_only, verification_evidence=verification_evidence
    )
    typer.echo(response.markdown, nl=False)
    if response.exit_code:
        raise typer.Exit(response.exit_code)
