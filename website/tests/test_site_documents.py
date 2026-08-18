# ruff: noqa: F405

import json
import sys
import threading
import time
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from chromium_test_support import DevToolsConnection, cleanup_profile, devtools_target
from site_test_support import *  # noqa: F403


# WCAG 2.2 AA: 1.4.3 for normal-size text, 1.4.11 for graphics and interface parts.
TEXT_CONTRAST = 4.5
NON_TEXT_CONTRAST = 3.0


def css_tokens(output: Path) -> dict[str, str]:
    """The custom properties the built stylesheet declares on the document root."""
    css = (output / "static/site.css").read_text(encoding="utf-8")
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-fA-F]{6})\b", root))


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


def browser_geometry(
    output: Path,
    width: int,
    *,
    chromium_path: str | None = None,
    connection_factory: Callable[[str], DevToolsConnection] = DevToolsConnection,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    target_factory: Callable[[Path, subprocess.Popen[bytes]], str] = devtools_target,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    chromium = chromium_path or next(
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
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="site-geometry-browser-server")
    thread.start()
    profile = tempfile.TemporaryDirectory()
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    try:
        port = server.server_address[1]
        process = popen_factory(
            (
                chromium,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-allow-origins=*",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile.name}",
                "about:blank",
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        connection = connection_factory(target_factory(Path(profile.name), process))
        for domain in ("Runtime", "Page"):
            connection.call(f"{domain}.enable")
        connection.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        )
        loaded_url = f"http://127.0.0.1:{port}/geometry.html"
        connection.call("Page.navigate", {"url": loaded_url})
        readiness = (
            f"location.href === {json.dumps(loaded_url)} && "
            "document.readyState === 'complete' && "
            "document.querySelector('#result')?.textContent !== 'pending'"
        )
        for _ in range(200):
            if connection.evaluate(readiness):
                break
            sleep(0.025)
        else:
            raise AssertionError("Chromium did not return responsive layout geometry")
        result = connection.evaluate("JSON.parse(document.querySelector('#result').textContent)")
        if not isinstance(result, dict):
            raise AssertionError("Chromium returned invalid responsive layout geometry")
        return result
    finally:
        active_error = sys.exception()
        cleanup_errors: list[BaseException] = []

        def cleanup(action: Callable[[], object]) -> None:
            try:
                action()
            except BaseException as error:
                cleanup_errors.append(error)

        if connection is not None:
            cleanup(connection.close)
        if process is not None and process.poll() is None:
            cleanup(process.terminate)
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cleanup(process.kill)
                    cleanup(lambda: process.wait(timeout=5))
        cleanup(server.shutdown)
        cleanup(server.server_close)
        cleanup(lambda: thread.join(timeout=5))
        cleanup(lambda: cleanup_profile(profile))
        if active_error is None and cleanup_errors:
            raise cleanup_errors[0]


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
            "lander": (None, "https://agentworks.build/lander/"),
            "404": (None, "https://agentworks.build/404.html"),
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
                if expected[name][0] is not None:
                    self.assertIn(f"<title>{expected[name][0]}</title>", self.pages[name])
                canonical = [tag for tag in document.tags("link") if tag.get("rel") == "canonical"]
                self.assertEqual(canonical[0]["href"], expected[name][1])
                self.assertTrue(any(tag.get("name") == "description" for tag in document.tags("meta")))
                policies = [tag for tag in document.tags("meta") if tag.get("http-equiv") == "Content-Security-Policy"]
                self.assertEqual(policies[0]["content"], CSP)
                skip_links = [tag for tag in document.tags("a") if tag.get("class") == "skip-link"]
                self.assertEqual([tag.get("href") for tag in skip_links], ["#main-content"])

    def test_home_onboarding_preserves_both_progressively_enhanced_paths(self) -> None:
        document = self.documents["home"]
        self.assertNotIn("problem", document.ids)
        self.assertNotIn("principles", document.ids)
        self.assertFalse(document.tags("article"))
        self.assertEqual(document.text_by_id["onboarding-prompt"], ONBOARDING_PROMPT)
        self.assertEqual(
            document.text_by_id["onboarding-prompt"].encode(),
            (self.root / site_builder.AGENT_ONBOARDING_PROMPT_SOURCE).read_bytes(),
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
        tab_lists = [tag for tag in document.tags("div") if tag.get("id") == "onboarding-tab-list"]
        self.assertEqual(len(tab_lists), 1)
        self.assertEqual(
            {key: value for key, value in tab_lists[0].items() if key != "aria-label"},
            {"id": "onboarding-tab-list", "class": "onboarding-tabs", "hidden": None},
        )
        self.assertTrue(str(tab_lists[0].get("aria-label") or "").strip())
        tabs = [tag for tag in document.tags("button") if tag.get("data-panel")]
        self.assertEqual(
            [(tab.get("id"), tab.get("type"), tab.get("data-panel")) for tab in tabs],
            [
                ("via-agent-tab", "button", "via-agent-panel"),
                ("manual-tab", "button", "manual-panel"),
            ],
        )
        panels = [
            attributes
            for tag, attributes, _ in document.elements
            if tag == "section" and attributes.get("class") == "onboarding-panel"
        ]
        self.assertEqual(
            [(panel.get("id"), panel.get("aria-labelledby"), panel.get("hidden")) for panel in panels],
            [
                ("via-agent-panel", "via-agent-heading", None),
                ("manual-panel", "manual-heading", None),
            ],
        )
        prompts = [tag for tag in document.tags("pre") if tag.get("class") == "onboarding-prompt"]
        self.assertEqual(prompts, [{"class": "onboarding-prompt"}])
        buttons = [tag for tag in document.tags("button") if tag.get("id") == "copy-onboarding-prompt"]
        self.assertEqual(
            buttons,
            [
                {
                    "id": "copy-onboarding-prompt",
                    "class": "copy-prompt-button",
                    "type": "button",
                    "aria-label": buttons[0]["aria-label"],
                    "title": buttons[0]["aria-label"],
                    "hidden": None,
                }
            ],
        )
        self.assertTrue(buttons[0]["aria-label"].strip())
        copy_icons = [
            attributes
            for tag, attributes, ancestors in document.elements
            if tag == "svg"
            and any(parent == "button" and attrs.get("id") == "copy-onboarding-prompt" for parent, attrs in ancestors)
        ]
        self.assertEqual(copy_icons, [{"aria-hidden": "true", "focusable": "false", "viewbox": "0 0 24 24"}])
        statuses = [tag for tag in document.tags("p") if tag.get("id") == "copy-status"]
        self.assertEqual(
            statuses,
            [
                {
                    "id": "copy-status",
                    "class": "copy-status",
                    "role": "status",
                    "aria-live": "polite",
                    "aria-atomic": "true",
                }
            ],
        )
        old_school_commands = [tag for tag in document.tags("pre") if tag.get("class") == "onboarding-commands"]
        self.assertEqual(old_school_commands, [{"class": "onboarding-commands"}, {"class": "onboarding-commands"}])
        self.assertEqual(
            [tag.get("href") for tag in document.tags("a")].count(site_builder.REPOSITORY_URL),
            2,
        )
        hero = [image for image in document.tags("img") if image.get("class") == "hero-mark"]
        self.assertEqual(len(hero), 1)
        self.assertEqual(hero[0].get("src"), "/assets/agw-rocket.svg")
        self.assertEqual(hero[0].get("alt"), "AGW rocket mark")
        scripts = document.tags("script")
        self.assertEqual(scripts, [{"type": "module", "src": "/static/onboarding.js"}])

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
                    "/lander/",
                ):
                    expected_count = 2 if name == "home" and destination == site_builder.REPOSITORY_URL else 1
                    self.assertEqual(hrefs.count(destination), expected_count)
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
                self.assertEqual(len(game_links), 1)
                game_link = game_links[0]
                self.assertEqual(game_link.get("href"), "/lander/")
                self.assertTrue(game_link.get("aria-label"))
                self.assertEqual(game_link.get("title"), game_link.get("aria-label"))
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
        self.assertFalse([tag for tag in document.tags("a") if tag.get("id") == "home-link"])
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
            outcome = next(tag for tag in document.tags("div") if tag.get("id") == "lander-outcome")
            self.assertIn("hidden", outcome)
            self.assertEqual(len(document.tags("script")), 1)
        self.assertEqual(
            self.documents["home"].tags("script"),
            [{"type": "module", "src": "/static/onboarding.js"}],
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

    def test_chromium_geometry_owns_the_devtools_process_and_cleanup(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.terminated = False
                self.killed = False

            def poll(self) -> int | None:
                return 0 if self.terminated or self.killed else None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return 0

        class FakeConnection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object] | None]] = []
                self.closed = False

            def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
                self.calls.append((method, params))
                return {}

            def evaluate(self, expression: str) -> object:
                if expression.startswith("JSON.parse"):
                    return {"display": "block", "title": {}, "toc": {}, "body": {}}
                return True

            def close(self) -> None:
                self.closed = True

        process = FakeProcess()
        connection = FakeConnection()
        spawn = mock.Mock(return_value=process)
        result = browser_geometry(
            self.output,
            390,
            chromium_path="chromium-test",
            connection_factory=lambda url: connection,
            popen_factory=spawn,
            target_factory=lambda profile, owned: "ws://chromium.test",
        )

        command = spawn.call_args.args[0]
        self.assertIn("--remote-debugging-port=0", command)
        self.assertEqual(command[-1], "about:blank")
        self.assertEqual(result["display"], "block")
        self.assertTrue(connection.closed)
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertFalse(any(
            thread.name == "site-geometry-browser-server" and thread.is_alive()
            for thread in threading.enumerate()
        ))

    def test_chromium_geometry_bounds_readiness_and_kills_a_stuck_process(self) -> None:
        class StuckProcess:
            def __init__(self) -> None:
                self.terminated = False
                self.killed = False
                self.waits = 0

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("chromium-test", 5)
                return 0

        class NeverReadyConnection:
            def __init__(self) -> None:
                self.expressions: list[str] = []
                self.closed = False

            def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
                del method, params
                return {}

            def evaluate(self, expression: str) -> bool:
                self.expressions.append(expression)
                return False

            def close(self) -> None:
                self.closed = True

        process = StuckProcess()
        connection = NeverReadyConnection()
        with self.assertRaises(AssertionError):
            browser_geometry(
                self.output,
                390,
                chromium_path="chromium-test",
                connection_factory=lambda url: connection,
                popen_factory=lambda *args, **kwargs: process,
                target_factory=lambda profile, owned: "ws://chromium.test",
                sleep=lambda seconds: None,
            )

        self.assertEqual(len(connection.expressions), 200)
        self.assertTrue(connection.closed)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, 2)

    def test_chromium_geometry_preserves_primary_errors_and_surfaces_cleanup_errors(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.terminated = False

            def poll(self) -> int | None:
                return 0 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return 0

        primary = RuntimeError()
        cleanup_error = OSError()

        def run(*, evaluation_error: BaseException | None) -> None:
            class FailingConnection:
                def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
                    del method, params
                    return {}

                def evaluate(self, expression: str) -> object:
                    if evaluation_error is not None:
                        raise evaluation_error
                    if expression.startswith("JSON.parse"):
                        return {"display": "block"}
                    return True

                def close(self) -> None:
                    raise cleanup_error

            process = FakeProcess()
            browser_geometry(
                self.output,
                390,
                chromium_path="chromium-test",
                connection_factory=lambda url: FailingConnection(),
                popen_factory=lambda *args, **kwargs: process,
                target_factory=lambda profile, owned: "ws://chromium.test",
            )

        with self.assertRaises(RuntimeError) as caught:
            run(evaluation_error=primary)
        self.assertIs(caught.exception, primary)
        with self.assertRaises(OSError) as caught:
            run(evaluation_error=None)
        self.assertIs(caught.exception, cleanup_error)

    def test_palette_contrast_meets_text_and_non_text_thresholds(self) -> None:
        """Every shipped foreground stays legible against the surface it is painted on.

        The colors are read out of the built stylesheet rather than repeated here, so
        this checks the palette the site actually serves. The invariant is WCAG's
        inequality, never a particular ratio: adjusting a color is free until it
        crosses the threshold, and then it fails.
        """
        palette = css_tokens(self.output)
        for foreground, background in (("--ink", "--canvas"), ("--ink-muted", "--canvas"), ("--ink", "--panel")):
            with self.subTest(text=foreground, on=background):
                self.assertGreaterEqual(contrast(palette[foreground], palette[background]), TEXT_CONTRAST)
        for foreground, background in (("--line-subtle", "--canvas"), ("--accent", "--canvas")):
            with self.subTest(graphic=foreground, on=background):
                self.assertGreaterEqual(contrast(palette[foreground], palette[background]), NON_TEXT_CONTRAST)


if __name__ == "__main__":
    unittest.main()
