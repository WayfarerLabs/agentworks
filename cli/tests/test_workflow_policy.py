"""Deploy-path policy invariants for the CI and Pages workflows.

``pages.yml`` is the only thing that mints a GitHub Pages deployment for
agentworks.build, and ``ci.yml``'s ``ci-success`` job is the single required check on
main. The properties asserted here are the ones an ordinary edit can break without
anyone noticing: least-privilege token scopes, credentials that never reach a step
that runs repository code, deployment tied to main and to the exact commit that was
tested, an artifact that is exactly the directory the verified build wrote, and build
scripts that abort on the first failure.

The workflow text is an authored artifact, so nothing here pins its wording, its step
names, its action versions, or its formatting. Every assertion reads parsed structure,
derives one part of the file from another, or executes the shell the workflow runs.

These files live at the repository root rather than under ``cli/``. They are tested
from here because this suite already has PyYAML; the website suite that used to own
this file runs on a bare runner with no package installation step, so it cannot parse
YAML at all. ``tests/assistance/test_contract.py`` is the same pattern.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

# YAML 1.1 reads the bare key ``on`` as a boolean, so a workflow's trigger block
# arrives under ``True``. GitHub Actions reads the same file as the string key.
TRIGGERS = True

# The Actions expression form and the shell environment form name one directory.
RUNNER_TEMP = re.compile(r"\$\{\{\s*runner\.temp\s*\}\}|\$\{RUNNER_TEMP\}|\$RUNNER_TEMP")
STEP_OUTPUT = re.compile(r"\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)\s*\}\}")
SHELL_OPERATORS = frozenset({"|", "||", "&", "&&", ";", ";;", ">", ">>", "<"})


def _workflow(name: str) -> dict[Any, Any]:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(loaded, dict)
    return loaded


CI = _workflow("ci.yml")
PAGES = _workflow("pages.yml")


def _jobs(workflow: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    return dict(workflow["jobs"])


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps", ()))


def _deploy_path_jobs() -> list[dict[str, Any]]:
    """Every job that runs repository code on the way to a published artifact."""
    return [*_jobs(PAGES).values(), _jobs(CI)["website"]]


def _conditions(job: dict[str, Any]) -> set[str]:
    """The conjuncts of a job condition, whitespace-normalized."""
    return {" ".join(part.split()) for part in job["if"].split("&&")}


def _referenced_step(job: dict[str, Any], expression: str) -> tuple[dict[str, Any], str]:
    """Resolve a ``steps.<id>.outputs.<name>`` expression to its step and output name."""
    match = STEP_OUTPUT.fullmatch(expression.strip())
    assert match is not None, f"not a step-output reference: {expression}"
    named = [step for step in _steps(job) if step.get("id") == match.group(1)]
    assert len(named) == 1, f"expected exactly one step with id {match.group(1)!r}"
    return named[0], match.group(2)


def _step_using(job: dict[str, Any], action: str) -> dict[str, Any]:
    used = [step for step in _steps(job) if str(step.get("uses", "")).startswith(f"{action}@")]
    assert len(used) == 1, f"expected exactly one {action} step"
    return used[0]


def _commands(script: str) -> list[list[str]]:
    """Split a straight-line run script into shell words, one list per command."""
    joined = script.replace("\\\n", " ")
    return [shlex.split(line) for line in joined.splitlines() if line.strip()]


def _flag(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def _site_builds(script: str) -> list[list[str]]:
    return [command for command in _commands(script) if "website/build.py" in command]


def _build_script(job: dict[str, Any]) -> str:
    scripts = [str(step["run"]) for step in _steps(job) if "website/build.py" in str(step.get("run", ""))]
    assert len(scripts) == 1
    return scripts[0]


def _source_verifiers(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Steps that check the checked-out commit against the event commit."""
    return [step for step in _steps(job) if "EXPECTED_SHA" in step.get("env", {})]


