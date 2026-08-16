"""Closed template, shared-shell, CSS, and local-reference validation."""

from __future__ import annotations

import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, NamedTuple

from site_asset_validation import validate_head_links
from site_content import (
    CLI_SECRETS_URL,
    EMAIL_ADDRESS_PATTERN,
    IDEMPOTENCY_URL,
    PYPI_URL,
    README_SOURCE_URL,
    REPORTING_URL,
    REPOSITORY_URL,
)
from site_game_validation import validate_game_contract

SITE_BASE_TOKEN: Final = "{{SITE_BASE}}"
LANDER_GAME_TOKEN: Final = "{{LANDER_GAME}}"
SITE_BASE_PATTERN = re.compile(r"/(?:[A-Za-z0-9][A-Za-z0-9._~-]*/)*\Z", re.ASCII)
TOKEN_PATTERN = re.compile(r"{{[A-Z][A-Z0-9_]*}}")
HTTP_URL_PATTERN = re.compile(r"(?i)https?://")
QUOTED_PROTOCOL_RELATIVE_URL_PATTERN = re.compile(r"""["'`]//""")
STATIC_IMPORT_PATTERN = re.compile(
    r"\b(?:import|export)\s+(?:[^;\"']*?\s+from\s+)?([\"'])([^\"']+)\1",
    re.DOTALL,
)

SERVICE_ICON_PATHS: Final = {
    REPOSITORY_URL: (
        "M8 .7a7.5 7.5 0 0 0-2.4 14.6v-2c-1.8.4-2.2-.8-2.2-.8-.3-.8-.8-1-1-1.1-.7-.5.1-.5.1-.5.8.1 "
        "1.2.8 1.2.8.7 1.2 1.8.9 2.2.7.1-.5.3-.9.5-1.1-1.5-.2-3-.7-3-3.3 0-.7.2-1.3.7-1.8-.1-.2-.3-.9.1-1.8 0 "
        "0 .6-.2 2.1.7A7 7 0 0 1 8 4.1a7 7 0 0 1 1.9.3c1.5-.9 2.1-.7 2.1-.7.4.9.2 1.6.1 1.8.5.5.7 1.1.7 1.8 0 "
        "2.6-1.6 3.1-3 3.3.3.2.5.6.5 1.2v3.5A7.5 7.5 0 0 0 8 .7Z"
    ),
    PYPI_URL: (
        "M7.8 1.1c-3.4 0-3.2 1.5-3.2 1.5v1.5h3.3v.5H3.3S1 4.3 1 8s2 3.6 2 3.6h1.2V9.9s-.1-2 2-2h3.3s1.9 0 "
        "1.9-1.8V3s.3-1.9-3.6-1.9Zm-1.8 1a.6.6 0 1 1 0 1.2.6.6 0 0 1 0-1.2Zm2.2 12.8c3.4 0 3.2-1.5 "
        "3.2-1.5v-1.5H8.1v-.5h4.6S15 11.7 15 8s-2-3.6-2-3.6h-1.2v1.7s.1 2-2 2H6.5s-1.9 0-1.9 1.8V13s-.3 1.9 "
        "3.6 1.9Zm1.8-1a.6.6 0 1 1 0-1.2.6.6 0 0 1 0 1.2Z"
    ),
}
# fmt: off
APPROVED_EXTERNAL_URLS: Final = frozenset({
    "https://agentworks.build/", "https://agentworks.build/manifesto/",
    "https://agentworks.build/security/", "https://agentworks.build/lander/",
    "https://agentworks.build/404.html", REPOSITORY_URL, PYPI_URL, README_SOURCE_URL,
    IDEMPOTENCY_URL, f"{REPOSITORY_URL}/security/policy", f"{REPOSITORY_URL}/issues/224",
    CLI_SECRETS_URL, REPORTING_URL,
})
SHELL_DESTINATION_LABELS: Final = {REPOSITORY_URL: "GitHub", PYPI_URL: "PyPI", f"{SITE_BASE_TOKEN}manifesto/": "Agentworks Manifesto", f"{SITE_BASE_TOKEN}security/": "We take security seriously"}  # noqa: E501
CURRENT_PAGE_LABELS: Final = {"index.html": "Home", "manifesto.html": "Manifesto", "security.html": "Security", "lander.html": "Lander", "404.html": "404"}  # noqa: E501
TEMPLATE_METADATA: Final[dict[str, tuple[str | None, str]]] = {
    "index.html": ("Agentworks", "https://agentworks.build/"),
    "manifesto.html": ("Agentworks Manifesto", "https://agentworks.build/manifesto/"),
    "security.html": ("Security | Agentworks", "https://agentworks.build/security/"),
    "lander.html": (None, "https://agentworks.build/lander/"),
    "404.html": (None, "https://agentworks.build/404.html"),
}
GAME_CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
    "connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
TEMPLATE_DESTINATIONS: Final = {
    "index.html": Path("index.html"),
    "manifesto.html": Path("manifesto/index.html"),
    "security.html": Path("security/index.html"),
    "lander.html": Path("lander/index.html"),
    "404.html": Path("404.html"),
}
MAIN_ATTRIBUTES: Final = {
    "index.html": {"id": "main-content", "class": "home-main"},
    "manifesto.html": {"id": "main-content", "class": "manifesto-main detail-main"},
    "security.html": {"id": "main-content", "class": "detail-main"},
    "lander.html": {"id": "main-content", "class": "detail-main game-main"},
    "404.html": {"id": "main-content", "class": "detail-main game-main"},
}
GAME_DETAIL_TEMPLATES: Final = frozenset({"lander.html", "404.html"})
LONG_FORM_TEMPLATES: Final = frozenset({"manifesto.html", "security.html"})

