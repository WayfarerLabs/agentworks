# ruff: noqa: F405

import html
import json
import threading
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from site_test_support import *  # noqa: F403


def plain_markdown(value: str) -> str:
    protected: list[str] = []

    def protect(match) -> str:  # noqa: ANN001
        protected.append(match.group(1))
        return f"\0{len(protected) - 1}\0"

    value = re.sub(r"`([^`]+)`", protect, value)
    value = re.sub(r"\[([^]]+)](?:\([^)]*\)|\[[^]]+])", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"_([^_]+)_", r"\1", value)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", value)
    for index, text_value in enumerate(protected):
        value = value.replace(f"\0{index}\0", text_value)
    return " ".join(value.split())


def source_semantics(source: str) -> tuple[list[tuple[str, str]], list[str]]:
    lines = source.splitlines()
    definitions: dict[str, str] = {}
    skipped: set[int] = set()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"[ ]{0,3}\[([^]]+)]\:[ \t]*(\S*)[ \t]*", line)
        if match is None:
            continue
        label, destination = match.groups()
        skipped.add(index)
        if not destination:
            destination = lines[index + 1].strip()
            skipped.add(index + 1)
        definitions[label] = destination

    blocks: list[tuple[str, str]] = []
    links: list[str] = []
    index = 0
    while index < len(lines):
        if index in skipped or not lines[index].strip():
            index += 1
            continue
        line = lines[index].rstrip()
        if heading := re.fullmatch(r"[ ]{0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?", line):
            blocks.append((f"h{len(heading.group(1))}", plain_markdown(heading.group(2))))
            index += 1
            continue
        if item := re.fullmatch(r"( {0,3})[*+-][ \t]+(.+)", line):
            list_indent = item.group(1)
            list_item = re.compile(rf"({re.escape(list_indent)})[*+-][ \t]+(.+)")
            while item is not None:
                parts = [item.group(2)]
                index += 1
                while (
                    index < len(lines)
                    and list_item.fullmatch(lines[index].rstrip()) is None
                    and re.match(r"^[ ]{2,}\S", lines[index])
                ):
                    parts.append(lines[index].strip())
                    index += 1
                blocks.append(("li", plain_markdown(" ".join(parts))))
                item = list_item.fullmatch(lines[index].rstrip()) if index < len(lines) else None
            continue
        parts = [line]
        index += 1
        while index < len(lines) and index not in skipped:
            continuation = lines[index].rstrip()
            if (
                not continuation
                or re.fullmatch(r"[ ]{0,3}#{1,6}[ \t]+(?:.*?)(?:[ \t]+#+[ \t]*)?", continuation)
                or re.fullmatch(r"[ ]{0,3}[*+-][ \t]+.+", continuation)
            ):
                break
            parts.append(continuation)
            index += 1
        blocks.append(("p", plain_markdown(" ".join(parts))))

    for match in re.finditer(r"\[[^]]+]\(([^)]+)\)|\[[^]]+]\[([^]]+)]", source):
        destination, label = match.groups()
        if label is not None:
            links.append(definitions[label])
        else:
            links.append(site_builder.SOURCE_RELATIVE_URLS.get(destination, destination))
    return blocks, links


class LongFormSemantics(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_article = False
        self.in_toc = False
        self.active_tag = ""
        self.active_text: list[str] = []
        self.blocks: list[tuple[str, str]] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "article" and attributes.get("class") == "long-form-content sourced-content":
            self.in_article = True
        elif self.in_article and tag == "nav" and attributes.get("class") == "page-toc":
            self.in_toc = True
        elif self.in_article and not self.in_toc and tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}:
            self.active_tag = tag
            self.active_text = []
        if self.in_article and not self.in_toc and tag == "a":
            self.links.append(str(attributes["href"]))

    def handle_endtag(self, tag: str) -> None:
        if self.in_article and tag == self.active_tag:
            self.blocks.append((tag, " ".join("".join(self.active_text).split())))
            self.active_tag = ""
            self.active_text = []
        elif self.in_toc and tag == "nav":
            self.in_toc = False
        elif tag == "article":
            self.in_article = False

    def handle_data(self, data: str) -> None:
        if self.active_tag:
            self.active_text.append(data)


