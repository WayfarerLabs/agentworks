"""Focused validation for the reusable continuous Lander fragment."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

EXPECTED_GAME_IDS = frozenset(
    {
        "lander-game",
        "lander-scene-shell",
        "lander-scene",
        "lander-scene-title",
        "lander-scene-description",
        "scene-sky",
        "scene-stars",
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
        "lander-fuel-label",
        "lander-fuel-value",
        "lander-target-direction",
        "lander-controls",
        "lander-actions",
        "lander-exit",
        "lander-restart",
        "lander-status",
    }
)
EXPECTED_GAME_FILES = frozenset(
    {
        Path("static/lander.css"),
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
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        identifier = attributes.get("id")
        if identifier is not None:
            self.ids.append(identifier)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _one(parser: _FragmentParser, identifier: str) -> tuple[str, dict[str, str | None]]:
    matches = [element for element in parser.tags if element[1].get("id") == identifier]
    if len(matches) != 1:
        raise ValueError(f"lander-game.html: expected exactly one #{identifier}")
    return matches[0]


def validate_game_contract(template: str) -> None:
    """Validate the complete static, no-JavaScript game recovery subtree."""
    stripped = template.strip()
    if not stripped.startswith('<section id="lander-game"') or not stripped.endswith("</section>"):
        raise ValueError("lander-game.html: root section must contain the complete scene")
    parser = _FragmentParser()
    parser.feed(template)
    if len(parser.ids) != len(set(parser.ids)):
        raise ValueError("lander-game.html: IDs must be unique")
    if frozenset(parser.ids) != EXPECTED_GAME_IDS:
        missing = sorted(EXPECTED_GAME_IDS - set(parser.ids))
        extra = sorted(set(parser.ids) - EXPECTED_GAME_IDS)
        raise ValueError(f"lander-game.html: game ID contract differs; missing={missing}, extra={extra}")
    if parser.tags[0] != ("section", {"id": "lander-game", "aria-label": "Lunar deployment scene"}):
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
    if start != {
        "id": "lander-start",
        "type": "button",
        "hidden": None,
        "disabled": None,
        "aria-label": "Start lunar deployment mission",
    }:
        raise ValueError("lander-game.html: static Start must be hidden and disabled")
    _, output = _one(parser, "lander-fuel-value")
    if output != {"id": "lander-fuel-value", "aria-labelledby": "lander-fuel-label"}:
        raise ValueError("lander-game.html: fuel output naming contract is invalid")
    _, restart = _one(parser, "lander-restart")
    if restart != {"id": "lander-restart", "type": "button", "hidden": None, "disabled": None}:
        raise ValueError("lander-game.html: static Restart must be hidden and disabled")
    normalized = " ".join(" ".join(parser.text).split())
    controls = (
        "Thrust: Space or Up. Turn: Left/H or Right/L. Tap or hold to thrust; drag to turn. "
        "R restarts after a crash. Escape exits."
    )
    if controls not in normalized:
        raise ValueError("lander-game.html: control text contract is invalid")
    positions = [template.index(f'id="{identifier}"') for identifier in ("lander-scene-shell", "lander-fuel", "lander-target-direction", "lander-controls", "lander-actions", "lander-status")]
    if positions != sorted(positions):
        raise ValueError("lander-game.html: scene and active chrome order is invalid")
    for required in (
        'class="terrain-chunk"',
        'data-chunk-index="-1"',
        'data-chunk-index="0"',
        'data-chunk-index="1"',
        'data-chunk-index="2"',
        'class="lander-site"',
        'data-site-id="0"',
        'data-can="present"',
        'data-power="off"',
        'data-noc-stage="0"',
        'class="landing-platform"',
        'class="platform-supports"',
        'class="gas-can"',
        'class="noc-building"',
        'class="noc-battery"',
        'class="battery-terminal"',
        'class="battery-bar battery-bar-1"',
        'class="battery-bar battery-bar-2"',
        'class="battery-bar battery-bar-3"',
        'class="battery-bar battery-bar-4"',
        'class="noc-antenna antenna-mast"',
    ):
        if required not in template:
            raise ValueError(f"lander-game.html: missing static world contract {required}")
    support = [attributes for _, attributes in parser.tags if attributes.get("class") == "platform-supports"]
    support_prefix = "M312 471.6557689513639H408V476.1557689513639H312Z"
    if len(support) != 1 or not (support[0].get("d") or "").startswith(support_prefix):
        raise ValueError("lander-game.html: static platform support geometry is invalid")
    battery_contract = (
        '<rect x="452" y="412.1557689513639" width="22" height="40" rx="2" />',
        '<path class="battery-terminal" d="M458 412.1557689513639v-6h10v6" />',
        '<path class="battery-bar battery-bar-1" d="M457 442.1557689513639h12v5h-12Z" />',
        '<path class="battery-bar battery-bar-2" d="M457 434.1557689513639h12v5h-12Z" />',
        '<path class="battery-bar battery-bar-3" d="M457 426.1557689513639h12v5h-12Z" />',
        '<path class="battery-bar battery-bar-4" d="M457 418.1557689513639h12v5h-12Z" />',
    )
    battery_positions = [template.find(fragment) for fragment in battery_contract]
    if -1 in battery_positions or battery_positions != sorted(battery_positions):
        raise ValueError("lander-game.html: static battery geometry or order is invalid")


def validate_game_manifest(manifest: frozenset[Path]) -> None:
    """Require the exact four-file game closure inside the complete artifact."""
    found = frozenset(path for path in manifest if path.name.startswith("lander"))
    if found != EXPECTED_GAME_FILES:
        raise ValueError(f"game artifact closure differs: {sorted(found)}")
