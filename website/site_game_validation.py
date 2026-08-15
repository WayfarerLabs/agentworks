"""Focused validation for the reusable continuous Lander fragment."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

EXPECTED_GAME_IDS = frozenset(
    {
        "lander-game",
        "lander-scene-shell",
        "lander-scene-stage",
        "lander-scene",
        "lander-scene-title",
        "lander-scene-description",
        "scene-sky",
        "lander-sky-world",
        "scene-stars",
        "scene-landmarks",
        "lander-world",
        "terrain-layer",
        "site-layer",
        "debris-layer",
        "mission-lander",
        "mission-left-engine",
        "mission-right-engine",
        "mission-mark",
        "mission-bay-lip",
        "mission-agent",
        "crash-flash",
        "next-site-cue",
        "lander-start",
        "lander-fuel",
        "lander-fuel-gauge",
        "lander-fuel-gauge-fill",
        "lander-fuel-label",
        "lander-fuel-value",
        "lander-target-direction",
        "lander-outcome",
        "lander-controls",
        "lander-controls-rail",
        "lander-exit",
        "lander-restart",
        "lander-status",
    }
)
EXPECTED_GAME_FILES = frozenset(
    {
        Path("static/lander.css"),
        Path("static/lander-collision.js"),
        Path("static/lander-game.js"),
        Path("static/lander-model.js"),
        Path("static/lander-world.js"),
    }
)


class _FragmentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.id_indexes: dict[str, int] = {}
        self.parent_indexes: list[int | None] = []
        self.children: list[list[int]] = []
        self.direct_text: list[list[str]] = []
        self.stack: list[tuple[str, int]] = []
        self.text: list[str] = []
        self.top_level_text: list[str] = []
        self.malformed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        index = len(self.tags)
        parent = self.stack[-1][1] if self.stack else None
        self.tags.append((tag, attributes))
        self.parent_indexes.append(parent)
        self.children.append([])
        self.direct_text.append([])
        if parent is not None:
            self.children[parent].append(index)
        identifier = attributes.get("id")
        if identifier is not None:
            self.ids.append(identifier)
            self.id_indexes[identifier] = index
        self.stack.append((tag, index))

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1][0] != tag:
            self.malformed = True
        while self.stack:
            open_tag, _ = self.stack.pop()
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self.stack:
            self.direct_text[self.stack[-1][1]].append(data)
        else:
            self.top_level_text.append(data)


def _one(parser: _FragmentParser, identifier: str) -> tuple[str, dict[str, str | None]]:
    matches = [element for element in parser.tags if element[1].get("id") == identifier]
    if len(matches) != 1:
        raise ValueError(f"lander-game.html: expected exactly one #{identifier}")
    return matches[0]


def _children(parser: _FragmentParser, identifier: str) -> list[tuple[str, dict[str, str | None]]]:
    index = parser.id_indexes[identifier]
    return [parser.tags[child] for child in parser.children[index]]


def _nonempty(value: str | None) -> bool:
    return bool(" ".join((value or "").split()))


def validate_game_contract(template: str) -> None:
    """Validate the complete static, no-JavaScript game recovery subtree."""
    parser = _FragmentParser()
    parser.feed(template)
    top_level = [index for index, parent in enumerate(parser.parent_indexes) if parent is None]
    if top_level != [0] or parser.stack or parser.malformed or _nonempty("".join(parser.top_level_text)):
        raise ValueError("lander-game.html: reviewed root must be the sole well-formed top-level element")
    if len(parser.ids) != len(set(parser.ids)):
        raise ValueError("lander-game.html: IDs must be unique")
    if frozenset(parser.ids) != EXPECTED_GAME_IDS:
        missing = sorted(EXPECTED_GAME_IDS - set(parser.ids))
        extra = sorted(set(parser.ids) - EXPECTED_GAME_IDS)
        raise ValueError(f"lander-game.html: game ID contract differs; missing={missing}, extra={extra}")
    expected_tags = {
        "lander-game": "section",
        "lander-scene-shell": "div",
        "lander-scene-stage": "div",
        "lander-scene": "svg",
        "lander-scene-title": "title",
        "lander-scene-description": "desc",
        "scene-sky": "rect",
        "lander-sky-world": "g",
        "scene-stars": "path",
        "scene-landmarks": "path",
        "lander-world": "g",
        "terrain-layer": "g",
        "site-layer": "g",
        "debris-layer": "g",
        "mission-lander": "g",
        "mission-left-engine": "use",
        "mission-right-engine": "use",
        "mission-mark": "use",
        "mission-bay-lip": "path",
        "mission-agent": "g",
        "crash-flash": "g",
        "next-site-cue": "g",
        "lander-start": "button",
        "lander-fuel": "p",
        "lander-fuel-gauge": "span",
        "lander-fuel-gauge-fill": "span",
        "lander-fuel-label": "span",
        "lander-fuel-value": "span",
        "lander-target-direction": "span",
        "lander-outcome": "div",
        "lander-status": "p",
        "lander-restart": "button",
        "lander-controls-rail": "div",
        "lander-controls": "p",
        "lander-exit": "button",
    }
    if any(parser.tags[parser.id_indexes[identifier]][0] != tag for identifier, tag in expected_tags.items()):
        raise ValueError("lander-game.html: required game element tag identity is invalid")
    root_tag, root_attributes = parser.tags[0]
    if (
        root_tag != "section"
        or set(root_attributes) != {"id", "aria-label"}
        or root_attributes["id"] != "lander-game"
        or not _nonempty(root_attributes["aria-label"])
    ):
        raise ValueError("lander-game.html: root section contract is invalid")
    _, scene = _one(parser, "lander-scene")
    if scene != {
        "id": "lander-scene",
        "viewbox": "0 0 1000 640",
        "preserveaspectratio": "xMidYMid meet",
        "role": "img",
        "aria-labelledby": "lander-scene-title lander-scene-description",
    }:
        raise ValueError("lander-game.html: named static SVG contract is invalid")
    _, start = _one(parser, "lander-start")
    if (
        set(start) != {"id", "type", "hidden", "disabled", "aria-label"}
        or start["id"] != "lander-start"
        or start["type"] != "button"
        or start["hidden"] is not None
        or start["disabled"] is not None
        or not _nonempty(start["aria-label"])
    ):
        raise ValueError("lander-game.html: static Start must be hidden and disabled")
    tag, fuel_value = _one(parser, "lander-fuel-value")
    if tag != "span" or fuel_value != {
        "id": "lander-fuel-value",
        "class": "visually-hidden",
    }:
        raise ValueError("lander-game.html: hidden fuel value contract is invalid")
    tag, fuel_label = _one(parser, "lander-fuel-label")
    if tag != "span" or fuel_label != {"id": "lander-fuel-label", "class": "visually-hidden"}:
        raise ValueError("lander-game.html: hidden fuel label contract is invalid")
    _, restart = _one(parser, "lander-restart")
    if restart != {
        "id": "lander-restart",
        "type": "button",
        "hidden": None,
        "disabled": None,
        "aria-keyshortcuts": "r",
    }:
        raise ValueError("lander-game.html: static Retry action must be hidden and disabled")
    _, exit_action = _one(parser, "lander-exit")
    if exit_action != {
        "id": "lander-exit",
        "type": "button",
        "disabled": None,
        "aria-keyshortcuts": "Escape",
    }:
        raise ValueError("lander-game.html: static Exit must be disabled")
    _, gauge = _one(parser, "lander-fuel-gauge")
    if gauge != {"id": "lander-fuel-gauge", "aria-hidden": "true"}:
        raise ValueError("lander-game.html: fuel gauge must remain decorative")
    _, outcome = _one(parser, "lander-outcome")
    if outcome != {"id": "lander-outcome", "hidden": None}:
        raise ValueError("lander-game.html: static outcome must be hidden")
    _, status = _one(parser, "lander-status")
    if status != {
        "id": "lander-status",
        "role": "status",
        "aria-live": "polite",
        "aria-atomic": "true",
    }:
        raise ValueError("lander-game.html: sole status live region contract is invalid")
    _, rail = _one(parser, "lander-controls-rail")
    if rail != {"id": "lander-controls-rail", "hidden": None}:
        raise ValueError("lander-game.html: static controls rail must be hidden")
    tag, controls = _one(parser, "lander-controls")
    if tag != "p" or controls != {"id": "lander-controls"}:
        raise ValueError("lander-game.html: controls must be ordinary rail prose")
    if sum(attributes.get("aria-live") is not None for _, attributes in parser.tags) != 1:
        raise ValueError("lander-game.html: exactly one live region is required")
    if any(tag in {"meter", "output", "progress"} for tag, _ in parser.tags):
        raise ValueError("lander-game.html: duplicate semantic fuel authorities are forbidden")
    stage_positions = [
        template.index(f'id="{identifier}"')
        for identifier in (
            "lander-scene-stage",
            "lander-scene",
            "lander-start",
            "lander-fuel",
            "lander-target-direction",
            "lander-outcome",
            "lander-status",
            "lander-restart",
            "lander-controls-rail",
            "lander-controls",
            "lander-exit",
        )
    ]
    if stage_positions != sorted(stage_positions):
        raise ValueError("lander-game.html: stage, outcome, Restart, and controls rail order is invalid")
    expected_children = {
        "lander-game": ["lander-scene-shell"],
        "lander-scene-shell": ["lander-scene-stage", "lander-controls-rail"],
        "lander-scene-stage": [
            "lander-scene",
            "lander-start",
            "lander-fuel",
            "lander-target-direction",
            "lander-outcome",
        ],
        "lander-outcome": ["lander-status", "lander-restart"],
        "lander-controls-rail": ["lander-controls", "lander-exit"],
    }
    for parent, identifiers in expected_children.items():
        children = _children(parser, parent)
        parent_index = parser.id_indexes[parent]
        if [attributes.get("id") for _, attributes in children] != identifiers or any(
            parser.parent_indexes[parser.id_indexes[identifier]] != parent_index for identifier in identifiers
        ):
            raise ValueError(f"lander-game.html: #{parent} immediate child structure is invalid")
    sky_children = _children(parser, "lander-sky-world")
    if _one(parser, "lander-sky-world")[1] != {"id": "lander-sky-world", "aria-hidden": "true"} or [
        attributes.get("id") for _, attributes in sky_children
    ] != ["scene-stars", "scene-landmarks"]:
        raise ValueError("lander-game.html: bounded decorative sky structure is invalid")
    if _children(parser, "lander-status"):
        raise ValueError("lander-game.html: status must contain direct prose only")
    control_lines = _children(parser, "lander-controls")
    if control_lines != [
        ("span", {"class": "lander-controls-line lander-controls-keyboard"}),
        ("span", {"class": "lander-controls-line lander-controls-touch"}),
    ]:
        raise ValueError("lander-game.html: controls require keyboard then touch line sources")
    controls_index = parser.id_indexes["lander-controls"]
    if _nonempty("".join(parser.direct_text[controls_index])):
        raise ValueError("lander-game.html: controls prose must be owned by its two line sources")
    for child in parser.children[controls_index]:
        if parser.children[child] or not _nonempty("".join(parser.direct_text[child])):
            raise ValueError("lander-game.html: each controls line must be one nonempty text source")
    for identifier in ("lander-scene-title", "lander-scene-description", "lander-fuel-label", "lander-fuel-value"):
        index = parser.id_indexes[identifier]
        if parser.children[index] or not _nonempty("".join(parser.direct_text[index])):
            raise ValueError(f"lander-game.html: #{identifier} must be a nonempty direct text source")
    for identifier in ("lander-restart", "lander-exit"):
        children = _children(parser, identifier)
        if children != [
            ("span", {"class": "lander-action-label"}),
            ("span", {"class": "lander-key-hint", "aria-hidden": "true"}),
        ]:
            raise ValueError(f"lander-game.html: #{identifier} label and hint structure is invalid")
        for child in parser.children[parser.id_indexes[identifier]]:
            if parser.children[child] or not _nonempty("".join(parser.direct_text[child])):
                raise ValueError(f"lander-game.html: #{identifier} action text sources are invalid")
    for required in (
        'class="terrain-fill"',
        'class="terrain-surface"',
        'class="world-termini"',
        'class="lander-site"',
        'data-site-id="0"',
        'data-can="present"',
        'data-power="off"',
        'data-agent="absent"',
        'data-noc-stage="0"',
        'class="landing-platform"',
        'class="site-scaffold"',
        'class="gas-can"',
        'class="noc-building"',
        'class="noc-battery"',
        'class="battery-bar battery-bar-1"',
        'class="battery-bar battery-bar-2"',
        'class="battery-bar battery-bar-3"',
        'class="battery-bar battery-bar-4"',
        'class="noc-antenna antenna-mast"',
        'class="noc-antenna antenna-signal antenna-signal-1"',
        'class="noc-antenna antenna-signal antenna-signal-2"',
        'class="noc-antenna antenna-signal antenna-signal-3"',
    ):
        if required not in template:
            raise ValueError(f"lander-game.html: missing static world contract {required}")
    terrain_children = _children(parser, "terrain-layer")
    if len(terrain_children) != 3:
        raise ValueError("lander-game.html: terrain requires fill, surface, and terminus paths")
    fill_tag, fill = terrain_children[0]
    surface_tag, surface = terrain_children[1]
    fill_path = fill.get("d") or ""
    if (
        fill_tag != "path"
        or fill.get("class") != "terrain-fill"
        or fill.get("fill") != "#d7d2c4"
        or fill.get("stroke") != "none"
        or not fill_path.endswith("Z")
        or fill_path.count(" 648") != 2
    ):
        raise ValueError("lander-game.html: terrain fill closure contract is invalid")
    surface_path = surface.get("d") or ""
    if (
        surface_tag != "path"
        or surface.get("class") != "terrain-surface"
        or surface.get("fill") != "none"
        or surface.get("stroke") != "#4b4e55"
        or surface.get("stroke-width") != "2"
        or surface.get("stroke-linejoin") != "miter"
        or surface.get("stroke-miterlimit") != "2"
        or "Z" in surface_path
        or "V" in surface_path
        or " 648" in surface_path
    ):
        raise ValueError("lander-game.html: open terrain surface contract is invalid")
    terminus_tag, terminus = terrain_children[2]
    if (
        terminus_tag != "path"
        or terminus.get("class") != "world-termini"
        or terminus.get("fill") != "none"
        or terminus.get("stroke") != "#4b4e55"
        or terminus.get("stroke-width") != "2"
        or terminus.get("stroke-linecap") != "butt"
        or terminus.get("d") != "M-3932160 0V416M3932160 0V416"
    ):
        raise ValueError("lander-game.html: physical world terminus contract is invalid")
    scaffold = [attributes for _, attributes in parser.tags if attributes.get("class") == "site-scaffold"]
    scaffold_contract = {
        "fill": "none",
        "stroke": "#4b4e55",
        "stroke-width": "2",
        "stroke-linecap": "butt",
        "stroke-linejoin": "round",
    }
    if (
        len(scaffold) != 1
        or any(scaffold[0].get(key) != value for key, value in scaffold_contract.items())
        or not (scaffold[0].get("d") or "").startswith("M312 493.7H498M312 501.2H498M312 493.7L327.5 501.2")
        or (scaffold[0].get("d") or "").count("M") != 61
        or "Z" in (scaffold[0].get("d") or "")
        or not (scaffold[0].get("d") or "").endswith("M488 557.7L498 562.4")
    ):
        raise ValueError("lander-game.html: static open scaffold geometry is invalid")
    battery_contract = (
        '<rect x="452" y="434.2" width="22" height="40" />',
        '<path class="battery-bar battery-bar-1" d="M457 464.2h12v5h-12Z" />',
        '<path class="battery-bar battery-bar-2" d="M457 456.2h12v5h-12Z" />',
        '<path class="battery-bar battery-bar-3" d="M457 448.2h12v5h-12Z" />',
        '<path class="battery-bar battery-bar-4" d="M457 440.2h12v5h-12Z" />',
    )
    battery_positions = [template.find(fragment) for fragment in battery_contract]
    if -1 in battery_positions or battery_positions != sorted(battery_positions):
        raise ValueError("lander-game.html: static battery geometry or order is invalid")
    if "battery-terminal" in template:
        raise ValueError("lander-game.html: battery terminal is forbidden")
    signal_contract = (
        'd="M455 380.2Q463 372.2 471 380.2"',
        'd="M448 379.2Q463 364.2 478 379.2"',
        'd="M440 378.2Q463 355.2 486 378.2"',
    )
    if any(fragment not in template for fragment in signal_contract):
        raise ValueError("lander-game.html: static symmetric antenna signal geometry is invalid")


def validate_game_manifest(manifest: frozenset[Path]) -> None:
    """Require the exact five-file game closure inside the complete artifact."""
    found = frozenset(path for path in manifest if path.name.startswith("lander"))
    if found != EXPECTED_GAME_FILES:
        raise ValueError(f"game artifact closure differs: {sorted(found)}")
