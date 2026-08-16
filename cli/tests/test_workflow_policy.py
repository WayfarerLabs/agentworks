"""Shape checks for the Pages deployment workflow.

These tests read workflows that live in the same tree they do and change in the same
commits, so they are not a security boundary: anything that can edit `pages.yml` can
edit this file beside it. What they catch is an **accidental regression in the
workflow's shape**, and that is the whole promise. Static proof that a
repository-authored workflow cannot be subverted is not attempted, because the class
of ways to subvert it is open-ended and self-verification cannot close it.

What actually holds at runtime is not restated here. The workflows run the website
suites, build twice and diff the results, and verify the checked-out commit and a
clean worktree before uploading; each of those fails the job on its own. The controls
on the `github-pages` environment are configured on GitHub rather than in this
repository. Those are the guarantees; these four checks only notice if the shape they
depend on drifts.

This file sits in `cli/tests/` because it needs a YAML parser: the website suite runs
on a bare runner with no package installation step. `tests/assistance/test_contract.py`
is the same pattern.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"

# YAML 1.1 reads a bare `on` key as a boolean, so triggers arrive under True.
NEEDS_RESULT = re.compile(r"\$\{\{\s*needs\.([A-Za-z0-9_-]+)\.result\s*\}\}")
JOINED_RESULTS = re.compile(r"\$\{\{\s*join\(\s*needs\.\*\.result\s*,[^)]*\)\s*\}\}")


def _workflow(name: str) -> dict[Any, Any]:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(loaded, dict)
    return loaded


CI = _workflow("ci.yml")
PAGES = _workflow("pages.yml")


def _jobs(workflow: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    return dict(workflow["jobs"])


def _needs(job: dict[str, Any]) -> list[str]:
    required = job.get("needs", [])
    return [required] if isinstance(required, str) else list(required)


def _deploying() -> dict[str, dict[str, Any]]:
    """The jobs holding write scopes, found by their grants rather than by name."""
    return {name: job for name, job in _jobs(PAGES).items() if "write" in set(job.get("permissions", {}).values())}


def test_only_the_deployment_job_holds_write_permissions() -> None:
    """Everything that runs repository code stays read-only."""
    deploying = _deploying()
    assert len(deploying) == 1
    read_only = [PAGES.get("permissions", {})]
    read_only += [job.get("permissions", {}) for name, job in _jobs(PAGES).items() if name not in deploying]
    for granted in read_only:
        assert set(granted.values()) <= {"read", "none"}


def test_the_deployment_job_runs_only_the_deploy_action() -> None:
    """The job holding the token does one thing, so nothing of ours executes beside it."""
    for name, job in _deploying().items():
        used = [str(step.get("uses", "")).split("@", 1)[0] for step in job.get("steps", ())]
        assert used == ["actions/deploy-pages"], name


def test_the_deployment_job_waits_for_the_build_and_uses_the_pages_environment() -> None:
    """Deployment follows the job that produced the artifact, through the environment.

    The build job is identified by the upload action rather than by its name, and the
    environment is named because that is where the GitHub-side branch restrictions and
    deployment reviewers attach.
    """
    producers = {
        name
        for name, job in _jobs(PAGES).items()
        if any(str(step.get("uses", "")).startswith("actions/upload-pages-artifact@") for step in job.get("steps", ()))
    }
    assert producers
    for name, job in _deploying().items():
        assert producers <= set(_needs(job)), name
        assert job["environment"]["name"] == "github-pages", name


def test_the_gate_is_wired_to_every_dependency_result() -> None:
    """Every other job is required, and the gate step is handed each of their results.

    This is a wiring check and holds only that much: `needs` covers every other job in
    the workflow, so none can fail without blocking a merge, and the gate step's
    environment carries a dependency-result expression for each required job rather
    than constants. It does not hold that the script reads that environment or acts on
    what it finds. Nothing in this file proves what a script does.
    """
    gate = _jobs(CI)["ci-success"]
    required = _needs(gate)
    assert set(required) == set(_jobs(CI)) - {"ci-success"}
    assert gate["if"] == "always()"

    supplied = " ".join(str(value) for step in gate.get("steps", ()) for value in step.get("env", {}).values())
    assert JOINED_RESULTS.search(supplied) or set(NEEDS_RESULT.findall(supplied)) == set(required)
