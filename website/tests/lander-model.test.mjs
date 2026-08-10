import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtemp, readFile } from "node:fs/promises";
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
    stepFlight,
    transitionMission,
    updateRetention,
} from "../static/lander-model.js";

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

test("independent derivation CLI reproduces canonical bytes and rejects misuse", async () => {
    const directory = await mkdtemp(join(tmpdir(), "agw-route-test-"));
    const output = join(directory, "routes.json");
    const tool = join(ROOT, "tools/derive_lander_routes.mjs");
    const geometry = join(ROOT, "tests/fixtures/lander-route-geometry-v1.json");
    const fixture = join(ROOT, "tests/fixtures/lander-route-derived-v1.json");
    execFileSync(process.execPath, [tool, "--geometry", geometry, "--output", output, "--verify", fixture]);
    assert.equal(await readFile(output, "utf8"), await readFile(fixture, "utf8"));
    assert.equal(spawnSync(process.execPath, [tool, "--bogus"]).status, 2);
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
    for (let index = 0; index < 65; index += 1) clock = enqueueInputEdge(clock, { timestamp: 1, left: index % 2, right: 0 });
    assert.equal(clock.queue.length, 1);
    assert.equal(clock.queue[0].snapshot, true);
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
