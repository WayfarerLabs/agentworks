import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    MAX_LANDING_ANGLE,
    MAX_LANDING_ANGULAR_SPEED,
    MAX_LANDING_DESCENT_SPEED,
    MAX_LANDING_HORIZONTAL_SPEED,
    REFERENCE_TEMPLATES,
    ROUTE_DIGESTS,
    createRun,
    mixDigitalInput,
    pointerEngineRequests,
    stepFlight,
} from "../static/lander-model.js";
import { FakeElement, controllerClasses, controllerFixture } from "./lander-test-dom.mjs";

const ROOT = new URL("../", import.meta.url);
const EXPECTED_DIGESTS = Object.freeze({
    geometryDigest: "a5120d97782b73afb43cabae038412252f644656f41c0ab9e33f5413da9be7ca",
    outputDigest: "a922372760f850386810fd6eb60f7aa807bac8b03ee5f0a2b1dec1968ee27b69",
    physicsDigest: "e08f8260b723dd245db88de9ae2cdbac54bf9a97cb0bed1b6f170eda362c48dc",
    worldDigest: "c666bb42918301f93386bb1373e92da662d333006d8684946fd80a10761d1e32",
});

async function controllerAt(model = createRun({ seed: 1 })) {
    const { LanderGameController } = await controllerClasses();
    globalThis.document.hidden = true;
    const fixture = controllerFixture();
    const controller = new LanderGameController(fixture.root);
    controller.model = model;
    controller.render();
    return { controller, ...fixture };
}

function keyEvent(target, path, key, code = key, type = "keydown") {
    return {
        type,
        target,
        key,
        code,
        repeat: false,
        ctrlKey: false,
        altKey: false,
        metaKey: false,
        shiftKey: false,
        timeStamp: 10,
        composedPath: path ? () => path : undefined,
        preventDefault() { this.defaultPrevented = true; },
    };
}

function inputSnapshot(controller) {
    return {
        model: structuredClone(controller.model),
        held: [...controller.heldKeys],
        pointerToken: controller.pointerToken,
        pulse: structuredClone(controller.collectivePulse),
        queue: structuredClone(controller.clock.queue),
    };
}

test("Phase 4K pins the exact landing profile, digests, and sole changed route", () => {
    assert.deepEqual([
        MAX_LANDING_HORIZONTAL_SPEED,
        MAX_LANDING_DESCENT_SPEED,
        MAX_LANDING_ANGLE,
        MAX_LANDING_ANGULAR_SPEED,
    ], [2.2, 3.6, 18, 26]);
    assert.deepEqual(ROUTE_DIGESTS, EXPECTED_DIGESTS);
    const route = REFERENCE_TEMPLATES.find(({ templateId }) => templateId === "route-93-flat");
    assert.deepEqual(route.runs.slice(24), [[3, 46], [4, 1], [3, 36], [4, 49]]);
    assert.equal(route.scheduleDigest, 1498651857);
    assert.deepEqual(route.success, {
        contactStep: 2875,
        burn: 8.121999999999856,
        classification: "safe",
        pose: { x: 91.590782713, y: 0.269134094, vx: 1.608662298, vy: -1.223992666,
            angle: -9.683765214, angularVelocity: 5.733285716 },
    });
    assert.deepEqual(route.smallerFailure, {
        allowance: 8.1,
        burn: 8.100000000000005,
        exhaustionStep: 2872,
        pose: { x: 91.562983183, y: 0.290173104, vx: 1.699073271, vy: -1.281241753,
            angle: -9.783560214, angularVelocity: 6.322201732 },
    });
});

test("production, validator, and fake DOM sources contain no native Launch authority", async () => {
    const sources = await Promise.all([
        "templates/lander-game.html",
        "static/lander-game.js",
        "static/lander.css",
        "site_game_validation.py",
        "tests/lander-test-dom.mjs",
    ].map((path) => readFile(new URL(path, ROOT), "utf8")));
    for (const source of sources) {
        assert.equal(source.includes("lander-launch"), false);
        assert.equal(source.includes("launch-button"), false);
    }
});

