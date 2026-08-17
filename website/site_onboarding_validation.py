"""Structural validation for the progressively enhanced Home onboarding chooser."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import NamedTuple

from site_content import REPOSITORY_URL

SITE_BASE_TOKEN = "{{SITE_BASE}}"
PROMPT_TOKEN = "{{ONBOARDING_PROMPT}}"


class Element(NamedTuple):
    tag: str
    attributes: dict[str, str | None]
    parent: int | None
    text: list[str]


class Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[int] = []
        self.elements: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            raise ValueError(f"{tag}: duplicate HTML attribute name")
        parent = self.stack[-1] if self.stack else None
        self.elements.append(Element(tag, attributes, parent, []))
        self.stack.append(len(self.elements) - 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            raise ValueError(f"{tag}: duplicate HTML attribute name")
        parent = self.stack[-1] if self.stack else None
        self.elements.append(Element(tag, attributes, parent, []))

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, -1, -1):
            if self.elements[self.stack[position]].tag == tag:
                del self.stack[position:]
                return

    def handle_data(self, data: str) -> None:
        for index in self.stack:
            self.elements[index].text.append(data)


def children(parser: Parser, parent: int) -> list[int]:
    return [
        index
        for index, element in enumerate(parser.elements)
        if element.parent == parent
    ]


def normalized_text(parser: Parser, index: int) -> str:
    return " ".join("".join(parser.elements[index].text).split())


def visible_leaf(parser: Parser, index: int, attributes: dict[str, str | None]) -> bool:
    return (
        parser.elements[index].attributes == attributes
        and "hidden" not in parser.elements[index].attributes
        and not children(parser, index)
        and bool(normalized_text(parser, index))
    )


def validate_onboarding_template(name: str, template: str) -> None:
    """Reject drift from the closed Home onboarding structure without pinning its prose."""
    if name != "index.html":
        return
    parser = Parser()
    parser.feed(template)
    elements = parser.elements
    sections = [
        index
        for index, element in enumerate(elements)
        if element.tag == "section" and element.attributes.get("id") == "onboarding"
    ]
    if len(sections) != 1:
        raise ValueError("index.html: exactly one onboarding section is required")
    section = sections[0]
    if elements[section].attributes != {
        "id": "onboarding",
        "class": "status-panel",
        "aria-labelledby": "onboarding-heading",
    }:
        raise ValueError("index.html: onboarding section attributes are invalid")
    direct = children(parser, section)
    if [elements[index].tag for index in direct] != [
        "p",
        "h2",
        "p",
        "div",
        "section",
        "section",
    ]:
        raise ValueError("index.html: onboarding content structure is invalid")
    label, heading, introduction, tab_list, agent_panel, old_panel = direct
    if not all(
        visible_leaf(parser, index, attributes)
        for index, attributes in (
            (label, {"class": "status-label"}),
            (heading, {"id": "onboarding-heading"}),
            (introduction, {}),
        )
    ):
        raise ValueError("index.html: onboarding introduction structure is invalid")

    tabs = children(parser, tab_list)
    tab_attributes = elements[tab_list].attributes
    if (
        tab_attributes.get("id") != "onboarding-tab-list"
        or tab_attributes.get("class") != "onboarding-tabs"
        or not str(tab_attributes.get("aria-label") or "").strip()
        or "hidden" not in tab_attributes
        or set(tab_attributes) != {"id", "class", "aria-label", "hidden"}
        or [elements[index].tag for index in tabs] != ["button", "button"]
    ):
        raise ValueError("index.html: onboarding tab list is invalid")
    for tab, (tab_id, panel_id) in zip(
        tabs,
        (
            ("agent-assisted-tab", "agent-assisted-panel"),
            ("old-school-tab", "old-school-panel"),
        ),
        strict=True,
    ):
        if not visible_leaf(
            parser, tab, {"id": tab_id, "type": "button", "data-panel": panel_id}
        ):
            raise ValueError("index.html: onboarding tab is invalid")

    contracts = (
        (
            agent_panel,
            "agent-assisted-panel",
            "agent-assisted-heading",
            ["h3", "p", "div", "p"],
        ),
        (
            old_panel,
            "old-school-panel",
            "old-school-heading",
            ["h3", "p", "pre", "p", "pre"],
        ),
    )
    for panel, panel_id, heading_id, tags in contracts:
        if (
            elements[panel].attributes
            != {
                "id": panel_id,
                "class": "onboarding-panel",
                "aria-labelledby": heading_id,
            }
            or [elements[index].tag for index in children(parser, panel)] != tags
            or "hidden" in elements[panel].attributes
        ):
            raise ValueError("index.html: onboarding panel is invalid")

    agent_heading, agent_intro, prompt_shell, status = children(parser, agent_panel)
    if not visible_leaf(
        parser, agent_heading, {"id": "agent-assisted-heading"}
    ) or not visible_leaf(parser, agent_intro, {}):
        raise ValueError("index.html: agent-assisted panel introduction is invalid")
    prompt, button = children(parser, prompt_shell)
    if elements[prompt_shell].attributes != {"class": "onboarding-prompt-shell"} or [
        elements[index].tag for index in children(parser, prompt_shell)
    ] != ["pre", "button"]:
        raise ValueError("index.html: onboarding prompt shell is invalid")
    prompt_children = children(parser, prompt)
    if (
        elements[prompt].attributes != {"class": "onboarding-prompt"}
        or len(prompt_children) != 1
        or elements[prompt_children[0]].tag != "code"
        or elements[prompt_children[0]].attributes != {"id": "onboarding-prompt"}
        or children(parser, prompt_children[0])
        or "".join(elements[prompt_children[0]].text) != PROMPT_TOKEN
    ):
        raise ValueError("index.html: onboarding prompt projection is invalid")

    button_attributes = elements[button].attributes
    icons = children(parser, button)
    if (
        button_attributes.get("id") != "copy-onboarding-prompt"
        or button_attributes.get("class") != "copy-prompt-button"
        or button_attributes.get("type") != "button"
        or "hidden" not in button_attributes
        or not button_attributes.get("aria-label")
        or button_attributes.get("title") != button_attributes.get("aria-label")
        or set(button_attributes)
        != {"id", "class", "type", "aria-label", "title", "hidden"}
        or [elements[index].tag for index in icons] != ["svg"]
        or normalized_text(parser, button)
    ):
        raise ValueError("index.html: onboarding copy button is invalid")
    icon = icons[0]
    paths = children(parser, icon)
    if (
        elements[icon].attributes
        != {"aria-hidden": "true", "focusable": "false", "viewbox": "0 0 24 24"}
        or len(paths) != 1
        or elements[paths[0]].tag != "path"
        or set(elements[paths[0]].attributes) != {"d"}
        or not elements[paths[0]].attributes["d"]
        or children(parser, paths[0])
    ):
        raise ValueError("index.html: onboarding copy icon is invalid")
    if (
        elements[status].attributes
        != {
            "id": "copy-status",
            "class": "copy-status",
            "role": "status",
            "aria-live": "polite",
            "aria-atomic": "true",
        }
        or children(parser, status)
        or normalized_text(parser, status)
    ):
        raise ValueError("index.html: onboarding copy status is invalid")

    old_heading, repository_step, install_commands, guide_step, guide_command = (
        children(parser, old_panel)
    )
    if (
        not visible_leaf(parser, old_heading, {"id": "old-school-heading"})
        or elements[repository_step].attributes
        or elements[guide_step].attributes
        or not normalized_text(parser, repository_step)
        or not normalized_text(parser, guide_step)
    ):
        raise ValueError("index.html: old-school guidance structure is invalid")
    repository_children = children(parser, repository_step)
    if (
        [elements[index].tag for index in repository_children] != ["a", "code"]
        or not visible_leaf(parser, repository_children[0], {"href": REPOSITORY_URL})
        or not visible_leaf(parser, repository_children[1], {})
    ):
        raise ValueError("index.html: old-school repository guidance is invalid")
    for command in (install_commands, guide_command):
        command_children = children(parser, command)
        if (
            elements[command].attributes != {"class": "onboarding-commands"}
            or len(command_children) != 1
            or elements[command_children[0]].tag != "code"
            or not visible_leaf(parser, command_children[0], {})
        ):
            raise ValueError("index.html: old-school command structure is invalid")
    scripts = [
        index for index, element in enumerate(elements) if element.tag == "script"
    ]
    if len(scripts) != 1 or elements[scripts[0]].attributes != {
        "type": "module",
        "src": f"{SITE_BASE_TOKEN}static/onboarding.js",
    }:
        raise ValueError("index.html: onboarding module is invalid")
