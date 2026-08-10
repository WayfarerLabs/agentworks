import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    FAILURE_STATUS,
    FUEL_CAPACITY,
    MAX_CATCH_UP_STEPS,
    STEP_SECONDS,
    SUCCESS_STATUS,
    DeploymentMission,
    advanceCue,
    advanceMissionSequence,
    advanceSimulation,
    contactForPose,
    createCueState,
    createFlightModel,
    createPreflightModel,
    createSimulationClock,
    effectiveThrust,
    enqueueInputEdge,
    mixDigitalInput,
    mixEngineRequests,
    normalizeDegrees,
    plumeForThrust,
    pointerEngineRequests,
    settleCue,
    stepFlight,
    transformLocalPoint,
    transitionMission,
} from "../static/lander-model.js";

const TOLERANCE = 1e-10;
const ZERO_INPUT = { left: 0, right: 0 };

function close(actual, expected, tolerance = TOLERANCE) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `expected ${actual} to be within ${tolerance} of ${expected}`);
}

function vectorModel(overrides = {}) {
    return {
        ...createFlightModel(),
        pose: {
            x: 10,
            y: 30,
            vx: 0,
            vy: 0,
            angle: 0,
            angularVelocity: 0,
            ...overrides,
        },
    };
}

function runSteps(model, count, input) {
    let current = model;
    for (let index = 0; index < count; index += 1) {
        current = stepFlight(current, input);
    }
    return current;
}

function schedule(hertz, edges = []) {
    let model = createFlightModel();
    let clock = createSimulationClock(0);
    for (const edge of edges) {
        clock = enqueueInputEdge(clock, edge);
    }
    for (let frame = 1; frame <= hertz; frame += 1) {
        const result = advanceSimulation(clock, model, (frame * 1000) / hertz);
        clock = result.clock;
        model = result.model;
    }
    return { clock, model };
}

class FakeElement {
    constructor(parentElement = null) {
        this.parentElement = parentElement;
        this.hidden = false;
        this.disabled = false;
        this.tabIndex = -1;
        this.textContent = "";
        this.dataset = {};
        this.attributes = new Map();
        this.style = { setProperty() {} };
    }

    addEventListener() {}

    setAttribute(name, value) {
        this.attributes.set(name, value);
    }

    removeAttribute(name) {
        this.attributes.delete(name);
    }

    focus() {
        document.activeElement = this;
    }
}

function createControllerFixture() {
    const root = new FakeElement();
    const actions = new FakeElement(root);
    const elements = {
        "#lander-scene-shell": new FakeElement(root),
        "#lander-scene": new FakeElement(root),
        "#lander-start": new FakeElement(root),
        "#lander-controls": new FakeElement(root),
        "#lander-actions": actions,
        "#lander-exit": new FakeElement(actions),
        "#lander-restart": new FakeElement(actions),
        "#lander-status": new FakeElement(root),
    };
    root.querySelector = (selector) => elements[selector];
    return { root, ...Object.fromEntries(Object.entries(elements).map(([key, value]) => [key.slice(1), value])) };
}

let controllerModule;

async function loadControllerModule() {
    if (!controllerModule) {
        const automaticFixture = createControllerFixture();
        globalThis.Element = FakeElement;
        globalThis.document = {
            activeElement: null,
            body: new FakeElement(),
            hidden: true,
            addEventListener() {},
            querySelector: () => automaticFixture.root,
        };
        globalThis.window = {
            addEventListener() {},
            setTimeout,
        };
        globalThis.matchMedia = () => ({
            matches: false,
            addEventListener() {},
        });
        globalThis.requestAnimationFrame = () => 1;
        globalThis.cancelAnimationFrame = () => {};
        controllerModule = await import("../static/lander-game.js");
    }
    return controllerModule;
}

function isEffectivelyVisible(element) {
    for (let current = element; current; current = current.parentElement) {
        if (current.hidden) {
            return false;
        }
    }
    return true;
}

function isEffectivelyFocusable(element) {
    return isEffectivelyVisible(element) && !element.disabled;
}