test("launch-ready departure uses ordinary keyboard, vi, pointer, and touch requests", () => {
    const ready = { ...createRun({ seed: 1 }), state: "launching", launchStarted: false, status: "sentinel" };
    const keyboard = (...codes) => mixDigitalInput(Object.fromEntries(codes.map((code) => [code, true])));
    const requests = [
        ["Space", keyboard("Space")],
        ["Up", keyboard("ArrowUp")],
        ["Space plus vi", keyboard("Space", "KeyH")],
        ["Up plus arrow", keyboard("ArrowUp", "ArrowRight")],
        ["pointer hold", pointerEngineRequests(0, 1000)],
        ["touch hold", pointerEngineRequests(80, 1000)],
    ];
    for (const [authority, request] of requests) {
        const departed = stepFlight(ready, request);
        assert.equal(departed.launchStarted, true, authority);
        assert.equal(departed.status, "", authority);
        assert.ok(departed.fuel < ready.fuel, authority);
    }
    const turnOnly = stepFlight(ready, keyboard("KeyL"));
    assert.equal(turnOnly.launchStarted, false);
    assert.equal(turnOnly.fuel, ready.fuel);
});

test("active projection exposes persistent Exit and failure-only Restart in exact focus order", async () => {
    const { controller, elements } = await controllerAt();
    controller.model = { ...controller.model, state: "preflight" };
    controller.render();
    assert.equal(elements["lander-controls-rail"].hidden, true);
    assert.equal(elements["lander-exit"].disabled, true);
    assert.equal(elements["lander-restart"].hidden, true);

    controller.start(false, 0);
    assert.equal(elements["lander-scene-shell"].tabIndex, 0);
    assert.equal(elements["lander-controls-rail"].hidden, false);
    assert.equal(elements["lander-exit"].disabled, false);
    assert.equal(elements["lander-restart"].hidden, true);
    assert.deepEqual(elements["lander-scene-shell"].attributes.get("aria-describedby").split(" "),
        ["lander-scene-description", "lander-controls", "lander-fuel-label", "lander-fuel-value",
            "lander-status"]);
    assert.deepEqual(elements["lander-outcome"].children,
        [elements["lander-status"], elements["lander-restart"]]);
    assert.deepEqual(elements["lander-controls-rail"].children,
        [elements["lander-controls"], elements["lander-exit"]]);
    assert.equal(elements["lander-restart"].attributes.get("aria-keyshortcuts"), "r");
    assert.equal(elements["lander-exit"].attributes.get("aria-keyshortcuts"), "Escape");
    for (const action of ["lander-restart", "lander-exit"]) {
        assert.equal(elements[`${action}-hint`].attributes.get("aria-hidden"), "true");
        assert.equal(elements[`${action}-hint`].className, "lander-key-hint");
    }
    assert.equal("lander_launch" in controller, false);

    controller.model = { ...controller.model, state: "launching", launchStarted: false };
    controller.render();
    assert.equal(elements["lander-restart"].hidden, true);
    assert.equal(elements["lander-exit"].disabled, false);
    controller.model = { ...controller.model, state: "failed" };
    controller.render();
    assert.equal(elements["lander-restart"].hidden, false);
    assert.equal(elements["lander-restart"].disabled, false);
    assert.equal(elements["lander-exit"].disabled, false);
    controller.destroy();
});

test("outside-shell game keys are inert through composed and ancestor fallback paths", async () => {
    for (const state of ["flying", "launching", "failed"]) {
        const model = { ...createRun({ seed: 1 }), state, launchStarted: state !== "launching" };
        const { controller } = await controllerAt(model);
        const header = new FakeElement(globalThis.document.body);
        const child = new FakeElement(header);
        for (const [key, code] of [["Escape", "Escape"], ["r", "KeyR"], [" ", "Space"],
            ["ArrowUp", "ArrowUp"], ["ArrowLeft", "ArrowLeft"], ["h", "KeyH"], ["l", "KeyL"]]) {
            for (const composed of [true, false]) {
                const before = inputSnapshot(controller);
                const path = composed ? [child, header, globalThis.document.body] : null;
                const down = keyEvent(child, path, key, code);
                controller.onKeyDown(down);
                const up = keyEvent(child, path, key, code, "keyup");
                controller.onKeyUp(up);
                assert.equal(Boolean(down.defaultPrevented || up.defaultPrevented), false,
                    `${state}:${code}:${composed}`);
                assert.deepEqual(inputSnapshot(controller), before, `${state}:${code}:${composed}`);
            }
        }
        controller.destroy();
    }
});