REQUIRED_404_REFERENCES = {
    f'href="{SITE_BASE_TOKEN}"',
    f'href="{SITE_BASE_TOKEN}static/lander.css"',
    f'src="{SITE_BASE_TOKEN}static/lander-game.js"',
    LANDER_GAME_TOKEN,
}

TEMPLATE_TOKENS: Final = {
    "index.html": {
        SITE_BASE_TOKEN,
        "{{HOME_META_DESCRIPTION}}",
        "{{HOME_IDENTITY}}",
        "{{ONBOARDING_PROMPT}}",
    },
    "manifesto.html": {
        SITE_BASE_TOKEN,
        "{{MANIFESTO_META_DESCRIPTION}}",
        "{{MANIFESTO_CONTENT}}",
    },
    "security.html": {
        SITE_BASE_TOKEN,
        "{{SECURITY_META_DESCRIPTION}}",
        "{{SECURITY_CONTENT}}",
    },
    "404.html": {SITE_BASE_TOKEN, LANDER_GAME_TOKEN},
    "lander.html": {SITE_BASE_TOKEN, LANDER_GAME_TOKEN},
    "lander-game.html": {SITE_BASE_TOKEN},
}
TEMPLATE_REQUIRED_LITERALS: Final = {
    "index.html": {
        f'<script type="module" src="{SITE_BASE_TOKEN}static/onboarding-copy.js"></script>',
        '<button id="copy-onboarding-prompt" type="button" hidden>Copy prompt</button>',
        '<p id="copy-status" role="status" aria-live="polite" aria-atomic="true"></p>',
    },
    "manifesto.html": set(),
    "security.html": set(),
    "404.html": REQUIRED_404_REFERENCES | {LANDER_GAME_TOKEN},
    "lander.html": REQUIRED_404_REFERENCES | {LANDER_GAME_TOKEN},
    "lander-game.html": {
        f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-mark"',
        f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-engine-left"',
        f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-engine-right"',
        'id="lander-outcome" hidden',
    },
}
CONTENT_TOKEN_PLACEMENTS: Final = {
    "index.html": {
        "{{HOME_META_DESCRIPTION}}": ("meta", "description"),
        "{{HOME_IDENTITY}}": ("section-class", "identity-panel"),
        "{{ONBOARDING_PROMPT}}": ("element-id", "onboarding-prompt"),
    },
    "manifesto.html": {
        "{{MANIFESTO_META_DESCRIPTION}}": ("meta", "description"),
        "{{MANIFESTO_CONTENT}}": ("article-class", "long-form-content sourced-content"),
    },
    "security.html": {
        "{{SECURITY_META_DESCRIPTION}}": ("meta", "description"),
        "{{SECURITY_CONTENT}}": ("article-class", "long-form-content sourced-content"),
    },
    "404.html": {},
    "lander.html": {},
    "lander-game.html": {},
}
# fmt: on


def validate_site_base(value: str) -> str:
    """Validate an ASCII same-origin path made from safe URL segment characters."""
    if SITE_BASE_PATTERN.fullmatch(value) is None:
        raise ValueError("site base must be an ASCII URL path with safe slash-bounded segments")
    return value


def _attribute_map(tag: str, attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
    names = [name for name, _ in attrs]
    if len(names) != len(set(names)):
        raise ValueError(f"{tag}: duplicate HTML attribute name")
    return dict(attrs)


class _TemplatePlacementParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.placements: dict[str, list[tuple[str, str, tuple[tuple[str, dict[str, str | None]], ...]]]] = {}
        self.exact_text_placements: set[str] = set()
        self.description_content_tokens: set[str] = set()
        self.onboarding_sections: list[dict[str, str | None]] = []
        self.onboarding_headings = 0
        self.anchors: list[tuple[str, dict[str, str | None], list[str]]] = []
        self.active_anchor_indexes: list[int] = []

    def _record_attributes(self, tag: str, attributes: dict[str, str | None]) -> None:
        for attribute, value in attributes.items():
            if value is None:
                continue
            for token in TOKEN_PATTERN.findall(value):
                self.placements.setdefault(token, []).append(("attribute", f"{tag}:{attribute}", tuple(self.stack)))
                if (
                    tag == "meta"
                    and attribute == "content"
                    and attributes.get("name") == "description"
                    and value == token
                ):
                    self.description_content_tokens.add(token)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = _attribute_map(tag, attrs)
        self._record_attributes(tag, attributes)
        if tag == "a" and attributes.get("href") is not None:
            self.anchors.append((str(attributes["href"]), attributes, []))
            self.active_anchor_indexes.append(len(self.anchors) - 1)
        self.stack.append((tag, attributes))
        if tag == "section" and attributes.get("id") == "onboarding":
            self.onboarding_sections.append(attributes)
        if (
            tag == "h2"
            and attributes.get("id") == "onboarding-heading"
            and any(ancestor == "section" and values.get("id") == "onboarding" for ancestor, values in self.stack[:-1])
        ):
            self.onboarding_headings += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = _attribute_map(tag, attrs)
        self._record_attributes(tag, attributes)
        if tag == "a" and attributes.get("href") is not None:
            self.anchors.append((str(attributes["href"]), attributes, []))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.active_anchor_indexes:
            self.active_anchor_indexes.pop()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        for anchor_index in self.active_anchor_indexes:
            self.anchors[anchor_index][2].append(data)
        for token in TOKEN_PATTERN.findall(data):
            location = self.stack[-1][0] if self.stack else "document"
            self.placements.setdefault(token, []).append(("text", location, tuple(self.stack)))
            if data.strip() == token:
                self.exact_text_placements.add(token)


class _ShellElement(NamedTuple):
    tag: str
    attributes: dict[str, str | None]
    parent: int | None
    text: list[str]


class _LocalReference(NamedTuple):
    target: Path
    fragment: str | None


class _ShellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[int] = []
        self.elements: list[_ShellElement] = []

    def _append(self, tag: str, attrs: list[tuple[str, str | None]]) -> int:
        parent = self.stack[-1] if self.stack else None
        self.elements.append(_ShellElement(tag, _attribute_map(tag, attrs), parent, []))
        return len(self.elements) - 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack.append(self._append(tag, attrs))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, -1, -1):
            if self.elements[self.stack[position]].tag == tag:
                del self.stack[position:]
                return

    def handle_data(self, data: str) -> None:
        for element_index in self.stack:
            self.elements[element_index].text.append(data)


