from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_PATH = REPO_ROOT / ".github/workflows/ci.yml"
PAGES_PATH = REPO_ROOT / ".github/workflows/pages.yml"

PYTHON_TEST_COMMAND = "python3 -m unittest discover -s website/tests -p 'test_*.py'"
NODE_TEST_COMMAND = (
    "node --test website/tests/lander-world.test.mjs website/tests/lander-model.test.mjs "
    "website/tests/lander-phase4i.test.mjs website/tests/lander-phase4j.test.mjs "
    "website/tests/lander-phase4k.test.mjs website/tests/lander-phase4l.test.mjs"
)

CI_DETERMINISTIC_BUILD_SCRIPT = '''\
python3 website/build.py --repo-root . --output "${RUNNER_TEMP}/site-root-a" --site-base /
python3 website/build.py --repo-root . --output "${RUNNER_TEMP}/site-root-b" --site-base /
diff --recursive --no-dereference "${RUNNER_TEMP}/site-root-a" "${RUNNER_TEMP}/site-root-b"
python3 website/build.py --repo-root . --output "${RUNNER_TEMP}/site-project-a" --site-base /agentworks/
python3 website/build.py --repo-root . --output "${RUNNER_TEMP}/site-project-b" --site-base /agentworks/
diff --recursive --no-dereference "${RUNNER_TEMP}/site-project-a" "${RUNNER_TEMP}/site-project-b"'''

PAGES_DETERMINISTIC_BUILD_SCRIPT = '''\
python3 website/build.py \\
  --repo-root . \\
  --output "${RUNNER_TEMP}/agentworks-site" \\
  --site-base "$SITE_BASE"
python3 website/build.py \\
  --repo-root . \\
  --output "${RUNNER_TEMP}/agentworks-site-repeat" \\
  --site-base "$SITE_BASE"
diff --recursive --no-dereference \\
  "${RUNNER_TEMP}/agentworks-site" \\
  "${RUNNER_TEMP}/agentworks-site-repeat"'''

CI_SUCCESS_SCRIPT = '''\
read -r -a results <<< "$REQUIRED_RESULTS"
if [[ "${#results[@]}" -ne 6 ]]; then
  echo "::error::Expected six required CI job results."
  exit 1
fi
for result in "${results[@]}"; do
  if [[ "$result" != "success" ]]; then
    echo "::error::A required CI job ended with non-success result: $result."
    exit 1
  fi
done
echo "All required CI jobs passed."'''

TESTED_SOURCE_SCRIPT = '''\
checked_out_sha="$(git rev-parse HEAD)"
if [[ "$checked_out_sha" != "$EXPECTED_SHA" ]]; then
  echo "::error::Checked out $checked_out_sha instead of event commit $EXPECTED_SHA."
  exit 1
fi
worktree_state="$(git status --porcelain=v1 --untracked-files=all)"
if [[ -n "$worktree_state" ]]; then
  echo "::error::Repository tests left tracked or untracked worktree changes."
  printf '%s\\n' "$worktree_state"
  exit 1
fi
printf 'sha=%s\\n' "$checked_out_sha" >> "$GITHUB_OUTPUT"'''

UPLOAD_SOURCE_SCRIPT = """\
checked_out_sha="$(git rev-parse HEAD)"
if [[ "$checked_out_sha" != "$EXPECTED_SHA" ]]; then
  echo "::error::Checked out $checked_out_sha instead of event commit $EXPECTED_SHA."
  exit 1
fi
worktree_state="$(git status --porcelain=v1 --untracked-files=all)"
if [[ -n "$worktree_state" ]]; then
  echo "::error::Pages build left tracked or untracked worktree changes."
  printf '%s\\n' "$worktree_state"
  exit 1
fi"""


def block(source: str, heading: str, indent: int) -> str:
    lines = source.splitlines()
    marker = f"{' ' * indent}{heading}"
    matches = [index for index, line in enumerate(lines) if line == marker]
    if len(matches) != 1:
        raise AssertionError(f"workflow block must occur exactly once: {heading}")
    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            end = index
            break
    return "\n".join(lines[start:end])


