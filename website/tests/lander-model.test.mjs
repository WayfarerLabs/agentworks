import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
    ENGINE_ACCELERATION,
    FAILURE_STATUS,
    FUEL_QUANTUM,
    REFERENCE_TEMPLATES,
    ROUTE_DIGESTS,
    STEP_SECONDS,
    advanceMissionSequence,
    advanceSimulation,
    classifySweptContact,
    checkpointPoseForContact,
    createCueState,
    createPreflightModel,
    createRun,
    createSimulationClock,
    effectiveThrust,
    enqueueInputEdge,
    integratePose,
    mixDigitalInput,
    nextAwardRatio,
    plumeForThrust,
    proveTemplate,
    removeQueuedInputEdges,
    stepFlight,
    transitionMission,
    updateRetention,
} from "../static/lander-model.js";
import {
    STATIC_WORLD_SEED,
    instantiateTemplateSite,
    siteFoundationBottom,
} from "../static/lander-world.js";

const ROOT = new URL("../", import.meta.url).pathname;

function close(actual, expected, tolerance = 1e-10) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

function stepMany(pose, request, fuel, count) {
    let current = pose;
    let reserve = fuel;
    for (let index = 0; index < count; index += 1) {
        const result = integratePose(current, request, reserve);
        current = result.pose;
        reserve = result.thrust.fuel;
    }
    return { pose: current, fuel: reserve };
}

class FakeElement {
    constructor(parentElement = null) {
        this.parentElement = parentElement; this.hidden = false; this.disabled = false; this.tabIndex = -1;
        this.textContent = ""; this.value = "0.0"; this.dataset = {}; this.attributes = new Map(); this.children = [];
        this.setCount = 0;
        this.listeners = new Map(); this.capturedPointers = new Set();
        const properties = new Map();
        this.style = { setProperty: (name, value) => properties.set(name, value),
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
        event.target ??= this; event.currentTarget = this;
        event.composedPath ??= () => [this];
        event.preventDefault ??= () => { event.defaultPrevented = true; };
        for (const listener of [...(this.listeners.get(event.type) ?? [])]) listener(event);
        return !event.defaultPrevented;
    }
    setAttribute(name, value) {
        this.setCount += 1;
        this.attributes.set(name, String(value));
        if (name === "class") this.className = String(value);
        if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    }
    removeAttribute(name) { this.attributes.delete(name); }
    append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    replaceWith(node) { this.replacement = node; }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((node) => node !== this); }
    focus() { globalThis.document.activeElement = this; }
    setPointerCapture(pointerId) { this.capturedPointers.add(pointerId); }
    hasPointerCapture(pointerId) { return this.capturedPointers.has(pointerId); }
    releasePointerCapture(pointerId) {
        if (!this.capturedPointers.delete(pointerId)) return;
        this.dispatchEvent({ type: "lostpointercapture", pointerId, timeStamp: performance.now() });
    }
    getBoundingClientRect() { return { width: 1000 }; }
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

function controllerFixture() {
    const root = new FakeElement(); const actions = new FakeElement(root);
    const ids = ["lander-scene-shell", "lander-scene", "lander-start", "lander-fuel", "lander-fuel-value",
        "lander-target-direction", "lander-controls", "lander-actions", "lander-exit", "lander-restart", "lander-status",
        "terrain-layer", "site-layer", "debris-layer", "mission-agent"];
    const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement(root)]));
    elements["lander-actions"] = actions; elements["lander-exit"].parentElement = actions;
    elements["lander-restart"].parentElement = actions;
    elements["lander-start"].hidden = true; elements["lander-start"].disabled = true;
    actions.hidden = true; elements["lander-exit"].disabled = true;
    elements["lander-restart"].hidden = true; elements["lander-restart"].disabled = true;
    root.querySelector = (selector) => elements[selector.slice(1)] ?? FakeElement.prototype.querySelector.call(root, selector);
    root.cloneNode = () => controllerFixture().root;
    root.elements = elements;
    return { root, elements };
}

