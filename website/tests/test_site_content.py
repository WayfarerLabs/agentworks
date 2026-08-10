# ruff: noqa: F405

from site_test_support import *  # noqa: F403


class SourceContractTests(RepositoryFixture):
    def test_every_contract_extracts_from_permanent_sources(self) -> None:
        content = site_builder.extract_content(self.root)
        expected = {contract.contract_id for contract in site_builder.CONTRACTS}
        expected.update(
            {
                "HOME_META_DESCRIPTION",
                "MANIFESTO_CONTENT",
                "MANIFESTO_META_DESCRIPTION",
                "SECURITY_META_DESCRIPTION",
            }
        )
        self.assertEqual(set(content), expected)
        self.assertNotIn("HOME_PROBLEM", content)
        self.assertNotIn("HOME_PRINCIPLES", content)
        self.assertIn("<strong>Durable agents</strong>", content["HOME_IDENTITY"])
        self.assertTrue(content["HOME_META_DESCRIPTION"].startswith("A comprehensive toolkit"))
        self.assertIn("<em>when</em>", content["SECURITY_BOUNDARIES"])
        self.assertIn(site_builder.CLI_SECRETS_URL, content["SECURITY_SECRETS"])
        self.assertIn(site_builder.REPORTING_URL, content["SECURITY_REPORTING"])
        self.assertIn(
            '<h2 id="the-problem-space">The Problem Space</h2>',
            content["MANIFESTO_CONTENT"],
        )
        self.assertIn('<h2 id="key-principles">Key Principles</h2>', content["MANIFESTO_CONTENT"])
        for destination in site_builder.SOURCE_RELATIVE_URLS.values():
            self.assertIn(destination, content["MANIFESTO_CONTENT"])

    def test_crlf_does_not_break_selected_readme_content(self) -> None:
        readme = self.root / "README.md"
        readme.write_bytes(readme.read_text(encoding="utf-8").replace("\n", "\r\n").encode())
        self.assertIn("HOME_IDENTITY", site_builder.extract_content(self.root))

    def test_manifesto_heading_or_prose_drift_fails_closed(self) -> None:
        rationale = self.root / "docs/why-agentworks.md"
        original = rationale.read_text(encoding="utf-8")
        variants = (
            (
                original.replace("### Opinionated Consistency", "### Consistent Opinions", 1),
                "heading structure drift",
            ),
            (
                original.replace("Agentworks is opinionated", "Agentworks has opinions", 1),
                "content drift",
            ),
            (original + "\n## Additional argument\n", "heading structure drift"),
        )
        for changed, reason in variants:
            with self.subTest(reason=reason):
                rationale.write_text(changed, encoding="utf-8")
                with self.assertRaisesRegex(site_builder.ContractError, rf"MANIFESTO .*{reason}"):
                    site_builder.extract_content(self.root)
        rationale.write_text(original, encoding="utf-8")

    def test_heading_shaped_fenced_canary_is_ignored_and_unclosed_fence_fails(
        self,
    ) -> None:
        rationale = self.root / "docs/why-agentworks.md"
        source = rationale.read_text(encoding="utf-8")
        rationale.write_text("```sh\n# hidden\n" + source, encoding="utf-8")
        with self.assertRaisesRegex(site_builder.ContractError, "unclosed fence"):
            site_builder.extract_content(self.root)

    def test_missing_duplicate_and_drifted_content_fail_with_contract_context(
        self,
    ) -> None:
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
            with self.subTest(reason=reason):
                readme.write_text(changed, encoding="utf-8")
                with self.assertRaisesRegex(site_builder.ContractError, rf"HOME_IDENTITY README.md .*{reason}"):
                    site_builder.extract_content(self.root)
                readme.write_text(original, encoding="utf-8")

    def test_duplicate_expected_sequence_within_one_section_fails(self) -> None:
        rationale = self.root / "docs/why-agentworks.md"
        source = rationale.read_text(encoding="utf-8")
        contract = next(contract for contract in site_builder.CONTRACTS if contract.contract_id == "SECURITY_THREATS")
        duplicate = "\n\n".join(block.markdown for block in contract.expected)
        rationale.write_text(
            source.replace("### Security\n", f"### Security\n\n{duplicate}\n", 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(site_builder.ContractError, "duplicate expected block sequence"):
            site_builder.extract_content(self.root)

    def test_manifesto_relative_links_are_explicitly_mapped_and_other_relative_links_fail(
        self,
    ) -> None:
        contract = site_builder.MANIFESTO_CONTRACT
        for source, destination in site_builder.SOURCE_RELATIVE_URLS.items():
            with self.subTest(source=source):
                rendered = site_builder._render_inline(f"[label]({source})", contract, {})
                self.assertEqual(rendered, f'<a href="{destination}">label</a>')
        with self.assertRaisesRegex(site_builder.ContractError, "invalid link"):
            site_builder._render_inline("[label](../unreviewed.md)", contract, {})

    def test_reviewed_link_url_is_escaped_as_one_href_attribute(self) -> None:
        contract = site_builder.MANIFESTO_CONTRACT
        source = "../README.md"
        malicious = 'https://example.test/?q="quote\'&next=x" onmouseover="pwned'
        with mock.patch.dict(site_builder.SOURCE_RELATIVE_URLS, {source: malicious}):
            rendered = site_builder._render_inline(f"[label]({source})", contract, {})
        document = parse(rendered)
        anchors = document.tags("a")
        self.assertEqual(anchors, [{"href": malicious}])
        self.assertNotIn("onmouseover", anchors[0])
        self.assertIn("&quot;", rendered)
        self.assertIn("&amp;", rendered)

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
                    site_builder.ContractError,
                    "reference definition|reporting-link drift|content drift",
                ):
                    site_builder.extract_content(self.root)
        policy.write_text(original, encoding="utf-8")

    def test_reporting_reference_scan_ignores_code_and_rejects_unclosed_fences(
        self,
    ) -> None:
        policy = self.root / "SECURITY.md"
        original = policy.read_text(encoding="utf-8")
        canaries = (
            "```text\n[gh-private]: https://example.com/backtick\n```\n\n",
            "~~~text\n[gh-private]: https://example.com/tilde\n~~~\n\n",
            "    [gh-private]: https://example.com/indented\n\n",
        )
        policy.write_text("".join(canaries) + original, encoding="utf-8")
        self.assertIn("SECURITY_REPORTING", site_builder.extract_content(self.root))
        policy.write_text(
            "```text\n[gh-private]: https://example.com/unclosed\n" + original,
            encoding="utf-8",
        )
        with self.assertRaisesRegex(site_builder.ContractError, "unclosed fence"):
            site_builder.extract_content(self.root)

    def test_inline_renderer_escapes_text_and_rejects_unsupported_markdown(
        self,
    ) -> None:
        contract = site_builder.CONTRACTS[0]
        rendered = site_builder._render_inline('plain & "quoted" plus `<tag attr="value">`', contract, {})
        self.assertEqual(
            rendered,
            "plain &amp; &quot;quoted&quot; plus <code>&lt;tag attr=&quot;value&quot;&gt;</code>",
        )
        unsupported = (
            "<b>html</b>",
            "> quote",
            "![image](https://example.com/a.png)",
            "[bad](http://example.com)",
        )
        for value in unsupported:
            with (
                self.subTest(value=value),
                self.assertRaises(site_builder.ContractError),
            ):
                site_builder._render_inline(value, contract, {})
