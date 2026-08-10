# ruff: noqa: F405

from site_test_support import *  # noqa: F403


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
            "manifesto": (self.output / "manifesto/index.html").read_text(encoding="utf-8"),
            "security": (self.output / "security/index.html").read_text(encoding="utf-8"),
            "lander": (self.output / "lander/index.html").read_text(encoding="utf-8"),
            "404": (self.output / "404.html").read_text(encoding="utf-8"),
        }
        self.documents = {name: parse(source) for name, source in self.pages.items()}

    def test_all_pages_have_metadata_landmarks_skip_link_and_one_h1(self) -> None:
        expected = {
            "home": ("Agentworks", "https://agentworks.build/"),
            "manifesto": (
                "Agentworks Manifesto",
                "https://agentworks.build/manifesto/",
            ),
            "security": ("Security | Agentworks", "https://agentworks.build/security/"),
            "lander": (
                "Lunar deployment | Agentworks",
                "https://agentworks.build/lander/",
            ),
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

    def test_home_outline_and_interim_guards_are_exact(self) -> None:
        document = self.documents["home"]
        self.assertEqual(document.headings, ["Agentworks", "Guided onboarding"])
        self.assertNotIn("problem", document.ids)
        self.assertNotIn("principles", document.ids)
        self.assertNotIn(
            "Anyone who has had more than a few parallel agentic sessions",
            self.pages["home"],
        )
        self.assertNotIn("A few convictions shape the whole design", self.pages["home"])
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
        hero = [image for image in document.tags("img") if image.get("class") == "hero-mark"]
        self.assertEqual(len(hero), 1)
        self.assertEqual(hero[0].get("src"), "/assets/agw-rocket.svg")
        self.assertEqual(hero[0].get("alt"), "AGW rocket mark")
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

    def test_shared_header_footer_breadcrumb_icons_and_logo_exception_are_exact(
        self,
    ) -> None:
        current_labels = {
            "home": "Home",
            "manifesto": "Manifesto",
            "security": "Security",
            "lander": "Lander",
            "404": "404",
        }
        for name, document in self.documents.items():
            with self.subTest(name=name):
                hrefs = [anchor.get("href") for anchor in document.tags("a")]
                for destination in (
                    "/",
                    site_builder.REPOSITORY_URL,
                    site_builder.PYPI_URL,
                    "/manifesto/",
                    "/security/",
                    "/lander/#lander-game",
                ):
                    self.assertEqual(hrefs.count(destination), 1)
                currents = [tag for tag in document.tags("span") if tag.get("aria-current") == "page"]
                self.assertEqual(len(currents), 1)
                self.assertIn(
                    f'aria-current="page">{current_labels[name]}</span>',
                    self.pages[name],
                )
                separators = [tag for tag in document.tags("span") if tag.get("class") == "breadcrumb-separator"]
                self.assertEqual([tag.get("aria-hidden") for tag in separators], ["true"])
                icons = [tag for tag in document.tags("svg") if tag.get("class") == "service-icon"]
                self.assertEqual(len(icons), 2)
                self.assertTrue(all(icon.get("aria-hidden") == "true" for icon in icons))
                self.assertTrue(all(icon.get("focusable") == "false" for icon in icons))
                header_marks = [image for image in document.tags("img") if image.get("class") == "header-mark"]
                self.assertEqual(len(header_marks), 0 if name == "home" else 1)
                if header_marks:
                    self.assertEqual(header_marks[0].get("alt"), "")
                self.assertEqual(self.pages[name].count("Product of Wayfarer Labs, LLC"), 1)
                self.assertEqual(self.pages[name].count(">Agentworks Manifesto</a>"), 1)
                self.assertEqual(self.pages[name].count(">We take security seriously</a>"), 1)
                game_links = [tag for tag in document.tags("a") if tag.get("class") == "footer-game-link"]
                self.assertEqual(
                    game_links,
                    [
                        {
                            "class": "footer-game-link",
                            "href": "/lander/#lander-game",
                            "aria-label": "Play Lunar Lander",
                        }
                    ],
                )
                game_marks = [image for image in document.tags("img") if image.get("class") == "footer-game-mark"]
                self.assertEqual(len(game_marks), 1)
                self.assertEqual(game_marks[0].get("alt"), "")

    def test_manifesto_is_complete_semantic_source_projection_with_mapped_links(
        self,
    ) -> None:
        document = self.documents["manifesto"]
        self.assertIn(
            '<main id="main-content" class="manifesto-main detail-main">',
            self.pages["manifesto"],
        )
        self.assertNotIn("Repository sourced / Long-form argument", self.pages["manifesto"])
        source = (self.root / site_builder.MANIFESTO_CONTRACT.source).read_text(encoding="utf-8")
        expected_headings = [
            match.group(2).strip()
            for line in source.splitlines()
            if (match := re.fullmatch(r"(#{1,6})[ \t]+(.+?)", line))
        ]
        self.assertEqual(document.headings, expected_headings)
        for passage in (
            "Agentworks is opinionated",
            "Anyone who has had more than a few parallel agentic sessions",
            "A significant and growing part of the ecosystem",
            "This gives agents two modes",
            "Environment variables and secrets are first-class",
        ):
            self.assertIn(passage, self.pages["manifesto"])
        for source, destination in site_builder.SOURCE_RELATIVE_URLS.items():
            self.assertNotIn(f'href="{source}"', self.pages["manifesto"])
            self.assertIn(f'href="{destination}"', self.pages["manifesto"])
        self.assertFalse(document.tags("script"))

    def test_security_outline_and_reporting_links_are_exact(self) -> None:
        document = self.documents["security"]
        self.assertIn('<main id="main-content" class="detail-main">', self.pages["security"])
        self.assertNotIn("Security model / Repository sourced", self.pages["security"])
        self.assertEqual(
            document.headings,
            [
                "Security at Agentworks",
                "Reporting a vulnerability",
                "Threat model",
                "Boundaries and current limitations",
                "Operator posture",
                "Credentials and secrets",
                "Scope and upstream guidance",
            ],
        )
        hrefs = [anchor.get("href") for anchor in document.tags("a")]
        self.assertEqual(hrefs.count(site_builder.REPORTING_URL), 1)
        self.assertIn("https://github.com/WayfarerLabs/agentworks/issues/224", hrefs)
        self.assertNotIn("email", self.pages["security"].lower())
        self.assertFalse(document.tags("script"))

    def test_404_retains_fallback_and_has_only_its_local_module(self) -> None:
        document = self.documents["404"]
        self.assertIn("Page not found", self.pages["404"])
        self.assertFalse([tag for tag in document.tags("a") if tag.get("id") == "home-link"])
        self.assertNotIn("Return to agentworks.build", self.pages["404"])
        self.assertNotIn('class="error-code"', self.pages["404"])
        self.assertEqual([tag.get("href") for tag in document.tags("a")].count("/"), 1)
        scripts = document.tags("script")
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0]["src"], "/static/lander-game.js")
        self.assertEqual(scripts[0]["type"], "module")

    def test_lander_and_404_share_one_byte_identical_game_subtree(self) -> None:
        def subtree(page: str) -> str:
            start = page.index('<section id="lander-game"')
            end = page.index("</section>", start) + len("</section>")
            return page[start:end]

        self.assertEqual(subtree(self.pages["lander"]), subtree(self.pages["404"]))
        for name in ("lander", "404"):
            document = self.documents[name]
            self.assertEqual(document.ids.count("lander-game"), 1)
            actions = next(tag for tag in document.tags("div") if tag.get("id") == "lander-actions")
            self.assertIn("hidden", actions)
            self.assertEqual(len(document.tags("script")), 1)
        for name in ("home", "manifesto", "security"):
            self.assertFalse(self.documents[name].tags("script"))

    def test_footer_game_target_and_focus_area_are_pinned_in_ordinary_flow(
        self,
    ) -> None:
        css = (self.output / "static/site.css").read_text(encoding="utf-8")
        link_rule = css.split(".footer-game-link {", 1)[1].split("}", 1)[0]
        self.assertIn("display: inline-flex", link_rule)
        self.assertIn("width: 24px", link_rule)
        self.assertIn("height: 24px", link_rule)
        self.assertNotIn("position:", link_rule)
        self.assertIn("a:focus-visible", css)

    def test_runtime_assets_are_local_and_privacy_surfaces_are_absent(self) -> None:
        for page in self.pages.values():
            document = parse(page)
            for tag_name, attribute in (
                ("script", "src"),
                ("link", "href"),
                ("img", "src"),
            ):
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
        for fake_terminal in (
            "window-control",
            "crt",
            "green-on-black",
            "prompt-glyph",
        ):
            self.assertNotIn(fake_terminal, lowered)

    def test_home_identity_grid_and_rocket_hero_reflow_at_desktop_width(self) -> None:
        css = (self.output / "static/site.css").read_text(encoding="utf-8")
        heading_rule = css.split(".identity-panel h1 {", 1)[1].split("}", 1)[0]
        self.assertIn("max-width: 100%", heading_rule)
        self.assertIn("font-size: clamp(2.7rem, 6vw, 4.75rem)", heading_rule)
        default_identity = css.split(".identity-panel {", 1)[1].split("}", 1)[0]
        self.assertNotIn("grid-template-columns", default_identity)
        desktop = css.split("@media (min-width: 48rem)", 1)[1]
        identity_rule = desktop.split(".identity-panel {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", identity_rule)
        hero_rule = css.split(".hero-mark {", 1)[1].split("}", 1)[0]
        self.assertIn("width: clamp(3.2rem, 12vw, 4.8rem)", hero_rule)
        self.assertIn("height: clamp(4.8rem, 18vw, 7.2rem)", hero_rule)
        self.assertIn("object-fit: contain", hero_rule)
        home_main_rule = css.split(".home-main {", 1)[1].split("}", 1)[0]
        self.assertIn("gap: clamp(1.5rem, 4vw, 2.75rem)", home_main_rule)
        detail_main_rule = css.split(".detail-main {", 1)[1].split("}", 1)[0]
        self.assertIn("padding-block-start: clamp(0.75rem, 2vw, 1.25rem)", detail_main_rule)
        detail_heading_rule = css.split(".detail-main .page-heading {", 1)[1].split("}", 1)[0]
        self.assertIn("padding-top: 0", detail_heading_rule)

    def test_pinned_color_contrasts_meet_text_component_and_status_thresholds(
        self,
    ) -> None:
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