let controllerModule;
async function controllerClasses() {
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

function focusable(element) {
    for (let current = element; current; current = current.parentElement) if (current.hidden) return false;
    return !element.disabled;
}

test("8.4 engine physics, fuel exhaustion, input, and plumes match fixed vectors", () => {
    assert.equal(ENGINE_ACCELERATION, 8.4);
    const pose = { x: 10, y: 30, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    const gravity = stepMany(pose, { left: 0, right: 0 }, 30, 120);
    close(gravity.pose.y, 28.4875); close(gravity.pose.vy, -3); close(gravity.fuel, 30);
    const collective = stepMany(pose, { left: 0.72, right: 0.72 }, 30, 120);
    close(collective.pose.y, 34.5859); close(collective.pose.vy, 9.096); close(collective.fuel, 28.56);
    const exhausted = effectiveThrust({ left: 1, right: 1 }, 0.005);
    close(exhausted.left, 0.3); close(exhausted.right, 0.3); assert.equal(exhausted.fuel, 0);
    assert.deepEqual(mixDigitalInput({ Space: true, ArrowLeft: true }), { left: 0.72, right: 1 });
    assert.deepEqual(plumeForThrust(0.5), { scaleY: 0.54, opacity: 0.625 });
});

test("award ratio starts at three, strictly decays, and remains above one", () => {
    let ratio = 3;
    close(nextAwardRatio(ratio), 2.64);
    ratio = nextAwardRatio(ratio); close(nextAwardRatio(ratio), 2.3448);
    for (let index = 0; index < 1000; index += 1) {
        const next = nextAwardRatio(ratio);
        assert.ok(next <= ratio); assert.ok(next > 1); ratio = next;
    }
    assert.equal(ratio, 1 + Number.EPSILON);
});

test("all nine copied route literals pass exactly two defensive replays", () => {
    assert.equal(REFERENCE_TEMPLATES.length, 9);
    for (const template of REFERENCE_TEMPLATES) {
        assert.deepEqual(template.runs[0], [1, 90]);
        assert.ok(template.runs.reduce((total, run) => total + run[1], 0) <= 2880);
        close(template.demonstratedMinimum / FUEL_QUANTUM, Math.round(template.demonstratedMinimum / FUEL_QUANTUM));
        const proof = proveTemplate(template);
        assert.equal(proof.success.classification, "safe");
        assert.ok(proof.smallerFailure.exhaustionStep < proof.success.contactStep);
    }
    assert.match(ROUTE_DIGESTS.outputDigest, /^[0-9a-f]{64}$/);
});

test("every accepted touchdown margin settles to the same proven centered checkpoint", () => {
    for (const template of REFERENCE_TEMPLATES) {
        const originSite = { id: 0, center: 20, platformLeft: 15.2, platformRight: 24.8,
            platformTop: 4, platformBottom: 3.65, canCollected: true, powered: true, nocStage: 5 };
        const targetSite = instantiateTemplateSite(STATIC_WORLD_SEED, 1, originSite, template);
        for (const x of [originSite.platformLeft + 1.621, originSite.center, originSite.platformRight - 1.621]) {
            const contact = { x, y: originSite.platformTop, vx: 0, vy: -1, angle: 0, angularVelocity: 0 };
            const pose = checkpointPoseForContact(originSite, contact);
            assert.equal(pose.x, originSite.center); assert.equal(pose.y, originSite.platformTop);
            const proof = proveTemplate(template, { seed: STATIC_WORLD_SEED, originSite, targetSite, pose });
            assert.equal(proof.success.classification, "safe");
            assert.equal(proof.smallerFailure.allowance, template.demonstratedMinimum - FUEL_QUANTUM);
            assert.ok(proof.smallerFailure.exhaustionStep < proof.success.contactStep);
        }
    }
});

test("independent derivation CLI reproduces canonical bytes and rejects misuse", async () => {
    const directory = await mkdtemp(join(tmpdir(), "agw-route-test-"));
    const output = join(directory, "routes.json");
    const tool = join(ROOT, "tools/derive_lander_routes.mjs");
    const geometry = join(ROOT, "tests/fixtures/lander-route-geometry-v1.json");
    const fixture = join(ROOT, "tests/fixtures/lander-route-derived-v1.json");
    execFileSync(process.execPath, [tool, "--geometry", geometry, "--output", output, "--verify", fixture]);
    assert.equal(await readFile(output, "utf8"), await readFile(fixture, "utf8"));
    const derived = JSON.parse(await readFile(fixture, "utf8"));
    assert.deepEqual(REFERENCE_TEMPLATES, derived.routes);
    assert.equal(ROUTE_DIGESTS.outputDigest, derived.outputDigest);
    assert.equal(ROUTE_DIGESTS.physicsDigest, derived.physicsDigest);
    assert.equal(ROUTE_DIGESTS.geometryDigest, derived.geometryDigest);
    assert.equal(derived.routes[0].runs[1][1], 200, "the selected route is not the first ranged candidate");
    assert.equal(spawnSync(process.execPath, [tool, "--bogus"]).status, 2);

    const blockedGeometry = join(directory, "blocked-geometry.json");
    const blocked = JSON.parse(await readFile(geometry, "utf8"));
    blocked.templates[0].clearanceKnots[2][1] = 50;
    await writeFile(blockedGeometry, `${JSON.stringify(blocked)}\n`, "utf8");
    assert.equal(spawnSync(process.execPath, [tool, "--geometry", blockedGeometry, "--output", output,
        "--verify", fixture]).status, 1);

    const changedTool = join(directory, "changed-recipes.mjs");
    const changedSource = (await readFile(tool, "utf8")).replace("[3,199,201]", "[3,1,1]");
    assert.notEqual(changedSource, await readFile(tool, "utf8"));
    await writeFile(changedTool, changedSource, "utf8");
    assert.equal(spawnSync(process.execPath, [changedTool, "--geometry", geometry, "--output", output]).status, 1);

    const obstructedWorldTool = join(directory, "obstructed-world.mjs");
    const obstructedWorldSource = (await readFile(tool, "utf8")).replace(
        "const y = raw > cap ? Math.max(0.75, cap - 0.15 * sampleUnit(trial.seed, 4, index >>> 0)) : raw;",
        "const y = raw + 20;",
    );
    assert.notEqual(obstructedWorldSource, await readFile(tool, "utf8"));
    await writeFile(obstructedWorldTool, obstructedWorldSource, "utf8");
    assert.equal(spawnSync(process.execPath, [obstructedWorldTool, "--geometry", geometry, "--output", output]).status, 1);
});

test("the old route-78 target crossing is rejected by the exact landing envelope", () => {
    const current = REFERENCE_TEMPLATES[0];
    const unsafe = { ...current, runs: [[1,90],[3,200],[2,200],[1,20],[2,274],[3,274],[1,44],
        [3,189],[2,189],[0,362],[1,118]], success: { ...current.success, contactStep: 1960,
        burn: 8.236500000000081, classification: "safe", pose: { x: 77.81635462654064, y: 0,
            vx: -0.16132550005166613, vy: -2.0400613638522276, angle: 1.4109374999979991,
            angularVelocity: 9.658940314238862e-15 } } };
    assert.throws(() => proveTemplate(unsafe), /Route proof mismatch/);
    const originSite = { id: 0, center: 0, platformLeft: -4.8, platformRight: 4.8,
        platformTop: 0, platformBottom: -0.35, canCollected: true, powered: true, nocStage: 5 };
    const targetSite = { id: 1, center: 78, platformLeft: 73.2, platformRight: 82.8,
        platformTop: 0, platformBottom: -0.35, clearanceKnots: current.clearanceKnots };
    assert.throws(() => proveTemplate(current, { seed: 1, originSite, targetSite,
        terrainVertices: [[4.8,-0.8],[72,50],[73.2,-0.8],[82.8,-0.8],[100,-0.8]] }), /Route proof mismatch/);
});

test("safe target top is inclusive and epsilon excess is unsafe", () => {
    const model = createRun({ seed: 1 });
    const target = model.retainedSites[0];
    const previous = { x: target.center, y: target.platformTop + 0.25, vx: 1.4, vy: -2.2,
        angle: -8, angularVelocity: 12 };
    const next = { ...previous, y: target.platformTop + 0.2 };
    assert.equal(classifySweptContact(model, previous, next).kind, "safe");
    assert.equal(classifySweptContact(model, { ...previous, vx: 1.400000001 }, { ...next, vx: 1.400000001 }).kind, "unsafe");
    const tangent = { x: target.center, y: target.platformTop, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, tangent, { ...tangent, x: tangent.x + 0.01 }).cause, "grazing");

    const isolated = { ...model, terrainVertices: [[-100, -20], [200, -20]] };
    for (const x of [target.platformLeft - 20, target.platformRight + 20]) {
        const clear = { x, y: target.platformTop + 0.1, vx: 0, vy: -1, angle: 0, angularVelocity: 0 };
        assert.equal(classifySweptContact(isolated, clear, { ...clear, y: target.platformTop - 0.1 }), null);
    }
});

test("closed unsafe geometry catches slopes, platform equality, pylons, mast, and precedence", () => {
    const base = createRun({ seed: 1 });
    const site = base.retainedSites[0];
    const slopeModel = { ...base, retainedSites: [], targetSiteId: null, terrainVertices: [[0, 0], [10, 10]] };
    const slopePose = { x: 5, y: 6.62, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(slopeModel, slopePose, slopePose).cause, "terrain");
    const sidePose = { x: site.platformLeft - 1.6 - 0.02, y: site.platformTop - 0.1,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(base, sidePose, sidePose).cause, "platform");
    const pylonPose = { x: site.platformLeft + 1.4, y: site.platformTop - 0.6,
        vx: 0, vy: 0, angle: 180, angularVelocity: 0 };
    assert.equal(classifySweptContact(base, pylonPose, pylonPose).cause, "pylon");
    const buildingLeft = site.platformRight + 2;
    const roof = site.platformTop + 7.2;
    const mastPose = { x: buildingLeft + 3.5, y: roof + 0.1,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(base, mastPose, mastPose).cause, "mast");
    const earlierUnsafe = { x: site.platformRight - 0.8, y: site.platformTop + 0.7,
        vx: 0, vy: -1, angle: 20, angularVelocity: 0 };
    const laterTop = { ...earlierUnsafe, y: site.platformTop + 0.1 };
    assert.equal(classifySweptContact(base, earlierUnsafe, laterTop).cause, "noc");
});

test("safe landing creates next target, adds uncapped award, and begins service", () => {
    let model = createRun({ seed: 1 });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
        angle: 0, angularVelocity: 0 }, fuel: 100 };
    const landed = stepFlight(model, { left: 0, right: 0 });
    assert.equal(landed.state, "landed");
    assert.equal(landed.completedSites, 1);
    assert.equal(landed.retainedSites.length, 2);
    assert.equal(landed.retainedSites[0].canCollected, true);
    assert.ok(landed.fuel > 100);
    close(landed.awardRatio, 2.64);
    assert.equal(landed.targetSiteId, 1);
    assert.ok(landed.targetRouteProof);
});

