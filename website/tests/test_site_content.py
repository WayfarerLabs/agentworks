# ruff: noqa: F405

import html

from site_test_support import *  # noqa: F403


def write_assistance_projection(root: Path, source: bytes) -> None:
    source_path = root / site_builder.AGENT_ONBOARDING_PROMPT_SOURCE
    source_path.write_bytes(source)
    readme = root / "README.md"
    content = readme.read_bytes()
    begin_marker = site_builder.ASSISTANCE_README_BEGIN
    end_marker = site_builder.ASSISTANCE_README_END
    begin = content.index(begin_marker) + len(begin_marker)
    end = content.index(end_marker)
    longest = max((len(match.group()) for match in re.finditer(br"`+", source)), default=2)
    fence = b"`" * max(3, longest + 1)
    projection = b"\n\n" + fence + b"markdown\n" + source + fence + b"\n\n"
    readme.write_bytes(content[:begin] + projection + content[end:])


def synthetic_document(contract, body: str = "") -> str:  # noqa: ANN001
    reporting = ""
    definitions = ""
    if contract.github_only_reporting:
        reporting = "\n## Private reports\n\nUse the [private channel][gh-private].\n"
        definitions = f"\n[gh-private]:\n  {site_builder.REPORTING_URL}\n"
    return f"# Synthetic document\n\nOpening paragraph.{reporting}{body}{definitions}"