class TocSemantics(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_toc = False
        self.list_depth = 0
        self.active_link: tuple[int, str, list[str]] | None = None
        self.nav_attributes: list[dict[str, str | None]] = []
        self.entries: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "nav" and attributes.get("class") == "page-toc":
            self.in_toc = True
            self.nav_attributes.append(attributes)
        elif self.in_toc and tag == "ol":
            self.list_depth += 1
        elif self.in_toc and tag == "a":
            self.active_link = (self.list_depth + 1, str(attributes["href"]), [])

    def handle_endtag(self, tag: str) -> None:
        if self.active_link is not None and tag == "a":
            level, destination, parts = self.active_link
            self.entries.append((level, destination, " ".join("".join(parts).split())))
            self.active_link = None
        elif self.in_toc and tag == "ol":
            self.list_depth -= 1
        elif self.in_toc and tag == "nav":
            self.in_toc = False

    def handle_data(self, data: str) -> None:
        if self.active_link is not None:
            self.active_link[2].append(data)


def source_toc(source: str) -> list[tuple[int, str, str]]:
    entries: list[tuple[int, str, str]] = []
    for line in source.splitlines():
        match = re.fullmatch(r"[ ]{0,3}(#{2,3})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?", line.rstrip())
        if match is None:
            continue
        label = plain_markdown(match.group(2))
        identifier = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        entries.append((len(match.group(1)), f"#{identifier}", label))
    return entries


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def browser_geometry(output: Path, width: int) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser") if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for responsive website geometry tests")
    harness = output / "geometry.html"
    manifesto = (output / "manifesto/index.html").read_text(encoding="utf-8")
    harness.write_text(
        manifesto.replace(
            "</body>",
            '<pre id="result">pending</pre><script src="/geometry.js"></script></body>',
            1,
        ),
        encoding="utf-8",
    )
    (output / "geometry.js").write_text(
        """const rect = (selector) => {
    const value = document.querySelector(selector).getBoundingClientRect();
    return {top: value.top, right: value.right, bottom: value.bottom, left: value.left};
};
const layout = document.querySelector(".long-form-layout");
document.querySelector("#result").textContent = JSON.stringify({
    display: getComputedStyle(layout).display,
    title: rect(".long-form-layout > h1"),
    toc: rect(".long-form-layout > .page-toc"),
    body: rect(".long-form-layout > .long-form-body"),
});
""",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.TemporaryDirectory()
    try:
        port = server.server_address[1]
        completed = subprocess.run(
            (
                chromium,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--user-data-dir={profile.name}",
                f"--window-size={width},1000",
                "--virtual-time-budget=1000",
                "--dump-dom",
                f"http://127.0.0.1:{port}/geometry.html",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        profile.cleanup()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    match = re.search(r'<pre id="result">([^<]+)</pre>', completed.stdout)
    if match is None or match.group(1) == "pending":
        raise AssertionError(f"Chromium did not return layout geometry: {completed.stderr[-1000:]}")
    return json.loads(html.unescape(match.group(1)))


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

    def test_home_onboarding_is_the_exact_canonical_thin_bootstrap(self) -> None:
        document = self.documents["home"]
        self.assertEqual(document.headings, ["Agentworks", "Agentworks CLI bootstrap"])
        self.assertNotIn("problem", document.ids)
        self.assertNotIn("principles", document.ids)
        self.assertFalse(document.tags("article"))
        self.assertEqual(document.text_by_id["onboarding-prompt"], ONBOARDING_PROMPT)
        self.assertEqual(
            document.text_by_id["onboarding-prompt"].encode(),
            (self.root / site_builder.ASSISTANCE_SOURCE).read_bytes(),
        )
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
        prompts = [tag for tag in document.tags("pre") if tag.get("class") == "onboarding-prompt"]
        self.assertEqual(prompts, [{"class": "onboarding-prompt"}])
        buttons = [tag for tag in document.tags("button") if tag.get("id") == "copy-onboarding-prompt"]
        self.assertEqual(
            buttons,
            [{"id": "copy-onboarding-prompt", "type": "button", "hidden": None}],
        )
        statuses = [tag for tag in document.tags("p") if tag.get("id") == "copy-status"]
        self.assertEqual(
            statuses,
            [{"id": "copy-status", "role": "status", "aria-live": "polite", "aria-atomic": "true"}],
        )
        hero = [image for image in document.tags("img") if image.get("class") == "hero-mark"]
        self.assertEqual(len(hero), 1)
        self.assertEqual(hero[0].get("src"), "/assets/agw-rocket.svg")
        self.assertEqual(hero[0].get("alt"), "AGW rocket mark")
        lowered = ONBOARDING_PROMPT.lower()
        self.assertIn("uv tool install --upgrade", lowered)
        self.assertIn("agw version", lowered)
        self.assertIn("agw guide --agent", lowered)
        forbidden = ("source review", "security posture", "protected candidate", "model usage", "startup disclosure")
        for value in forbidden:
            self.assertNotIn(value, lowered)
        scripts = document.tags("script")
        self.assertEqual(scripts, [{"type": "module", "src": "/static/onboarding-copy.js"}])

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

    def test_long_form_pages_are_complete_semantic_source_projections(self) -> None:
        pages = {
            "manifesto": site_builder.MANIFESTO_CONTRACT,
            "security": site_builder.SECURITY_CONTRACT,
        }
        for name, contract in pages.items():
            source = (self.root / contract.source).read_text(encoding="utf-8")
            expected_blocks, expected_links = source_semantics(source)
            rendered = LongFormSemantics()
            rendered.feed(self.pages[name])
            with self.subTest(name=name):
                self.assertEqual(rendered.blocks, expected_blocks)
                self.assertEqual(rendered.links, expected_links)
                self.assertFalse(self.documents[name].tags("script"))
    def test_long_form_contents_navigation_matches_source_h2_h3_structure(self) -> None:
        pages = {
            "manifesto": site_builder.MANIFESTO_CONTRACT,
            "security": site_builder.SECURITY_CONTRACT,
        }
        for name, contract in pages.items():
            source = (self.root / contract.source).read_text(encoding="utf-8")
            toc = TocSemantics()
            toc.feed(self.pages[name])
            article_children = [
                tag
                for tag, _, ancestors in self.documents[name].elements
                if ancestors
                and ancestors[-1][0] == "article"
                and ancestors[-1][1].get("class") == "long-form-content sourced-content"
            ]
            with self.subTest(name=name):
                self.assertEqual(toc.nav_attributes, [{"class": "page-toc", "aria-label": "On this page"}])
                self.assertEqual(toc.entries, source_toc(source))
                self.assertEqual(article_children, ["div"])
                layout_children = [
                    tag
                    for tag, _, ancestors in self.documents[name].elements
                    if ancestors and ancestors[-1][0] == "div" and ancestors[-1][1].get("class") == "long-form-layout"
                ]
                self.assertEqual(layout_children, ["h1", "nav", "div"])
                for _, destination, _ in toc.entries:
                    self.assertEqual(self.documents[name].ids.count(destination[1:]), 1)

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
        self.assertEqual(
            self.documents["home"].tags("script"),
            [{"type": "module", "src": "/static/onboarding-copy.js"}],
        )
        for name in ("manifesto", "security"):
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

    def test_long_form_contents_navigation_is_inline_then_becomes_a_left_rail(self) -> None:
        css = (self.output / "static/site.css").read_text(encoding="utf-8")
        default_toc = css.split(".page-toc {", 1)[1].split("}", 1)[0]
        self.assertIn("margin-block: 1.5rem 2.5rem", default_toc)
        self.assertIn("font-size: 0.92rem", default_toc)
        wide = css.split("@media (min-width: 64rem)", 1)[1]
        wide_layout = wide.split(".long-form-layout {", 1)[1].split("}", 1)[0]
        wide_toc = wide.split(".long-form-layout > .page-toc {", 1)[1].split("}", 1)[0]
        wide_body = wide.split(".long-form-body {", 1)[1].split("}", 1)[0]
        self.assertIn("display: grid", wide_layout)
        self.assertIn("grid-template-columns: minmax(10rem, 13rem) minmax(0, 1fr)", wide_layout)
        self.assertIn("grid-column: 1", wide_toc)
        self.assertIn("grid-row: 1 / span 2", wide_toc)
        self.assertIn("grid-column: 2", wide_body)
        self.assertIn("grid-row: 2", wide_body)

    def test_chromium_geometry_keeps_wide_body_beside_toc_and_narrow_toc_inline(self) -> None:
        wide = browser_geometry(self.output, 1600)
        narrow = browser_geometry(self.output, 390)
        wide_title = wide["title"]
        wide_toc = wide["toc"]
        wide_body = wide["body"]
        self.assertEqual(wide["display"], "grid")
        self.assertLess(wide_toc["right"], wide_title["left"])
        self.assertAlmostEqual(wide_title["left"], wide_body["left"], delta=1)
        self.assertGreaterEqual(wide_body["top"], wide_title["bottom"])
        self.assertLess(wide_body["top"], wide_toc["bottom"])

        narrow_title = narrow["title"]
        narrow_toc = narrow["toc"]
        narrow_body = narrow["body"]
        self.assertEqual(narrow["display"], "block")
        self.assertLessEqual(narrow_title["bottom"], narrow_toc["top"])
        self.assertLessEqual(narrow_toc["bottom"], narrow_body["top"])

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
