# ruff: noqa: F405

from site_test_support import *  # noqa: F403


class BuildAndInstallTests(RepositoryFixture):
    def test_dual_base_builds_have_the_exact_complete_manifest(self) -> None:
        for site_base in ("/", "/agentworks/"):
            with self.subTest(site_base=site_base):
                output = Path(self.temporary.name) / f"build-{site_base.count('agentworks')}"
                site_builder.build_site(self.root, output, site_base)
                self.assertEqual({Path(path) for path in snapshot(output)}, EXPECTED_FILES)
                self.assertEqual(site_builder.FULL_MANIFEST, EXPECTED_FILES)
                for path in output.rglob("*"):
                    self.assertFalse(path.is_symlink())
                page = (output / "404.html").read_text(encoding="utf-8")
                self.assertIn(f'href="{site_base}"', page)
                self.assertNotIn("{{", page)

    def test_builder_has_no_partial_output_api_or_cli_option(self) -> None:
        self.assertFalse(hasattr(site_builder, "build_404"))
        self.assertFalse(hasattr(site_builder, "FOCUSED_MANIFEST"))
        builder_source = BUILD_PATH.read_text(encoding="utf-8")
        for retired in ("build_404", "FOCUSED_MANIFEST", 'add_argument("--only"'):
            self.assertNotIn(retired, builder_source)
        result = subprocess.run(
            [
                "python3",
                str(BUILD_PATH),
                "--only",
                "404",
                "--repo-root",
                str(self.root),
                "--output",
                str(Path(self.temporary.name) / "partial"),
                "--site-base",
                "/",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments: --only 404", result.stderr)

    def test_repeated_builds_are_byte_deterministic(self) -> None:
        first = self.build("/agentworks/")
        before = snapshot(first)
        site_builder.build_site(self.root, first, "/agentworks/")
        self.assertEqual(snapshot(first), before)
        second = Path(self.temporary.name) / "second"
        site_builder.build_site(self.root, second, "/agentworks/")
        self.assertEqual(snapshot(second), before)

    def test_favicon_rejects_every_unreviewed_svg_node(self) -> None:
        favicon = self.root / "website/assets/agw-favicon.svg"
        source = favicon.read_text(encoding="utf-8")
        mutations = (
            source.replace("</g>", '<path d="M0 0" />\n    </g>', 1),
            source.replace("</g>", '<animateTransform attributeName="transform" />\n    </g>', 1),
            source.replace("</g>", '<set attributeName="fill" to="red" />\n    </g>', 1),
            source.replace("</g>", "<!-- anonymous extra node -->\n    </g>", 1),
            f"<!-- document comment -->\n{source}",
            f"<?favicon test?>\n{source}",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation[-80:]):
                favicon.write_text(mutation, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "agw-favicon.svg"):
                    site_builder._render_artifact(self.root, "/")

    def test_favicon_rejects_non_path_canonical_mark_geometry(self) -> None:
        rocket = self.root / "website/assets/agw-rocket.svg"
        source = rocket.read_text(encoding="utf-8")
        rocket.write_text(
            source.replace('<path\n            id="agw-letter-w"', '<rect\n            id="agw-letter-w"', 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "canonical mark structure"):
            site_builder._render_artifact(self.root, "/")

    def test_unapproved_external_url_fails_before_output_changes(self) -> None:
        output = self.build()
        before = snapshot(output)
        manifesto = self.root / site_builder.MANIFESTO_CONTRACT.source
        source = manifesto.read_text(encoding="utf-8")
        manifesto.write_text(
            source + "\nAn [unapproved destination](https://example.com/unapproved).\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid link"):
            site_builder.build_site(self.root, output, "/agentworks/")
        self.assertEqual(snapshot(output), before)

    def test_absent_local_route_has_no_partial_manifest_suppression(self) -> None:
        rendered, manifest = site_builder._render_artifact(self.root, "/")
        changed = dict(rendered)
        changed[Path("security/index.html")] = changed[Path("security/index.html")].replace(
            b"</main>", b'<a href="/missing/">Missing route</a>\n</main>', 1
        )
        with self.assertRaisesRegex(ValueError, "local reference is absent from manifest"):
            site_builder._validate_local_references(changed, manifest, "/")

    def test_cross_document_fragment_must_exist_on_its_actual_target(self) -> None:
        rendered, manifest = site_builder._render_artifact(self.root, "/agentworks/")
        changed = dict(rendered)
        changed[Path("security/index.html")] = changed[Path("security/index.html")].replace(
            b"</main>",
            b'<a href="/agentworks/404.html#not-a-real-id">Missing cross-page fragment</a>\n</main>',
            1,
        )
        with self.assertRaisesRegex(ValueError, "local reference fragment is absent"):
            site_builder._validate_local_references(changed, manifest, "/agentworks/")

    def test_source_heading_cannot_duplicate_a_shell_id(self) -> None:
        manifesto = self.root / site_builder.MANIFESTO_CONTRACT.source
        manifesto.write_text(
            "# Synthetic document\n\nOpening paragraph.\n\n## Main content\n\nCollision witness.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate element id"):
            site_builder._render_artifact(self.root, "/")

    def test_rendered_reference_validation_retains_duplicate_ids(self) -> None:
        rendered, manifest = site_builder._render_artifact(self.root, "/")
        changed = dict(rendered)
        changed[Path("manifesto/index.html")] = changed[Path("manifesto/index.html")].replace(
            b"</article>", b'<p id="main-content">Collision witness.</p></article>', 1
        )
        with self.assertRaisesRegex(ValueError, "duplicate element id"):
            site_builder._validate_local_references(changed, manifest, "/")

    def test_manifest_without_root_index_cannot_suppress_root_reference_failure(
        self,
    ) -> None:
        rendered, manifest = site_builder._render_artifact(self.root, "/")
        without_index = manifest - {Path("index.html")}
        with self.assertRaisesRegex(ValueError, "local reference is absent from manifest"):
            site_builder._validate_local_references(rendered, without_index, "/")

    def test_shared_css_cannot_conceal_reviewed_shell_content(self) -> None:
        output = self.build()
        before = snapshot(output)
        stylesheet = self.root / "website/static/site.css"
        source = stylesheet.read_text(encoding="utf-8")
        mutations = (
            ".canary { display /* normalized */ : none; }",
            ".canary { display: var(--concealed); }",
            ".canary { display: table; }",
            ".canary { display: block; }",
            r".canary { dis\play: none; }",
            r".canary { d\69 splay: none; }",
            ".canary { VISIBILITY : collapse; }",
            ".canary { visibility: visible; }",
            r".canary { v\69 sibility: hidden; }",
            ".canary { opacity : 0; }",
            ".canary { opacity: 0%; }",
            ".canary { opacity: -0.0%; }",
            ".canary { opac/**/ity: calc(0); }",
            ".canary { opacity: 0.75; }",
            r".canary { \6f pacity: 0; }",
            ".canary { content-visibility: hidden; }",
            ".canary { content-visibility: auto; }",
            r".canary { content-v\69 sibility: hidden; }",
        )
        for mutation in mutations:
            stylesheet.write_text(f"{source}\n{mutation}\n", encoding="utf-8")
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(ValueError, "escape sequences|outside the reviewed layout contract"),
            ):
                site_builder.build_site(self.root, output, "/")
            self.assertEqual(snapshot(output), before)
        stylesheet.write_text(source, encoding="utf-8")

    def test_same_document_fragment_must_resolve_before_output_changes(self) -> None:
        output = self.build()
        before = snapshot(output)
        template = self.root / "website/templates/404.html"
        source = template.read_text(encoding="utf-8")
        template.write_text(source.replace('id="main-content"', 'id="renamed-main"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "main landmark|same-document fragment|shared Lander fragment"):
            site_builder.build_site(self.root, output, "/")
        self.assertEqual(snapshot(output), before)

    def test_duplicate_rendered_references_cannot_hide_first_browser_destination(
        self,
    ) -> None:
        manifest = frozenset({Path("index.html"), Path("static/site.css")})
        canaries = (
            '<a href="https://example.com/unapproved" href="/">unsafe external first</a>',
            f'<a href="/missing" href="{site_builder.REPOSITORY_URL}">unsafe local first</a>',
            '<script src="https://example.com/unapproved.js" src="/static/site.css"></script>',
            f'<img src="/missing.png" src="{site_builder.REPOSITORY_URL}" />',
        )
        for canary in canaries:
            rendered = {Path("index.html"): f'<main id="main-content">{canary}</main>'.encode()}
            with (
                self.subTest(canary=canary),
                self.assertRaisesRegex(ValueError, "duplicate HTML attribute"),
            ):
                site_builder._validate_local_references(rendered, manifest, "/")

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
                self.assertEqual({Path(path) for path in snapshot(output)}, EXPECTED_FILES)

    def test_every_static_module_import_resolves_to_the_literal_manifest(self) -> None:
        rendered, manifest = site_builder._render_artifact(self.root, "/")
        self.assertEqual(manifest, EXPECTED_FILES)
        game = Path("static/lander-game.js")
        changed = dict(rendered)
        changed[game] = changed[game].replace(b'"./lander-model.js"', b'"./missing-model.js"', 1)
        with self.assertRaisesRegex(ValueError, "JavaScript module import is absent from manifest"):
            site_builder._validate_local_references(changed, EXPECTED_FILES, "/")

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

    def test_existing_top_level_directory_symlink_is_rejected_without_touching_target(
        self,
    ) -> None:
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

    def test_existing_top_level_broken_symlink_is_rejected_without_replacement(
        self,
    ) -> None:
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

    def test_content_or_template_failure_leaves_existing_output_byte_identical(
        self,
    ) -> None:
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
            (
                "website/static/site.css",
                '\nbody { content: "//cdn.example.com/theme.css"; }\n',
            ),
            (
                "website/static/lander-game.js",
                '\nconst remote = "https://example.com/game.js";\n',
            ),
            (
                "website/static/lander-game.js",
                '\nconst remote = "//cdn.example.com/game.js";\n',
            ),
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
        script.write_text(
            script.read_text(encoding="utf-8") + "\n// harmless local note\n",
            encoding="utf-8",
        )
        output = self.build()
        self.assertIn(
            "// harmless local note",
            (output / "static/lander-game.js").read_text(encoding="utf-8"),
        )

    def test_failure_before_and_during_each_swap_boundary_restores_exact_output(
        self,
    ) -> None:
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

            def injected_verify(
                path: Path,
                manifest: frozenset[Path],
                failure_boundary: int = failing_call,
            ) -> None:
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
        self.assertIn(
            'href="/agentworks/security/"',
            (output / "index.html").read_text(encoding="utf-8"),
        )

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