class SourceContractTests(RepositoryFixture):
    def test_permanent_sources_render_the_complete_content_vocabulary(self) -> None:
        content = site_builder.extract_content(self.root)
        self.assertEqual(
            set(content),
            {
                "HOME_IDENTITY",
                "HOME_META_DESCRIPTION",
                "ONBOARDING_PROMPT",
                "MANIFESTO_CONTENT",
                "MANIFESTO_META_DESCRIPTION",
                "SECURITY_CONTENT",
                "SECURITY_META_DESCRIPTION",
            },
        )
        self.assertIn("<strong>Durable agents</strong>", content["HOME_IDENTITY"])
        self.assertIn("<strong>SSH-over-Tailscale control plane</strong>", content["HOME_IDENTITY"])
        home_contract = site_builder.CONTRACTS[0]
        self.assertEqual(
            content["HOME_META_DESCRIPTION"],
            html.escape(str(home_contract.expected[1].value), quote=True),
        )
        self.assertEqual(content["ONBOARDING_PROMPT"], html.escape(ONBOARDING_PROMPT, quote=False))
        for contract in site_builder.DOCUMENT_CONTRACTS:
            rendered = content[f"{contract.contract_id}_CONTENT"]
            self.assertEqual(rendered.count("<h1 "), 1)
            self.assertTrue(content[f"{contract.contract_id}_META_DESCRIPTION"])
        self.assertIn(site_builder.REPORTING_URL, content["SECURITY_CONTENT"])

    def test_supported_source_additions_render_without_contract_edits(self) -> None:
        addition = (
            "\n   ## Added section\n\n"
            "A new paragraph with **strong text**, _underscore emphasis_, "
            "*asterisk emphasis*, and `code`.\n\n"
            "   - Hyphen item.\n"
            "   * Asterisk item.\n"
            "   + Plus item.\n"
        )
        for contract in site_builder.DOCUMENT_CONTRACTS:
            path = self.root / contract.source
            path.write_text(synthetic_document(contract, addition), encoding="utf-8")
            with self.subTest(contract=contract.contract_id):
                rendered = site_builder.extract_content(self.root)[f"{contract.contract_id}_CONTENT"]
                self.assertIn('<h2 id="added-section">Added section</h2>', rendered)
                self.assertIn(
                    "<p>A new paragraph with <strong>strong text</strong>, "
                    "<em>underscore emphasis</em>, <em>asterisk emphasis</em>, "
                    "and <code>code</code>.</p>",
                    rendered,
                )
                self.assertIn(
                    "<ul><li>Hyphen item.</li><li>Asterisk item.</li><li>Plus item.</li></ul>",
                    rendered,
                )

    def test_contents_navigation_derives_nested_h2_h3_links_and_escapes_labels(self) -> None:
        contract = site_builder.MANIFESTO_CONTRACT
        source = synthetic_document(
            contract,
            "\n## [Parent section](../README.md)\n\nParent paragraph.\n\n"
            "### Child & `detail`\n\nChild paragraph.\n\n"
            "## Final section\n\nFinal paragraph.\n",
        )
        path = self.root / contract.source
        path.write_text(source, encoding="utf-8")
        rendered = site_builder.extract_content(self.root)["MANIFESTO_CONTENT"]
        self.assertIn(
            '<nav class="page-toc" aria-label="On this page"><p class="page-toc-title">On this page</p>',
            rendered,
        )
        self.assertIn(
            '<li><a href="#parent-section">Parent section</a>'
            '<ol class="page-toc-sublist"><li><a href="#child-detail">'
            "Child &amp; detail</a></li></ol></li>",
            rendered,
        )
        self.assertIn('<li><a href="#final-section">Final section</a></li>', rendered)
        self.assertIn(
            f'<h2 id="parent-section"><a href="{site_builder.SOURCE_RELATIVE_URLS["../README.md"]}">'
            "Parent section</a></h2>",
            rendered,
        )
        self.assertLess(rendered.index("</h1>"), rendered.index('class="page-toc"'))
        self.assertLess(rendered.index('class="page-toc"'), rendered.index('<h2 id="parent-section">'))

    def test_document_without_h2_or_h3_omits_contents_navigation(self) -> None:
        contract = site_builder.MANIFESTO_CONTRACT
        path = self.root / contract.source
        path.write_text("# Root\n\nOpening paragraph.\n", encoding="utf-8")
        rendered = site_builder.extract_content(self.root)["MANIFESTO_CONTENT"]
        self.assertNotIn('class="page-toc"', rendered)

    def test_manifesto_source_path_is_single(self) -> None:
        self.assertEqual(site_builder.MANIFESTO_CONTRACT.source, Path("docs/manifesto.md"))
        production = (WEBSITE / "site_content.py").read_text(encoding="utf-8")
        self.assertNotIn("docs/why-agentworks.md", production)

    def test_crlf_does_not_break_complete_documents(self) -> None:
        paths = [contract.source for contract in site_builder.DOCUMENT_CONTRACTS]
        for relative in paths:
            path = self.root / relative
            path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode())
        content = site_builder.extract_content(self.root)
        self.assertIn("HOME_IDENTITY", content)
        for contract in site_builder.DOCUMENT_CONTRACTS:
            self.assertIn(f"{contract.contract_id}_CONTENT", content)

    def test_assistance_projection_is_exactly_canonical_and_html_escaped(self) -> None:
        source = b"# Bootstrap <agent> & helper\n\nRun `agw guide --agent`.\n"
        write_assistance_projection(self.root, source)
        self.assertEqual(
            site_builder.extract_assistance_prompt(self.root),
            source.decode("utf-8"),
        )
        self.assertEqual(
            site_builder.extract_content(self.root)["ONBOARDING_PROMPT"],
            "# Bootstrap &lt;agent&gt; &amp; helper\n\nRun `agw guide --agent`.\n",
        )
        document = parse((self.build() / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(document.text_by_id["onboarding-prompt"].encode(), source)

    def test_website_sources_do_not_duplicate_the_authored_prompt_body(self) -> None:
        template = (self.root / "website/templates/index.html").read_text(encoding="utf-8")
        self.assertNotIn(ONBOARDING_PROMPT, template)
        self.assertEqual(template.count("{{ONBOARDING_PROMPT}}"), 1)
        for path in WEBSITE.rglob("*"):
            if path.is_file() and "tests" not in path.parts:
                with self.subTest(path=path.relative_to(WEBSITE)):
                    self.assertNotIn(ONBOARDING_PROMPT.encode(), path.read_bytes())

    def test_assistance_projection_drift_and_framing_fail_closed(self) -> None:
        readme = self.root / "README.md"
        original = readme.read_bytes()
        begin_marker = site_builder.ASSISTANCE_README_BEGIN
        end_marker = site_builder.ASSISTANCE_README_END
        canonical_body = ONBOARDING_PROMPT.encode()
        variants = (
            original.replace(begin_marker, b"<!-- changed -->", 1),
            original.replace(end_marker, begin_marker, 1),
            original.replace(b"```markdown\n", b"```text\n", 1),
            original.replace(canonical_body, b" " + canonical_body, 1),
            original.replace(begin_marker, begin_marker + b"\n" + begin_marker, 1),
        )
        for changed in variants:
            readme.write_bytes(changed)
            with self.subTest(change=changed[:80]), self.assertRaisesRegex(
                ValueError,
                "projection|markers|fence",
            ):
                site_builder.extract_assistance_prompt(self.root)

    def test_assistance_source_requires_exact_regular_lf_utf8(self) -> None:
        source_path = self.root / site_builder.AGENT_ONBOARDING_PROMPT_SOURCE
        original = source_path.read_bytes()
        variants = (
            b"",
            original.rstrip(b"\n"),
            original.replace(b"\n", b"\r\n"),
            b"A\x00B\n",
            b"\xef\xbb\xbf" + original,
            b"\xff\n",
        )
        for changed in variants:
            source_path.write_bytes(changed)
            with self.subTest(change=changed[:20]), self.assertRaisesRegex(ValueError, "UTF-8|NUL-free|LF-terminated"):
                site_builder.extract_assistance_prompt(self.root)

    def test_missing_or_symlinked_assistance_source_fails_closed(self) -> None:
        source_path = self.root / site_builder.AGENT_ONBOARDING_PROMPT_SOURCE
        source_path.unlink()
        with self.assertRaisesRegex(ValueError, "missing/unreadable"):
            site_builder.extract_assistance_prompt(self.root)
        source_path.symlink_to(self.root / "README.md")
        with self.assertRaisesRegex(ValueError, "missing/unreadable"):
            site_builder.extract_assistance_prompt(self.root)

    def test_document_structure_failures_are_closed(self) -> None:
        path = self.root / site_builder.MANIFESTO_CONTRACT.source
        variants = (
            ("## Starts too deep\n\nParagraph.\n", "begin|heading structure"),
            ("# First\n\nParagraph.\n\n# Second\n\nParagraph.\n", "exactly one h1"),
            ("# Root\n\nParagraph.\n\n### Jump\n\nParagraph.\n", "heading structure"),
            (
                "# Root\n\nParagraph.\n\n## Repeated\n\nFirst.\n\n## Repeated\n\nSecond.\n",
                "duplicate heading identifier",
            ),
            ("# Heading only\n", "meaningful paragraph"),
        )
        for changed, reason in variants:
            path.write_text(changed, encoding="utf-8")
            with self.subTest(reason=reason), self.assertRaisesRegex(site_builder.ContractError, reason):
                site_builder.extract_content(self.root)

    def test_unsupported_markdown_and_hard_breaks_fail_closed(self) -> None:
        path = self.root / site_builder.MANIFESTO_CONTRACT.source
        additions = (
            "\n```text\nclosed fence\n```\n",
            "\n```text\nunclosed fence\n",
            "\n<div>raw HTML</div>\n",
            "\n![image](https://example.com/image.png)\n",
            "\n1. Numbered item\n",
            "\n> Quoted text\n",
            "\nSetext heading\n===============\n",
            "\nSetext heading\n---------------\n",
            "\n- \n",
            "\n+ \n",
            "\n* \n",
            "\nParagraph with a hard break.  \nNext line.\n",
            "\nParagraph with a backslash break.\\\nNext line.\n",
        )
        for addition in additions:
            path.write_text(synthetic_document(site_builder.MANIFESTO_CONTRACT, addition), encoding="utf-8")
            with (
                self.subTest(addition=addition[:30]),
                self.assertRaisesRegex(site_builder.ContractError, "unsupported block or inline Markdown"),
            ):
                site_builder.extract_content(self.root)

    def test_unmatched_reserved_inline_markers_fail_instead_of_rendering_literally(self) -> None:
        contract = site_builder.MANIFESTO_CONTRACT
        for value in (
            "unmatched * marker",
            "spaces * around * markers",
            "unmatched _marker",
            "marker_ unmatched",
            "spaces _ around _ markers",
            "unfinished **strong marker",
            "~~strikethrough~~",
            r"\*escaped marker\*",
            "unmatched `code",
            "unmatched [link",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(site_builder.ContractError, "unsupported"):
                site_builder._render_inline(value, contract, {})

    def test_home_selection_still_fails_on_missing_duplicate_or_drift(self) -> None:
        readme = self.root / "README.md"
        original = readme.read_text(encoding="utf-8")
        expected = str(site_builder.CONTRACTS[0].expected[1].value)
        pattern = re.compile(r"\s+".join(re.escape(part) for part in expected.split()))
        cases = (
            (original.replace("# Agentworks", "# Different", 1), "missing heading"),
            (original + "\n# Agentworks\n", "duplicate heading"),
            (pattern.sub(lambda match: f"{match.group(0)} drift", original, count=1), "content drift"),
        )
        for changed, reason in cases:
            readme.write_text(changed, encoding="utf-8")
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(site_builder.ContractError, rf"HOME_IDENTITY README.md .*{reason}"),
            ):
                site_builder.extract_content(self.root)
            readme.write_text(original, encoding="utf-8")

    def test_source_relative_links_are_explicitly_mapped_and_others_fail(self) -> None:
        contract = site_builder.MANIFESTO_CONTRACT
        for source, destination in site_builder.SOURCE_RELATIVE_URLS.items():
            with self.subTest(source=source):
                rendered = site_builder._render_inline(f"[label]({source})", contract, {})
                self.assertEqual(rendered, f'<a href="{destination}">label</a>')
        for destination in ("../unreviewed.md", "http://example.com", "https://example.com"):
            with (
                self.subTest(destination=destination),
                self.assertRaisesRegex(site_builder.ContractError, "invalid link"),
            ):
                site_builder._render_inline(f"[label]({destination})", contract, {})

    def test_reviewed_link_url_is_escaped_as_one_href_attribute(self) -> None:
        contract = site_builder.MANIFESTO_CONTRACT
        source = "../README.md"
        malicious = 'https://example.test/?q="quote\'&next=x" onmouseover="pwned'
        with mock.patch.dict(site_builder.SOURCE_RELATIVE_URLS, {source: malicious}):
            rendered = site_builder._render_inline(f"[label]({source})", contract, {})
        anchors = parse(rendered).tags("a")
        self.assertEqual(anchors, [{"href": malicious}])
        self.assertNotIn("onmouseover", anchors[0])
        self.assertIn("&quot;", rendered)
        self.assertIn("&amp;", rendered)

    def test_invalid_utf8_bom_missing_and_symlinked_input_fail_closed(self) -> None:
        policy = self.root / site_builder.SECURITY_CONTRACT.source
        original = policy.read_bytes()
        for value in (b"\xef\xbb\xbf" + original, b"\xff"):
            policy.write_bytes(value)
            with self.subTest(value=value[:3]), self.assertRaisesRegex(site_builder.ContractError, "invalid UTF-8"):
                site_builder.extract_content(self.root)
            policy.write_bytes(original)
        policy.unlink()
        with self.assertRaisesRegex(site_builder.ContractError, "missing/unreadable input"):
            site_builder.extract_content(self.root)
        policy.write_bytes(original)
        target = self.root / "security-target.md"
        target.write_bytes(original)
        policy.unlink()
        try:
            policy.symlink_to(target)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"file symlinks unavailable: {error}")
        with self.assertRaisesRegex(site_builder.ContractError, "missing/unreadable input"):
            site_builder.extract_content(self.root)

    def test_security_reference_and_github_only_reporting_are_exact(self) -> None:
        policy = self.root / site_builder.SECURITY_CONTRACT.source
        original = synthetic_document(site_builder.SECURITY_CONTRACT)
        changes = (
            original.replace(site_builder.REPORTING_URL, "https://example.com/private"),
            original.replace("[gh-private]:", "[renamed]:"),
            original + f"\n[gh-private]: {site_builder.REPORTING_URL}\n",
            original.replace("[private channel][gh-private]", "private reporting"),
            original.replace("Opening paragraph.", "Contact security@example.test.", 1),
        )
        for changed in changes:
            policy.write_text(changed, encoding="utf-8")
            with (
                self.subTest(change=changed[-80:]),
                self.assertRaisesRegex(
                    site_builder.ContractError, "reference definition|GitHub-only reporting violation"
                ),
            ):
                site_builder.extract_content(self.root)

        policy.write_text(
            original.replace("Opening paragraph.", "We do not accept email reports.", 1),
            encoding="utf-8",
        )
        rendered = site_builder.extract_content(self.root)["SECURITY_CONTENT"]
        self.assertIn("We do not accept email reports.", rendered)

    def test_reference_definition_is_consumed_but_not_rendered(self) -> None:
        policy = self.root / site_builder.SECURITY_CONTRACT.source
        policy.write_text(synthetic_document(site_builder.SECURITY_CONTRACT), encoding="utf-8")
        content = site_builder.extract_content(self.root)["SECURITY_CONTENT"]
        self.assertEqual(content.count(f'href="{site_builder.REPORTING_URL}"'), 1)
        self.assertNotIn("[gh-private]", content)

    def test_inline_renderer_escapes_text_and_supports_both_emphasis_markers(self) -> None:
        contract = site_builder.CONTRACTS[0]
        rendered = site_builder._render_inline(
            'plain & "quoted", *star*, _under_, file_name and other_value, and `<tag attr="value">`',
            contract,
            {},
        )
        self.assertEqual(
            rendered,
            "plain &amp; &quot;quoted&quot;, <em>star</em>, <em>under</em>, file_name and other_value, "
            "and <code>&lt;tag attr=&quot;value&quot;&gt;</code>",
        )
        for value in ("<b>html</b>", "> quote", "![image](https://example.com/a.png)", "[bad](http://example.com)"):
            with self.subTest(value=value), self.assertRaises(site_builder.ContractError):
                site_builder._render_inline(value, contract, {})