test("in-shell action descendants reject flight keys before all input mutation", async () => {
    for (const [action, state] of [["lander-exit", "flying"], ["lander-restart", "failed"]]) {
        const { controller, elements } = await controllerAt({ ...createRun({ seed: 1 }), state });
        const actionElement = elements[action];
        const ancestors = action === "lander-restart" ?
            [elements["lander-outcome"], elements["lander-scene-stage"], elements["lander-scene-shell"]] :
            [elements["lander-controls-rail"], elements["lander-scene-shell"]];
        for (const target of [actionElement, ...actionElement.children]) {
            for (const [key, code] of [[" ", "Space"], ["Enter", "Enter"], ["ArrowUp", "ArrowUp"],
                ["ArrowLeft", "ArrowLeft"], ["ArrowRight", "ArrowRight"], ["h", "KeyH"], ["l", "KeyL"]]) {
                const before = inputSnapshot(controller);
                const event = keyEvent(target, [target, actionElement, ...ancestors], key, code);
                controller.onKeyDown(event);
                assert.equal(Boolean(event.defaultPrevented), false, `${action}:${code}`);
                assert.deepEqual(inputSnapshot(controller), before, `${action}:${code}`);
            }
        }
        controller.destroy();
    }
});

test("in-shell Escape and failed-state r precede interactive rejection", async () => {
    const flying = await controllerAt(createRun({ seed: 1 }));
    let exits = 0;
    flying.controller.exit = () => { exits += 1; };
    const escape = keyEvent(flying.elements["lander-exit"], [flying.elements["lander-exit"],
        flying.elements["lander-controls-rail"], flying.elements["lander-scene-shell"]], "Escape", "Escape");
    flying.controller.onKeyDown(escape);
    assert.equal(exits, 1);
    assert.equal(escape.defaultPrevented, true);
    flying.controller.destroy();

    const failed = await controllerAt({ ...createRun({ seed: 1 }), state: "failed" });
    let restarts = 0;
    failed.controller.restart = () => { restarts += 1; };
    const restart = keyEvent(failed.elements["lander-restart-hint"], [failed.elements["lander-restart-hint"],
        failed.elements["lander-restart"], failed.elements["lander-outcome"],
        failed.elements["lander-scene-stage"], failed.elements["lander-scene-shell"]], "r", "KeyR");
    failed.controller.onKeyDown(restart);
    assert.equal(restarts, 1);
    assert.equal(restart.defaultPrevented, true);
    failed.controller.destroy();
});

test("accepted in-shell flight edges consume keydown and matching keyup only", async () => {
    const { controller, elements } = await controllerAt(createRun({ seed: 1 }));
    const path = [elements["lander-scene-shell"]];
    const down = keyEvent(elements["lander-scene-shell"], path, " ", "Space");
    controller.onKeyDown(down);
    assert.equal(down.defaultPrevented, true);
    assert.deepEqual([...controller.heldKeys], ["Space"]);
    assert.equal(controller.clock.queue.length, 1);
    const up = keyEvent(elements["lander-scene-shell"], path, " ", "Space", "keyup");
    controller.onKeyUp(up);
    assert.equal(up.defaultPrevented, true);
    assert.deepEqual([...controller.heldKeys], []);
    assert.equal(controller.clock.queue.length, 2);
    assert.deepEqual(Object.keys(controller.clock.queue[0].physical.collectivePulse).sort(),
        ["active", "deadline", "token"]);
    controller.destroy();
});