def _same_path(first: str, second: str) -> bool:
    return RUNNER_TEMP.sub("<runner-temp>", first) == RUNNER_TEMP.sub("<runner-temp>", second)


def _bash(script: str, environment: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **environment},
        timeout=30,
    )


def _committed_repository(directory: Path) -> str:
    """A one-commit git repository, returning its HEAD sha."""
    directory.mkdir()
    (directory / "README.md").write_text("clean\n")
    subprocess.run(["git", "init", "--quiet"], cwd=directory, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=directory, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Workflow Test",
            "-c",
            "user.email=workflow@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "test fixture",
        ],
        cwd=directory,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def test_only_the_pages_deploy_job_may_hold_write_scopes() -> None:
    """The token that can publish a deployment reaches no step that runs our code.

    The build job runs the test suite and the site builder, so a write scope there
    would be reachable by anything those execute. Splitting build from deploy is the
    whole point of the two-job shape, and it survives only while deploy is the sole
    holder of write scopes.
    """
    assert PAGES["permissions"] == {"contents": "read"}
    for name, job in _jobs(PAGES).items():
        granted = job.get("permissions", {})
        if name == "deploy":
            assert granted == {"pages": "write", "id-token": "write"}
        else:
            assert set(granted.values()) <= {"read", "none"}, name

    # CI builds the site on pull requests from forks; it never needs Pages at all.
    assert CI["permissions"] == {"contents": "read"}
    for name, job in _jobs(CI).items():
        granted = job.get("permissions", {})
        assert set(granted.values()) <= {"read", "none"}, name
        assert {"pages", "id-token"}.isdisjoint(granted), name