def _normalized_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _children(parser: _ShellParser, parent: int) -> list[int]:
    return [index for index, element in enumerate(parser.elements) if element.parent == parent]


def _descendants(parser: _ShellParser, parent: int, tag: str | None = None) -> list[int]:
    matches: list[int] = []
    pending = _children(parser, parent)
    while pending:
        index = pending.pop(0)
        element = parser.elements[index]
        if tag is None or element.tag == tag:
            matches.append(index)
        pending[0:0] = _children(parser, index)
    return matches


def _ancestors(parser: _ShellParser, index: int) -> list[int]:
    result: list[int] = []
    parent = parser.elements[index].parent
    while parent is not None:
        result.append(parent)
        parent = parser.elements[parent].parent
    return result


def _hidden(parser: _ShellParser, index: int) -> bool:
    for candidate in (index, *_ancestors(parser, index)):
        attributes = parser.elements[candidate].attributes
        if "hidden" in attributes or attributes.get("aria-hidden") == "true":
            return True
    return False


def _one(parser: _ShellParser, indexes: list[int], reason: str) -> int:
    if len(indexes) != 1:
        raise ValueError(reason)
    return indexes[0]


def _resolve_local_reference(reference: str | None, base: str, source: Path | None = None) -> _LocalReference | None:
    if reference is None:
        return None
    if reference.startswith("#"):
        if source is None:
            return None
        return _LocalReference(source, reference[1:])
    if not reference.startswith(base):
        return None
    location, separator, fragment = reference[len(base) :].partition("#")
    if not location or location == "index.html":
        target = Path("index.html")
    elif location.endswith("/"):
        target = Path(f"{location}index.html")
    else:
        target = Path(location)
    return _LocalReference(target, fragment if separator else None)


def _validate_visible_leaf(
    parser: _ShellParser,
    index: int,
    attributes: dict[str, str | None],
    text: str,
    reason: str,
) -> None:
    element = parser.elements[index]
    if (
        element.attributes != attributes
        or _hidden(parser, index)
        or _children(parser, index)
        or _normalized_text(element.text) != text
    ):
        raise ValueError(reason)


def _validate_service_anchor(parser: _ShellParser, index: int, destination: str, label: str) -> None:
    anchor = parser.elements[index]
    if anchor.tag != "a" or anchor.attributes != {"href": destination}:
        raise ValueError(f"service destination {destination} must use visible label {label!r}")
    if _hidden(parser, index):
        raise ValueError(f"service destination {destination} must remain visible")
    children = _children(parser, index)
    if [parser.elements[child].tag for child in children] != ["svg", "span"]:
        raise ValueError(f"service destination {destination} must contain exactly one icon before its label")
    icon = parser.elements[children[0]]
    if icon.attributes != {
        "class": "service-icon",
        "aria-hidden": "true",
        "focusable": "false",
        "viewbox": "0 0 16 16",
    }:
        raise ValueError(f"service destination {destination} requires one hidden decorative icon")
    icon_children = _children(parser, children[0])
    if (
        len(icon_children) != 1
        or parser.elements[icon_children[0]].tag != "path"
        or parser.elements[icon_children[0]].attributes != {"d": SERVICE_ICON_PATHS[destination]}
        or _children(parser, icon_children[0])
        or _normalized_text(parser.elements[icon_children[0]].text)
    ):
        raise ValueError(f"service destination {destination} requires its exact reviewed icon path")
    _validate_visible_leaf(
        parser,
        children[1],
        {},
        label,
        f"service destination {destination} has a misplaced visible label",
    )
    if _normalized_text(anchor.text) != label:
        raise ValueError(f"service destination {destination} must use visible label {label!r}")