function assertDestroyedActions(fixture) {
    assert.equal(fixture["lander-actions"].hidden, true);
    assert.equal(isEffectivelyVisible(fixture["lander-exit"]), false);
    assert.equal(fixture["lander-exit"].disabled, true);
    assert.equal(isEffectivelyFocusable(fixture["lander-exit"]), false);
    assert.equal(fixture["lander-restart"].hidden, true);
    assert.equal(isEffectivelyVisible(fixture["lander-restart"]), false);
    assert.equal(fixture["lander-restart"].disabled, true);
    assert.equal(isEffectivelyFocusable(fixture["lander-restart"]), false);
}

test("the controller imports the one model scheduler instead of duplicating it", async () => {
    const controller = await readFile(new URL("../static/lander-game.js", import.meta.url), "utf8");
    assert.match(controller, /advanceSimulation/);
    assert.doesNotMatch(controller, /STEP_SECONDS|MAX_CATCH_UP_STEPS|while \(accumulator/);
});

test("DeploymentMission accepts only the legal event matrix", () => {
    const machine = new DeploymentMission();
    const preflight = machine.model;
    assert.equal(machine.send("RESTART"), preflight);
    assert.equal(machine.send("START").state, "flying");
    const flying = machine.model;
    assert.equal(machine.send("LANDING_SETTLED"), flying);
    assert.equal(machine.send("UNSAFE_CONTACT", { cause: "noc" }).state, "failed");
    assert.equal(machine.model.failureCause, "noc");
    assert.equal(machine.model.status, FAILURE_STATUS);
    assert.equal(machine.send("RESTART").state, "flying");
    assert.equal(machine.send("OUT_OF_BOUNDS").failureCause, "bounds");
    assert.equal(machine.send("EXIT").state, "preflight");
});

test("normal and reduced-motion success transitions enforce entry invariants", () => {
    const angle = -8;
    const y = 1.6 * Math.abs(Math.sin((angle * Math.PI) / 180));
    const flying = {
        ...createFlightModel(),
        pose: {
            x: 30,
            y,
            vx: 1.4,
            vy: -2.2,
            angle,
            angularVelocity: 12,
        },
    };
    const landed = transitionMission(flying, "SAFE_CONTACT");
    assert.equal(landed.state, "landed");
    assert.deepEqual(landed.commanded, { left: 0, right: 0 });
    assert.equal(landed.status, "Touchdown confirmed. Deploying agent.");
    const deploying = transitionMission(landed, "LANDING_SETTLED");
    assert.equal(deploying.bayOpen, true);
    assert.equal(deploying.agentVisible, true);
    const powering = transitionMission(deploying, "AGENT_ENTERED");
    assert.equal(powering.agentVisible, false);
    const departing = transitionMission(powering, "NOC_POWERED");
    assert.equal(departing.nocPower, true);
    assert.equal(departing.nocStage, 4);
    assert.deepEqual(departing.commanded, { left: 0.82, right: 0.82 });
    const succeeded = transitionMission(departing, "LANDER_DEPARTED");
    assert.equal(succeeded.state, "succeeded");
    assert.equal(succeeded.landerVisible, false);
    assert.equal(succeeded.status, SUCCESS_STATUS);

    const reduced = transitionMission(flying, "SAFE_CONTACT", { reducedMotion: true });
    assert.equal(reduced.state, "succeeded");
    assert.equal(reduced.nocPower, true);
    assert.equal(reduced.nocStage, 4);
    assert.equal(reduced.agentVisible, false);
    assert.equal(reduced.landerVisible, false);
    assert.equal(reduced.status, SUCCESS_STATUS);
});

test("digital aliases, hold-through-start, pointer bias, and source mixing are exact", () => {
    assert.deepEqual(mixDigitalInput({ Space: true }), { left: 0.72, right: 0.72 });
    assert.deepEqual(mixDigitalInput({ ArrowUp: true, ArrowLeft: true }), {
        left: 0.72,
        right: 1,
    });
    assert.deepEqual(mixDigitalInput({ Space: true, ArrowUp: true, KeyH: true, KeyL: true }), {
        left: 1,
        right: 1,
    });
    assert.deepEqual(pointerEngineRequests(0, 1000), { left: 0.72, right: 0.72 });
    assert.deepEqual(pointerEngineRequests(180, 1000), { left: 1, right: 0.43999999999999995 });
    assert.deepEqual(pointerEngineRequests(-180, 1000), { left: 0.43999999999999995, right: 1 });
    assert.deepEqual(mixEngineRequests({ left: 0.72, right: 1 }, { left: 0.9, right: 0.4 }), { left: 0.9, right: 1 });

    let clock = enqueueInputEdge(createSimulationClock(0), {
        timestamp: 0,
        left: 0.72,
        right: 0.72,
    });
    const result = advanceSimulation(clock, createFlightModel(), 1000 / 120);
    assert.deepEqual(result.model.commanded, { left: 0.72, right: 0.72 });
});

test("gravity vector is deterministic for 120 steps", () => {
    const model = runSteps(vectorModel(), 120, { left: 0, right: 0 });
    close(model.pose.x, 10);
    close(model.pose.y, 28.4875);
    close(model.pose.vx, 0);
    close(model.pose.vy, -3);
    close(model.fuel, 30);
});

test("collective vector is deterministic for 120 steps", () => {
    const model = runSteps(vectorModel(), 120, { left: 0.72, right: 0.72 });
    close(model.pose.y, 31.5367);
    close(model.pose.vy, 3.048);
    close(model.pose.x, 10);
    close(model.pose.angle, 0);
    close(model.fuel, 28.56);
});

test("one right-engine step matches the pinned semi-implicit vector", () => {
    const model = stepFlight(vectorModel(), { left: 0, right: 1 });
    close(model.pose.x, 10);
    close(model.pose.y, 30.0000833333333);
    close(model.pose.vy, 0.01);
    close(model.pose.angularVelocity, -0.583333333333);
    close(model.pose.angle, -0.00486111111111);
    close(model.fuel, 29.9916666666667);
});

test("fuel exhaustion scales engines equally and never goes negative", () => {
    const thrust = effectiveThrust({ left: 1, right: 1 }, 0.005);
    close(thrust.left, 0.3);
    close(thrust.right, 0.3);
    assert.equal(thrust.fuel, 0);
    assert.deepEqual(effectiveThrust({ left: 1, right: 1 }, 0), {
        left: 0,
        right: 0,
        fuel: 0,
    });
});

test("plume mapping is exact and clamps requests", () => {
    assert.deepEqual(plumeForThrust(0), { scaleY: 0.08, opacity: 0.25 });
    assert.deepEqual(plumeForThrust(0.5), { scaleY: 0.54, opacity: 0.625 });
    assert.deepEqual(plumeForThrust(1), { scaleY: 1, opacity: 1 });
    assert.deepEqual(plumeForThrust(2), plumeForThrust(1));
});

test("clockwise local transforms define feet, hull, render, and bay geometry", () => {
    assert.deepEqual(transformLocalPoint({ x: 10, y: 20, angle: 0 }, 2, 3), { x: 12, y: 23 });
    const rotated = transformLocalPoint({ x: 10, y: 20, angle: 90 }, 2, 3);
    close(rotated.x, 13);
    close(rotated.y, 18);
    assert.equal(normalizeDegrees(180), -180);
    assert.equal(normalizeDegrees(540), -180);
});

test("safe landing includes every envelope edge and epsilon violations fail", () => {
    const angle = -8;
    const y = 1.6 * Math.abs(Math.sin((angle * Math.PI) / 180));
    const edgePose = {
        x: 30,
        y,
        vx: 1.4,
        vy: -2.2,
        angle,
        angularVelocity: 12,
    };
    assert.equal(contactForPose(edgePose).safe, true);
    for (const [field, value] of [
        ["vx", 1.4 + 1e-9],
        ["vy", -2.2 - 1e-9],
        ["angle", -8 - 1e-9],
        ["angularVelocity", 12 + 1e-9],
    ]) {
        assert.equal(contactForPose({ ...edgePose, [field]: value }).safe, false, field);
    }
    const outsideZone = { ...edgePose, x: 43.7 };
    assert.equal(contactForPose(outsideZone).safe, false);
});

test("a lower ground crossing settles safely from the raw pose instead of failing bounds", () => {
    const landed = stepFlight(
        vectorModel({ x: 30, y: 0.001, vx: 0, vy: -1, angle: 0, angularVelocity: 0 }),
        ZERO_INPUT,
    );
    assert.equal(landed.state, "landed");
    assert.equal(landed.failureCause, null);
    const lowerFoot = Math.min(...contactForPose(landed.pose).feet.map((foot) => foot.y));
    assert.equal(lowerFoot, 0);
});

test("contact precedence is NOC, unsafe surface, bounds, then safe touchdown", () => {
    const noc = stepFlight(vectorModel({ x: 55, y: 0.01, vx: 0, vy: -1, angle: 0, angularVelocity: 0 }), ZERO_INPUT);
    assert.equal(noc.failureCause, "noc");

    const surfaceAndBound = stepFlight(
        vectorModel({ x: 94, y: 0.001, vx: 0, vy: -2.2, angle: 0, angularVelocity: 0 }),
        ZERO_INPUT,
    );
    assert.equal(surfaceAndBound.failureCause, "surface");
    assert.equal(surfaceAndBound.pose.x, 93);
    assert.equal(surfaceAndBound.pose.y, 0);

    const bound = stepFlight(vectorModel({ x: 30, y: 48, vx: 0, vy: 1, angle: 0, angularVelocity: 0 }), ZERO_INPUT);
    assert.equal(bound.failureCause, "bounds");
    assert.equal(bound.pose.y, 48);
});

test("30, 60, and 120 Hz schedules produce the same 120-step flight", () => {
    for (const hertz of [30, 60, 120]) {
        const { clock, model } = schedule(hertz);
        assert.equal(clock.cursor, 120);
        close(model.pose.x, 30.8);
        close(model.pose.y, 30.0875);
        close(model.pose.vx, 0.8);
        close(model.pose.vy, -3.4);
        close(model.pose.angle, 0);
        close(model.pose.angularVelocity, 0);
        close(model.fuel, FUEL_CAPACITY);
    }
});

test("timestamped input ties are frame-schedule independent", () => {
    const edges = [
        { timestamp: 125, left: 0.72, right: 0.72 },
        { timestamp: 375, left: 0.72, right: 1 },
        { timestamp: 625, left: 0.72, right: 0.72 },
        { timestamp: 875, left: 0, right: 0 },
    ];
    for (const hertz of [30, 60, 120]) {
        const { clock, model } = schedule(hertz, edges);
        assert.equal(clock.cursor, 120);
        close(model.pose.x, 30.789294822951447);
        close(model.pose.y, 32.5627822785585);
        close(model.pose.vx, 0.7612478269876443);
        close(model.pose.vy, 1.4296050729753735);
        close(model.pose.angle, -2.5112500000010414);
        close(model.pose.angularVelocity, -4.9);
        close(model.fuel, 28.85);
    }
});

test("edge timestamp ties retain enqueue sequence", () => {
    let clock = createSimulationClock(0);
    clock = enqueueInputEdge(clock, { timestamp: 1000 / 120, left: 1, right: 0 });
    clock = enqueueInputEdge(clock, { timestamp: 1000 / 120, left: 0, right: 1 });
    const result = advanceSimulation(clock, createFlightModel(), 1000 / 120);
    assert.deepEqual(result.model.commanded, { left: 0, right: 1 });
});

test("catch-up is bounded and a stall discards time before one clean resume step", () => {
    const capped = advanceSimulation(createSimulationClock(0), createFlightModel(), 100);
    assert.equal(capped.steps, MAX_CATCH_UP_STEPS);

    let clock = createSimulationClock(0);
    let model = createFlightModel();
    let result = advanceSimulation(clock, model, 100.000001);
    assert.equal(result.discarded, true);
    assert.equal(result.steps, 0);
    assert.equal(result.model, model);
    clock = result.clock;
    result = advanceSimulation(clock, model, 108.333335);
    assert.equal(result.discarded, false);
    assert.equal(result.steps, 1);
});

test("deployment clocks route the agent, power each stage, and script departure", () => {
    const touchdown = {
        ...createFlightModel(),
        state: "landed",
        pose: { x: 30, y: 0, vx: 0, vy: 0, angle: 6, angularVelocity: 0 },
        touchdownPose: { x: 30, y: 0, vx: 0, vy: 0, angle: 6, angularVelocity: 0 },
        status: "Touchdown confirmed. Deploying agent.",
    };
    const deploying = advanceMissionSequence(touchdown, 0.3);
    assert.equal(deploying.state, "deploying");
    assert.equal(deploying.bayOpen, true);
    const descending = advanceMissionSequence(deploying, 0.35);
    close(descending.agentPosition.y, 0);
    const powering = advanceMissionSequence(deploying, 2.2);
    assert.equal(powering.state, "powering");
    assert.equal(powering.agentVisible, false);
    assert.equal(advanceMissionSequence(powering, 0.199).nocStage, 0);
    assert.equal(advanceMissionSequence(powering, 0.2).nocStage, 1);
    assert.equal(advanceMissionSequence(powering, 0.4).nocStage, 2);
    assert.equal(advanceMissionSequence(powering, 0.6).nocStage, 3);
    assert.equal(advanceMissionSequence(powering, 0.8).nocStage, 4);
    const departing = advanceMissionSequence(powering, 1);
    assert.equal(departing.state, "departing");
    assert.equal(departing.nocPower, true);
    const midpoint = advanceMissionSequence(departing, 0.9);
    close(midpoint.pose.x, 33);
    close(midpoint.pose.y, 31);
    close(midpoint.pose.angle, 3);
    assert.deepEqual(midpoint.commanded, { left: 0.82, right: 0.82 });
    const succeeded = advanceMissionSequence(midpoint, 0.9);
    assert.equal(succeeded.state, "succeeded");
    assert.equal(succeeded.landerVisible, false);
    assert.deepEqual(succeeded.commanded, ZERO_INPUT);
    assert.equal(succeeded.status, SUCCESS_STATUS);
});

test("reduced motion atomically completes any decorative sequence", () => {
    for (const state of ["landed", "deploying", "powering", "departing"]) {
        const model = { ...createFlightModel(), state };
        const result = advanceMissionSequence(model, 0, true);
        assert.equal(result.state, "succeeded");
        assert.equal(result.nocPower, true);
        assert.equal(result.nocStage, 4);
        assert.equal(result.landerVisible, false);
        assert.equal(result.status, SUCCESS_STATUS);
    }
});

test("the subtle cue is one-shot and exit creates strict settled preflight", () => {
    const cue = createCueState(false);
    assert.equal(advanceCue(cue, 2.399).state, "running");
    assert.deepEqual(advanceCue(cue, 2.4), { state: "settled", elapsed: 2.4 });
    assert.equal(createCueState(true).state, "settled");
    assert.equal(settleCue().state, "settled");

    const powered = {
        ...createFlightModel(),
        state: "succeeded",
        nocPower: true,
        nocStage: 4,
        fuel: 1,
        landerVisible: false,
        status: SUCCESS_STATUS,
    };
    const exited = transitionMission(powered, "EXIT");
    assert.deepEqual(exited, createPreflightModel());
    const restarted = transitionMission(powered, "RESTART");
    assert.deepEqual(restarted, createFlightModel());
});

test("destroy hides and disables native actions from an active controller", async () => {
    const { LanderGameController } = await loadControllerModule();
    const fixture = createControllerFixture();
    const controller = new LanderGameController(fixture.root);
    controller.start(false, 0);

    assert.equal(fixture["lander-actions"].hidden, false);
    assert.equal(isEffectivelyFocusable(fixture["lander-exit"]), true);
    assert.equal(fixture["lander-restart"].hidden, true);
    assert.equal(isEffectivelyFocusable(fixture["lander-restart"]), false);

    controller.destroy();

    assert.equal(controller.model.state, "preflight");
    assertDestroyedActions(fixture);
});

test("destroy removes focusable native actions from a terminal controller", async () => {
    const { LanderGameController } = await loadControllerModule();
    const fixture = createControllerFixture();
    const controller = new LanderGameController(fixture.root);
    controller.start(false, 0);
    controller.model = transitionMission(controller.model, "UNSAFE_CONTACT");
    controller.render();

    assert.equal(controller.model.state, "failed");
    assert.equal(fixture["lander-actions"].hidden, false);
    assert.equal(isEffectivelyFocusable(fixture["lander-exit"]), true);
    assert.equal(fixture["lander-restart"].hidden, false);
    assert.equal(isEffectivelyFocusable(fixture["lander-restart"]), true);

    controller.destroy();

    assert.equal(controller.model.state, "preflight");
    assertDestroyedActions(fixture);
});
