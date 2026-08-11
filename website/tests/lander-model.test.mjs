import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
    ENGINE_ACCELERATION,
    MAX_THRUST_VECTOR,
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
    mixEngineRequests,
    nextAwardRatio,
    plumeForThrust,
    pointerEngineRequests,
    proveTemplate,
    removeQueuedInputEdges,
    stepFlight,
    transitionMission,
    updateRetention,
} from "../static/lander-model.js";
import {
    STATIC_WORLD_SEED,
    corridorVertices,
    instantiateTemplateSite,
    siteFoundationBottom,
    terrainSample,
    terrainVerticesForWindow,
} from "../static/lander-world.js";

const ROOT = new URL("../", import.meta.url).pathname;

function close(actual, expected, tolerance = 1e-10) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    }
    return value;
}

function digest(value) {
    return createHash("sha256").update(JSON.stringify(canonical(value)), "utf8").digest("hex");
}

function heightFromVertices(vertices, x) {
    for (let index = 1; index < vertices.length; index += 1) {
        const [leftX, leftY] = vertices[index - 1]; const [rightX, rightY] = vertices[index];
        if (x < leftX || x > rightX) continue;
        return leftY + (rightY - leftY) * (x - leftX) / (rightX - leftX);
    }
    throw new RangeError(`Witness vertices do not cover ${x}`);
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

function descendantCount(element) {
    return element.children.reduce((total, child) => total + 1 + descendantCount(child), 0);
}

test("9.0 engine physics, true gimbal, assist, input arbitration, and plumes match fixed vectors", () => {
    assert.equal(ENGINE_ACCELERATION, 9);
    assert.equal(MAX_THRUST_VECTOR, 18);
    const pose = { x: 10, y: 30, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    const gravity = stepMany(pose, { left: 0, right: 0 }, 30, 120);
    close(gravity.pose.y, 28.4875); close(gravity.pose.vy, -3); close(gravity.fuel, 30);
    const collective = stepMany(pose, { left: 0.72, right: 0.72 }, 30, 120);
    close(collective.pose.y, 35.0215); close(collective.pose.vy, 9.96); close(collective.fuel, 28.56);
    const turn = integratePose(pose, { left: 0, right: 0.375 }, 30);
    close((turn.pose.vx - pose.vx) / STEP_SECONDS, -1.04293235601545);
    close((turn.pose.vy - pose.vy) / STEP_SECONDS, 0.209815742496143);
    close(turn.pose.angularVelocity, -0.25); close(turn.pose.angle, -0.00208333333333);
    close(turn.thrust.vectorAngle, -18); close(turn.thrust.fuel, 29.996875);
    const assisted = integratePose({ ...pose, angularVelocity: 15 }, { left: 0.72, right: 0.72 }, 30);
    close(assisted.thrust.left, 0.66); close(assisted.thrust.right, 0.78);
    close(assisted.pose.angularVelocity, 14.92); close(assisted.pose.angle, 0.124333333333);
    const coasting = integratePose({ ...pose, angularVelocity: 15 }, { left: 0, right: 0 }, 30);
    assert.deepEqual({ left: coasting.thrust.left, right: coasting.thrust.right,
        vectorAngle: coasting.thrust.vectorAngle }, { left: 0, right: 0, vectorAngle: 0 });
    close(coasting.pose.angularVelocity, 15); close(coasting.pose.angle, 0.125);
    const exhausted = effectiveThrust({ left: 1, right: 1 }, 0.005);
    close(exhausted.left, 0.3); close(exhausted.right, 0.3); assert.equal(exhausted.fuel, 0);
    assert.equal(exhausted.vectorAngle, 0);
    const digitalRows = [
        [{}, { left: 0, right: 0 }],
        [{ Space: true }, { left: 0.72, right: 0.72 }],
        [{ ArrowLeft: true }, { left: 0, right: 0.375 }],
        [{ ArrowRight: true }, { left: 0.375, right: 0 }],
        [{ Space: true, ArrowLeft: true }, { left: 0.4125, right: 0.7875 }],
        [{ Space: true, ArrowRight: true }, { left: 0.7875, right: 0.4125 }],
        [{ ArrowLeft: true, ArrowRight: true }, { left: 0, right: 0 }],
        [{ Space: true, ArrowLeft: true, ArrowRight: true }, { left: 0.72, right: 0.72 }],
    ];
    for (const [input, expected] of digitalRows) assert.deepEqual(mixDigitalInput(input), expected);
    const pointer = pointerEngineRequests(1000, 1000);
    assert.deepEqual(pointer, { left: 0.7875, right: 0.4125 });
    assert.deepEqual(pointerEngineRequests(-1000, 1000), { left: 0.4125, right: 0.7875 });
    assert.deepEqual(pointerEngineRequests(0, 1000), { left: 0.72, right: 0.72 });
    assert.deepEqual(mixEngineRequests(mixDigitalInput({ Space: true }), pointer), pointer);
    assert.deepEqual(mixEngineRequests(mixDigitalInput({ ArrowLeft: true }), pointer),
        { left: 0.4125, right: 0.7875 });
    assert.ok(pointer.left + pointer.right <= 1.44);
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
    const fixture = join(ROOT, "tests/fixtures/lander-route-derived-v2.json");
    execFileSync(process.execPath, [tool, "--geometry", geometry, "--output", output, "--verify", fixture]);
    assert.equal(await readFile(output, "utf8"), await readFile(fixture, "utf8"));
    const derived = JSON.parse(await readFile(fixture, "utf8"));
    assert.equal(derived.schema, "agw-lander-route-derived/v2");
    assert.equal(derived.deriverVersion, "agw-lander-route-deriver/v3");
    assert.equal(derived.recipeVersion, "agw-lander-route-recipes/v2");
    assert.equal(derived.canonicalPoseDecimals, 9);
    assert.deepEqual(REFERENCE_TEMPLATES, derived.routes);
    assert.equal(ROUTE_DIGESTS.outputDigest, derived.outputDigest);
    assert.equal(ROUTE_DIGESTS.physicsDigest, derived.physicsDigest);
    assert.equal(ROUTE_DIGESTS.geometryDigest, derived.geometryDigest);
    assert.equal(ROUTE_DIGESTS.worldDigest, derived.worldDigest);
    const { outputDigest, ...unsignedDerived } = derived;
    assert.equal(digest(unsignedDerived), outputDigest);
    assert.equal(derived.worldWitnesses.length, 81);
    assert.deepEqual([...new Set(derived.worldWitnesses.map(({ descriptor }) => descriptor.seed))],
        [1, 0x12345678, 0xffffffff]);
    assert.deepEqual([...new Set(derived.worldWitnesses.map(({ descriptor }) =>
        `${descriptor.origin.center}:${descriptor.origin.top}`))], ["36:3.5", "117:5", "-42:6.5"]);
    const expectedWitnessOrder = [1, 0x12345678, 0xffffffff].flatMap((seed) =>
        [[36, 3.5], [117, 5], [-42, 6.5]].map(([center, top]) => ({ seed, center, top })));
    for (let templateIndex = 0; templateIndex < 9; templateIndex += 1) {
        const witnesses = derived.worldWitnesses.slice(templateIndex * 9, templateIndex * 9 + 9);
        assert.deepEqual(witnesses.map(({ descriptor }) => ({ seed: descriptor.seed,
            center: descriptor.origin.center, top: descriptor.origin.top })), expectedWitnessOrder);
    }
    assert.equal(digest(derived.worldWitnesses), derived.worldDigest);
    assert.deepEqual([derived.worldWitnesses[0].digest, derived.worldWitnesses[40].digest,
        derived.worldWitnesses.at(-1).digest], [
        "cea87dac5bab063d0cc916f2f11daccf51cd31cb31851b8d5089e120bfc0c42c",
        "fc7a867f17c662416f5f2388bdfbade634e4e32039b59264b1c6d9c910e67e81",
        "46df6e6052a6f972a1a5e852b97f7d610b0998b3701a00bd8c36c8c2b9b198e1",
    ]);
    assert.ok(derived.worldWitnesses.some(({ descriptor }) =>
        descriptor.corridorSamples.some((sample) => sample.relieved)));
    assert.ok(derived.worldWitnesses.some(({ descriptor }) => descriptor.vertices.some(([, value]) =>
        value !== Number(value.toFixed(derived.canonicalPoseDecimals)))));
    assert.equal(derived.geometryDigest, "a45465787699a9b737b22bb32e0f40ae50913ce14cc3c6c2aeb9300f287ed8d8");
    assert.equal(derived.worldDigest, "9ab22205ef9fbdad86112d1d411b2836ce15f24f234029f441cc52167bd69d73");
    for (const witness of derived.worldWitnesses) {
        const { descriptor } = witness;
        const template = REFERENCE_TEMPLATES.find((candidate) => candidate.templateId === descriptor.templateId);
        const originSite = { id: 0, center: descriptor.origin.center,
            platformLeft: descriptor.origin.center - 4.8, platformRight: descriptor.origin.center + 4.8,
            shelfRight: descriptor.origin.center + 4.8 + 9,
            platformTop: descriptor.origin.top, platformBottom: descriptor.origin.top - 0.35,
            canCollected: true, powered: true, nocStage: 5 };
        const targetSite = instantiateTemplateSite(descriptor.seed, 1, originSite, template);
        assert.deepEqual({ center: targetSite.center, top: targetSite.platformTop }, descriptor.target);
        assert.deepEqual(terrainVerticesForWindow(descriptor.seed, [originSite, targetSite],
            descriptor.vertices[0][0], descriptor.vertices.at(-1)[0]), descriptor.vertices);
        assert.equal(digest(descriptor), witness.digest);
        assert.ok(descriptor.corridorSamples.length > 0);
        assert.ok(descriptor.corridorSamples.every((sample) => sample.y === (sample.relieved ?
            Math.max(0.5, sample.cap - 0.15 * sample.reliefUnit) : sample.raw)));
        for (const sample of descriptor.corridorSamples.concat(descriptor.nativeResumeSamples)) {
            assert.equal(sample.raw ?? sample.y, terrainSample(descriptor.seed, sample.index));
            assert.ok(descriptor.vertices.some(([x, y]) => x === sample.x && y === sample.y));
        }
        assert.deepEqual(descriptor.blendSegments.targetLeft[1],
            [descriptor.target.center - 4.8, descriptor.target.top - 0.8]);
        assert.deepEqual(descriptor.blendSegments.targetRight[0],
            [descriptor.target.center + 4.8 + 9, descriptor.target.top - 0.8]);
        assert.equal(descriptor.sites.length, 2);
        for (const site of descriptor.sites) {
            close(site.platform.right - site.platform.left, 9.6);
            close(site.riser.bottom, site.platform.top - 0.8);
            close(site.riser.top, site.platform.bottom);
            close(site.riser.right - site.riser.left, 9.6);
            close(site.noc.right - site.noc.left, 7);
            close(site.noc.top - site.platform.top, 7.2);
            close(site.noc.bottom, site.platform.top - 0.8);
            close(site.mast.right - site.mast.left, 0.5);
            close(site.mast.top - site.mast.bottom, 3.2);
        }
    }
    assert.ok(derived.routes.every((route) => route.combinationsEvaluated === 81));
    for (const route of derived.routes) {
        for (const result of [route.success, route.smallerFailure]) {
            for (const value of Object.values(result.pose)) assert.equal(value, Number(value.toFixed(9)));
        }
    }
    assert.equal(derived.routes[0].runs[1][1], 209, "the selected route is not the first ranged candidate");
    assert.equal(spawnSync(process.execPath, [tool, "--bogus"]).status, 2);

    const blockedGeometry = join(directory, "blocked-geometry.json");
    const blocked = JSON.parse(await readFile(geometry, "utf8"));
    blocked.templates[0].clearanceKnots[2][1] = 50;
    await writeFile(blockedGeometry, `${JSON.stringify(blocked)}\n`, "utf8");
    assert.equal(spawnSync(process.execPath, [tool, "--geometry", blockedGeometry, "--output", output,
        "--verify", fixture]).status, 1);

    const changedTool = join(directory, "changed-recipes.mjs");
    const changedSource = (await readFile(tool, "utf8")).replace("[3,208,210]", "[3,1,1]");
    assert.notEqual(changedSource, await readFile(tool, "utf8"));
    await writeFile(changedTool, changedSource, "utf8");
    assert.equal(spawnSync(process.execPath, [changedTool, "--geometry", geometry, "--output", output]).status, 1);

    const obstructedWorldTool = join(directory, "obstructed-world.mjs");
    const obstructedWorldSource = (await readFile(tool, "utf8")).replace(
        "const y = raw > cap ? Math.max(0.5, cap - 0.15 * reliefUnit) : raw;",
        "const y = raw + 20;",
    );
    assert.notEqual(obstructedWorldSource, await readFile(tool, "utf8"));
    await writeFile(obstructedWorldTool, obstructedWorldSource, "utf8");
    assert.equal(spawnSync(process.execPath, [obstructedWorldTool, "--geometry", geometry, "--output", output]).status, 1);

    const subtleReliefTool = join(directory, "subtle-relief-world.mjs");
    const subtleReliefSource = (await readFile(tool, "utf8")).replace("cap - 0.15 * reliefUnit", "cap - 0.14 * reliefUnit");
    assert.notEqual(subtleReliefSource, await readFile(tool, "utf8"));
    await writeFile(subtleReliefTool, subtleReliefSource, "utf8");
    assert.equal(spawnSync(process.execPath, [subtleReliefTool, "--geometry", geometry, "--output", output,
        "--verify", fixture]).status, 1);

    const jitterTool = join(directory, "jittered-replay.mjs");
    const jitterSource = (await readFile(tool, "utf8")).replace(
        "return Number(value.toFixed(REPLAY_POSE_DECIMAL_PLACES));",
        "return Number((value + 4e-13).toFixed(REPLAY_POSE_DECIMAL_PLACES));",
    );
    assert.notEqual(jitterSource, await readFile(tool, "utf8"));
    await writeFile(jitterTool, jitterSource, "utf8");
    assert.equal(spawnSync(process.execPath, [jitterTool, "--geometry", geometry, "--output", output,
        "--verify", fixture]).status, 0);

    const precisionTool = join(directory, "changed-replay-precision.mjs");
    const precisionSource = (await readFile(tool, "utf8")).replace(
        "const REPLAY_POSE_DECIMAL_PLACES = 9;", "const REPLAY_POSE_DECIMAL_PLACES = 10;",
    );
    assert.notEqual(precisionSource, await readFile(tool, "utf8"));
    await writeFile(precisionTool, precisionSource, "utf8");
    assert.equal(spawnSync(process.execPath, [precisionTool, "--geometry", geometry, "--output", output,
        "--verify", fixture]).status, 1);
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
    const previous = { x: target.center, y: target.platformTop + 0.5, vx: 1.6, vy: -2.5,
        angle: -10, angularVelocity: 15 };
    const next = { ...previous, y: target.platformTop + 0.2 };
    assert.equal(classifySweptContact(model, previous, next).kind, "safe");
    const limits = [
        ["vx", 1.6], ["vy", -2.5], ["angle", -10], ["angularVelocity", 15],
    ];
    for (const [field, limit] of limits) {
        const excess = limit + Math.sign(limit) * 1e-9;
        assert.equal(classifySweptContact(model, { ...previous, [field]: excess },
            { ...next, [field]: excess }).kind, "unsafe", `${field} beyond the inclusive limit must crash`);
    }
    const tangent = { x: target.center, y: target.platformTop, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, tangent, { ...tangent, x: tangent.x + 0.01 }).cause, "grazing");

    const isolated = { ...model, terrainVertices: [[-100, -20], [200, -20]] };
    for (const x of [target.platformLeft - 20, target.platformRight + 20]) {
        const clear = { x, y: target.platformTop + 0.1, vx: 0, vy: -1, angle: 0, angularVelocity: 0 };
        assert.equal(classifySweptContact(isolated, clear, { ...clear, y: target.platformTop - 0.1 }), null);
    }
});

test("closed unsafe geometry catches slopes, platform equality, solid riser, mast, and precedence", () => {
    const base = createRun({ seed: 1 });
    const site = base.retainedSites[0];
    const slopeModel = { ...base, retainedSites: [], targetSiteId: null, terrainVertices: [[0, 0], [10, 10]] };
    const slopePose = { x: 5, y: 6.62, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(slopeModel, slopePose, slopePose).cause, "terrain");
    const sidePose = { x: site.platformLeft - 1.6 - 0.02, y: site.platformTop - 0.1,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(base, sidePose, sidePose).cause, "platform");
    const riserPose = { x: site.center, y: site.platformTop - 0.6,
        vx: 0, vy: 0, angle: 180, angularVelocity: 0 };
    assert.equal(classifySweptContact(base, riserPose, riserPose).cause, "riser");
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

test("collision uses exact retained 10 metre terrain and rejects the stale fallback seam", () => {
    const model = createRun({ seed: 1 });
    assert.ok(model.terrainVertices.some(([x, y]) => x === 10 && y === 6.055567677598447));
    const cornerPose = { x: 10, y: 5.8, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, cornerPose, cornerPose).cause, "terrain");
    assert.throws(() => classifySweptContact({ ...model, terrainVertices: null }, cornerPose, cornerPose),
        /requires retained terrain vertices/);
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
    const riser = { ...launching, pose: { ...launching.pose, x: active.center,
        y: active.platformTop - 0.6, vy: 1, angle: 180 } };
    assert.equal(stepFlight(riser, { left: 0, right: 0 }).failureCause, "riser");
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
    assert.equal(shell.hasPointerCapture(7), false);
    assert.equal(controller.pointer, null); assert.deepEqual(controller.completedPointer, { token: 1, deadline: 140 });
    assert.notEqual(controller.pulseTimer, null);
    assert.ok(controller.clock.queue.some((edge) => edge.token === 1 && edge.left === 0.72 && edge.right === 0.72));
    assert.equal(controller.clock.queue.at(-1).physical.completedPointerToken, 1);
    controller.completePointerPulse(1, 141, 140);
    assert.deepEqual(controller.completedPointer, { token: 1, deadline: 140 });
    controller.completePointerPulse(1, 140, 140);
    assert.equal(controller.completedPointer, null); assert.equal(controller.pulseTimer, null);
    assert.equal(controller.clock.queue.at(-1).timestamp, 140);
    assert.equal(controller.clock.queue.at(-1).left, 0);
    assert.equal(controller.clock.queue.at(-1).physical.completedPointerToken, null);
    controller.destroy();
});

test("a rapid reused pointer supersedes the old pulse and ignores its stale deadline", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = createRun({ seed: 1 });
    const shell = fixture.elements["lander-scene-shell"];
    shell.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 100, clientY: 50, timeStamp: 0 });
    shell.dispatchEvent({ type: "pointerup", pointerId: 7, isPrimary: true, button: 0,
        clientX: 102, clientY: 50, timeStamp: 20 });
    assert.deepEqual(controller.completedPointer, { token: 1, deadline: 140 });
    const firstTimer = controller.pulseTimer;
    shell.dispatchEvent({ type: "pointerdown", pointerId: 7, isPrimary: true, button: 0,
        clientX: 200, clientY: 50, timeStamp: 60 });
    assert.equal(controller.completedPointer, null); assert.equal(controller.pulseTimer, null);
    assert.equal(controller.pointer.token, 2); assert.notEqual(firstTimer, controller.pulseTimer);
    assert.deepEqual(controller.clock.queue.slice(-2).map(({ timestamp, left, right, token }) =>
        ({ timestamp, left, right, token })), [
        { timestamp: 60, left: 0, right: 0, token: null },
        { timestamp: 60, left: 0.72, right: 0.72, token: 2 },
    ]);
    controller.completePointerPulse(1, 140, 140);
    assert.equal(controller.pointer.token, 2);
    assert.deepEqual(controller.pointerInput, { left: 0.72, right: 0.72 });
    shell.dispatchEvent({ type: "pointerup", pointerId: 7, isPrimary: true, button: 0,
        clientX: 202, clientY: 50, timeStamp: 160 });
    assert.deepEqual(controller.completedPointer, { token: 2, deadline: 200 });
    assert.notEqual(controller.pulseTimer, null);
    controller.completePointerPulse(1, 140, 200);
    assert.deepEqual(controller.completedPointer, { token: 2, deadline: 200 });
    controller.completePointerPulse(2, 200, 200);
    assert.equal(controller.completedPointer, null); assert.equal(controller.pulseTimer, null);
    assert.deepEqual([controller.clock.queue.at(-1).timestamp, controller.clock.queue.at(-1).left,
        controller.clock.queue.at(-1).right], [200, 0, 0]);
    assert.equal(controller.clock.queue.at(-1).physical.completedPointerToken, null);
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

test("visibility teardown restores the complete zero command and neutral plume vector", async () => {
    const { LanderGameController } = await controllerClasses();
    document.hidden = false;
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = { ...createRun({ seed: 1 }),
        commanded: { left: 0.4125, right: 0.7875, vectorAngle: -18 } };
    controller.render();
    assert.equal(controller.root.style.properties.get("--thrust-vector-angle"), "-18deg");
    document.hidden = true;
    controller.onVisibilityChange(); controller.render();
    assert.deepEqual(controller.model.commanded, { left: 0, right: 0, vectorAngle: 0 });
    assert.equal(controller.root.style.properties.get("--thrust-vector-angle"), "0deg");
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
    assert.equal(group.querySelector(".noc-building").attributes.get("d"),
        `M${active.platformRight * 10 + 20} ${548 - active.foundationBottom * 10}` +
        `V${548 - active.platformTop * 10 - 72}h70V${548 - active.foundationBottom * 10}Z`);
    controller.model = updateRetention(advanceMissionSequence(model, 0.2)); controller.render();
    assert.equal(group.dataset.nocStage, "3");
    controller.destroy();
});

test("static and dynamic support, battery, shelf, and riser geometry stay identical", async () => {
    const model = updateRetention(createRun({ seed: STATIC_WORLD_SEED }));
    const active = model.retainedSites[0];
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    controller.model = model; controller.render();
    const group = fixture.elements["site-layer"].querySelector(`[data-site-id="${active.id}"]`);
    const template = await readFile(join(ROOT, "templates/lander-game.html"), "utf8");
    const staticSupport = template.match(/class="platform-supports" d="([^"]+)"/);
    assert.ok(staticSupport);
    const support = group.querySelector(".platform-supports");
    assert.equal(support.attributes.get("d"), staticSupport[1]);
    const left = active.platformLeft * 10; const right = active.platformRight * 10;
    const top = 548 - active.platformTop * 10; const bottom = 548 - active.platformBottom * 10;
    const shelf = 548 - active.foundationBottom * 10;
    assert.ok(support.attributes.get("d").startsWith(`M${left} ${bottom}H${right}V${shelf}H${left}Z`));
    const riserPose = { x: active.center, y: active.foundationBottom + 0.2,
        vx: 0, vy: 0, angle: 180, angularVelocity: 0 };
    assert.equal(classifySweptContact(model, riserPose, riserPose).cause, "riser");

    const battery = group.querySelector(".noc-battery");
    assert.deepEqual(battery.children.map((node) => node.className ?? null),
        [null, "battery-terminal", "battery-bar battery-bar-1", "battery-bar battery-bar-2",
            "battery-bar battery-bar-3", "battery-bar battery-bar-4"]);
    const buildingLeft = right + 20; const roof = top - 72;
    const rectangle = battery.children[0];
    assert.deepEqual(["x","y","width","height","rx"].map((name) => rectangle.attributes.get(name)),
        [buildingLeft + 24, roof + 16, 22, 40, 2].map(String));
    assert.equal(battery.children[1].attributes.get("d"), `M${buildingLeft + 30} ${roof + 16}v-6h10v6`);
    const barTops = [46, 38, 30, 22];
    for (let index = 1; index <= 4; index += 1) {
        assert.equal(battery.children[index + 1].attributes.get("d"),
            `M${buildingLeft + 29} ${roof + barTops[index - 1]}h12v5h-12Z`);
    }
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

test("worst-case world projection stays within eighty descendants during a crash", async () => {
    let model = createRun({ seed: 1 });
    const sites = [model.retainedSites[0]];
    sites.push(instantiateTemplateSite(model.seed, 1, sites[0], REFERENCE_TEMPLATES[0]));
    sites.push(instantiateTemplateSite(model.seed, 2, sites[1], REFERENCE_TEMPLATES[1]));
    model = updateRetention({ ...model, pose: { ...model.pose, x: sites[1].center }, retainedSites: sites,
        activeSiteId: 1, targetSiteId: 2 });
    assert.equal(model.retainedChunks.length, 5); assert.equal(model.retainedSites.length, 3);
    const fragments = Array.from({ length: 8 }, (_, id) => ({ id, x: model.pose.x, y: model.pose.y,
        vx: id / 10, vy: id / 20, angularVelocity: id, color: "#292b30" }));
    model = { ...model, state: "crashing", crash: { pose: model.pose, fragments }, sequenceSeconds: 0.1 };

    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture(); const controller = new LanderGameController(fixture.root);
    const world = new FakeElement(); const missionLander = new FakeElement();
    missionLander.append(...Array.from({ length: 4 }, () => new FakeElement()));
    fixture.elements["mission-agent"].append(new FakeElement(), new FakeElement());
    world.append(fixture.elements["terrain-layer"], fixture.elements["site-layer"],
        fixture.elements["debris-layer"], missionLander, fixture.elements["mission-agent"]);
    controller.model = model; controller.render();
    assert.equal(fixture.elements["terrain-layer"].children.length, 5);
    assert.equal(fixture.elements["site-layer"].children.length, 3);
    assert.equal(fixture.elements["debris-layer"].children.length, 8);
    assert.equal(descendantCount(world), 75);
    assert.ok(descendantCount(world) <= 80);
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
        assert.ok(model.retainedChunks.length <= 5);
        assert.ok(model.fuel >= 0);
    }
    assert.equal(model.completedSites, 100);
    assert.ok(model.awardRatio > 1);
    assert.ok(maximumGenerationMilliseconds < 50);
});