test("accepted touchdown margins award from the centered immutable checkpoint", () => {
    for (const side of ["left", "right"]) {
        let model = createRun({ seed: 1, reducedMotion: true });
        const target = model.retainedSites[0];
        const x = side === "left" ? target.platformLeft + 1.621 : target.platformRight - 1.621;
        model = { ...model, pose: { x, y: target.platformTop + 0.001,
            vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
        model = stepFlight(model, { left: 0, right: 0 });
        assert.equal(model.state, "launching"); assert.equal(model.completedSites, 1);
        assert.equal(model.pose.x, target.center); assert.equal(model.checkpoint.pose.x, target.center);
        assert.equal(model.targetRouteProof.success.classification, "safe");
        assert.ok(model.targetRouteProof.smallerFailure.exhaustionStep < model.targetRouteProof.success.contactStep);
    }
});

test("service powers the NOC, freezes a checkpoint, and launches with actual fuel", () => {
    let model = createRun({ seed: 1, reducedMotion: true });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
        angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "launching");
    assert.equal(model.retainedSites[0].powered, true);
    assert.ok(Object.isFrozen(model.checkpoint));
    const fuel = model.fuel;
    for (let index = 0; index < 90; index += 1) model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "flying");
    assert.ok(model.fuel < fuel);
});

