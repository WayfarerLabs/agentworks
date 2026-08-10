# ruff: noqa: F405

from site_test_support import *  # noqa: F403


class SourceContractTests(RepositoryFixture):
    def test_permanent_sources_render_the_complete_content_vocabulary(self) -> None:
        content = site_builder.extract_content(self.root)
        self.assertEqual(
            set(content),
            {
                "HOME_IDENTITY",
                "HOME_META_DESCRIPTION",
                "MANIFESTO_CONTENT",
                "MANIFESTO_META_DESCRIPTION",
                "SECURITY_CONTENT",
                "SECURITY_META_DESCRIPTION",
            },
        )
        self.assertIn("<strong>Durable agents</strong>", content["HOME_IDENTITY"])
        self.assertTrue(content["HOME_META_DESCRIPTION"].startswith("A comprehensive toolkit"))
        self.assertIn('<h1 id="why-agentworks">Why Agentworks</h1>', content["MANIFESTO_CONTENT"])
        self.assertIn(
            '<h1 id="security-at-agentworks">Security at Agentworks</h1>',
            content["SECURITY_CONTENT"],
        )
        self.assertIn('<h2 id="reporting-a-vulnerability">', content["SECURITY_CONTENT"])
        self.assertIn(site_builder.REPORTING_URL, content["SECURITY_CONTENT"])
        self.assertNotIn("email", content["SECURITY_CONTENT"].lower())
        for destination in site_builder.SOURCE_RELATIVE_URLS.values():
            self.assertIn(destination, content["MANIFESTO_CONTENT"])

    def test_supported_source_additions_render_without_contract_edits(self) -> None:
        addition = (
            "\n## Operational Notes\n\n"
            "A newly added paragraph with **strong text**, _emphasis_, and `code`.\n\n"
            "- First new item.\n"
            "- Second new item.\n"
        )
        for contract in site_builder.DOCUMENT_CONTRACTS:
            path = self.root / contract.source
            original = path.read_text(encoding="utf-8")
            if contract is site_builder.SECURITY_CONTRACT:
                changed = original.replace("\n[gh-private]:", f"{addition}\n[gh-private]:", 1)
            else:
                changed = original + addition
            path.write_text(changed, encoding="utf-8")
            with self.subTest(contract=contract.contract_id):
                rendered = site_builder.extract_content(self.root)[f"{contract.contract_id}_CONTENT"]
                self.assertIn('<h2 id="operational-notes">Operational Notes</h2>', rendered)
                self.assertIn(
                    "<p>A newly added paragraph with <strong>strong text</strong>, "
                    "<em>emphasis</em>, and <code>code</code>.</p>",
                    rendered,
                )
                self.assertIn(
                    "<ul><li>First new item.</li><li>Second new item.</li></ul>",
                    rendered,
                )
            path.write_text(original, encoding="utf-8")

    def test_supported_manifesto_heading_and_prose_changes_flow_through(self) -> None:
        rationale = self.root / site_builder.MANIFESTO_CONTRACT.source
        source = rationale.read_text(encoding="utf-8")
        rationale.write_text(
            source.replace("### Opinionated Consistency", "### Deliberate Consistency", 1).replace(
                "Agentworks is opinionated", "Agentworks makes deliberate choices", 1
            ),
            encoding="utf-8",
        )
        rendered = site_builder.extract_content(self.root)["MANIFESTO_CONTENT"]
        self.assertIn('<h3 id="deliberate-consistency">Deliberate Consistency</h3>', rendered)
        self.assertIn("Agentworks makes deliberate choices", rendered)

    def test_current_source_path_is_single_and_has_no_future_fallback(self) -> None:
        self.assertEqual(site_builder.MANIFESTO_CONTRACT.source, Path("docs/why-agentworks.md"))
        production = (WEBSITE / "site_content.py").read_text(encoding="utf-8")
        self.assertNotIn("docs/manifesto.md", production)

    def test_crlf_does_not_break_home_or_complete_documents(self) -> None:
        for relative in ("README.md", "docs/why-agentworks.md", "SECURITY.md"):
            path = self.root / relative
            path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode())
        content = site_builder.extract_content(self.root)
        self.assertIn("HOME_IDENTITY", content)
        self.assertIn("MANIFESTO_CONTENT", content)
        self.assertIn("SECURITY_CONTENT", content)

    def test_document_structure_failures_are_closed(self) -> None:
        rationale = self.root / site_builder.MANIFESTO_CONTRACT.source
        original = rationale.read_text(encoding="utf-8")
        variants = (
            (
                original.replace("# Why Agentworks", "## Why Agentworks", 1),
                "begin|heading structure",
            ),
            (original + "\n# Another root\n\nAnother paragraph.\n", "exactly one h1"),
            (
                original.replace("## The Problem Space", "#### The Problem Space", 1),
                "heading structure",
            ),
            (
                original + "\n## The Problem Space\n\nDuplicate identifier.\n",
                "duplicate heading identifier",
            ),
            ("# Heading only\n", "meaningful paragraph"),
        )
        for changed, reason in variants:
            rationale.write_text(changed, encoding="utf-8")
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(site_builder.ContractError, reason),
            ):
                site_builder.extract_content(self.root)
        rationale.write_text(original, encoding="utf-8")

    def test_unsupported_markdown_and_fences_fail_closed(self) -> None:
        rationale = self.root / site_builder.MANIFESTO_CONTRACT.source
        original = rationale.read_text(encoding="utf-8")
        additions = (
            "\n```text\nclosed fence\n```\n",
            "\n```text\nunclosed fence\n",
            "\n<div>raw HTML</div>\n",
            "\n![image](https://example.com/image.png)\n",
            "\n1. Numbered item\n",
            "\n> Quoted text\n",
        )
        for addition in additions:
            rationale.write_text(original + addition, encoding="utf-8")
            with (
                self.subTest(addition=addition[:20]),
                self.assertRaisesRegex(site_builder.ContractError, "unsupported block or inline Markdown"),
            ):
                site_builder.extract_content(self.root)
        rationale.write_text(original, encoding="utf-8")

    def test_home_selection_still_fails_on_missing_duplicate_or_drift(self) -> None:
        readme = self.root / "README.md"
        original = readme.read_text(encoding="utf-8")
        cases = (
            (original.replace("# Agentworks", "# Different", 1), "missing heading"),
            (original + "\n# Agentworks\n", "duplicate heading"),
            (
                original.replace("A comprehensive toolkit", "A partial toolkit", 1),
                "content drift",
            ),
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
        for destination in (
            "../unreviewed.md",
            "http://example.com",
            "https://example.com",
        ):
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
        policy = self.root / "SECURITY.md"
        original = policy.read_bytes()
        for value in (b"\xef\xbb\xbf" + original, b"\xff"):
            policy.write_bytes(value)
            with (
                self.subTest(value=value[:3]),
                self.assertRaisesRegex(site_builder.ContractError, "invalid UTF-8"),
            ):
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
        policy = self.root / "SECURITY.md"
        original = policy.read_text(encoding="utf-8")
        changes = (
            original.replace(site_builder.REPORTING_URL, "https://example.com/private"),
            original.replace("[gh-private]:", "[renamed]:"),
            original + f"\n[gh-private]: {site_builder.REPORTING_URL}\n",
            original.replace("[private vulnerability reporting][gh-private]", "private reporting"),
            original.replace("rather than opening", "or by email rather than opening", 1),
            original.replace("rather than opening", "to security@example.test rather than opening", 1),
        )
        for changed in changes:
            policy.write_text(changed, encoding="utf-8")
            with (
                self.subTest(change=changed[-80:]),
                self.assertRaisesRegex(
                    site_builder.ContractError,
                    "reference definition|GitHub-only reporting violation",
                ),
            ):
                site_builder.extract_content(self.root)
        policy.write_text(original, encoding="utf-8")

    def test_reference_definition_is_consumed_but_not_rendered(self) -> None:
        content = site_builder.extract_content(self.root)["SECURITY_CONTENT"]
        self.assertEqual(content.count(f'href="{site_builder.REPORTING_URL}"'), 1)
        self.assertNotIn("[gh-private]", content)

    def test_inline_renderer_escapes_text_and_rejects_unsupported_markdown(
        self,
    ) -> None:
        contract = site_builder.CONTRACTS[0]
        rendered = site_builder._render_inline('plain & "quoted" plus `<tag attr="value">`', contract, {})
        self.assertEqual(
            rendered,
            "plain &amp; &quot;quoted&quot; plus <code>&lt;tag attr=&quot;value&quot;&gt;</code>",
        )
        for value in (
            "<b>html</b>",
            "> quote",
            "![image](https://example.com/a.png)",
            "[bad](http://example.com)",
        ):
            with (
                self.subTest(value=value),
                self.assertRaises(site_builder.ContractError),
            ):
                site_builder._render_inline(value, contract, {})
