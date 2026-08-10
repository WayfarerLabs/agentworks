import assert from "node:assert/strict";
import test from "node:test";

import { copyPrompt, initializeCopy } from "../static/onboarding-copy.js";

function fixture() {
    const listeners = new Map();
    const prompt = { textContent: "line one\n`agw guide --agent`\n" };
    const button = {
        hidden: true,
        addEventListener(type, listener) {
            listeners.set(type, listener);
        },
    };
    const status = { textContent: "" };
    const elements = {
        "onboarding-prompt": prompt,
        "copy-onboarding-prompt": button,
        "copy-status": status,
    };
    const documentObject = {
        activeElement: { id: "focus-canary" },
        getElementById(id) {
            return elements[id];
        },
    };
    return { button, documentObject, listeners, prompt, status };
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

    assert.equal(current.button.hidden, false);
    assert.equal(current.listeners.size, 1);
    await current.listeners.get("click")();
    assert.deepEqual(writes, [current.prompt.textContent]);
    assert.equal(current.status.textContent, "Prompt copied.");
    assert.equal(current.documentObject.activeElement, activeElement);
});

test("copy failure reports a manual fallback without moving focus", async () => {
    const current = fixture();
    const activeElement = current.documentObject.activeElement;
    await copyPrompt(current.prompt, current.status, {
        async writeText() {
            throw new Error("clipboard denied");
        },
    });
    assert.equal(current.status.textContent, "Copy failed. Select the prompt and copy it manually.");
    assert.equal(current.documentObject.activeElement, activeElement);
});

test("unavailable clipboard keeps the enhancement hidden and explains manual copy", () => {
    const current = fixture();
    initializeCopy(current.documentObject, undefined);
    assert.equal(current.button.hidden, true);
    assert.equal(current.listeners.size, 0);
    assert.equal(current.status.textContent, "Select the prompt and copy it manually.");
});

test("missing optional enhancement elements fail safely", () => {
    assert.doesNotThrow(() => initializeCopy({ getElementById() {} }, { writeText() {} }));
});