test("automatic launch ignores only its rising start top and keeps other collisions", () => {
    let model = createRun({ seed: 1, reducedMotion: true });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
        angle: 0, angularVelocity: 0 } };
    const launching = stepFlight(model, { left: 0, right: 0 });
    assert.equal(launching.state, "launching");
    assert.equal(stepFlight({ ...launching, fuel: 0 }, { left: 0, right: 0 }).state, "failed");
    const active = launching.retainedSites.find((site) => site.id === launching.activeSiteId);
    const side = { ...launching, pose: { ...launching.pose, x: active.platformLeft + 1.6, vy: 1 } };
    assert.equal(stepFlight(side, { left: 0, right: 0 }).failureCause, "platform");
    const pylon = { ...launching, pose: { ...launching.pose, x: active.platformLeft + 1.4,
        y: active.platformTop - 0.6, vy: 1, angle: 180 } };
    assert.equal(stepFlight(pylon, { left: 0, right: 0 }).failureCause, "pylon");
    const buildingLeft = active.platformRight + 2;
    const noc = { ...launching, pose: { ...launching.pose, x: buildingLeft + 3.5,
        y: active.platformTop + 1, vy: 1 } };
    assert.equal(stepFlight(noc, { left: 0, right: 0 }).failureCause, "noc");
});

