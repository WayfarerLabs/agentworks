from __future__ import annotations

import contextlib
import importlib.util
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
WEBSITE = REPO_ROOT / "website"
BUILD_PATH = WEBSITE / "build.py"
SPEC = importlib.util.spec_from_file_location("site_builder", BUILD_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load website builder")
site_builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(site_builder)

NOTICE = (
    "Guided onboarding is not yet published. You can still explore the repository, PyPI package, "
    "rationale, and security model."
)
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
    "connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)


def relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.start_tags: list[tuple[str, dict[str, str | None]]] = []
        self.end_tags: list[str] = []
        self.ids: list[str] = []
        self.headings: list[str] = []
        self._heading: list[str] | None = None
        self._id_stack: list[str] = []
        self.text_by_id: dict[str, str] = {}
        self.all_text: list[str] = []
        self._element_stack: list[tuple[str, dict[str, str | None]]] = []
        self.elements: list[tuple[str, dict[str, str | None], tuple[tuple[str, dict[str, str | None]], ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.start_tags.append((tag, attributes))
        self.elements.append((tag, attributes, tuple(self._element_stack)))
        self._element_stack.append((tag, attributes))
        element_id = attributes.get("id") or ""
        self._id_stack.append(element_id)
        if element_id:
            self.ids.append(element_id)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self.end_tags.append(tag)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._heading is not None:
            self.headings.append(" ".join("".join(self._heading).split()))
            self._heading = None
        if self._id_stack:
            self._id_stack.pop()
        for index in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[index][0] == tag:
                del self._element_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.all_text.append(data)
        if self._heading is not None:
            self._heading.append(data)
        for element_id in reversed(self._id_stack):
            if element_id:
                self.text_by_id[element_id] = self.text_by_id.get(element_id, "") + data
                break

    def tags(self, name: str) -> list[dict[str, str | None]]:
        return [attributes for tag, attributes in self.start_tags if tag == name]


def parse(source: str) -> Document:
    document = Document()
    document.feed(source)
    return document


def snapshot(path: Path) -> dict[str, bytes]:
    return {item.relative_to(path).as_posix(): item.read_bytes() for item in path.rglob("*") if item.is_file()}


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        shutil.copy2(REPO_ROOT / "README.md", self.root / "README.md")
        shutil.copy2(REPO_ROOT / "SECURITY.md", self.root / "SECURITY.md")
        (self.root / "docs").mkdir()
        shutil.copy2(REPO_ROOT / "docs/why-agentworks.md", self.root / "docs/why-agentworks.md")
        shutil.copytree(WEBSITE / "templates", self.root / "website/templates")
        shutil.copytree(WEBSITE / "assets", self.root / "website/assets")
        shutil.copytree(WEBSITE / "static", self.root / "website/static")

    def build(self, site_base: str = "/", *, focused: bool = False) -> Path:
        output = Path(self.temporary.name) / ("focused" if focused else "site")
        site_builder.build_site(self.root, output, site_base, focused=focused)
        return output


class SourceContractTests(RepositoryFixture):
    def test_every_contract_extracts_from_permanent_sources(self) -> None:
        content = site_builder.extract_content(self.root)
        expected = {contract.contract_id for contract in site_builder.CONTRACTS}
        expected.update({"HOME_META_DESCRIPTION", "SECURITY_META_DESCRIPTION"})
        self.assertEqual(set(content), expected)
        self.assertIn("<strong>Durable agents</strong>", content["HOME_IDENTITY"])
        self.assertIn("<em>when</em>", content["SECURITY_BOUNDARIES"])
        self.assertIn(site_builder.CLI_SECRETS_URL, content["SECURITY_SECRETS"])
        self.assertIn(site_builder.REPORTING_URL, content["SECURITY_REPORTING"])

    def test_crlf_and_unrelated_reordering_do_not_break_selection(self) -> None:
        readme = self.root / "README.md"
        readme.write_bytes(readme.read_text(encoding="utf-8").replace("\n", "\r\n").encode())
        rationale = self.root / "docs/why-agentworks.md"
        source = rationale.read_text(encoding="utf-8")
        source = source.replace("# Why Agentworks\n", "# Why Agentworks\n\n## Unrelated\n\nStable.\n")
        rationale.write_text(source, encoding="utf-8")
        self.assertIn("HOME_PROBLEM", site_builder.extract_content(self.root))

    def test_heading_shaped_fenced_canary_is_ignored_and_unclosed_fence_fails(self) -> None:
        rationale = self.root / "docs/why-agentworks.md"
        source = rationale.read_text(encoding="utf-8")
        canary = "```sh\n# Why Agentworks\n## The Problem Space\n### Workload Management\n```\n\n"
        rationale.write_text(canary + source, encoding="utf-8")
        self.assertIn("HOME_PROBLEM", site_builder.extract_content(self.root))
        rationale.write_text("```sh\n# hidden\n" + source, encoding="utf-8")
        with self.assertRaisesRegex(site_builder.ContractError, "unclosed fence"):
            site_builder.extract_content(self.root)

    def test_missing_duplicate_and_drifted_content_fail_with_contract_context(self) -> None:
        readme = self.root / "README.md"
        original = readme.read_text(encoding="utf-8")
        cases = (
            (original.replace("# Agentworks", "# Different", 1), "missing heading"),
            (original + "\n# Agentworks\n", "duplicate heading"),
            (original.replace("A comprehensive toolkit", "A partial toolkit", 1), "content drift"),
        )
        for changed, reason in cases:
            with self.subTest(reason=reason):
                readme.write_text(changed, encoding="utf-8")
                with self.assertRaisesRegex(site_builder.ContractError, rf"HOME_IDENTITY README.md .*{reason}"):
                    site_builder.extract_content(self.root)
                readme.write_text(original, encoding="utf-8")

    def test_duplicate_expected_sequence_within_one_section_fails(self) -> None:
        rationale = self.root / "docs/why-agentworks.md"
        source = rationale.read_text(encoding="utf-8")
        before, remainder = source.split("### Workload Management\n", 1)
        body, after = remainder.split("### Consistency", 1)
        rationale.write_text(
            before + "### Workload Management\n" + body + body + "### Consistency" + after,
            encoding="utf-8",
        )
        with self.assertRaisesRegex(site_builder.ContractError, "duplicate expected block sequence"):
            site_builder.extract_content(self.root)

    def test_invalid_utf8_bom_and_missing_input_fail_closed(self) -> None:
        policy = self.root / "SECURITY.md"
        original = policy.read_bytes()
        for value in (b"\xef\xbb\xbf" + original, b"\xff"):
            with self.subTest(value=value[:3]):
                policy.write_bytes(value)
                with self.assertRaisesRegex(site_builder.ContractError, "invalid UTF-8"):
                    site_builder.extract_content(self.root)
                policy.write_bytes(original)
        policy.unlink()
        with self.assertRaisesRegex(site_builder.ContractError, "missing/unreadable input"):
            site_builder.extract_content(self.root)

    def test_reporting_definition_and_reference_are_exact(self) -> None:
        policy = self.root / "SECURITY.md"
        original = policy.read_text(encoding="utf-8")
        changes = (
            original.replace(site_builder.REPORTING_URL, "https://example.com/private"),
            original.replace("[gh-private]:", "[renamed]:"),
            original + f"\n[gh-private]: {site_builder.REPORTING_URL}\n",
            original.replace("[private vulnerability reporting][gh-private]", "private reporting"),
        )
        for changed in changes:
            with self.subTest(change=changed[-80:]):
                policy.write_text(changed, encoding="utf-8")
                with self.assertRaisesRegex(
                    site_builder.ContractError, "reference definition|reporting-link drift|content drift"
                ):
                    site_builder.extract_content(self.root)
        policy.write_text(original, encoding="utf-8")

    def test_reporting_reference_scan_ignores_code_and_rejects_unclosed_fences(self) -> None:
        policy = self.root / "SECURITY.md"
        original = policy.read_text(encoding="utf-8")
        canaries = (
            "```text\n[gh-private]: https://example.com/backtick\n```\n\n",
            "~~~text\n[gh-private]: https://example.com/tilde\n~~~\n\n",
            "    [gh-private]: https://example.com/indented\n\n",
        )
        policy.write_text("".join(canaries) + original, encoding="utf-8")
        self.assertIn("SECURITY_REPORTING", site_builder.extract_content(self.root))
        policy.write_text("```text\n[gh-private]: https://example.com/unclosed\n" + original, encoding="utf-8")
        with self.assertRaisesRegex(site_builder.ContractError, "unclosed fence"):
            site_builder.extract_content(self.root)

    def test_inline_renderer_escapes_text_and_rejects_unsupported_markdown(self) -> None:
        contract = site_builder.CONTRACTS[0]
        rendered = site_builder._render_inline('plain & "quoted" plus `<tag attr="value">`', contract, {})
        self.assertEqual(
            rendered,
            "plain &amp; &quot;quoted&quot; plus <code>&lt;tag attr=&quot;value&quot;&gt;</code>",
        )
        unsupported = ("<b>html</b>", "> quote", "![image](https://example.com/a.png)", "[bad](http://example.com)")
        for value in unsupported:
            with self.subTest(value=value), self.assertRaises(site_builder.ContractError):
                site_builder._render_inline(value, contract, {})


class TemplateContractTests(RepositoryFixture):
    def test_each_template_has_only_its_closed_vocabulary(self) -> None:
        for name, expected in site_builder.TEMPLATE_TOKENS.items():
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            self.assertEqual(set(site_builder.TOKEN_PATTERN.findall(template)), expected)
            site_builder._validate_template(name, template)

    def test_unknown_missing_duplicate_wrong_template_and_brace_tokens_fail(self) -> None:
        path = self.root / "website/templates/index.html"
        template = path.read_text(encoding="utf-8")
        variants = (
            template + "{{UNKNOWN}}",
            template.replace("{{HOME_IDENTITY}}", ""),
            template.replace("{{HOME_IDENTITY}}", "{{HOME_IDENTITY}}{{HOME_IDENTITY}}"),
            template.replace("{{HOME_IDENTITY}}", "{{SECURITY_THREATS}}"),
            template + "{{not-a-token}}",
            template.replace('href="{{SITE_BASE}}"', 'data-base="{{SITE_BASE}}"', 1),
        )
        for changed in variants:
            with self.subTest(changed=changed[-40:]), self.assertRaises(ValueError):
                site_builder._validate_template("index.html", changed)

    def test_extracted_content_cannot_expand_a_template_token(self) -> None:
        template = (self.root / "website/templates/index.html").read_text(encoding="utf-8")
        substitutions = site_builder.extract_content(self.root)
        for injection in (
            "&lt;script&gt;{{ATTACK}}&lt;/script&gt;",
            "{{HOME_PROBLEM}}",
        ):
            with self.subTest(injection=injection):
                substitutions["HOME_IDENTITY"] = injection
                with self.assertRaisesRegex(ValueError, "brace-like token syntax"):
                    site_builder.render_named_template("index.html", template, "/", substitutions)

    def test_content_tokens_cannot_move_to_hostile_or_unreviewed_contexts(self) -> None:
        path = self.root / "website/templates/index.html"
        template = path.read_text(encoding="utf-8")
        identity = '<div class="sourced-content">{{HOME_IDENTITY}}</div>'
        metadata = 'content="{{HOME_META_DESCRIPTION}}"'
        variants = (
            template.replace(metadata, 'content="safe" data-copy="{{HOME_META_DESCRIPTION}}"'),
            template.replace(identity, "<script>{{HOME_IDENTITY}}</script>"),
            template.replace(identity, "<style>{{HOME_IDENTITY}}</style>"),
            template.replace(identity, '<div class="unreviewed">{{HOME_IDENTITY}}</div>'),
            template.replace(identity, '<div class="sourced-content">prefix {{HOME_IDENTITY}}</div>'),
            template.replace(
                identity,
                '</section><section id="unreviewed"><div class="sourced-content">{{HOME_IDENTITY}}</div>',
            ),
        )
        for changed in variants:
            with (
                self.subTest(changed=changed[changed.find("{{HOME_") - 30 : changed.find("{{HOME_") + 50]),
                self.assertRaisesRegex(ValueError, "content token|metadata token|sourced-content|reviewed section"),
            ):
                site_builder._validate_template("index.html", changed)

    def test_interim_notice_section_and_heading_relationship_are_guarded(self) -> None:
        template = (self.root / "website/templates/index.html").read_text(encoding="utf-8")
        variants = (
            template.replace("</body>", f"<p>{NOTICE}</p></body>"),
            template.replace('id="onboarding"', 'id="onboarding-moved"'),
            template.replace('aria-labelledby="onboarding-heading"', 'aria-labelledby="other"', 1),
            template.replace('<h2 id="onboarding-heading">', '<h2 id="other">'),
        )
        for changed in variants:
            with self.subTest(change=changed[-100:]), self.assertRaisesRegex(ValueError, "onboarding|notice"):
                site_builder._validate_template("index.html", changed)

    def test_reviewed_destination_and_reporting_literals_cannot_drift(self) -> None:
        for name, old, new in (
            ("index.html", site_builder.RATIONALE_URL, "https://example.com/rationale"),
            ("security.html", site_builder.REPORTING_URL, "https://example.com/report"),
            (
                "security.html",
                "https://github.com/WayfarerLabs/agentworks/security/policy",
                "https://example.com/policy",
            ),
        ):
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            with self.subTest(name=name, old=old), self.assertRaisesRegex(ValueError, "required reviewed literals"):
                site_builder._validate_template(name, template.replace(old, new))


class BuildAndInstallTests(RepositoryFixture):
    def test_full_and_focused_dual_base_builds_have_exact_manifests(self) -> None:
        for focused, manifest in (
            (False, site_builder.FULL_MANIFEST),
            (True, site_builder.FOCUSED_MANIFEST),
        ):
            for site_base in ("/", "/agentworks/"):
                with self.subTest(focused=focused, site_base=site_base):
                    output = Path(self.temporary.name) / f"build-{focused}-{site_base.count('agentworks')}"
                    site_builder.build_site(self.root, output, site_base, focused=focused)
                    self.assertEqual({Path(path) for path in snapshot(output)}, manifest)
                    for path in output.rglob("*"):
                        self.assertFalse(path.is_symlink())
                    page = (output / "404.html").read_text(encoding="utf-8")
                    self.assertIn(f'href="{site_base}"', page)
                    self.assertNotIn("{{", page)

    def test_repeated_builds_are_byte_deterministic(self) -> None:
        first = self.build("/agentworks/")
        before = snapshot(first)
        site_builder.build_site(self.root, first, "/agentworks/")
        self.assertEqual(snapshot(first), before)
        second = Path(self.temporary.name) / "second"
        site_builder.build_site(self.root, second, "/agentworks/")
        self.assertEqual(snapshot(second), before)

    def test_unapproved_external_url_fails_before_output_changes(self) -> None:
        output = self.build()
        before = snapshot(output)
        template = self.root / "website/templates/index.html"
        source = template.read_text(encoding="utf-8")
        template.write_text(
            source.replace("</main>", '<a href="https://example.com/unapproved">Unapproved</a>\n</main>'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "unapproved external URL"):
            site_builder.build_site(self.root, output, "/agentworks/")
        self.assertEqual(snapshot(output), before)

    def test_same_document_fragment_must_resolve_before_output_changes(self) -> None:
        output = self.build()
        before = snapshot(output)
        template = self.root / "website/templates/404.html"
        source = template.read_text(encoding="utf-8")
        template.write_text(source.replace('id="main-content"', 'id="renamed-main"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "same-document fragment"):
            site_builder.build_site(self.root, output, "/")
        self.assertEqual(snapshot(output), before)

    def test_output_inside_repository_is_rejected_without_source_writes(self) -> None:
        before = snapshot(self.root / "website")
        targets = (self.root, self.root / "website", self.root / "website/generated")
        for target in targets:
            with self.subTest(target=target), self.assertRaises(ValueError):
                site_builder.build_site(self.root, target, "/")
        self.assertEqual(snapshot(self.root / "website"), before)

    def test_output_accepts_ordinary_spaced_and_hidden_directory_names(self) -> None:
        for name in ("site output", ".site-output"):
            output = Path(self.temporary.name) / name
            with self.subTest(name=name):
                site_builder.build_site(self.root, output, "/")
                self.assertEqual({Path(path) for path in snapshot(output)}, site_builder.FULL_MANIFEST)

    def test_output_rejects_dot_traversal(self) -> None:
        target = Path(self.temporary.name) / "parent" / ".." / "escaped"
        with self.assertRaisesRegex(ValueError, "dot traversal"):
            site_builder.build_site(self.root, target, "/")
        self.assertFalse(target.exists())

    def test_output_rejects_parent_symlink_into_repository_when_supported(self) -> None:
        linked_parent = Path(self.temporary.name) / "linked-parent"
        try:
            linked_parent.symlink_to(self.root, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "repository"):
            site_builder.build_site(self.root, linked_parent / "generated", "/")
        self.assertFalse((self.root / "generated").exists())

    def test_existing_top_level_directory_symlink_is_rejected_without_touching_target(self) -> None:
        target = Path(self.temporary.name) / "external-target"
        target.mkdir()
        sentinel = target / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        output = Path(self.temporary.name) / "published"
        try:
            output.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "real directory"):
            site_builder.build_site(self.root, output, "/")
        self.assertTrue(output.is_symlink())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_existing_top_level_broken_symlink_is_rejected_without_replacement(self) -> None:
        missing_target = Path(self.temporary.name) / "missing-target"
        output = Path(self.temporary.name) / "published-broken"
        try:
            output.symlink_to(missing_target, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "real directory"):
            site_builder.build_site(self.root, output, "/")
        self.assertTrue(output.is_symlink())
        self.assertFalse(missing_target.exists())

    def test_existing_output_may_contain_only_selected_owned_entries(self) -> None:
        output = self.build()
        (output / "index.html").unlink()
        site_builder.build_site(self.root, output, "/")
        (output / "unknown.txt").write_text("preserve", encoding="utf-8")
        before = snapshot(output)
        with self.assertRaisesRegex(ValueError, "not owned"):
            site_builder.build_site(self.root, output, "/")
        self.assertEqual(snapshot(output), before)

    def test_content_or_template_failure_leaves_existing_output_byte_identical(self) -> None:
        output = self.build()
        before = snapshot(output)
        readme = self.root / "README.md"
        original_readme = readme.read_text(encoding="utf-8")
        readme.write_text(
            original_readme.replace("A comprehensive toolkit", "A drifting toolkit", 1),
            encoding="utf-8",
        )
        with self.assertRaises(site_builder.ContractError):
            site_builder.build_site(self.root, output, "/agentworks/")
        self.assertEqual(snapshot(output), before)
        readme.write_text(original_readme, encoding="utf-8")

        template = self.root / "website/templates/index.html"
        template.write_text(template.read_text(encoding="utf-8") + "{{UNKNOWN}}", encoding="utf-8")
        with self.assertRaises(ValueError):
            site_builder.build_site(self.root, output, "/agentworks/")
        self.assertEqual(snapshot(output), before)

    def test_symlink_entry_in_existing_output_is_rejected_when_supported(self) -> None:
        output = Path(self.temporary.name) / "unsafe"
        output.mkdir()
        try:
            (output / "static").symlink_to(self.root / "website/static", target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "symlink"):
            site_builder.build_site(self.root, output, "/")

    def test_fifo_entry_in_existing_output_is_rejected_when_supported(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation unavailable")
        output = Path(self.temporary.name) / "unsafe"
        output.mkdir()
        try:
            os.mkfifo(output / "pipe")
        except OSError as error:
            self.skipTest(f"FIFO creation unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "special entry"):
            site_builder.build_site(self.root, output, "/")

    def test_non_directory_output_is_rejected(self) -> None:
        output = Path(self.temporary.name) / "unsafe"
        output.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "real directory"):
            site_builder.build_site(self.root, output, "/")

    def test_remote_runtime_asset_references_fail_before_output_changes(self) -> None:
        output = self.build()
        before = snapshot(output)
        mutations = (
            ("website/static/site.css", '\n@import "theme.css";\n'),
            ("website/static/site.css", "\nbody { background: url('/local.png'); }\n"),
            ("website/static/site.css", "\n/* https://example.com/theme.css */\n"),
            ("website/static/site.css", '\nbody { content: "//cdn.example.com/theme.css"; }\n'),
            ("website/static/lander-game.js", '\nconst remote = "https://example.com/game.js";\n'),
            ("website/static/lander-game.js", '\nconst remote = "//cdn.example.com/game.js";\n'),
        )
        for relative, addition in mutations:
            path = self.root / relative
            original = path.read_text(encoding="utf-8")
            with self.subTest(relative=relative, addition=addition.strip()):
                path.write_text(original + addition, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "CSS|JavaScript"):
                    site_builder.build_site(self.root, output, "/")
                self.assertEqual(snapshot(output), before)
                path.write_text(original, encoding="utf-8")

    def test_harmless_javascript_line_comment_is_allowed(self) -> None:
        script = self.root / "website/static/lander-game.js"
        script.write_text(script.read_text(encoding="utf-8") + "\n// harmless local note\n", encoding="utf-8")
        output = self.build()
        self.assertIn("// harmless local note", (output / "static/lander-game.js").read_text(encoding="utf-8"))

    def test_failure_before_and_during_each_swap_boundary_restores_exact_output(self) -> None:
        output = self.build()
        before = snapshot(output)
        real_replace = Path.replace
        for failing_call in (1, 2):
            calls = 0

            def injected_replace(path: Path, target: Path, failure_boundary: int = failing_call) -> Path:
                nonlocal calls
                calls += 1
                if calls == failure_boundary:
                    raise OSError(f"rename {failure_boundary} failed")
                return real_replace(path, target)

            with (
                self.subTest(failing_call=failing_call),
                mock.patch.object(Path, "replace", autospec=True, side_effect=injected_replace),
                self.assertRaisesRegex(OSError, "rename"),
            ):
                site_builder.build_site(self.root, output, "/agentworks/")
            self.assertEqual(snapshot(output), before)

        real_verify = site_builder._verify_manifest
        for failing_call in (1, 2):
            calls = 0

            def injected_verify(path: Path, manifest: frozenset[Path], failure_boundary: int = failing_call) -> None:
                nonlocal calls
                calls += 1
                if calls == failure_boundary:
                    raise RuntimeError(f"verification {failure_boundary} failed")
                real_verify(path, manifest)

            with (
                self.subTest(verification_boundary=failing_call),
                mock.patch.object(site_builder, "_verify_manifest", side_effect=injected_verify),
                self.assertRaisesRegex(RuntimeError, "verification"),
            ):
                site_builder.build_site(self.root, output, "/agentworks/")
            self.assertEqual(snapshot(output), before)

    def test_backup_cleanup_failure_warns_after_successful_commit(self) -> None:
        output = self.build()
        real_rmtree = shutil.rmtree

        def injected_rmtree(path: Path, *args: object, **kwargs: object) -> None:
            if ".backup-" in Path(path).name:
                raise OSError("cleanup failed")
            real_rmtree(path, *args, **kwargs)

        errors = io.StringIO()
        with (
            mock.patch.object(site_builder.shutil, "rmtree", side_effect=injected_rmtree),
            contextlib.redirect_stderr(errors),
        ):
            site_builder.build_site(self.root, output, "/agentworks/")
        self.assertIn("warning: installed output is valid; retained backup", errors.getvalue())
        self.assertIn('href="/agentworks/"', (output / "index.html").read_text(encoding="utf-8"))

    def test_cli_requires_full_shape_and_reports_one_line_errors(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(BUILD_PATH),
                "--repo-root",
                str(self.root),
                "--output",
                str(Path(self.temporary.name) / "cli"),
                "--site-base",
                "bad",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, r"^error: [^\n]+\n$")


class GeneratedDocumentTests(RepositoryFixture):
    @classmethod
    def forbidden_runtime_patterns(cls) -> tuple[str, ...]:
        return (
            r"\bfetch\s*\(",
            r"\bXMLHttpRequest\b",
            r"\bWebSocket\b",
            r"\bEventSource\b",
            r"\bsendBeacon\b",
            r"\bdocument\.cookie\b",
            r"\b(?:localStorage|sessionStorage|indexedDB|serviceWorker|caches)\b",
        )

    def setUp(self) -> None:
        super().setUp()
        self.output = self.build()
        self.pages = {
            "home": (self.output / "index.html").read_text(encoding="utf-8"),
            "security": (self.output / "security/index.html").read_text(encoding="utf-8"),
            "404": (self.output / "404.html").read_text(encoding="utf-8"),
        }
        self.documents = {name: parse(source) for name, source in self.pages.items()}

    def test_all_pages_have_metadata_landmarks_skip_link_and_one_h1(self) -> None:
        expected = {
            "home": ("Agentworks", "https://agentworks.build/"),
            "security": ("Security | Agentworks", "https://agentworks.build/security/"),
            "404": ("Page not found | Agentworks", "https://agentworks.build/404.html"),
        }
        for name, document in self.documents.items():
            with self.subTest(name=name):
                self.assertIn('<html lang="en">', self.pages[name])
                self.assertEqual(len(document.tags("header")), 1)
                self.assertEqual(len(document.tags("main")), 1)
                self.assertEqual(len(document.tags("footer")), 1)
                self.assertEqual(len(document.tags("h1")), 1)
                self.assertEqual(len(document.ids), len(set(document.ids)))
                self.assertEqual([tag for tag in document.tags("title") if "id" not in tag], [{}])
                self.assertIn(f"<title>{expected[name][0]}</title>", self.pages[name])
                canonical = [tag for tag in document.tags("link") if tag.get("rel") == "canonical"]
                self.assertEqual(canonical[0]["href"], expected[name][1])
                self.assertTrue(any(tag.get("name") == "description" for tag in document.tags("meta")))
                policies = [tag for tag in document.tags("meta") if tag.get("http-equiv") == "Content-Security-Policy"]
                self.assertEqual(policies[0]["content"], CSP)
                skip_links = [tag for tag in document.tags("a") if tag.get("class") == "skip-link"]
                self.assertEqual([tag.get("href") for tag in skip_links], ["#main-content"])

    def test_home_outline_links_and_interim_guards_are_exact(self) -> None:
        document = self.documents["home"]
        self.assertEqual(
            document.headings,
            ["Agentworks", "Guided onboarding", "The problem space", "Why it is built this way"],
        )
        notice = " ".join(document.text_by_id["onboarding-availability"].split())
        self.assertEqual(notice, NOTICE)
        document_text = " ".join("".join(document.all_text).split())
        self.assertEqual(document_text.count(NOTICE), 1)
        onboarding = [
            attributes
            for tag, attributes, _ in document.elements
            if tag == "section" and attributes.get("id") == "onboarding"
        ]
        self.assertEqual(len(onboarding), 1)
        self.assertEqual(onboarding[0].get("aria-labelledby"), "onboarding-heading")
        self.assertTrue(" ".join(document.text_by_id["onboarding"].split()))
        headings = [
            attributes
            for tag, attributes, ancestors in document.elements
            if tag == "h2"
            and attributes.get("id") == "onboarding-heading"
            and any(parent == "section" and attrs.get("id") == "onboarding" for parent, attrs in ancestors)
        ]
        self.assertEqual(len(headings), 1)
        self.assertEqual(self.pages["home"].count("We take security seriously."), 1)
        links = document.tags("a")
        self.assertIn("/security/", [link.get("href") for link in links])
        self.assertTrue(any(link.get("class") == "security-link" for link in links))
        forbidden = (
            "<pre",
            "clipboard",
            "copy",
            "bootstrap",
            "disabled",
            "uv tool install",
            "pipx install",
            "git clone",
            "agw config init",
            "preview mode",
            "release mode",
        )
        lowered = self.pages["home"].lower()
        for value in forbidden:
            self.assertNotIn(value, lowered)
        self.assertFalse(document.tags("script"))

    def test_security_outline_and_reporting_links_are_exact(self) -> None:
        document = self.documents["security"]
        self.assertEqual(
            document.headings,
            [
                "Security at Agentworks",
                "Threat model",
                "Boundaries and current limitations",
                "Operator posture",
                "Credentials and secrets",
                "Report a vulnerability",
            ],
        )
        hrefs = [anchor.get("href") for anchor in document.tags("a")]
        self.assertEqual(hrefs.count(site_builder.REPORTING_URL), 2)
        self.assertIn("https://github.com/WayfarerLabs/agentworks/security/policy", hrefs)
        self.assertFalse(document.tags("script"))

    def test_404_retains_fallback_and_has_only_its_local_module(self) -> None:
        document = self.documents["404"]
        self.assertIn("Page not found", self.pages["404"])
        home = next(tag for tag in document.tags("a") if tag.get("id") == "home-link")
        self.assertEqual(home["href"], "/")
        scripts = document.tags("script")
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0]["src"], "/static/lander-game.js")
        self.assertEqual(scripts[0]["type"], "module")

    def test_runtime_assets_are_local_and_privacy_surfaces_are_absent(self) -> None:
        for page in self.pages.values():
            document = parse(page)
            for tag_name, attribute in (("script", "src"), ("link", "href"), ("img", "src")):
                for tag in document.tags(tag_name):
                    value = tag.get(attribute)
                    if tag_name == "link" and tag.get("rel") == "canonical":
                        continue
                    self.assertFalse(value and value.startswith(("http://", "https://", "//")))
            for forbidden in ("form", "iframe", "audio", "canvas"):
                self.assertFalse(document.tags(forbidden))
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.output.rglob("*")
            if path.is_file() and path.suffix in {".js", ".css"}
        )
        for pattern in self.forbidden_runtime_patterns():
            self.assertIsNone(re.search(pattern, production))

    def test_shared_css_pins_tokens_reflow_focus_and_terminal_cues(self) -> None:
        css = (self.output / "static/site.css").read_text(encoding="utf-8")
        for token, value in {
            "--canvas": "#f5f2e8",
            "--panel": "#ebe7dc",
            "--ink": "#292b30",
            "--ink-muted": "#4b4e55",
            "--line-subtle": "#8a867c",
            "--accent": "#d94a1e",
            "--hot": "#ffe09a",
            "--status": "#7de2c5",
        }.items():
            self.assertIn(f"{token}: {value}", css)
        for contract in (
            "width: min(100%, 60rem)",
            "padding: clamp(1rem, 4vw, 3rem)",
            "min-width: 0",
            "overflow-wrap: anywhere",
            "outline: 3px solid var(--accent)",
            'ui-monospace, "Cascadia Code", "Liberation Mono", Menlo, monospace',
            "letter-spacing: 0.08em",
            "@media (min-width: 48rem)",
            "@media (prefers-reduced-motion: reduce)",
        ):
            self.assertIn(contract, css)
        lowered = css.lower()
        for fake_terminal in ("window-control", "crt", "green-on-black", "prompt-glyph"):
            self.assertNotIn(fake_terminal, lowered)

    def test_home_identity_grid_contains_its_scoped_heading_at_desktop_width(self) -> None:
        css = (self.output / "static/site.css").read_text(encoding="utf-8")
        heading_rule = css.split(".identity-panel h1 {", 1)[1].split("}", 1)[0]
        self.assertIn("max-width: 100%", heading_rule)
        self.assertIn("font-size: clamp(2.7rem, 6vw, 4.75rem)", heading_rule)
        default_identity = css.split(".identity-panel {", 1)[1].split("}", 1)[0]
        self.assertNotIn("grid-template-columns", default_identity)
        desktop = css.split("@media (min-width: 48rem)", 1)[1]
        identity_rule = desktop.split(".identity-panel {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", identity_rule)

    def test_pinned_color_contrasts_meet_text_component_and_status_thresholds(self) -> None:
        expected = (
            ("#292b30", "#f5f2e8", 12.646),
            ("#4b4e55", "#f5f2e8", 7.440),
            ("#8a867c", "#f5f2e8", 3.243),
            ("#d94a1e", "#f5f2e8", 3.789),
            ("#292b30", "#ebe7dc", 11.464),
            ("#ffe09a", "#292b30", 11.049),
            ("#7de2c5", "#20232a", 10.153),
        )
        for foreground, background, ratio in expected:
            with self.subTest(foreground=foreground, background=background):
                self.assertTrue(math.isclose(contrast(foreground, background), ratio, abs_tol=0.002))


if __name__ == "__main__":
    unittest.main()