def _validate_shared_shell(name: str, template: str) -> None:
    parser = _ShellParser()
    parser.feed(template)
    elements = parser.elements
    expected_title, expected_canonical = TEMPLATE_METADATA[name]
    if any(element.tag == "style" or "style" in element.attributes for element in elements):
        raise ValueError(f"{name}: inline style cannot alter the reviewed shell visibility")
    html_index = _one(
        parser,
        [i for i, element in enumerate(elements) if element.tag == "html"],
        f"{name}: one html root is required",
    )
    head_index = _one(
        parser,
        [i for i in _children(parser, html_index) if elements[i].tag == "head"],
        f"{name}: one head is required",
    )
    body_index = _one(
        parser,
        [i for i in _children(parser, html_index) if elements[i].tag == "body"],
        f"{name}: one body is required",
    )
    body_attributes = {"class": "game-page"} if name in {"lander.html", "404.html"} else {}
    if (
        elements[html_index].attributes != {"lang": "en"}
        or elements[head_index].attributes
        or elements[body_index].attributes != body_attributes
    ):
        raise ValueError(f"{name}: html, head, and body root attributes are invalid")
    title_index = _one(
        parser,
        [i for i in _children(parser, head_index) if elements[i].tag == "title"],
        f"{name}: one document title is required",
    )
    title = _normalized_text(elements[title_index].text)
    if not title:
        raise ValueError(f"{name}: document title must be nonempty")
    if expected_title is not None and title != expected_title:
        raise ValueError(f"{name}: document title must be {expected_title!r}")
    validate_head_links(
        name,
        [(elements[i].tag, elements[i].attributes) for i in _children(parser, head_index)],
        expected_canonical,
    )
    if name in GAME_DETAIL_TEMPLATES:
        description_index = _one(
            parser,
            [
                i
                for i in _children(parser, head_index)
                if elements[i].tag == "meta" and elements[i].attributes.get("name") == "description"
            ],
            f"{name}: one description is required",
        )
        csp_index = _one(
            parser,
            [
                i
                for i in _children(parser, head_index)
                if elements[i].tag == "meta" and elements[i].attributes.get("http-equiv") == "Content-Security-Policy"
            ],
            f"{name}: one exact Content Security Policy is required",
        )
        description_attributes = elements[description_index].attributes
        if (
            set(description_attributes) != {"name", "content"}
            or description_attributes["name"] != "description"
            or not " ".join((description_attributes["content"] or "").split())
        ):
            raise ValueError(f"{name}: description metadata is invalid")
        if elements[csp_index].attributes != {
            "http-equiv": "Content-Security-Policy",
            "content": GAME_CSP,
        }:
            raise ValueError(f"{name}: Content Security Policy is invalid")
    body_children = _children(parser, body_index)
    if [elements[index].tag for index in body_children] != [
        "a",
        "header",
        "main",
        "footer",
    ]:
        raise ValueError(f"{name}: skip link, header, main, and footer must occur once in source order")
    skip_index, header_index, main_index, footer_index = body_children
    _validate_visible_leaf(
        parser,
        skip_index,
        {"class": "skip-link", "href": "#main-content"},
        "Skip to main content",
        f"{name}: exactly one visible reviewed skip link is required",
    )
    if elements[header_index].attributes != {"class": "site-header"}:
        raise ValueError(f"{name}: header requires the exact site-header class")
    if elements[main_index].attributes != MAIN_ATTRIBUTES[name]:
        raise ValueError(f"{name}: main landmark requires its exact CSS-critical attributes")
    if elements[footer_index].attributes != {"class": "site-footer"}:
        raise ValueError(f"{name}: footer requires the exact site-footer class")
    if name in LONG_FORM_TEMPLATES:
        main_children = _children(parser, main_index)
        if len(main_children) != 1 or (
            elements[main_children[0]].tag != "article"
            or elements[main_children[0]].attributes != {"class": "long-form-content sourced-content"}
        ):
            raise ValueError(f"{name}: main must contain only its complete sourced document")
        source_token = "{{MANIFESTO_CONTENT}}" if name == "manifesto.html" else "{{SECURITY_CONTENT}}"
        article_index = main_children[0]
        if _children(parser, article_index) or _normalized_text(elements[article_index].text) != source_token:
            raise ValueError(f"{name}: long-form article must contain only its complete source token")
    if name in GAME_DETAIL_TEMPLATES:
        main_children = _children(parser, main_index)
        if not main_children or (
            elements[main_children[0]].tag != "div"
            or elements[main_children[0]].attributes != {"class": "page-heading"}
        ):
            raise ValueError(f"{name}: detail page heading must be the first main child")
        heading_children = _children(parser, main_children[0])
        if [elements[index].tag for index in heading_children] != ["h1"]:
            raise ValueError(f"{name}: detail page heading must contain only its reviewed h1")
        heading = elements[heading_children[0]]
        if (
            heading.attributes
            or _hidden(parser, heading_children[0])
            or _children(parser, heading_children[0])
            or not _normalized_text(heading.text)
        ):
            raise ValueError(f"{name}: detail page heading requires one visible nonempty h1")
        if name == "lander.html" and main_children != [main_children[0]]:
            raise ValueError(f"{name}: heading must be the only shell element before the shared game")
        if name == "404.html":
            if [elements[index].tag for index in main_children] != ["div", "p"]:
                raise ValueError(f"{name}: heading and not-found message must precede the shared game")
            message = elements[main_children[1]]
            if (
                message.attributes != {"id": "not-found-message"}
                or _hidden(parser, main_children[1])
                or _children(parser, main_children[1])
                or not _normalized_text(message.text)
            ):
                raise ValueError(f"{name}: not-found message must be visible ordinary prose")
        if name in {"lander.html", "404.html"} and any(
            marker in template for marker in ('class="error-code"', 'class="eyebrow"')
        ):
            raise ValueError(f"{name}: game detail shells cannot include pre-title labels")
    header_children = _children(parser, header_index)
    if name == "index.html":
        if [elements[index].tag for index in header_children] != ["nav", "nav"]:
            raise ValueError(f"{name}: header must contain breadcrumb then external navigation")
        breadcrumb_index, external_index = header_children
    else:
        if [elements[index].tag for index in header_children] != ["div", "nav"]:
            raise ValueError(f"{name}: header must contain identity then external navigation")
        identity_index, external_index = header_children
        if elements[identity_index].attributes != {"class": "header-identity"}:
            raise ValueError(f"{name}: header identity requires its CSS-critical class")
        identity_children = _children(parser, identity_index)
        if [elements[index].tag for index in identity_children] != ["img", "nav"]:
            raise ValueError(f"{name}: small rocket must occur immediately before the breadcrumb")
        mark_index, breadcrumb_index = identity_children
        if elements[mark_index].attributes != {
            "class": "header-mark",
            "src": f"{SITE_BASE_TOKEN}assets/agw-rocket.svg",
            "alt": "",
        }:
            raise ValueError(f"{name}: small header rocket contract is invalid")
    if elements[breadcrumb_index].attributes != {
        "class": "breadcrumbs",
        "aria-label": "Breadcrumb",
    }:
        raise ValueError(f"{name}: breadcrumb requires its exact class and accessible label")
    breadcrumb_children = _children(parser, breadcrumb_index)
    if [elements[index].tag for index in breadcrumb_children] != ["a", "span", "span"]:
        raise ValueError(f"{name}: breadcrumb must contain home, separator, and current item in order")
    home_index, separator_index, current_index = breadcrumb_children
    _validate_visible_leaf(
        parser,
        home_index,
        {"href": SITE_BASE_TOKEN},
        "Agentworks",
        f"{name}: Agentworks home crumb contract is invalid",
    )
    if (
        elements[separator_index].attributes != {"class": "breadcrumb-separator", "aria-hidden": "true"}
        or _children(parser, separator_index)
        or _normalized_text(elements[separator_index].text) != "/"
    ):
        raise ValueError(f"{name}: breadcrumb separator contract is invalid")
    _validate_visible_leaf(
        parser,
        current_index,
        {"aria-current": "page"},
        CURRENT_PAGE_LABELS[name],
        f"{name}: breadcrumb current-page state is invalid",
    )
    if elements[external_index].attributes != {
        "class": "service-links",
        "aria-label": "External",
    }:
        raise ValueError(f"{name}: external navigation requires its exact class and accessible label")
    external_children = _children(parser, external_index)
    if [elements[index].tag for index in external_children] != ["a", "a"]:
        raise ValueError(f"{name}: external navigation must contain exactly GitHub then PyPI")
    _validate_service_anchor(parser, external_children[0], REPOSITORY_URL, "GitHub")
    _validate_service_anchor(parser, external_children[1], PYPI_URL, "PyPI")
    service_icons = [
        index
        for index, element in enumerate(elements)
        if element.tag == "svg" and element.attributes.get("class") == "service-icon"
    ]
    if (
        len(_descendants(parser, external_index, "svg")) != 2
        or len(_descendants(parser, header_index, "svg")) != 2
        or len(service_icons) != 2
        or len([element for element in elements if element.tag == "svg"]) != 2
    ):
        raise ValueError(f"{name}: header must contain only the two reviewed service icons")
    images = [index for index, element in enumerate(elements) if element.tag == "img"]
    if len(images) != 2:
        raise ValueError(f"{name}: document must contain exactly its header/hero and footer rocket images")
    if name == "index.html":
        hero_index = images[0]
        if elements[hero_index].attributes != {
            "class": "hero-mark",
            "src": f"{SITE_BASE_TOKEN}assets/agw-rocket.svg",
            "alt": "AGW rocket mark",
        }:
            raise ValueError(f"{name}: Home must contain only its reviewed main hero rocket")
        hero_heading_index = elements[hero_index].parent
        if (
            hero_heading_index is None
            or elements[hero_heading_index].attributes != {"class": "hero-heading"}
            or [elements[index].tag for index in _children(parser, hero_heading_index)] != ["img", "div"]
            or main_index not in _ancestors(parser, hero_heading_index)
        ):
            raise ValueError(f"{name}: Home hero rocket must lead the reviewed hero heading")
    elif images[0] != mark_index:
        raise ValueError(f"{name}: the small header rocket must precede the footer rocket")

    footer_children = _children(parser, footer_index)
    if [elements[index].tag for index in footer_children] != ["p", "nav"]:
        raise ValueError(f"{name}: footer ownership must precede footer navigation")
    ownership_index, footer_nav_index = footer_children
    _validate_visible_leaf(
        parser,
        ownership_index,
        {},
        "Product of Wayfarer Labs, LLC",
        f"{name}: footer ownership text is invalid",
    )
    if elements[footer_nav_index].attributes != {"aria-label": "Footer"}:
        raise ValueError(f"{name}: footer navigation label is invalid")
    footer_links = _children(parser, footer_nav_index)
    expected_footer = (
        (f"{SITE_BASE_TOKEN}manifesto/", "Agentworks Manifesto"),
        (f"{SITE_BASE_TOKEN}security/", "We take security seriously"),
    )
    if [elements[index].tag for index in footer_links] != ["a", "a", "a"]:
        raise ValueError(f"{name}: footer must contain exactly Manifesto, Security, then Lander")
    for index, (destination, label) in zip(footer_links[:2], expected_footer, strict=True):
        _validate_visible_leaf(
            parser,
            index,
            {"href": destination},
            label,
            f"{name}: footer destination {destination} is invalid",
        )
    game_link = elements[footer_links[2]]
    game_label = game_link.attributes.get("aria-label")
    if (
        game_link.attributes
        != {
            "class": "footer-game-link",
            "href": f"{SITE_BASE_TOKEN}lander/",
            "aria-label": game_label,
            "title": game_label,
        }
        or not game_label
        or not game_label.strip()
        or _hidden(parser, footer_links[2])
    ):
        raise ValueError(f"{name}: footer Lander link contract is invalid")
    game_children = _children(parser, footer_links[2])
    if (
        len(game_children) != 1
        or elements[game_children[0]].tag != "img"
        or elements[game_children[0]].attributes
        != {
            "class": "footer-game-mark",
            "src": f"{SITE_BASE_TOKEN}assets/agw-rocket.svg",
            "alt": "",
        }
    ):
        raise ValueError(f"{name}: footer Lander link requires one decorative selected rocket")
    if images[1] != game_children[0] or _normalized_text(game_link.text):
        raise ValueError(f"{name}: footer Lander link must be final and icon-only")

    anchors = [element for element in elements if element.tag == "a"]
    invalid_local_hrefs = [
        anchor.attributes.get("href")
        for anchor in anchors
        if not str(anchor.attributes.get("href") or "").startswith(("#", "https://", SITE_BASE_TOKEN))
    ]
    if invalid_local_hrefs:
        raise ValueError(f"{name}: local template links must use SITE_BASE: {invalid_local_hrefs}")
    for destination, label in {
        SITE_BASE_TOKEN: "Agentworks",
        **SHELL_DESTINATION_LABELS,
    }.items():
        matching = [anchor for anchor in anchors if anchor.attributes.get("href") == destination]
        if len(matching) != 1 or _normalized_text(matching[0].text) != label:
            raise ValueError(f"{name}: destination {destination} must occur once with label {label!r}")
    local_routes = [
        reference.target
        for anchor in anchors
        if (
            reference := _resolve_local_reference(
                anchor.attributes.get("href"),
                SITE_BASE_TOKEN,
                TEMPLATE_DESTINATIONS[name],
            )
        )
        is not None
        and str(anchor.attributes.get("href")).startswith(SITE_BASE_TOKEN)
    ]
    duplicates = sorted({route.as_posix() for route in local_routes if local_routes.count(route) > 1})
    if duplicates:
        raise ValueError(f"{name}: duplicate normalized local route destinations: {duplicates}")