def test_no_deploy_path_checkout_leaves_credentials_on_disk() -> None:
    """The build steps run repository code, so the token must not survive checkout.

    ``actions/checkout`` writes an authorization header into ``.git/config`` unless
    this is turned off, which would leave a usable credential in the working tree
    while the tests, the builder, and every action after them run.
    """
    checkouts = [
        step
        for job in _deploy_path_jobs()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False


def test_pages_deploys_only_for_pushes_to_main() -> None:
    """Nothing but a push to main can reach the deployment environment.

    The trigger keeps other events out, and the job condition keeps a re-run or a
    future added trigger from reaching deploy behind the trigger's back.
    """
    assert PAGES[TRIGGERS] == {"push": {"branches": ["main"]}}
    conditions = _conditions(_jobs(PAGES)["deploy"])
    assert "github.event_name == 'push'" in conditions
    assert "github.ref == 'refs/heads/main'" in conditions


def test_deploy_publishes_the_exact_artifact_the_verified_build_produced() -> None:
    """What gets deployed is the directory a verified, reproducible build wrote.

    Four links have to hold at once: the published ``source_sha`` comes from a step
    that compared the checkout against the event commit, deploy runs only while that
    recorded sha is still the event commit, the uploaded path is the first build
    output rather than the workspace or the repeat build, and deploy asks for the
    artifact this job uploaded.
    """
    build = _jobs(PAGES)["build"]
    verifier, _ = _referenced_step(build, build["outputs"]["source_sha"])
    assert verifier["env"]["EXPECTED_SHA"] == "${{ github.sha }}"
    assert "needs.build.outputs.source_sha == github.sha" in _conditions(_jobs(PAGES)["deploy"])

    upload = _step_using(build, "actions/upload-pages-artifact")
    outputs = [_flag(command, "--output") for command in _site_builds(_build_script(build))]
    assert len(set(outputs)) == len(outputs) > 1
    assert _same_path(upload["with"]["path"], outputs[0])

    deployment = _step_using(_jobs(PAGES)["deploy"], "actions/deploy-pages")
    assert deployment["with"]["artifact_name"] == upload["with"]["name"]

    # Nothing runs between the last source-state check and the upload.
    steps = _steps(build)
    assert steps[steps.index(upload) - 1] in _source_verifiers(build)


def test_every_site_build_is_proven_reproducible_before_it_is_used() -> None:
    """Each configured base path is built twice into separate trees and diffed.

    A build that is not reproducible publishes something nobody reviewed. The check
    is only worth having while it can fail the job, so the scripts stay straight-line
    sequences under the default fail-fast shell: one swallowed exit code and the diff
    becomes decoration.
    """
    for label, job in (("pages.build", _jobs(PAGES)["build"]), ("ci.website", _jobs(CI)["website"])):
        script = _build_script(job)
        commands = _commands(script)
        assert not any(SHELL_OPERATORS.intersection(command) for command in commands), label

        by_base: dict[str, list[str]] = {}
        for command in _site_builds(script):
            by_base.setdefault(_flag(command, "--site-base"), []).append(_flag(command, "--output"))
        assert by_base, label

        diffs = [command for command in commands if command[0] == "diff"]
        assert len(diffs) == len(by_base), label
        compared = [{word for word in diff[1:] if not word.startswith("-")} for diff in diffs]
        for base, outputs in by_base.items():
            assert len(set(outputs)) == 2, f"{label}: {base} is not built twice into separate trees"
            assert set(outputs) in compared, f"{label}: {base} builds are not compared"


def test_no_alternate_shell_disarms_the_deploy_path_scripts() -> None:
    """Every script keeps the fail-fast shell its checks depend on.

    The ``bash`` keyword shell runs with ``-eo pipefail``. A custom template such as
    ``bash {0}`` drops that, which turns every unchecked command in the verification
    and build scripts into an ignored failure. ``defaults`` may still set a working
    directory, which does not affect exit handling.
    """
    for name, workflow in (("ci.yml", CI), ("pages.yml", PAGES)):
        shells = [workflow.get("defaults", {}).get("run", {}).get("shell")]
        for job in _jobs(workflow).values():
            shells.append(job.get("defaults", {}).get("run", {}).get("shell"))
            shells.extend(step.get("shell") for step in _steps(job))
        for shell in shells:
            assert shell in (None, "bash"), f"{name}: unexpected shell {shell!r}"


def test_no_deploy_path_step_can_be_skipped_or_swallow_a_failure() -> None:
    """The tests and verifications that gate a deployment always run and always count.

    A step-level condition or ``continue-on-error`` on any of them would let the
    artifact reach Pages without the thing that was supposed to have checked it.
    ``deploy`` carries the one sanctioned job condition; nothing else may.
    """
    for job in _deploy_path_jobs():
        assert "continue-on-error" not in job
        for step in _steps(job):
            assert "if" not in step, step.get("name")
            assert "continue-on-error" not in step, step.get("name")
    assert "if" not in _jobs(PAGES)["build"]


def test_the_deploy_path_runs_only_first_party_actions() -> None:
    """No third-party action shares a workflow with the Pages deployment token."""
    for job in _jobs(PAGES).values():
        for step in _steps(job):
            if "uses" in step:
                assert step["uses"].startswith("actions/"), step["uses"]


def test_ci_success_requires_every_other_job_and_always_reports() -> None:
    """The single required check covers the whole workflow, including new jobs.

    Branch protection requires ``ci-success`` alone, so a job missing from ``needs``
    is a job whose failure cannot block a merge. ``always()`` is what makes the gate
    fail loudly instead of being skipped along with its failed dependency.
    """
    jobs = _jobs(CI)
    gate = jobs["ci-success"]
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == set(jobs) - {"ci-success"}


def test_ci_success_rejects_a_short_result_list_and_every_non_success_result() -> None:
    """Executing the gate script: only a full sweep of successes passes it.

    ``skipped`` is the case that matters. A required job that never ran reports
    ``skipped``, not ``failure``, and a check that only looked for ``failure`` would
    wave it through.
    """
    gate = _jobs(CI)["ci-success"]
    script = str(_steps(gate)[0]["run"])
    required = len(gate["needs"])

    assert _bash(script, {"REQUIRED_RESULTS": " ".join(["success"] * required)}).returncode == 0
    assert _bash(script, {"REQUIRED_RESULTS": " ".join(["success"] * (required - 1))}).returncode != 0
    for result in ("failure", "cancelled", "skipped", "neutral"):
        values = ["success"] * required
        values[-1] = result
        assert _bash(script, {"REQUIRED_RESULTS": " ".join(values)}).returncode != 0, result


def test_pages_base_normalization_accepts_only_the_builder_grammar() -> None:
    """Executing the normalizer: whatever Pages reports is confined to a safe base.

    ``configure-pages`` output is external input, and the builder embeds the result
    in every generated link. The script is the boundary that keeps a traversal, a
    scheme, or a query string out of the built site.
    """
    build = _jobs(PAGES)["build"]
    builder = next(step for step in _steps(build) if "SITE_BASE" in step.get("env", {}))
    normalizer, output_name = _referenced_step(build, builder["env"]["SITE_BASE"])
    script = str(normalizer["run"])

    accepted = {
        "": "/",
        "/": "/",
        "/agentworks": "/agentworks/",
        "/agentworks/": "/agentworks/",
        "/team/site-1.0~": "/team/site-1.0~/",
    }
    rejected = (
        "agentworks",
        "//agentworks",
        "/../agentworks",
        "/agentworks//nested",
        "/agent works",
        "/café",
        "/%2e%2e",
        "/agentworks?preview=1",
        "https://example.com",
    )
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "github-output"
        for raw, expected in accepted.items():
            result = _bash(script, {"PAGES_BASE_PATH": raw, "GITHUB_OUTPUT": str(path)})
            assert result.returncode == 0, raw
            assert _outputs(path)[output_name] == expected, raw
            path.unlink()
        for raw in rejected:
            assert _bash(script, {"PAGES_BASE_PATH": raw, "GITHUB_OUTPUT": str(path)}).returncode != 0, raw


def test_source_verifiers_reject_a_wrong_head_and_tracked_or_untracked_drift() -> None:
    """Executing the verifiers: the deployed tree is the reviewed tree, unmodified.

    Between checkout and upload the job runs the test suite and the builder. Either
    could write into the working tree, and the artifact would carry whatever they
    left. Each verifier refuses a checkout that is not the event commit and any
    worktree change at all, tracked or not. The one wired to the job output also
    publishes the sha it just verified.
    """
    build = _jobs(PAGES)["build"]
    publisher, output_name = _referenced_step(build, build["outputs"]["source_sha"])
    verifiers = _source_verifiers(build)
    assert publisher in verifiers

    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary) / "repo"
        head = _committed_repository(repository)
        readme = repository / "README.md"
        untracked = repository / "untracked.txt"
        path = Path(temporary) / "github-output"

        for step in verifiers:
            script = str(step["run"])
            clean = _bash(script, {"EXPECTED_SHA": head, "GITHUB_OUTPUT": str(path)}, cwd=repository)
            assert clean.returncode == 0, clean.stderr
            if step is publisher:
                assert _outputs(path)[output_name] == head
            path.unlink(missing_ok=True)

            readme.write_text("mutated after verification\n")
            assert _bash(script, {"EXPECTED_SHA": head, "GITHUB_OUTPUT": str(path)}, cwd=repository).returncode != 0
            readme.write_text("clean\n")

            untracked.write_text("drift\n")
            assert _bash(script, {"EXPECTED_SHA": head, "GITHUB_OUTPUT": str(path)}, cwd=repository).returncode != 0
            untracked.unlink()

            wrong = _bash(script, {"EXPECTED_SHA": "0" * 40, "GITHUB_OUTPUT": str(path)}, cwd=repository)
            assert wrong.returncode != 0
            path.unlink(missing_ok=True)