test("destroy restores the pristine static DOM from active and failed controllers", async () => {
    const { LanderGameController } = await controllerClasses();
    for (const terminal of [false, true]) {
        const fixture = controllerFixture();
        const controller = new LanderGameController(fixture.root);
        const elements = fixture.elements;
        elements["lander-scene-shell"].setAttribute("role", "application");
        elements["lander-actions"].hidden = false; elements["lander-exit"].disabled = false;
        elements["lander-status"].textContent = terminal ? FAILURE_STATUS : "Mission underway.";
        if (terminal) { elements["lander-restart"].hidden = false; elements["lander-restart"].disabled = false; }
        fixture.root.style.setProperty("--lander-x", "999px");
        assert.equal(focusable(elements["lander-exit"]), true);
        controller.destroy();
        const restored = fixture.root.replacement;
        assert.equal(controller.model.state, "preflight");
        assert.ok(restored); assert.equal(restored.style.properties.size, 0);
        assert.equal(restored.elements["lander-start"].hidden, true);
        assert.equal(restored.elements["lander-start"].disabled, true);
        assert.equal(restored.elements["lander-actions"].hidden, true);
        assert.equal(restored.elements["lander-exit"].disabled, true);
        assert.equal(restored.elements["lander-restart"].hidden, true);
        assert.equal(restored.elements["lander-restart"].disabled, true);
        assert.equal(focusable(restored.elements["lander-exit"]), false);
        assert.equal(restored.elements["lander-status"].textContent, "");
    }
});

test("render clears stale live status on crash entry", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture();
    const controller = new LanderGameController(fixture.root);
    fixture.elements["lander-status"].textContent = "Mission underway.";
    controller.model = { ...createPreflightModel(), state: "crashing", status: "", crash: null };
    controller.render();
    assert.equal(fixture.elements["lander-status"].textContent, "");
    controller.destroy();
});

test("short pointer tap survives automatic lost capture for its full pulse", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = createRun({ seed: 1 });
    const shell = fixture.elements["lander-scene-shell"];
    shell.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 100, clientY: 50, timeStamp: 0 });
    shell.dispatchEvent({ type: "pointerup", pointerId: 7, isPrimary: true, button: 0,
        clientX: 103, clientY: 52, timeStamp: 50 });
    assert.equal(shell.hasPointerCapture(7), true);
    shell.releasePointerCapture(7);
    assert.equal(controller.pointer.id, 7); assert.equal(controller.pointer.pulseDeadline, 140);
    assert.notEqual(controller.pulseTimer, null);
    assert.ok(controller.clock.queue.some((edge) => edge.token === 7 && edge.left === 0.72 && edge.right === 0.72));
    controller.finishPointer(7, 140);
    assert.equal(controller.pointer, null); assert.equal(controller.pulseTimer, null);
    assert.deepEqual(controller.clock.queue.at(-1), {
        timestamp: 140, left: 0, right: 0, token: null,
        physical: { heldCodes: [], pointer: { active: false } }, sequence: 1,
    });
    controller.destroy();
});

test("live reduced-motion changes persist into crash behavior in both directions", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = createRun({ seed: 7, reducedMotion: false });
    controller.onMotionChange({ matches: true });
    assert.equal(controller.model.reducedMotion, true);
    const impact = { ...controller.model, pose: { x: -4.99, y: 20, vx: -10, vy: 0, angle: 0, angularVelocity: 0 } };
    assert.equal(stepFlight(impact, { left: 0, right: 0 }).state, "failed");
    controller.model = createRun({ seed: 7, reducedMotion: true });
    controller.onMotionChange({ matches: false });
    assert.equal(controller.model.reducedMotion, false);
    const animated = { ...controller.model, pose: impact.pose };
    assert.equal(stepFlight(animated, { left: 0, right: 0 }).state, "crashing");
    controller.destroy();
});