def _validate_content_token_placements(name: str, template: str) -> _TemplatePlacementParser:
    parser = _TemplatePlacementParser()
    parser.feed(template)
    for token, (placement_kind, placement_value) in CONTENT_TOKEN_PLACEMENTS[name].items():
        placements = parser.placements.get(token, [])
        if len(placements) != 1:
            raise ValueError(f"{name}: content token {token} must have exactly one parsed placement")
        kind, location, ancestors = placements[0]
        if placement_kind == "meta":
            if (
                kind != "attribute"
                or location != "meta:content"
                or placement_value != "description"
                or token not in parser.description_content_tokens
            ):
                raise ValueError(f"{name}: metadata token {token} must be the exact description content attribute")
            continue
        if placement_kind == "element-id":
            if (
                kind != "text"
                or location != "code"
                or token not in parser.exact_text_placements
                or not ancestors
                or ancestors[-1] != ("code", {"id": placement_value})
                or not any(tag == "section" and attrs.get("id") == "onboarding" for tag, attrs in ancestors)
            ):
                raise ValueError(f"{name}: prompt token {token} must be the exact onboarding code text")
            continue
        allowed_locations = {"div"} if placement_kind != "article-class" else {"article"}
        if kind != "text" or location not in allowed_locations or token not in parser.exact_text_placements:
            raise ValueError(f"{name}: block token {token} must be text in its sourced-content container")
        container = ancestors[-1][1]
        if placement_kind == "article-class":
            if container.get("class") != placement_value:
                raise ValueError(f"{name}: block token {token} must be in the reviewed long-form article")
            continue
        if container.get("class") != "sourced-content":
            raise ValueError(f"{name}: block token {token} must be in an exact sourced-content container")
        section = next((attrs for tag, attrs in reversed(ancestors[:-1]) if tag == "section"), None)
        if (
            section is None
            or (placement_kind == "section-id" and section.get("id") != placement_value)
            or (placement_kind == "section-class" and section.get("class") != placement_value)
        ):
            raise ValueError(f"{name}: block token {token} is in the wrong reviewed section")
    return parser


