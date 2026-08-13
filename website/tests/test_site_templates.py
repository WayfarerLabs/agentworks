# ruff: noqa: F405

from site_test_support import *  # noqa: F403


class TemplateContractTests(RepositoryFixture):
    def test_each_template_has_only_its_closed_vocabulary(self) -> None:
        for name, expected in site_builder.TEMPLATE_TOKENS.items():
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            self.assertEqual(set(site_builder.TOKEN_PATTERN.findall(template)), expected)
            site_builder._validate_template(name, template)

    def test_unknown_missing_duplicate_wrong_template_and_brace_tokens_fail(
        self,
    ) -> None:
        path = self.root / "website/templates/index.html"
        template = path.read_text(encoding="utf-8")
        variants = (
            template + "{{UNKNOWN}}",
            template.replace("{{HOME_IDENTITY}}", ""),
            template.replace("{{HOME_IDENTITY}}", "{{HOME_IDENTITY}}{{HOME_IDENTITY}}"),
            template.replace("{{HOME_IDENTITY}}", "{{SECURITY_CONTENT}}"),
            template + "{{not-a-token}}",
            template.replace('href="{{SITE_BASE}}security/"', 'data-base="{{SITE_BASE}}security/"', 1),
        )
        for changed in variants:
            with self.subTest(changed=changed[-40:]), self.assertRaises(ValueError):
                site_builder._validate_template("index.html", changed)

    def test_extracted_content_cannot_expand_a_template_token(self) -> None:
        template = (self.root / "website/templates/index.html").read_text(encoding="utf-8")
        substitutions = site_builder.extract_content(self.root)
        for injection in (
            "&lt;script&gt;{{ATTACK}}&lt;/script&gt;",
            "{{HOME_IDENTITY}}",
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
        prompt = '<code id="onboarding-prompt">{{ONBOARDING_PROMPT}}</code>'
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
            template.replace(prompt, '<script>{{ONBOARDING_PROMPT}}</script>'),
            template.replace(prompt, '<code id="other">{{ONBOARDING_PROMPT}}</code>'),
            template.replace(prompt, '<code id="onboarding-prompt">prefix {{ONBOARDING_PROMPT}}</code>'),
            template.replace(prompt, '</section><code id="onboarding-prompt">{{ONBOARDING_PROMPT}}</code>'),
        )
        for changed in variants:
            with (
                self.subTest(changed=changed[changed.find("{{HOME_") - 30 : changed.find("{{HOME_") + 50]),
                self.assertRaisesRegex(
                    ValueError,
                    "content token|metadata token|prompt token|sourced-content|reviewed section",
                ),
            ):
                site_builder._validate_template("index.html", changed)

    def test_onboarding_projection_copy_controls_and_shared_destinations_are_guarded(
        self,
    ) -> None:
        template = (self.root / "website/templates/index.html").read_text(encoding="utf-8")
        variants = (
            template.replace('id="onboarding"', 'id="onboarding-moved"'),
            template.replace('aria-labelledby="onboarding-heading"', 'aria-labelledby="other"', 1),
            template.replace('<h2 id="onboarding-heading">', '<h2 id="other">'),
            template.replace('class="onboarding-prompt"', 'class="other"'),
            template.replace('id="copy-onboarding-prompt"', 'id="copy-other"'),
            template.replace(' type="button" hidden', ' type="button"'),
            template.replace('aria-live="polite"', 'aria-live="assertive"'),
            template.replace('static/onboarding-copy.js', 'static/other.js'),
            template.replace(
                "</footer>",
                '<a href="https://github.com/WayfarerLabs/agentworks">Repeated repository</a></footer>',
            ),
            template.replace("</footer>", '<a href="#main-content">Repeated skip link</a></footer>'),
        )
        for changed in variants:
            with (
                self.subTest(change=changed[-100:]),
                self.assertRaisesRegex(ValueError, "onboarding|destination|skip link|footer|reviewed literals"),
            ):
                site_builder._validate_template("index.html", changed)

    def test_shell_destination_labels_are_bound_to_their_reviewed_hrefs(self) -> None:
        template = (self.root / "website/templates/index.html").read_text(encoding="utf-8")
        swapped = template.replace("<span>GitHub</span>", "<span>SWAPPED</span>", 1)
        swapped = swapped.replace("<span>PyPI</span>", "<span>GitHub</span>", 1)
        swapped = swapped.replace("<span>SWAPPED</span>", "<span>PyPI</span>", 1)
        with self.assertRaisesRegex(ValueError, "destination"):
            site_builder._validate_template("index.html", swapped)

    def test_shell_css_classes_landmark_order_and_breadcrumb_order_fail_closed(
        self,
    ) -> None:
        for name in SHELL_TEMPLATES:
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            footer = re.search(
                r"\n        <footer class=\"site-footer\">.*?</footer>",
                template,
                re.DOTALL,
            )
            self.assertIsNotNone(footer)
            assert footer is not None
            reordered = template.replace(footer.group(0), "", 1).replace(
                "        <main ", f"{footer.group(0)}\n        <main ", 1
            )
            reversed_breadcrumb = re.sub(
                r'(<a href="\{\{SITE_BASE\}\}">Agentworks</a>)(\s*)'
                r'(<span class="breadcrumb-separator" aria-hidden="true">/</span>)',
                r"\3\2\1",
                template,
                count=1,
            )
            variants = (
                template.replace('class="site-header"', 'class="site-top"', 1),
                template.replace('class="site-footer"', 'class="site-bottom"', 1),
                template.replace('class="breadcrumbs"', 'class="crumbs"', 1),
                template.replace('class="service-links"', 'class="services"', 1),
                template.replace(
                    site_builder.MAIN_ATTRIBUTES[name].get("class", 'id="main-content"'),
                    "drifted",
                    1,
                ),
                template.replace('aria-label="External"', 'aria-label="Primary"', 1),
                template.replace('aria-label="Footer"', 'aria-label="Elsewhere"', 1),
                template.replace('aria-current="page"', 'aria-current="step"', 1),
                reversed_breadcrumb,
                reordered,
            )
            if name != "index.html":
                variants += (template.replace('class="header-identity"', 'class="identity"', 1),)
            for changed in variants:
                with (
                    self.subTest(name=name, change=changed[:80]),
                    self.assertRaises(ValueError),
                ):
                    site_builder._validate_template(name, changed)

    def test_detail_page_headings_reject_provenance_eyebrows(self) -> None:
        for name in ("lander.html", "404.html"):
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            heading = re.search(r"<h1>([^<]+)</h1>", template)
            self.assertIsNotNone(heading)
            assert heading is not None
            changed = template.replace(
                heading.group(0),
                f'<p class="eyebrow">Context</p>{heading.group(0)}',
                1,
            )
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "detail page heading"),
            ):
                site_builder._validate_template(name, changed)

    def test_long_form_templates_allow_only_the_complete_source_document(self) -> None:
        for name, token in (
            ("manifesto.html", "{{MANIFESTO_CONTENT}}"),
            ("security.html", "{{SECURITY_CONTENT}}"),
        ):
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            variants = (
                template.replace(token, f"<h1>Template title</h1>{token}", 1),
                template.replace(token, f"{token}<p>Template prose</p>", 1),
                template.replace(
                    '<article class="long-form-content sourced-content">',
                    '<section class="long-form-content sourced-content">',
                    1,
                ),
            )
            for changed in variants:
                with (
                    self.subTest(name=name, changed=changed[-120:]),
                    self.assertRaisesRegex(
                        ValueError,
                        "complete source|complete sourced document|block token",
                    ),
                ):
                    site_builder._validate_template(name, changed)

    def test_service_ctas_reject_hidden_ancestors_and_icon_bypasses(self) -> None:
        extra_icon = '<svg aria-hidden="true" focusable="false" viewBox="0 0 16 16"><path d="M0 0" /></svg>'
        for name in SHELL_TEMPLATES:
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            variants = (
                template.replace(
                    f'<a href="{site_builder.REPOSITORY_URL}"',
                    f'<a hidden href="{site_builder.REPOSITORY_URL}"',
                    1,
                ),
                template.replace(
                    f'<a href="{site_builder.REPOSITORY_URL}"',
                    f'<a style="display: none" href="{site_builder.REPOSITORY_URL}"',
                    1,
                ),
                template.replace(
                    '<header class="site-header">',
                    '<header class="site-header" hidden>',
                    1,
                ),
                template.replace("<body>", "<body hidden>", 1),
                template.replace(
                    '<nav class="service-links" aria-label="External">',
                    '<nav class="service-links" aria-label="External" aria-hidden="true">',
                    1,
                ),
                template.replace("<span>GitHub</span>", "<span hidden>GitHub</span>", 1),
                template.replace(
                    "</head>",
                    "<style>.service-links { display: none }</style></head>",
                    1,
                ),
                template.replace("<span>GitHub</span>", f"{extra_icon}<span>GitHub</span>", 1),
                template.replace(
                    "</nav>\n        </header>",
                    f"{extra_icon}</nav>\n        </header>",
                    1,
                ),
                template.replace("</main>", f"{extra_icon}</main>", 1),
                template.replace('class="service-icon"', 'class="icon"', 1),
                template.replace(
                    'aria-hidden="true" focusable="false" viewBox="0 0 16 16"',
                    'focusable="false" viewBox="0 0 16 16"',
                    1,
                ),
            )
            for changed in variants:
                with (
                    self.subTest(name=name, change=changed[:100]),
                    self.assertRaises(ValueError),
                ):
                    site_builder._validate_template(name, changed)

    def test_reviewed_shell_text_cannot_come_from_hidden_or_structural_descendants(
        self,
    ) -> None:
        template = (self.root / "website/templates/index.html").read_text(encoding="utf-8")
        variants = (
            template.replace(
                "Skip to main content",
                "<span hidden>Skip to main content</span>",
                1,
            ),
            template.replace(
                "<span>GitHub</span>",
                '<span><span aria-hidden="true">GitHub</span></span>',
                1,
            ),
            template.replace(
                '<a href="{{SITE_BASE}}">Agentworks</a>',
                '<a href="{{SITE_BASE}}"><span hidden>Agentworks</span></a>',
                1,
            ),
            template.replace(
                '<span aria-current="page">Home</span>',
                '<span aria-current="page"><template>Home</template></span>',
                1,
            ),
            template.replace(
                "Product of Wayfarer Labs, LLC",
                '<span aria-hidden="true">Product of Wayfarer Labs, LLC</span>',
                1,
            ),
            template.replace(
                ">Agentworks Manifesto</a>",
                '><span style="visibility: hidden">Agentworks Manifesto</span></a>',
                1,
            ),
        )
        for changed in variants:
            with self.subTest(change=changed[:120]), self.assertRaises(ValueError):
                site_builder._validate_template("index.html", changed)

    def test_service_icon_paths_are_exact_single_direct_children(self) -> None:
        for name in SHELL_TEMPLATES:
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            for destination, path_data in site_builder.SERVICE_ICON_PATHS.items():
                path = f'<path d="{path_data}" />'
                self.assertIn(path, template)
                variants = (
                    template.replace(path, "", 1),
                    template.replace(path, '<path d="M0 0" />', 1),
                    template.replace(path, "<path />", 1),
                    template.replace(path, f"{path}{path}", 1),
                    template.replace(path, f"<g>{path}</g>", 1),
                    template.replace(path, f'<path d="{path_data}">unexpected</path>', 1),
                )
                for changed in variants:
                    with (
                        self.subTest(name=name, destination=destination),
                        self.assertRaisesRegex(ValueError, "exact reviewed icon path"),
                    ):
                        site_builder._validate_template(name, changed)

    def test_rocket_inventory_rejects_unclassified_extra_and_misplaced_images(
        self,
    ) -> None:
        extra = '<img src="{{SITE_BASE}}assets/agw-rocket.svg" alt="" />'
        for name in SHELL_TEMPLATES:
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            variants = (
                template.replace(
                    '<header class="site-header">',
                    f'<header class="site-header">{extra}',
                    1,
                ),
                template.replace("</main>", f"{extra}</main>", 1),
            )
            if name != "index.html":
                variants += (
                    template.replace('class="header-mark"', 'class="unclassified"', 1),
                    template.replace(
                        '<img class="header-mark" src="{{SITE_BASE}}assets/agw-rocket.svg" alt="" />\n                '
                        '<nav class="breadcrumbs"',
                        '<nav class="breadcrumbs"',
                        1,
                    ).replace(
                        "</nav>\n            </div>",
                        '</nav>\n                <img class="header-mark" '
                        'src="{{SITE_BASE}}assets/agw-rocket.svg" alt="" />\n            </div>',
                        1,
                    ),
                )
            else:
                variants += (
                    template.replace(
                        '<img class="hero-mark" src="{{SITE_BASE}}assets/agw-rocket.svg" alt="AGW rocket mark" />',
                        "",
                        1,
                    ).replace(
                        "</main>",
                        '<img class="hero-mark" src="{{SITE_BASE}}assets/agw-rocket.svg" '
                        'alt="AGW rocket mark" /></main>',
                        1,
                    ),
                )
            for changed in variants:
                with (
                    self.subTest(name=name, change=changed[:100]),
                    self.assertRaises(ValueError),
                ):
                    site_builder._validate_template(name, changed)

    def test_fragment_scene_svg_cannot_move_outside_its_reviewed_section(self) -> None:
        template = (self.root / "website/templates/lander-game.html").read_text(encoding="utf-8")
        scene = re.search(r"\n            <svg\n.*?\n            </svg>", template, re.DOTALL)
        self.assertIsNotNone(scene)
        assert scene is not None
        changed = template.replace(scene.group(0), "", 1) + scene.group(0)
        with self.assertRaises(ValueError):
            site_builder._validate_template("lander-game.html", changed)

    def test_fragment_accessible_name_sources_must_be_structural_and_nonempty(self) -> None:
        template = (self.root / "website/templates/lander-game.html").read_text(encoding="utf-8")
        root_label = re.search(r'(<section id="lander-game" aria-label=")([^"]+)(")', template)
        start_label = re.search(r'(<button id="lander-start"[^>]* aria-label=")([^"]+)(")', template)
        scene_title = re.search(r'(<title id="lander-scene-title">)([^<]+)(</title>)', template)
        self.assertTrue(all(match is not None for match in (root_label, start_label, scene_title)))
        assert root_label is not None and start_label is not None and scene_title is not None
        for match in (root_label, start_label, scene_title):
            changed = template.replace(match.group(0), f"{match.group(1)}   {match.group(3)}", 1)
            with self.subTest(element=match.group(1)), self.assertRaisesRegex(ValueError, "root|Start|text source"):
                site_builder._validate_template("lander-game.html", changed)

    def test_fragment_support_and_battery_geometry_fail_closed(self) -> None:
        template = (self.root / "website/templates/lander-game.html").read_text(encoding="utf-8")
        bar_one = '<path class="battery-bar battery-bar-1" d="M457 423h12v5h-12Z" />'
        bar_four = '<path class="battery-bar battery-bar-4" d="M457 399h12v5h-12Z" />'
        mutations = (
            template.replace('class="site-scaffold"', 'class="missing-support"', 1),
            template.replace("M312 452.5H498", "M313 452.5H498", 1),
            template.replace("M488 484.5L498 491.77281586216765", "M488 485L498 491.77281586216765", 1),
            template.replace(bar_one, "BATTERY_SWAP", 1)
            .replace(bar_four, bar_one, 1)
            .replace(
                "BATTERY_SWAP",
                bar_four,
                1,
            ),
        )
        for changed in mutations:
            with self.subTest(change=changed[:100]), self.assertRaisesRegex(ValueError, "scaffold|battery"):
                site_builder._validate_template("lander-game.html", changed)

    def test_fragment_variants_cannot_duplicate_local_route_destinations(self) -> None:
        for name in SHELL_TEMPLATES:
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            variants = (
                (
                    template.replace(
                        "</main>",
                        '<a href="{{SITE_BASE}}manifesto/#the-problem-space">Duplicate Manifesto route</a></main>',
                        1,
                    ),
                    "duplicate normalized local route",
                ),
                (
                    template.replace(
                        "</main>",
                        '<a href="{{SITE_BASE}}manifesto/index.html#the-problem-space">'
                        "Aliased Manifesto route</a></main>",
                        1,
                    ),
                    "duplicate normalized local route",
                ),
                (
                    template.replace(
                        "</main>",
                        '<a href="{{SITE_BASE}}index.html">Aliased root route</a></main>',
                        1,
                    ),
                    "duplicate normalized local route",
                ),
                (
                    template.replace(
                        "</main>",
                        '<a href="/manifesto/#the-problem-space">Unbased route</a></main>',
                        1,
                    ),
                    "must use SITE_BASE",
                ),
            )
            for changed, reason in variants:
                with (
                    self.subTest(name=name, reason=reason),
                    self.assertRaises(ValueError),
                ):
                    site_builder._validate_template(name, changed)

    def test_shared_game_fragment_has_one_exact_shell_placement(self) -> None:
        for name in ("lander.html", "404.html"):
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            variants = (
                template.replace("{{LANDER_GAME}}", "", 1),
                template.replace("{{LANDER_GAME}}", "{{LANDER_GAME}}{{LANDER_GAME}}", 1),
                template.replace("{{LANDER_GAME}}", "<div>{{LANDER_GAME}}</div>", 1),
                template.replace("{{LANDER_GAME}}", "", 1).replace("</footer>", "{{LANDER_GAME}}</footer>", 1),
            )
            for changed in variants:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    site_builder._validate_template(name, changed)

    def test_footer_game_link_is_final_icon_only_and_accessibly_named(self) -> None:
        for name in SHELL_TEMPLATES:
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            footer_tag = re.search(r'<a\s+class="footer-game-link"[\s\S]*?>', template)
            self.assertIsNotNone(footer_tag)
            assert footer_tag is not None
            label = re.search(r'aria-label="([^"]+)"', footer_tag.group(0))
            self.assertIsNotNone(label)
            assert label is not None
            variants = (
                template.replace(label.group(0), 'aria-label=""', 1),
                template.replace(
                    'alt=""\n                /></a>',
                    'alt="Rocket"\n                /></a>',
                    1,
                ),
                template.replace('class="footer-game-link"', 'class="footer-rocket"', 1),
                template.replace('href="{{SITE_BASE}}lander/"', 'href="{{SITE_BASE}}lander/#lander-game"', 1),
                template.replace("/></a>", "/>Lander</a>", 1),
            )
            for changed in variants:
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(ValueError, "footer Lander"),
                ):
                    site_builder._validate_template(name, changed)

    def test_shell_title_and_canonical_metadata_fail_closed(self) -> None:
        for name, (_title, canonical) in site_builder.TEMPLATE_METADATA.items():
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            drifted_canonical = (
                "https://agentworks.build/security/"
                if canonical != "https://agentworks.build/security/"
                else "https://agentworks.build/"
            )
            title_mutation = re.sub(r"<title>[^<]+</title>", "<title></title>", template, count=1)
            for changed in (
                title_mutation,
                template.replace(f'href="{canonical}"', f'href="{drifted_canonical}"', 1),
                template.replace(
                    f'<link rel="canonical" href="{canonical}" />',
                    f'<link rel="canonical" href="{canonical}" />\n'
                    f'        <link rel="alternate canonical" href="{canonical}" />',
                    1,
                ),
            ):
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(ValueError, "title|canonical URL|canonical link"),
                ):
                    site_builder._validate_template(name, changed)

    def test_shell_favicon_is_exact_and_shared(self) -> None:
        favicon = '<link rel="icon" type="image/svg+xml" href="{{SITE_BASE}}assets/agw-favicon.svg" />'
        for name in site_builder.TEMPLATE_METADATA:
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            self.assertEqual(template.count(favicon), 1)
            variants = (
                template.replace(favicon, "", 1),
                template.replace("assets/agw-favicon.svg", "assets/agw-rocket.svg", 1),
                template.replace('type="image/svg+xml"', 'type="image/png"', 1),
                template.replace(favicon, f"{favicon}\n        {favicon}", 1),
                template.replace(
                    favicon,
                    f'{favicon}\n        <link rel="shortcut icon" href="{{{{SITE_BASE}}}}assets/agw-favicon.svg" />',
                    1,
                ),
            )
            for changed in variants:
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(ValueError, "favicon"),
                ):
                    site_builder._validate_template(name, changed)

    def test_game_shell_description_structure_and_csp_are_shared(self) -> None:
        policies = []
        for name in ("lander.html", "404.html"):
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            document = parse(template)
            csp = next(meta["content"] for meta in document.tags("meta") if meta.get("http-equiv"))
            description = next(meta for meta in document.tags("meta") if meta.get("name") == "description")
            policies.append(csp)
            for changed in (
                template.replace(csp, "default-src 'self'", 1),
                template.replace(description["content"], "", 1),
                template.replace(description["content"], "   ", 1),
                template.replace('name="description"', 'name="description" data-copy="duplicate"', 1),
                template.replace(
                    f'<meta name="description" content="{description["content"]}" />',
                    f'<meta name="description" content="{description["content"]}" />\n'
                    f'        <meta name="description" content="{description["content"]}" />',
                    1,
                ),
            ):
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(ValueError, "description|Content Security"),
                ):
                    site_builder._validate_template(name, changed)
        self.assertEqual(policies, [CSP, CSP])

    def test_duplicate_attributes_cannot_bypass_template_contracts(self) -> None:
        template = (self.root / "website/templates/index.html").read_text(encoding="utf-8")
        variants = (
            template.replace(
                'name="description" content="{{HOME_META_DESCRIPTION}}"',
                'name="description" content="safe" content="{{HOME_META_DESCRIPTION}}"',
            ),
            template.replace(
                '<section class="identity-panel" aria-labelledby="home-heading">',
                '<section class="unreviewed" class="identity-panel" aria-labelledby="home-heading">',
            ),
            template.replace(
                'aria-labelledby="onboarding-heading">',
                'aria-labelledby="other" aria-labelledby="onboarding-heading">',
                1,
            ),
        )
        for changed in variants:
            with (
                self.subTest(change=changed[:200]),
                self.assertRaisesRegex(ValueError, "duplicate HTML attribute"),
            ):
                site_builder._validate_template("index.html", changed)

    def test_security_template_rejects_email_address_reporting_canary(self) -> None:
        template = (self.root / "website/templates/security.html").read_text(encoding="utf-8")
        for address_text in (
            "Report privately to security@example.test",
            "Report privately to security@example.test.",
        ):
            changed = template.replace("</main>", f"<p>{address_text}</p>\n</main>")
            with (
                self.subTest(address_text=address_text),
                self.assertRaisesRegex(ValueError, "email reporting paths are forbidden"),
            ):
                site_builder._validate_template("security.html", changed)

    def test_long_form_tokens_cannot_move_out_of_reviewed_article_or_metadata(
        self,
    ) -> None:
        for name, prefix in (
            ("manifesto.html", "MANIFESTO"),
            ("security.html", "SECURITY"),
        ):
            template = (self.root / "website/templates" / name).read_text(encoding="utf-8")
            content_token = f"{{{{{prefix}_CONTENT}}}}"
            variants = (
                template.replace(
                    f'content="{{{{{prefix}_META_DESCRIPTION}}}}"',
                    f'data-copy="{{{{{prefix}_META_DESCRIPTION}}}}"',
                ),
                template.replace(
                    f'<article class="long-form-content sourced-content">{content_token}</article>',
                    f"<div>{content_token}</div>",
                ),
                template.replace(content_token, f"prefix {content_token}", 1),
            )
            for changed in variants:
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(
                        ValueError,
                        "content token|metadata token|long-form article|block token",
                    ),
                ):
                    site_builder._validate_template(name, changed)
