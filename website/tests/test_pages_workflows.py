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


def block(source: str, heading: str, indent: int) -> str:
    lines = source.splitlines()
    marker = f"{' ' * indent}{heading}"
    try:
        start = lines.index(marker)
    except ValueError as error:
        raise AssertionError(f"missing workflow block: {heading}") from error
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
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
    return "\n".join(script)


def simple_mapping(source: str, heading: str, indent: int) -> dict[str, str]:
    selected = block(source, heading, indent).splitlines()[1:]
    entries: dict[str, str] = {}
    for line in selected:
        match = re.fullmatch(rf"{' ' * (indent + 2)}([a-z-]+): ([a-z]+)", line)
        if match is not None:
            entries[match.group(1)] = match.group(2)
    return entries


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ci = CI_PATH.read_text(encoding="utf-8")
        cls.pages = PAGES_PATH.read_text(encoding="utf-8")

    def test_ci_runs_website_checks_for_prs_and_main_and_requires_them(self) -> None:
        trigger = block(self.ci, "on:", 0)
        self.assertIn("pull_request:", trigger)
        self.assertIn("push:", trigger)
        self.assertIn("branches: [main]", trigger)
        self.assertNotRegex(trigger, r"(?m)^\s+paths(?:-ignore)?:")

        website = block(self.ci, "website:", 2)
        self.assertIn("actions/checkout@v7", website)
        self.assertIn("python3 -m unittest discover -s website/tests -p 'test_*.py'", website)
        self.assertIn("node --test website/tests/lander-model.test.mjs", website)
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
        self.assertIn("node --test website/tests/lander-model.test.mjs", build)
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

    def _run_normalizer(self, script: str, raw: str) -> str | None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            environment = os.environ.copy()
            environment.update({"PAGES_BASE_PATH": raw, "GITHUB_OUTPUT": str(output)})
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            return values["site_base"]


if __name__ == "__main__":
    unittest.main()