def _validate_onboarding_template(name: str, parser: _TemplatePlacementParser, template: str) -> None:
    if name != "index.html":
        return
    if len(parser.onboarding_sections) != 1:
        raise ValueError("index.html: exactly one onboarding section is required")
    if parser.onboarding_headings != 1:
        raise ValueError("index.html: onboarding requires its reviewed heading")
    shell = _ShellParser()
    shell.feed(template)
    elements = shell.elements
    section = _one(
        shell,
        [index for index, element in enumerate(elements) if element.tag == "section" and element.attributes.get("id") == "onboarding"],
        "index.html: exactly one onboarding section is required",
    )
    if elements[section].attributes != {
        "id": "onboarding",
        "class": "status-panel",
        "aria-labelledby": "onboarding-heading",
    }:
        raise ValueError("index.html: onboarding section attributes are invalid")
    children = _children(shell, section)
    if [elements[index].tag for index in children] != ["p", "h2", "p", "pre", "div"]:
        raise ValueError("index.html: onboarding content structure is invalid")
    label, heading, introduction, prompt, controls = children
    _validate_visible_leaf(
        shell,
        label,
        {"class": "status-label"},
        "Get started / Agent",
        "index.html: onboarding label is invalid",
    )
    _validate_visible_leaf(
        shell,
        heading,
        {"id": "onboarding-heading"},
        "Agentworks CLI bootstrap",
        "index.html: onboarding heading is invalid",
    )
    _validate_visible_leaf(
        shell,
        introduction,
        {},
        "Copy this prompt into any capable assistant.",
        "index.html: onboarding introduction is invalid",
    )
    prompt_children = _children(shell, prompt)
    if (
        elements[prompt].attributes != {"class": "onboarding-prompt"}
        or len(prompt_children) != 1
        or elements[prompt_children[0]].tag != "code"
        or elements[prompt_children[0]].attributes != {"id": "onboarding-prompt"}
        or _children(shell, prompt_children[0])
        or "".join(elements[prompt_children[0]].text) != "{{ONBOARDING_PROMPT}}"
    ):
        raise ValueError("index.html: onboarding prompt projection is invalid")
    control_children = _children(shell, controls)
    if elements[controls].attributes != {"class": "copy-controls"} or [
        elements[index].tag for index in control_children
    ] != ["button", "p"]:
        raise ValueError("index.html: onboarding copy controls are invalid")
    button, status = control_children
    if (
        elements[button].attributes
        != {"id": "copy-onboarding-prompt", "type": "button", "hidden": None}
        or _children(shell, button)
        or _normalized_text(elements[button].text) != "Copy prompt"
    ):
        raise ValueError("index.html: onboarding copy button is invalid")
    if (
        elements[status].attributes
        != {"id": "copy-status", "role": "status", "aria-live": "polite", "aria-atomic": "true"}
        or _children(shell, status)
        or _normalized_text(elements[status].text)
    ):
        raise ValueError("index.html: onboarding copy status is invalid")
    scripts = [index for index, element in enumerate(elements) if element.tag == "script"]
    if len(scripts) != 1 or elements[scripts[0]].attributes != {
        "type": "module",
        "src": f"{SITE_BASE_TOKEN}static/onboarding-copy.js",
    }:
        raise ValueError("index.html: onboarding copy module is invalid")


