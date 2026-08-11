export class FakeElement {
    constructor(parentElement = null) {
        this.parentElement = parentElement; this.hidden = false; this.disabled = false; this.tabIndex = -1;
        this.textContent = ""; this.value = "0.0"; this.dataset = {}; this.attributes = new Map(); this.children = [];
        this.setCount = 0; this.listeners = new Map(); this.capturedPointers = new Set();
        this.rect = { left: 0, top: 0, width: 1000, height: 640 };
        const properties = new Map();
        this.style = { setProperty: (name, value) => properties.set(name, value),
            getPropertyValue: (name) => properties.get(name) ?? "",
            removeProperty: (name) => properties.delete(name), properties };
    }
    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) ?? [];
        listeners.push(listener); this.listeners.set(type, listeners);
    }
    removeEventListener(type, listener) {
        this.listeners.set(type, (this.listeners.get(type) ?? []).filter((candidate) => candidate !== listener));
    }
    dispatchEvent(event) {
        event.target ??= this; event.currentTarget = this; this.lastEventTime = event.timeStamp;
        event.composedPath ??= () => [this];
        event.preventDefault ??= () => { event.defaultPrevented = true; };
        for (const listener of [...(this.listeners.get(event.type) ?? [])]) listener(event);
        return !event.defaultPrevented;
    }
    setAttribute(name, value) {
        this.setCount += 1; this.attributes.set(name, String(value));
        if (name === "class") this.className = String(value);
        if (name.startsWith("data-")) {
            this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
        }
    }
    removeAttribute(name) { this.attributes.delete(name); }
    closest(selector) {
        for (let current = this; current; current = current.parentElement) {
            if (current.tagName === "button" && selector.includes("button")) return current;
            if (current.tagName === "a" && current.attributes.has("href") && selector.includes("a[href]")) return current;
            if (["input", "select", "textarea", "summary"].includes(current.tagName) &&
                selector.includes(current.tagName)) return current;
            if (current.attributes.has("contenteditable") &&
                current.attributes.get("contenteditable") !== "false" && selector.includes("[contenteditable]")) return current;
            const role = current.attributes.get("role");
            if (role && selector.includes(`[role="${role}"]`)) return current;
            if (current.attributes.has("tabindex") && current.attributes.get("tabindex") !== "-1" &&
                current !== current.root?.elements?.["lander-scene-shell"] && selector.includes("[tabindex]")) return current;
        }
        return null;
    }
    append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    replaceWith(node) { this.replacement = node; }
    remove() {
        if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((node) => node !== this);
    }
    focus() { globalThis.document.activeElement = this; }
    setPointerCapture(pointerId) { this.capturedPointers.add(pointerId); }
    hasPointerCapture(pointerId) { return this.capturedPointers.has(pointerId); }
    releasePointerCapture(pointerId) {
        if (!this.capturedPointers.delete(pointerId)) return;
        this.dispatchEvent({ type: "lostpointercapture", pointerId, timeStamp: this.lastEventTime ?? performance.now() });
    }
    getBoundingClientRect() { return { ...this.rect }; }
    get firstElementChild() { return this.children[0] ?? null; }
    get lastElementChild() { return this.children.at(-1) ?? null; }
    querySelector(selector) {
        const matches = (node) => selector.startsWith(".") ? node.className?.split(" ").includes(selector.slice(1)) :
            selector.startsWith("[data-site-id=") ? node.dataset.siteId === selector.match(/"(.*)"/)[1] :
                node.tagName === selector;
        for (const child of this.children) {
            if (matches(child)) return child;
            const nested = child.querySelector(selector); if (nested) return nested;
        }
        return null;
    }
    cloneNode(deep = false) {
        const clone = new FakeElement(); clone.hidden = this.hidden; clone.disabled = this.disabled;
        clone.tabIndex = this.tabIndex; clone.textContent = this.textContent; clone.value = this.value;
        clone.dataset = { ...this.dataset }; clone.attributes = new Map(this.attributes); clone.className = this.className;
        if (deep) clone.append(...this.children.map((child) => child.cloneNode(true)));
        return clone;
    }
}