test("intermediate NOC battery stages project from model to the retained site DOM", async () => {
    let model = createRun({ seed: 1 });
    const site = model.retainedSites[0];
    model = { ...model, pose: { x: site.center, y: site.platformTop + 0.001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    model = advanceMissionSequence(model, 0.3);
    model = advanceMissionSequence(model, 1.8);
    assert.equal(advanceMissionSequence(model, 0.199).nocStage, 0);
    assert.equal(advanceMissionSequence(model, 0.2).nocStage, 1);
    assert.equal(advanceMissionSequence(model, 0.4).nocStage, 2);
    assert.equal(advanceMissionSequence(model, 0.6).nocStage, 3);
    assert.equal(advanceMissionSequence(model, 0.8).nocStage, 4);
    assert.equal(advanceMissionSequence(model, 0.999).state, "powering");
    assert.equal(advanceMissionSequence(model, 1).state, "launching");
    assert.equal(advanceMissionSequence(model, 1).retainedSites[0].powered, true);
    model = updateRetention(advanceMissionSequence(model, 0.41));
    const active = model.retainedSites.find((candidate) => candidate.id === model.activeSiteId);
    assert.equal(model.state, "powering"); assert.equal(model.nocStage, 2); assert.equal(active.nocStage, 2);
    close(active.foundationBottom, siteFoundationBottom(model.terrainVertices, active));
    const buildingLeft = active.platformRight + 2;
    const foundationPose = { x: buildingLeft + 1.6, y: active.foundationBottom + 0.2,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, foundationPose, foundationPose).cause, "noc");

    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = model; controller.render();
    const group = fixture.elements["site-layer"].querySelector(`[data-site-id="${active.id}"]`);
    assert.equal(group.dataset.nocStage, "2");
    assert.equal(group.querySelector(".noc-building").firstElementChild.attributes.get("d"),
        `M${active.platformRight * 10 + 20} ${548 - active.foundationBottom * 10}` +
        `V${548 - active.platformTop * 10 - 72}h70V${548 - active.foundationBottom * 10}Z`);
    controller.model = updateRetention(advanceMissionSequence(model, 0.2)); controller.render();
    assert.equal(group.dataset.nocStage, "3");
    controller.destroy();
});

test("vacuum crash has exactly eight deterministic fragments and finite duration", () => {
    let model = createRun({ seed: 7 });
    model = { ...model, pose: { x: -4.99, y: 20, vx: -10, vy: 0, angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "crashing");
    assert.equal(model.crash.fragments.length, 8);
    assert.equal(model.crashOrdinal, 1);
    model = advanceMissionSequence(model, 0.6);
    assert.equal(model.state, "failed");
    assert.equal(model.status, FAILURE_STATUS);
});

test("reduced motion crashes atomically with zero debris", () => {
    let model = createRun({ seed: 7, reducedMotion: true });
    model = { ...model, pose: { x: -4.99, y: 20, vx: -10, vy: 0, angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "failed");
    assert.equal(model.crash, null);
});

test("checkpoint restart restores post-award fuel without duplicating progress", () => {
    let model = createRun({ seed: 1, reducedMotion: true });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
        angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    const expected = { fuel: model.checkpoint.fuel, completed: model.completedSites, ratio: model.awardRatio };
    model = { ...model, state: "failed", fuel: 0 };
    const restarted = transitionMission(model, "RESTART");
    assert.equal(restarted.state, "launching");
    assert.equal(restarted.fuel, expected.fuel);
    assert.equal(restarted.completedSites, expected.completed);
    assert.equal(restarted.awardRatio, expected.ratio);
    assert.equal(restarted.retainedSites[0].canCollected, true);
});

test("fixed scheduler is frame-rate equivalent and bounds its input queue", () => {
    for (const rate of [30, 60, 120]) {
        let clock = createSimulationClock(0);
        let model = createRun({ seed: 1 });
        for (let frame = 1; frame <= rate; frame += 1) {
            const result = advanceSimulation(clock, model, (frame * 1000) / rate);
            clock = result.clock; model = result.model;
        }
        close(model.pose.x, 30.8); close(model.pose.y, 30.0875); close(model.pose.vy, -3.4);
    }
    let clock = createSimulationClock(0);
    for (let index = 0; index < 64; index += 1) clock = enqueueInputEdge(clock, { timestamp: 1, left: index % 2, right: 0 });
    const physical = Object.freeze({ heldCodes: Object.freeze(["Space"]),
        pointer: Object.freeze({ active: true, id: 7, anchorX: 10, currentX: 90, pulseDeadline: null }) });
    clock = enqueueInputEdge(clock, { timestamp: 1, left: 1, right: 0.72, token: 7, physical });
    assert.equal(clock.queue.length, 1);
    assert.equal(clock.queue[0].snapshot, true);
    assert.equal(clock.queue[0].token, 7); assert.equal(clock.queue[0].physical, physical);
    clock = removeQueuedInputEdges(clock, 7);
    clock = enqueueInputEdge(clock, { timestamp: 2, left: 0.72, right: 0.72,
        physical: { heldCodes: ["Space"], pointer: { active: false } } });
    const continued = advanceSimulation(clock, createRun({ seed: 1 }), 1000 / 120);
    assert.deepEqual(continued.clock.input, { left: 0.72, right: 0.72 });
});

test("cue, preflight transition, and retention remain bounded", () => {
    assert.deepEqual(createCueState(true), { state: "settled", elapsed: 0 });
    const run = updateRetention(transitionMission(createPreflightModel(), "START", { seed: 1 }));
    assert.equal(run.state, "flying");
    assert.ok(run.retainedChunks.length <= 10);
    assert.ok(run.retainedSites.length <= 3);
    assert.ok(run.terrainVertices.length > 0);
    close(STEP_SECONDS, 1 / 120);
});

test("retention and DOM reconciliation change only at bounded window keys", async () => {
    let model = updateRetention(createRun({ seed: 1 }));
    const vertices = model.terrainVertices; const key = model.retentionKey;
    model = updateRetention({ ...model, pose: { ...model.pose, x: model.pose.x + 1 } });
    assert.equal(model.retentionKey, key); assert.equal(model.terrainVertices, vertices);
    const changed = updateRetention({ ...model, pose: { ...model.pose, x: model.pose.x + 25 } });
    assert.notEqual(changed.retentionKey, key); assert.notEqual(changed.terrainVertices, vertices);

    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = model; controller.render();
    const terrainPath = fixture.elements["terrain-layer"].children[0];
    const writes = terrainPath.setCount;
    controller.model = updateRetention({ ...model, pose: { ...model.pose, x: model.pose.x + 1 } });
    controller.render();
    assert.equal(terrainPath.setCount, writes);
    controller.model = changed; controller.render();
    assert.notEqual(controller.worldWindowKey, key);
    controller.root.style.setProperty("--crash-x", "900px");
    controller.exit();
    assert.equal(controller.model.state, "preflight"); assert.equal(controller.worldWindowKey, null);
    assert.equal(fixture.elements["terrain-layer"].children.length, 0);
    assert.equal(fixture.elements["site-layer"].children.length, 0);
    assert.equal(controller.root.style.properties.has("--crash-x"), false);
    assert.equal(fixture.elements["lander-start"].hidden, false);
    controller.destroy();
});

test("100-site deterministic mission keeps generation and retention bounded", () => {
    let model = updateRetention(createRun({ seed: 0x12345678, reducedMotion: true }));
    let maximumGenerationMilliseconds = 0;
    for (let completed = 0; completed < 100; completed += 1) {
        if (model.state === "launching") {
            for (let step = 0; step < 90; step += 1) model = updateRetention(stepFlight(model, { left: 0, right: 0 }));
        }
        const target = model.retainedSites.find((site) => site.id === model.targetSiteId);
        model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1,
            angle: 0, angularVelocity: 0 } };
        const started = performance.now();
        model = updateRetention(stepFlight(model, { left: 0, right: 0 }));
        maximumGenerationMilliseconds = Math.max(maximumGenerationMilliseconds, performance.now() - started);
        assert.equal(model.state, "launching");
        assert.ok(model.retainedSites.length <= 3);
        assert.ok(model.retainedChunks.length <= 10);
        assert.ok(model.fuel >= 0);
    }
    assert.equal(model.completedSites, 100);
    assert.ok(model.awardRatio > 1);
    assert.ok(maximumGenerationMilliseconds < 50);
});