def _validate_game_shell_placement(name: str, parser: _TemplatePlacementParser) -> None:
    if name not in {"lander.html", "404.html"}:
        return
    placements = parser.placements.get(LANDER_GAME_TOKEN, [])
    if len(placements) != 1:
        raise ValueError(f"{name}: shared Lander fragment must have exactly one placement")
    kind, location, ancestors = placements[0]
    if (
        kind != "text"
        or location != "main"
        or LANDER_GAME_TOKEN not in parser.exact_text_placements
        or not ancestors
        or ancestors[-1][0] != "main"
        or ancestors[-1][1] != MAIN_ATTRIBUTES[name]
    ):
        raise ValueError(f"{name}: shared Lander fragment must be an exact direct main placement")


def _validate_template(name: str, template: str) -> None:
    allowed = TEMPLATE_TOKENS[name]
    tokens = TOKEN_PATTERN.findall(template)
    if set(tokens) != allowed:
        raise ValueError(f"{name}: template token vocabulary must be exactly {sorted(allowed)}")
    for token in allowed - {SITE_BASE_TOKEN}:
        if tokens.count(token) != 1:
            raise ValueError(f"{name}: content token {token} must occur exactly once")
    if tokens.count(SITE_BASE_TOKEN) < 1:
        raise ValueError(f"{name}: SITE_BASE must occur at least once")
    masked = TOKEN_PATTERN.sub("", template)
    if "{{" in masked or "}}" in masked:
        raise ValueError(f"{name}: template contains brace-like unknown text")
    if name == "security.html" and EMAIL_ADDRESS_PATTERN.search(template):
        raise ValueError("security.html: email reporting paths are forbidden")
    for match in re.finditer(re.escape(SITE_BASE_TOKEN), template):
        prefix = template[max(0, match.start() - 160) : match.start()]
        if re.search(r"(?:href|src)=\"[^\"]*$", prefix) is None:
            raise ValueError(f"{name}: SITE_BASE may occur only in URL attributes")
    missing_literals = sorted(literal for literal in TEMPLATE_REQUIRED_LITERALS[name] if literal not in template)
    if missing_literals:
        raise ValueError(f"{name}: template is missing required reviewed literals: {missing_literals}")
    parser = _validate_content_token_placements(name, template)
    _validate_onboarding_template(name, parser, template)
    _validate_game_shell_placement(name, parser)
    if name == "lander-game.html":
        validate_game_contract(template)
        return
    _validate_shared_shell(name, template)