export function controllerFixture() {
    const root = new FakeElement();
    const ids = ["lander-scene-shell", "lander-scene", "lander-start", "lander-fuel", "lander-fuel-value",
        "lander-fuel-gauge", "lander-fuel-gauge-fill", "lander-target-direction", "lander-controls",
        "lander-scene-stage", "lander-outcome", "lander-controls-rail", "lander-exit",
        "lander-restart", "lander-status", "terrain-layer", "site-layer", "debris-layer", "mission-agent"];
    const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement(root)]));
    const shell = elements["lander-scene-shell"];
    const stage = elements["lander-scene-stage"];
    const outcome = elements["lander-outcome"];
    const rail = elements["lander-controls-rail"];
    root.append(shell);
    shell.append(stage, rail);
    stage.append(elements["lander-scene"], elements["lander-start"], elements["lander-fuel"],
        elements["lander-target-direction"], outcome);
    outcome.append(elements["lander-status"], elements["lander-restart"]);
    rail.append(elements["lander-controls"], elements["lander-exit"]);
    for (const [className, textContent] of [
        ["lander-controls-line lander-controls-keyboard", "Space/Up thrust; Left/H or Right/L turn."],
        ["lander-controls-line lander-controls-touch", "Touch: tap/hold to thrust; drag to turn."],
    ]) {
        const line = new FakeElement(elements["lander-controls"]); line.tagName = "span";
        line.setAttribute("class", className); line.textContent = textContent;
        elements["lander-controls"].append(line);
    }
    for (const id of ["lander-start", "lander-exit", "lander-restart"]) elements[id].tagName = "button";
    elements["lander-restart"].setAttribute("aria-keyshortcuts", "r");
    elements["lander-exit"].setAttribute("aria-keyshortcuts", "Escape");
    for (const id of ["lander-exit", "lander-restart"]) {
        const label = new FakeElement(elements[id]); label.tagName = "span";
        label.setAttribute("class", "lander-action-label");
        label.textContent = id === "lander-restart" ? "Retry" : "Exit mission";
        const hint = new FakeElement(elements[id]); hint.tagName = "span";
        hint.setAttribute("class", "lander-key-hint"); hint.setAttribute("aria-hidden", "true");
        hint.textContent = id === "lander-restart" ? "r" : "<esc>";
        elements[id].append(label, hint);
        elements[`${id}-label`] = label; elements[`${id}-hint`] = hint;
    }
    elements["lander-start"].hidden = true; elements["lander-start"].disabled = true;
    outcome.hidden = true; rail.hidden = true; elements["lander-exit"].disabled = true;
    elements["lander-restart"].hidden = true; elements["lander-restart"].disabled = true;
    elements["lander-fuel-gauge"].rect = { left: 12, top: 12, width: 16, height: 112 };
    root.querySelector = (selector) => elements[selector.slice(1)] ?? FakeElement.prototype.querySelector.call(root, selector);
    root.cloneNode = () => controllerFixture().root; root.elements = elements;
    for (const element of Object.values(elements)) element.root = root;
    return { root, elements };
}

let controllerModule;
export async function controllerClasses() {
    if (!controllerModule) {
        globalThis.Element = FakeElement;
        globalThis.document = { activeElement: null, body: new FakeElement(), hidden: true,
            addEventListener() {}, removeEventListener() {}, createElementNS: (_, name) => {
                const element = new FakeElement(); element.tagName = name; return element;
            }, querySelector: (selector) =>
                selector === "#lander-scene" ? { namespaceURI: "http://www.w3.org/2000/svg" } : null };
        globalThis.window = { addEventListener() {}, removeEventListener() {} };
        globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
        globalThis.requestAnimationFrame = () => 1; globalThis.cancelAnimationFrame = () => {};
        controllerModule = await import("../static/lander-game.js");
    }
    return controllerModule;
}

export function focusable(element) {
    for (let current = element; current; current = current.parentElement) if (current.hidden) return false;
    return !element.disabled;
}

export function descendantCount(element) {
    return element.children.reduce((total, child) => total + 1 + descendantCount(child), 0);
}

export function animationHarness() {
    let nextId = 1;
    const callbacks = new Map();
    return {
        cancel(id) { callbacks.delete(id); },
        get pending() { return callbacks.size; },
        request(callback) { const id = nextId; nextId += 1; callbacks.set(id, callback); return id; },
        step(timestamp) {
            const scheduled = [...callbacks.values()]; callbacks.clear();
            for (const callback of scheduled) callback(timestamp);
        },
        advance(from, to, interval = 1000 / 60) {
            for (let timestamp = from + interval; timestamp <= to + 1e-9; timestamp += interval) this.step(timestamp);
        },
    };
}
