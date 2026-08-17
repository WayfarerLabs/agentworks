import assert from "node:assert/strict";
import test from "node:test";

import {
    copyPrompt,
    initializeCopy,
    initializeOnboarding,
    initializeTabs,
} from "../static/onboarding.js";

function element({ hidden = false, textContent = "" } = {}) {
    const attributes = new Map();
    const listeners = new Map();
    return {
        attributes,
        focusCount: 0,
        hidden,
        listeners,
        tabIndex: 0,
        textContent,
        addEventListener(type, listener) {
            listeners.set(type, listener);
        },
        focus() {
            this.focusCount += 1;
        },
        getAttribute(name) {
            return attributes.get(name) ?? null;
        },
        setAttribute(name, value) {
            attributes.set(name, value);
        },
    };
}

function fixture() {
    const elements = {
        "copy-onboarding-prompt": element({ hidden: true }),
        "copy-status": element(),
        "manual-panel": element(),
        "manual-tab": element({ textContent: "manual" }),
        "onboarding-prompt": element({ textContent: "line one\n`agw guide --agent`\n" }),
        "onboarding-tab-list": element({ hidden: true }),
        "via-agent-panel": element(),
        "via-agent-tab": element({ textContent: "agent" }),
    };
    const documentObject = {
        activeElement: { id: "focus-canary" },
        getElementById(id) {
            return elements[id];
        },
    };
    return { documentObject, elements };
}

test("copy enhancement writes exact prompt text and preserves focus", async () => {
    const current = fixture();
    const writes = [];
    const activeElement = current.documentObject.activeElement;
    initializeCopy(current.documentObject, {
        async writeText(value) {
            writes.push(value);
        },
    });

    const button = current.elements["copy-onboarding-prompt"];
    assert.equal(button.hidden, false);
    assert.equal(button.listeners.size, 1);
    await button.listeners.get("click")();
    assert.deepEqual(writes, [current.elements["onboarding-prompt"].textContent]);
    assert.ok(current.elements["copy-status"].textContent.length > 0);
    assert.equal(current.documentObject.activeElement, activeElement);
});

test("copy outcomes expose distinct success, failure, and manual states", async () => {
    const success = fixture();
    await copyPrompt(success.elements["onboarding-prompt"], success.elements["copy-status"], {
        async writeText() {},
    });

    const failure = fixture();
    await copyPrompt(failure.elements["onboarding-prompt"], failure.elements["copy-status"], {
        async writeText() {
            throw new Error("clipboard denied");
        },
    });

    const manual = fixture();
    initializeCopy(manual.documentObject, undefined);

    const statuses = [success, failure, manual].map(({ elements }) => elements["copy-status"].textContent);
    assert.ok(statuses.every(Boolean));
    assert.equal(new Set(statuses).size, statuses.length);
    assert.equal(manual.elements["copy-onboarding-prompt"].hidden, true);
});

test("tabs progressively enhance with via Agent selected by default", () => {
    const current = fixture();
    initializeTabs(current.documentObject);

    const tabs = [current.elements["via-agent-tab"], current.elements["manual-tab"]];
    const panels = [current.elements["via-agent-panel"], current.elements["manual-panel"]];
    assert.equal(current.elements["onboarding-tab-list"].hidden, false);
    assert.equal(current.elements["onboarding-tab-list"].getAttribute("role"), "tablist");
    assert.deepEqual(
        tabs.map((tab) => [tab.getAttribute("role"), tab.getAttribute("aria-selected"), tab.tabIndex]),
        [
            ["tab", "true", 0],
            ["tab", "false", -1],
        ],
    );
    assert.deepEqual(
        panels.map((panel) => [panel.getAttribute("role"), panel.hidden]),
        [
            ["tabpanel", false],
            ["tabpanel", true],
        ],
    );
});

test("click and keyboard navigation select, wrap, and focus tabs", () => {
    const current = fixture();
    initializeTabs(current.documentObject);
    const agent = current.elements["via-agent-tab"];
    const manual = current.elements["manual-tab"];

    manual.listeners.get("click")();
    assert.equal(manual.getAttribute("aria-selected"), "true");
    assert.equal(current.elements["manual-panel"].hidden, false);
    assert.equal(current.elements["via-agent-panel"].hidden, true);

    let prevented = 0;
    manual.listeners.get("keydown")({
        key: "ArrowRight",
        preventDefault() {
            prevented += 1;
        },
    });
    assert.equal(prevented, 1);
    assert.equal(agent.getAttribute("aria-selected"), "true");
    assert.equal(agent.focusCount, 1);

    agent.listeners.get("keydown")({ key: "End", preventDefault() {} });
    assert.equal(manual.getAttribute("aria-selected"), "true");
    assert.equal(manual.focusCount, 1);

    const selectedBefore = manual.getAttribute("aria-selected");
    manual.listeners.get("keydown")({ key: "Escape", preventDefault() {} });
    assert.equal(manual.getAttribute("aria-selected"), selectedBefore);
});

test("combined and partial initialization fail safely", () => {
    const current = fixture();
    assert.doesNotThrow(() => initializeOnboarding(current.documentObject, { async writeText() {} }));
    assert.doesNotThrow(() => initializeCopy({ getElementById() {} }, { writeText() {} }));
    assert.doesNotThrow(() => initializeTabs({ getElementById() {} }));
});