def render_named_template(name: str, template: str, site_base: str, substitutions: dict[str, str]) -> str:
    """Render one template through its closed, builder-owned vocabulary."""
    _validate_template(name, template)
    values = {SITE_BASE_TOKEN: site_base}
    values.update({f"{{{{{token}}}}}": value for token, value in substitutions.items()})
    required = TEMPLATE_TOKENS[name]
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError(f"{name}: missing substitutions for required tokens: {missing}")
    for token in required:
        value = values[token]
        if TOKEN_PATTERN.search(value) or "{{" in value or "}}" in value:
            raise ValueError(f"{name}: substitution for {token} contains brace-like token syntax")
    rendered = TOKEN_PATTERN.sub(lambda match: values[match.group(0)], template)
    if TOKEN_PATTERN.search(rendered) or "{{" in rendered or "}}" in rendered:
        raise ValueError(f"{name}: rendered template contains an unexpanded token")
    if name == "security.html" and EMAIL_ADDRESS_PATTERN.search(rendered):
        raise ValueError("security.html: email reporting paths are forbidden")
    return rendered


class _ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = _attribute_map(tag, attrs)
        if attributes.get("id"):
            self.ids.append(str(attributes["id"]))
        for name in ("href", "src"):
            if attributes.get(name):
                self.references.append(str(attributes[name]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _validate_local_references(rendered: dict[Path, bytes], manifest: frozenset[Path], base: str) -> None:
    parsed: dict[Path, _ReferenceParser] = {}
    for path, content in rendered.items():
        if path.suffix not in {".html", ".svg"}:
            continue
        parser = _ReferenceParser()
        parser.feed(content.decode("utf-8"))
        if len(parser.ids) != len(set(parser.ids)):
            raise ValueError(f"{path}: duplicate element id in rendered document")
        parsed[path] = parser
    for path, parser in parsed.items():
        for reference in parser.references:
            if reference.startswith("https://"):
                if reference not in APPROVED_EXTERNAL_URLS:
                    raise ValueError(f"{path}: unapproved external URL: {reference}")
                continue
            local = _resolve_local_reference(reference, base, path)
            if local is None:
                raise ValueError(f"{path}: local reference is outside site base: {reference}")
            if local.target not in manifest:
                raise ValueError(f"{path}: local reference is absent from manifest: {reference}")
            if local.fragment is not None and (
                not local.fragment or local.target not in parsed or local.fragment not in parsed[local.target].ids
            ):
                raise ValueError(f"{path}: local reference fragment is absent: {reference}")
    for path, content in rendered.items():
        if path.suffix != ".js":
            continue
        source = content.decode("utf-8")
        for match in STATIC_IMPORT_PATTERN.finditer(source):
            reference = match.group(2)
            if not reference.startswith("./") and not reference.startswith("../"):
                raise ValueError(f"{path}: JavaScript module import must be same-origin relative: {reference}")
            normalized = posixpath.normpath((path.parent / reference).as_posix())
            target = Path(normalized)
            if normalized.startswith("../") or target not in manifest:
                raise ValueError(f"{path}: JavaScript module import is absent from manifest: {reference}")


def _validate_runtime_asset(path: Path, source: str) -> None:
    if path.suffix == ".css":
        if re.search(r"(?i)@import\b", source):
            raise ValueError(f"{path}: CSS imports are forbidden")
        if re.search(r"(?i)url\s*\(", source):
            raise ValueError(f"{path}: CSS url() references are forbidden")
        if HTTP_URL_PATTERN.search(source) or QUOTED_PROTOCOL_RELATIVE_URL_PATTERN.search(source):
            raise ValueError(f"{path}: remote CSS URLs are forbidden")
        if path == Path("static/site.css"):
            if "\\" in source:
                raise ValueError(f"{path}: shared CSS cannot contain escape sequences")
            without_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
            declarations = (
                (match.group(1).lower(), re.sub(r"\s+", "", match.group(2)).lower())
                for match in re.finditer(r"(?:^|[;{])\s*([A-Za-z-]+)\s*:\s*([^;{}]+)", without_comments)
            )
            forbidden = {"opacity", "visibility", "content-visibility"}
            display_values = {"grid", "flex", "inline-flex"}
            if any(
                property_name in forbidden or (property_name == "display" and value not in display_values)
                for property_name, value in declarations
            ):
                raise ValueError(f"{path}: shared CSS declaration is outside the reviewed layout contract")
    elif path.suffix == ".js":
        if HTTP_URL_PATTERN.search(source) or QUOTED_PROTOCOL_RELATIVE_URL_PATTERN.search(source):
            raise ValueError(f"{path}: remote JavaScript URLs are forbidden")