def step_with_action(job: str, action: str) -> str:
    lines = job.splitlines()
    action_index = next((index for index, line in enumerate(lines) if f"uses: {action}" in line), None)
    if action_index is None:
        raise AssertionError(f"missing action: {action}")
    start = action_index
    while start > 0 and not re.match(r"^      - ", lines[start]):
        start -= 1
    end = len(lines)
    for index in range(action_index + 1, len(lines)):
        if re.match(r"^      - ", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def literal_script(job: str, step_name: str) -> str:
    lines = job.splitlines()
    name_index = next((index for index, line in enumerate(lines) if line == f"      - name: {step_name}"), None)
    if name_index is None:
        raise AssertionError(f"missing workflow step: {step_name}")
    run_index = next(index for index in range(name_index + 1, len(lines)) if lines[index] == "        run: |")
    script: list[str] = []
    for line in lines[run_index + 1 :]:
        if line.startswith("          "):
            script.append(line[10:])
        elif not line.strip():
            script.append("")
        else:
            break
    return "\n".join(script).rstrip("\n")


def root_mapping(source: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in source.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+):(?: (.*))?", line)
        if match is None or "#" in (match.group(2) or ""):
            raise AssertionError(f"invalid top-level workflow entry: {line}")
        key, value = match.group(1), match.group(2) or ""
        if key in entries:
            raise AssertionError(f"duplicate top-level workflow key: {key}")
        entries[key] = value
    return entries


def simple_mapping(source: str, heading: str, indent: int) -> dict[str, str]:
    selected = block(source, heading, indent).splitlines()[1:]
    entries: dict[str, str] = {}
    for line in selected:
        if not line.strip() or len(line) - len(line.lstrip()) > indent + 2:
            continue
        match = re.fullmatch(rf"{' ' * (indent + 2)}([A-Za-z0-9_-]+):(?: (.*))?", line)
        if match is None or "#" in (match.group(2) or ""):
            raise AssertionError(f"invalid direct mapping child in {heading}: {line}")
        key, value = match.group(1), match.group(2) or ""
        if key in entries:
            raise AssertionError(f"duplicate direct mapping key in {heading}: {key}")
        entries[key] = value
    return entries


def workflow_steps(job: str) -> list[str]:
    lines = job.splitlines()
    markers = [index for index, line in enumerate(lines) if line == "    steps:"]
    if len(markers) != 1:
        raise AssertionError("job must contain exactly one steps mapping")
    starts = [index for index in range(markers[0] + 1, len(lines)) if lines[index].startswith("      - ")]
    if not starts:
        raise AssertionError("job must contain at least one step")
    for line in lines[markers[0] + 1 : starts[0]]:
        if line.strip():
            raise AssertionError(f"invalid content before first step: {line}")
    result: list[str] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        result.append("\n".join(lines[start:end]))
    return result


def step_mapping(step: str) -> dict[str, str]:
    lines = step.splitlines()
    entries: dict[str, str] = {}
    for index, line in enumerate(lines):
        if not line.strip() or len(line) - len(line.lstrip()) > 8:
            continue
        pattern = r"      - ([a-z-]+):(?: (.*))?" if index == 0 else r"        ([a-z-]+):(?: (.*))?"
        match = re.fullmatch(pattern, line)
        if match is None or "#" in (match.group(2) or ""):
            raise AssertionError(f"invalid direct step child: {line}")
        key, value = match.group(1), match.group(2) or ""
        if key in entries:
            raise AssertionError(f"duplicate direct step key: {key}")
        entries[key] = value
    return entries


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ci = CI_PATH.read_text(encoding="utf-8")
        cls.pages = PAGES_PATH.read_text(encoding="utf-8")

    def assert_steps_closed(
        self,
        job: str,
        expected: tuple[tuple[str, frozenset[str]], ...],
    ) -> dict[str, str]:
        steps = workflow_steps(job)
        mappings = [step_mapping(step) for step in steps]
        self.assertEqual([mapping.get("name") for mapping in mappings], [name for name, _ in expected])
        for mapping, (name, fields) in zip(mappings, expected, strict=True):
            self.assertEqual(frozenset(mapping), fields, name)
            self.assertNotIn("if", mapping, name)
            self.assertNotIn("continue-on-error", mapping, name)
        return {mapping["name"]: step for mapping, step in zip(mappings, steps, strict=True)}

    def assert_ci_closed_shape(self, source: str) -> None:
        self.assertEqual(
            root_mapping(source),
            {"name": "CI", "on": "", "permissions": "", "jobs": ""},
        )
        self.assertEqual(simple_mapping(source, "on:", 0), {"pull_request": "", "push": ""})
        self.assertEqual(simple_mapping(block(source, "push:", 2), "push:", 2), {"branches": "[main]"})
        self.assertEqual(simple_mapping(source, "permissions:", 0), {"contents": "read"})

        website = block(source, "website:", 2)
        self.assertEqual(
            simple_mapping(website, "website:", 2),
            {"name": "Website", "runs-on": "ubuntu-latest", "steps": ""},
        )
        website_steps = self.assert_steps_closed(
            website,
            (
                ("Check out source commit", frozenset({"name", "uses", "with"})),
                ("Set up Node.js", frozenset({"name", "uses", "with"})),
                ("Python website tests", frozenset({"name", "run"})),
                ("Node website model tests", frozenset({"name", "run"})),
                ("Verify deterministic full builds", frozenset({"name", "run"})),
            ),
        )
        self.assertEqual(
            simple_mapping(website_steps["Check out source commit"], "with:", 8),
            {"clean": "true", "persist-credentials": "false"},
        )
        self.assertEqual(
            simple_mapping(website_steps["Set up Node.js"], "with:", 8),
            {"node-version-file": ".node-version"},
        )
        self.assertEqual(
            step_mapping(website_steps["Check out source commit"])["uses"],
            "actions/checkout@v7",
        )
        self.assertEqual(
            step_mapping(website_steps["Set up Node.js"])["uses"],
            "actions/setup-node@v7",
        )
        self.assertEqual(
            step_mapping(website_steps["Python website tests"])["run"],
            PYTHON_TEST_COMMAND,
        )
        self.assertEqual(
            step_mapping(website_steps["Node website model tests"])["run"],
            NODE_TEST_COMMAND,
        )
        self.assertEqual(
            literal_script(website, "Verify deterministic full builds"),
            CI_DETERMINISTIC_BUILD_SCRIPT,
        )

        ci_success = block(source, "ci-success:", 2)
        self.assertEqual(
            simple_mapping(ci_success, "ci-success:", 2),
            {
                "if": "always()",
                "needs": "[python-checks, test, lint-files, rulesync-drift, locked-sdds, website]",
                "runs-on": "ubuntu-latest",
                "steps": "",
            },
        )
        success_steps = self.assert_steps_closed(
            ci_success,
            (("Verify all required jobs passed", frozenset({"name", "env", "run"})),),
        )
        success_step = success_steps["Verify all required jobs passed"]
        self.assertEqual(
            simple_mapping(success_step, "env:", 8),
            {"REQUIRED_RESULTS": "${{ join(needs.*.result, ' ') }}"},
        )
        self.assertEqual(literal_script(ci_success, "Verify all required jobs passed"), CI_SUCCESS_SCRIPT)
        self.assertNotIn("pages:", source)
        self.assertNotIn("id-token:", source)
        self.assertNotIn("actions/deploy-pages", source)

    def assert_pages_closed_shape(self, source: str) -> None:
        self.assertEqual(
            root_mapping(source),
            {
                "name": "Deploy website to Pages",
                "on": "",
                "permissions": "",
                "concurrency": "",
                "jobs": "",
            },
        )
        self.assertEqual(simple_mapping(source, "on:", 0), {"push": ""})
        self.assertEqual(simple_mapping(block(source, "push:", 2), "push:", 2), {"branches": "[main]"})
        self.assertEqual(simple_mapping(source, "permissions:", 0), {"contents": "read"})
        self.assertEqual(
            simple_mapping(source, "concurrency:", 0),
            {"group": "pages-${{ github.repository }}", "cancel-in-progress": "false"},
        )
        self.assertEqual(simple_mapping(source, "jobs:", 0), {"build": "", "deploy": ""})

        build = block(source, "build:", 2)
        self.assertEqual(
            simple_mapping(build, "build:", 2),
            {
                "name": "Build Pages artifact",
                "runs-on": "ubuntu-latest",
                "permissions": "",
                "outputs": "",
                "steps": "",
            },
        )
        self.assertEqual(
            simple_mapping(build, "permissions:", 4),
            {"contents": "read", "pages": "read"},
        )
        self.assertEqual(
            simple_mapping(build, "outputs:", 4),
            {"source_sha": "${{ steps.source.outputs.sha }}"},
        )
        build_steps = self.assert_steps_closed(
            build,
            (
                ("Check out source commit", frozenset({"name", "uses", "with"})),
                ("Set up Node.js", frozenset({"name", "uses", "with"})),
                ("Python website tests", frozenset({"name", "run"})),
                ("Node website model tests", frozenset({"name", "run"})),
                ("Verify tested source state", frozenset({"name", "id", "env", "run"})),
                ("Configure GitHub Pages", frozenset({"name", "id", "uses"})),
                ("Normalize Pages base path", frozenset({"name", "id", "shell", "env", "run"})),
                ("Build deterministic Pages artifact", frozenset({"name", "env", "run"})),
                ("Verify upload source state", frozenset({"name", "env", "run"})),
                ("Upload exact Pages artifact", frozenset({"name", "uses", "with"})),
            ),
        )
        self.assertEqual(
            simple_mapping(build_steps["Check out source commit"], "with:", 8),
            {"clean": "true", "persist-credentials": "false"},
        )
        self.assertEqual(
            simple_mapping(build_steps["Set up Node.js"], "with:", 8),
            {"node-version-file": ".node-version"},
        )
        self.assertEqual(
            simple_mapping(build_steps["Verify tested source state"], "env:", 8),
            {"EXPECTED_SHA": "${{ github.sha }}"},
        )
        self.assertEqual(
            simple_mapping(build_steps["Normalize Pages base path"], "env:", 8),
            {"PAGES_BASE_PATH": "${{ steps.pages.outputs.base_path }}"},
        )
        self.assertEqual(
            simple_mapping(build_steps["Build deterministic Pages artifact"], "env:", 8),
            {"SITE_BASE": "${{ steps.base.outputs.site_base }}"},
        )
        self.assertEqual(
            simple_mapping(build_steps["Verify upload source state"], "env:", 8),
            {"EXPECTED_SHA": "${{ github.sha }}"},
        )
        self.assertEqual(
            simple_mapping(build_steps["Upload exact Pages artifact"], "with:", 8),
            {"name": "github-pages", "path": "${{ runner.temp }}/agentworks-site"},
        )
        self.assertEqual(
            {
                name: step_mapping(build_steps[name])["uses"]
                for name in (
                    "Check out source commit",
                    "Set up Node.js",
                    "Configure GitHub Pages",
                    "Upload exact Pages artifact",
                )
            },
            {
                "Check out source commit": "actions/checkout@v7",
                "Set up Node.js": "actions/setup-node@v7",
                "Configure GitHub Pages": "actions/configure-pages@v6",
                "Upload exact Pages artifact": "actions/upload-pages-artifact@v5",
            },
        )
        self.assertEqual(
            literal_script(build, "Verify tested source state"),
            TESTED_SOURCE_SCRIPT,
        )
        self.assertEqual(
            step_mapping(build_steps["Python website tests"])["run"],
            PYTHON_TEST_COMMAND,
        )
        self.assertEqual(
            step_mapping(build_steps["Node website model tests"])["run"],
            NODE_TEST_COMMAND,
        )
        self.assertEqual(
            literal_script(build, "Build deterministic Pages artifact"),
            PAGES_DETERMINISTIC_BUILD_SCRIPT,
        )
        self.assertEqual(
            literal_script(build, "Verify upload source state"),
            UPLOAD_SOURCE_SCRIPT,
        )

        deploy = block(source, "deploy:", 2)
        self.assertEqual(
            simple_mapping(deploy, "deploy:", 2),
            {
                "name": "Deploy Pages artifact",
                "if": "github.event_name == 'push' && github.ref == 'refs/heads/main' && "
                "needs.build.outputs.source_sha == github.sha",
                "needs": "build",
                "runs-on": "ubuntu-latest",
                "permissions": "",
                "environment": "",
                "steps": "",
            },
        )
        self.assertEqual(
            simple_mapping(deploy, "permissions:", 4),
            {"pages": "write", "id-token": "write"},
        )
        self.assertEqual(
            simple_mapping(deploy, "environment:", 4),
            {"name": "github-pages", "url": "${{ steps.deployment.outputs.page_url }}"},
        )
        deploy_steps = self.assert_steps_closed(
            deploy,
            (("Deploy source commit to GitHub Pages", frozenset({"name", "id", "uses", "with"})),),
        )
        self.assertEqual(
            simple_mapping(deploy_steps["Deploy source commit to GitHub Pages"], "with:", 8),
            {"artifact_name": "github-pages"},
        )
        self.assertEqual(
            step_mapping(deploy_steps["Deploy source commit to GitHub Pages"])["uses"],
            "actions/deploy-pages@v5",
        )

    def test_ci_runs_website_checks_for_prs_and_main_and_requires_them(self) -> None:
        self.assert_ci_closed_shape(self.ci)
        trigger = block(self.ci, "on:", 0)
        self.assertIn("pull_request:", trigger)
        self.assertIn("push:", trigger)
        self.assertIn("branches: [main]", trigger)
        self.assertNotRegex(trigger, r"(?m)^\s+paths(?:-ignore)?:")

        website = block(self.ci, "website:", 2)
        self.assertIn("actions/checkout@v7", website)
        self.assertIn("python3 -m unittest discover -s website/tests -p 'test_*.py'", website)
        self.assertIn(NODE_TEST_COMMAND, website)
        self.assertEqual(website.count("python3 website/build.py"), 4)
        self.assertIn("--site-base /", website)
        self.assertIn("--site-base /agentworks/", website)
        self.assertEqual(website.count("diff --recursive --no-dereference"), 2)
        checkout = step_with_action(website, "actions/checkout@v7")
        self.assertIn("clean: true", checkout)
        self.assertIn("persist-credentials: false", checkout)
        self.assertNotIn("    permissions:", website)
        self.assertNotIn("actions/deploy-pages", self.ci)

        ci_success = block(self.ci, "ci-success:", 2)
        needs = re.search(r"(?m)^    needs: \[([^]]+)]$", ci_success)
        self.assertIsNotNone(needs)
        self.assertIn("website", {name.strip() for name in needs.group(1).split(",")})
        self.assertEqual(simple_mapping(self.ci, "permissions:", 0), {"contents": "read"})
        self.assertNotIn("pages:", self.ci)
        self.assertNotIn("id-token:", self.ci)

    def test_pages_trigger_permissions_dependency_and_concurrency_are_closed(self) -> None:
        self.assert_pages_closed_shape(self.pages)
        trigger = block(self.pages, "on:", 0)
        self.assertEqual(trigger.strip(), "on:\n  push:\n    branches: [main]")
        self.assertNotRegex(trigger, r"(?m)^\s+paths(?:-ignore)?:")
        self.assertNotIn("pull_request", trigger)
        self.assertNotIn("workflow_dispatch", trigger)
        self.assertEqual(simple_mapping(self.pages, "permissions:", 0), {"contents": "read"})

        concurrency = block(self.pages, "concurrency:", 0)
        self.assertIn("group: pages-${{ github.repository }}", concurrency)
        self.assertIn("cancel-in-progress: false", concurrency)

        build = block(self.pages, "build:", 2)
        self.assertEqual(
            simple_mapping(build, "permissions:", 4),
            {"contents": "read", "pages": "read"},
        )
        self.assertNotIn("pages: write", build)
        self.assertNotIn("id-token: write", build)

        deploy = block(self.pages, "deploy:", 2)
        self.assertIn("needs: build", deploy)
        self.assertEqual(
            simple_mapping(deploy, "permissions:", 4),
            {"pages": "write", "id-token": "write"},
        )
        self.assertIn("name: github-pages", block(deploy, "environment:", 4))
        self.assertIn(
            "if: github.event_name == 'push' && github.ref == 'refs/heads/main' && "
            "needs.build.outputs.source_sha == github.sha",
            deploy,
        )

    def test_pages_uses_reviewed_action_majors_and_clean_checkout(self) -> None:
        actions = re.findall(r"(?m)^\s+(?:-\s+)?uses: (\S+)$", self.pages)
        self.assertEqual(
            actions,
            [
                "actions/checkout@v7",
                "actions/setup-node@v7",
                "actions/configure-pages@v6",
                "actions/upload-pages-artifact@v5",
                "actions/deploy-pages@v5",
            ],
        )
        checkout = step_with_action(block(self.pages, "build:", 2), "actions/checkout@v7")
        self.assertIn("clean: true", checkout)
        self.assertIn("persist-credentials: false", checkout)

    def test_pages_build_tests_determinism_and_exact_artifact_boundary(self) -> None:
        build = block(self.pages, "build:", 2)
        self.assertIn("python3 -m unittest discover -s website/tests -p 'test_*.py'", build)
        self.assertIn(NODE_TEST_COMMAND, build)
        self.assertEqual(build.count("python3 website/build.py"), 2)
        self.assertIn('"${RUNNER_TEMP}/agentworks-site"', build)
        self.assertIn('"${RUNNER_TEMP}/agentworks-site-repeat"', build)
        self.assertIn("diff --recursive --no-dereference", build)

        upload = step_with_action(build, "actions/upload-pages-artifact@v5")
        self.assertIn("name: github-pages", upload)
        self.assertIn("path: ${{ runner.temp }}/agentworks-site", upload)
        self.assertNotIn("agentworks-site-repeat", upload)
        self.assertNotRegex(upload, r"(?m)^\s+path:\s*[.'\"]+\s*$")

    def test_pages_base_normalization_accepts_only_builder_grammar(self) -> None:
        build = block(self.pages, "build:", 2)
        self.assertIn("id: pages\n        uses: actions/configure-pages@v6", build)
        self.assertIn("PAGES_BASE_PATH: ${{ steps.pages.outputs.base_path }}", build)
        script = literal_script(build, "Normalize Pages base path")
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
        for raw, expected in accepted.items():
            with self.subTest(raw=raw):
                self.assertEqual(self._run_normalizer(script, raw), expected)
        for raw in rejected:
            with self.subTest(raw=raw):
                self.assertIsNone(self._run_normalizer(script, raw))

    def test_deploy_is_tied_to_the_checked_out_event_commit(self) -> None:
        build = block(self.pages, "build:", 2)
        deploy = block(self.pages, "deploy:", 2)
        self.assertIn("EXPECTED_SHA: ${{ github.sha }}", build)
        self.assertIn('checked_out_sha="$(git rev-parse HEAD)"', build)
        self.assertIn("source_sha: ${{ steps.source.outputs.sha }}", build)
        self.assertIn("needs.build.outputs.source_sha == github.sha", deploy)
        deployment = step_with_action(deploy, "actions/deploy-pages@v5")
        self.assertIn("artifact_name: github-pages", deployment)

    def test_ci_success_rejects_skipped_and_every_other_non_success_result(self) -> None:
        script = literal_script(block(self.ci, "ci-success:", 2), "Verify all required jobs passed")
        success = " ".join(["success"] * 6)
        self.assertEqual(self._run_bash(script, {"REQUIRED_RESULTS": success}).returncode, 0)
        for result in ("failure", "cancelled", "skipped", "neutral", ""):
            values = ["success"] * 6
            values[3] = result
            with self.subTest(result=result):
                self.assertNotEqual(
                    self._run_bash(script, {"REQUIRED_RESULTS": " ".join(values)}).returncode,
                    0,
                )

    def test_source_integrity_checks_reject_head_tracked_and_untracked_drift(self) -> None:
        build = block(self.pages, "build:", 2)
        tested_script = literal_script(build, "Verify tested source state")
        upload_script = literal_script(build, "Verify upload source state")
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            readme = repository / "README.md"
            readme.write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
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
                cwd=repository,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            github_output = Path(temporary) / "github-output"
            clean = self._run_bash(
                tested_script,
                {"EXPECTED_SHA": head, "GITHUB_OUTPUT": str(github_output)},
                cwd=repository,
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertEqual(github_output.read_text(encoding="utf-8"), f"sha={head}\n")

            readme.write_text("mutated after verification\n", encoding="utf-8")
            self.assertNotEqual(
                self._run_bash(upload_script, {"EXPECTED_SHA": head}, cwd=repository).returncode,
                0,
            )
            readme.write_text("clean\n", encoding="utf-8")
            (repository / "untracked.txt").write_text("drift\n", encoding="utf-8")
            self.assertNotEqual(
                self._run_bash(upload_script, {"EXPECTED_SHA": head}, cwd=repository).returncode,
                0,
            )
            (repository / "untracked.txt").unlink()
            self.assertNotEqual(
                self._run_bash(upload_script, {"EXPECTED_SHA": "0" * 40}, cwd=repository).returncode,
                0,
            )

    def test_closed_shapes_reject_control_permission_comment_and_step_bypasses(self) -> None:
        pages_mutations = (
            self.pages.replace(
                "\njobs:\n",
                "\ndefaults:\n  run:\n    shell: bash {0}\n\njobs:\n",
                1,
            ),
            self.pages.replace(
                "\njobs:\n",
                "\ndefaults:\n  run:\n    working-directory: /tmp\n\njobs:\n",
                1,
            ),
            self.pages.replace(
                "\njobs:\n",
                "\nenv:\n  BASH_ENV: /tmp/neutralize-checks\n\njobs:\n",
                1,
            ),
            self.pages.replace(
                "name: Deploy website to Pages\n",
                "name: Deploy website to Pages\nname: Shadow Pages workflow\n",
                1,
            ),
            self.pages.replace(
                "name: Deploy website to Pages\n",
                'name: Deploy website to Pages\n"defaults": {}\n',
                1,
            ),
            self.pages.replace("  build:\n", "  build:\n    if: false\n", 1),
            self.pages.replace(
                "      - name: Upload exact Pages artifact\n",
                "      - name: Upload exact Pages artifact\n        if: false\n",
                1,
            ),
            self.pages.replace(
                "      - name: Python website tests\n",
                "      - name: Python website tests\n        continue-on-error: true\n",
                1,
            ),
            self.pages.replace(
                f"        run: {PYTHON_TEST_COMMAND}\n",
                f'        run: echo "{PYTHON_TEST_COMMAND}"\n',
                1,
            ),
            self.pages.replace(
                f"        run: {NODE_TEST_COMMAND}\n",
                f'        run: echo "{NODE_TEST_COMMAND}"\n',
                1,
            ),
            self.pages.replace(
                '            "${RUNNER_TEMP}/agentworks-site-repeat"\n',
                '            "${RUNNER_TEMP}/agentworks-site-repeat" || true\n',
                1,
            ),
            self.pages.replace("      pages: read\n", "      pages: read # trusted\n", 1),
            self.pages.replace("      contents: read\n", "      contents: write\n", 1),
            self.pages.replace("      pages: read\n", "      pages: read\n      pages: write\n", 1),
            self.pages.replace("      pages: write\n", "      # pages: write\n", 1),
            self.pages.replace(
                "          path: ${{ runner.temp }}/agentworks-site\n",
                "          # path: ${{ runner.temp }}/agentworks-site\n          path: .\n",
                1,
            ),
            self.pages.replace(
                "      - name: Configure GitHub Pages\n",
                "      - name: Mutate tracked source\n        run: printf 'drift\\n' >> README.md\n\n"
                "      - name: Configure GitHub Pages\n",
                1,
            ),
            self.pages.replace(
                "    steps:\n      - name: Deploy source commit to GitHub Pages\n",
                "    steps:\n      - name: Extra privileged shell\n        run: echo unsafe\n\n"
                "      - name: Deploy source commit to GitHub Pages\n",
                1,
            ),
            self.pages.replace(
                "    steps:\n      - name: Deploy source commit to GitHub Pages\n",
                "    steps:\n      - name: Extra privileged action\n        uses: actions/checkout@v7\n\n"
                "      - name: Deploy source commit to GitHub Pages\n",
                1,
            ),
        )
        for changed in pages_mutations:
            self.assertNotEqual(changed, self.pages)
            with self.subTest(change=changed[:160]), self.assertRaises(AssertionError):
                self.assert_pages_closed_shape(changed)

        ci_mutations = (
            self.ci.replace(
                "\njobs:\n",
                "\ndefaults:\n  run:\n    shell: bash {0}\n\njobs:\n",
                1,
            ),
            self.ci.replace(
                "\njobs:\n",
                "\ndefaults:\n  run:\n    working-directory: /tmp\n\njobs:\n",
                1,
            ),
            self.ci.replace(
                "\njobs:\n",
                "\nenv:\n  BASH_ENV: /tmp/neutralize-checks\n\njobs:\n",
                1,
            ),
            self.ci.replace("name: CI\n", "name: CI\nname: Shadow CI\n", 1),
            self.ci.replace("name: CI\n", 'name: CI\n"defaults": {}\n', 1),
            self.ci.replace("  website:\n", "  website:\n    if: false\n", 1),
            self.ci.replace(
                "      - name: Python website tests\n",
                "      - name: Python website tests\n        continue-on-error: true\n",
                1,
            ),
            self.ci.replace(
                f"        run: {PYTHON_TEST_COMMAND}\n",
                f'        run: echo "{PYTHON_TEST_COMMAND}"\n',
                1,
            ),
            self.ci.replace(
                f"        run: {NODE_TEST_COMMAND}\n",
                f'        run: echo "{NODE_TEST_COMMAND}"\n',
                1,
            ),
            self.ci.replace(
                '          diff --recursive --no-dereference "${RUNNER_TEMP}/site-root-a" '
                '"${RUNNER_TEMP}/site-root-b"\n',
                '          diff --recursive --no-dereference "${RUNNER_TEMP}/site-root-a" '
                '"${RUNNER_TEMP}/site-root-b" || true\n',
                1,
            ),
            self.ci.replace(
                '          diff --recursive --no-dereference "${RUNNER_TEMP}/site-project-a" '
                '"${RUNNER_TEMP}/site-project-b"\n',
                '          diff --recursive --no-dereference "${RUNNER_TEMP}/site-project-a" '
                '"${RUNNER_TEMP}/site-project-b" || true\n',
                1,
            ),
            self.ci.replace(
                "          REQUIRED_RESULTS: ${{ join(needs.*.result, ' ') }}\n",
                "          # REQUIRED_RESULTS: ${{ join(needs.*.result, ' ') }}\n"
                "          REQUIRED_RESULTS: success success success success success success\n",
                1,
            ),
        )
        for changed in ci_mutations:
            self.assertNotEqual(changed, self.ci)
            with self.subTest(change=changed[-160:]), self.assertRaises(AssertionError):
                self.assert_ci_closed_shape(changed)

    def _run_normalizer(self, script: str, raw: str) -> str | None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            environment = os.environ.copy()
            environment.update({"PAGES_BASE_PATH": raw, "GITHUB_OUTPUT": str(output)})
            result = self._run_bash(script, environment)
            if result.returncode != 0:
                return None
            values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            return values["site_base"]

    def _run_bash(
        self,
        script: str,
        environment: dict[str, str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged_environment = os.environ.copy()
        merged_environment.update(environment)
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            env=merged_environment,
            timeout=5,
        )


if __name__ == "__main__":
    unittest.main()
