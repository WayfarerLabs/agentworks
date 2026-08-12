import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    createRun,
    stepFlight,
} from "../static/lander-model.js";
import {
    siteScaffoldMembers,
    siteScaffoldPath,
    siteStructure,
} from "../static/lander-world.js";
import { controllerClasses, controllerFixture } from "./lander-test-dom.mjs";

const ROOT = new URL("../", import.meta.url);
const DERIVED_URL = new URL("fixtures/lander-route-derived-v5.json", import.meta.url);

function checkpointRun() {
    let model = createRun({ seed: 1, reducedMotion: true });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
    model = stepFlight(model, { left: 0, right: 0 });
    assert.equal(model.state, "launching");
    assert.ok(model.checkpoint);
    return model;
}

function retryKey(shell) {
    return { type: "keydown", target: shell, key: "r", code: "KeyR", repeat: false,
        ctrlKey: false, altKey: false, metaKey: false, shiftKey: false,
        composedPath: () => [shell], timeStamp: 20,
        preventDefault() { this.defaultPrevented = true; } };
}

function assertRestored(model, checkpoint) {
    for (const [field, expected] of Object.entries(checkpoint)) {
        assert.deepEqual(model[field], expected, field);
    }
    assert.equal(model.state, "launching");
    assert.equal(model.launchStarted, false);
    assert.equal(model.refuel, null);
    assert.equal(model.failureCause, null);
    assert.equal(model.crash, null);
    assert.deepEqual(model.commanded, { left: 0, right: 0, vectorAngle: 0 });
}

test("Phase 4L controls are exactly two ordered nonwrapping DOM lines with narrow rail reflow", async () => {
    const { LanderGameController } = await controllerClasses();
    const fixture = controllerFixture();
    const controller = new LanderGameController(fixture.root);
    assert.deepEqual(fixture.elements["lander-controls"].children.map((line) => line.className), [
        "lander-controls-line lander-controls-keyboard",
        "lander-controls-line lander-controls-touch",
    ]);
    for (const line of fixture.elements["lander-controls"].children) {
        assert.equal(line.children.length, 0);
        assert.ok(line.textContent.trim().length > 0);
    }
    const [template, css] = await Promise.all([
        readFile(new URL("templates/lander-game.html", ROOT), "utf8"),
        readFile(new URL("static/lander.css", ROOT), "utf8"),
    ]);
    const controls = template.match(/<p id="lander-controls">([\s\S]*?)<\/p>/)?.[1] ?? "";
    assert.equal((controls.match(/<span /g) ?? []).length, 2);
    assert.ok(controls.indexOf("lander-controls-keyboard") < controls.indexOf("lander-controls-touch"));
    assert.match(css, /\.lander-controls-line\s*\{[^}]*display:\s*block;[^}]*white-space:\s*nowrap;/s);
    assert.match(css, /@media \(max-width:\s*32rem\)[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\);/);
    controller.destroy();
});

test("all canonical lattice columns render continuously from deck underside to independent feet", async () => {
    const derived = JSON.parse(await readFile(DERIVED_URL, "utf8"));
    for (const witness of derived.worldWitnesses) {
        for (const canonical of witness.descriptor.sites) {
            const site = { seed: witness.descriptor.seed,
                platformLeft: canonical.platform.left, platformRight: canonical.platform.right,
                platformTop: canonical.platform.top, platformBottom: canonical.platform.bottom };
            const structure = siteStructure(site);
            const members = siteScaffoldMembers(site);
            assert.deepEqual(members, canonical.scaffoldMembers);
            assert.equal((siteScaffoldPath(site).match(/M/g) ?? []).length, members.length);
            assert.equal(structure.supportColumns.length, 3);
            structure.supportColumns.forEach((column, index) => {
                const fixtureColumn = canonical.supportColumns[index];
                assert.deepEqual(column, fixtureColumn);
                assert.equal(column.leftFoot, fixtureColumn.leftFoot);
                assert.equal(column.rightFoot, fixtureColumn.rightFoot);
                assert.equal(column.collider.top, canonical.platform.bottom + 0.1);
                assert.equal(column.collider.bottom,
                    Math.min(fixtureColumn.leftFoot, fixtureColumn.rightFoot) - 0.1);
            });
        }
    }
});

test("Retry click and r restore the exact checkpoint twice after complete input and capture teardown", async () => {
    const { LanderGameController } = await controllerClasses();
    globalThis.document.hidden = true;
    const fixture = controllerFixture();
    const controller = new LanderGameController(fixture.root);
    const powered = checkpointRun();
    const checkpoint = structuredClone(powered.checkpoint);
    const awarded = {
        completedSites: powered.completedSites,
        refuelRatio: powered.refuelRatio,
        cans: powered.retainedSites.filter(({ canCollected }) => canCollected).map(({ id }) => id),
        powered: powered.retainedSites.filter((site) => site.powered).map(({ id }) => id),
    };

    const dirtyFailure = () => {
        controller.model = { ...powered, state: "failed", fuel: 0,
            pose: { ...powered.pose, x: powered.pose.x + 7 }, failureCause: "terrain" };
        controller.heldKeys.add("Space");
        controller.pointer = { id: 7, x: 1, y: 1, currentX: 3, started: 0, token: 9 };
        controller.pointerInput = { left: 0.72, right: 0.72 };
        controller.collectivePulse = { active: true, token: 9, deadline: 1000 };
        fixture.elements["lander-scene-stage"].setPointerCapture(7);
        controller.render();
    };
    const order = [];
    const render = controller.render.bind(controller);
    controller.render = () => { order.push("render"); render(); };
    fixture.elements["lander-scene-shell"].focus = (options) => {
        order.push(["focus", options]); globalThis.document.activeElement = fixture.elements["lander-scene-shell"];
    };

    dirtyFailure();
    order.length = 0;
    fixture.elements["lander-restart"].dispatchEvent({ type: "click", timeStamp: 10 });
    assert.deepEqual(order, ["render", ["focus", { preventScroll: true }]]);
    assertRestored(controller.model, checkpoint);
    assert.deepEqual([...controller.heldKeys], []);
    assert.equal(controller.pointer, null);
    assert.equal(controller.collectivePulse.active, false);
    assert.equal(fixture.elements["lander-scene-stage"].hasPointerCapture(7), false);
    assert.deepEqual(controller.clock.queue, []);

    dirtyFailure();
    order.length = 0;
    const event = retryKey(fixture.elements["lander-scene-shell"]);
    controller.onKeyDown(event);
    assert.equal(event.defaultPrevented, true);
    assert.deepEqual(order, ["render", ["focus", { preventScroll: true }]]);
    assertRestored(controller.model, checkpoint);
    assert.deepEqual({
        completedSites: controller.model.completedSites,
        refuelRatio: controller.model.refuelRatio,
        cans: controller.model.retainedSites.filter(({ canCollected }) => canCollected).map(({ id }) => id),
        powered: controller.model.retainedSites.filter((site) => site.powered).map(({ id }) => id),
    }, awarded);
    const retry = fixture.elements["lander-restart"];
    assert.deepEqual(retry.children.map(({ className }) => className),
        ["lander-action-label", "lander-key-hint"]);
    assert.ok(fixture.elements["lander-restart-label"].textContent.trim().length > 0);
    assert.ok(fixture.elements["lander-restart-hint"].textContent.trim().length > 0);
    assert.equal(fixture.elements["lander-restart-hint"].attributes.get("aria-hidden"), "true");
    assert.equal(retry.attributes.get("aria-keyshortcuts"), "r");
    controller.destroy();
});
