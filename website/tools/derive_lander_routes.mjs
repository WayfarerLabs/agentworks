#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";

const DERIVER_VERSION = "agw-lander-route-deriver/v1";
const RECIPE_VERSION = "agw-lander-route-recipes/v1";
const MAX_COMBINATIONS = 2_000_000;
const STEP_SECONDS = 1 / 120;
const FUEL_QUANTUM = 0.05;
const CONSTANTS = Object.freeze({
    COLLISION_MARGIN: 0.02,
    ENGINE_ACCELERATION: 8.4,
    FUEL_FLOW: 1,
    FUEL_QUANTUM,
    GRAVITY: 3,
    MAX_LANDING_ANGLE: 8,
    MAX_LANDING_ANGULAR_SPEED: 12,
    MAX_LANDING_DESCENT_SPEED: 2.2,
    MAX_LANDING_HORIZONTAL_SPEED: 1.4,
    MAX_PLAYABLE_Y: 56,
    STEP_SECONDS,
    TORQUE_ACCELERATION: 70,
});
const COMMANDS = Object.freeze([
    [0, 0],
    [0.72, 0.72],
    [0, 0.45],
    [0.45, 0],
    [0.72, 1],
    [1, 0.72],
    [0.45, 0.45],
    [1, 1],
]);
const RECIPES = new Map([
    [78, [[1, 90], [3, 200], [2, 200], [1, 20], [2, 274], [3, 274], [1, 44], [3, 189], [2, 189], [0, 362], [1, 183]]],
    [81, [[1, 90], [3, 202], [2, 202], [1, 20], [2, 262], [3, 262], [1, 79], [3, 165], [2, 165], [0, 597], [1, 187], [0, 120]]],
    [84, [[1, 90], [3, 189], [2, 189], [1, 35], [2, 272], [3, 272], [1, 20], [3, 195], [2, 195], [0, 471], [1, 157], [0, 30]]],
    [87, [[1, 90], [3, 205], [2, 205], [1, 20], [2, 290], [3, 290], [1, 20], [3, 204], [2, 204], [1, 100], [0, 451]]],
    [90, [[1, 90], [3, 211], [2, 211], [1, 23], [2, 271], [3, 271], [1, 92], [3, 171], [2, 171], [0, 362], [1, 127], [0, 94]]],
    [93, [[1, 90], [3, 209], [2, 209], [1, 20], [2, 284], [3, 284], [1, 34], [3, 190], [2, 190], [0, 64], [1, 91], [0, 93]]],
    [96, [[1, 90], [3, 219], [2, 219], [1, 20], [2, 276], [3, 276], [1, 102], [3, 168], [2, 168], [0, 138], [1, 58], [0, 357]]],
    [99, [[1, 90], [3, 207], [2, 207], [1, 42], [2, 273], [3, 273], [1, 93], [3, 180], [2, 180], [0, 264], [1, 178], [0, 202]]],
    [102, [[1, 90], [3, 209], [2, 209], [1, 38], [2, 279], [3, 279], [1, 70], [3, 183], [2, 183], [0, 91], [1, 71], [0, 261]]],
]);

function canonical(value) {
    if (Array.isArray(value)) {
        return value.map(canonical);
    }
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    }
    return value;
}

function canonicalBytes(value) {
    return JSON.stringify(canonical(value));
}

function digest(value) {
    return createHash("sha256").update(canonicalBytes(value), "utf8").digest("hex");
}

function normalizeDegrees(degrees) {
    return ((((degrees + 180) % 360) + 360) % 360) - 180;
}

function step(pose, engines, fuel = Infinity) {
    const requestedBurn = (engines[0] + engines[1]) * STEP_SECONDS;
    const scale = requestedBurn > fuel ? fuel / requestedBurn : 1;
    const left = engines[0] * scale;
    const right = engines[1] * scale;
    const radians = (pose.angle * Math.PI) / 180;
    const total = CONSTANTS.ENGINE_ACCELERATION * (left + right);
    const vx = pose.vx + total * Math.sin(radians) * STEP_SECONDS;
    const vy = pose.vy + (total * Math.cos(radians) - CONSTANTS.GRAVITY) * STEP_SECONDS;
    const angularVelocity = pose.angularVelocity + CONSTANTS.TORQUE_ACCELERATION * (left - right) * STEP_SECONDS;
    return {
        pose: {
            x: pose.x + vx * STEP_SECONDS,
            y: pose.y + vy * STEP_SECONDS,
            vx,
            vy,
            angle: normalizeDegrees(pose.angle + angularVelocity * STEP_SECONDS),
            angularVelocity,
        },
        burn: requestedBurn * scale,
        fuel: Math.max(0, fuel - requestedBurn * scale),
    };
}

function contactPose(previous, next, deckTop) {
    const fraction = (previous.y - deckTop) / (previous.y - next.y);
    const lerp = (left, right) => left + (right - left) * fraction;
    return {
        x: lerp(previous.x, next.x),
        y: deckTop,
        vx: lerp(previous.vx, next.vx),
        vy: lerp(previous.vy, next.vy),
        angle: normalizeDegrees(previous.angle + normalizeDegrees(next.angle - previous.angle) * fraction),
        angularVelocity: lerp(previous.angularVelocity, next.angularVelocity),
    };
}

function safe(pose, center) {
    return (
        pose.x - 1.6 >= center - 4.8 &&
        pose.x + 1.6 <= center + 4.8 &&
        pose.vy <= 0 &&
        Math.abs(pose.vx) <= CONSTANTS.MAX_LANDING_HORIZONTAL_SPEED &&
        Math.abs(pose.vy) <= CONSTANTS.MAX_LANDING_DESCENT_SPEED &&
        Math.abs(normalizeDegrees(pose.angle)) <= CONSTANTS.MAX_LANDING_ANGLE &&
        Math.abs(pose.angularVelocity) <= CONSTANTS.MAX_LANDING_ANGULAR_SPEED
    );
}

function replay(fullRuns, center, deckTop, allowance = Infinity) {
    let pose = { x: 0, y: 0, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    let fuel = allowance;
    let burn = 0;
    let stepIndex = 0;
    const runs = [];
    for (const [commandIndex, count] of fullRuns) {
        let used = 0;
        for (let index = 0; index < count; index += 1) {
            const previous = pose;
            const result = step(pose, COMMANDS[commandIndex], fuel);
            pose = result.pose;
            fuel = result.fuel;
            burn += result.burn;
            stepIndex += 1;
            used += 1;
            if (previous.y > deckTop && pose.y <= deckTop && pose.x >= center - 4.8 && pose.x <= center + 4.8) {
                runs.push([commandIndex, used]);
                const contact = contactPose(previous, pose, deckTop);
                return { burn, classification: safe(contact, center) ? "safe" : "unsafe", contactStep: stepIndex, pose: contact, runs };
            }
            if (Number.isFinite(allowance) && fuel === 0) {
                runs.push([commandIndex, used]);
                return { allowance, burn, exhaustionStep: stepIndex, pose, runs };
            }
            if (pose.y > CONSTANTS.MAX_PLAYABLE_Y) {
                throw new Error(`Recipe exceeded the playable ceiling at step ${stepIndex}`);
            }
        }
        runs.push([commandIndex, used]);
    }
    throw new Error("Recipe ended before target contact or fuel exhaustion");
}

function scheduleDigest(runs) {
    let value = 2166136261;
    for (const [command, count] of runs) {
        for (const byte of [command, count & 0xff, (count >>> 8) & 0xff]) {
            value = Math.imul(value ^ byte, 16777619) >>> 0;
        }
    }
    return value;
}

function* recipeCandidates(distance) {
    const reviewed = RECIPES.get(distance);
    if (!reviewed) return;
    const phases = reviewed.map(([commandIndex, count]) => ({ commandIndex, minimum: count, maximum: count }));
    const candidate = [];
    function* visit(index) {
        if (index === phases.length) {
            yield candidate.map((run) => [...run]);
            return;
        }
        const phase = phases[index];
        for (let count = phase.minimum; count <= phase.maximum; count += 1) {
            candidate.push([phase.commandIndex, count]);
            yield* visit(index + 1);
            candidate.pop();
        }
    }
    yield* visit(0);
}

function compareDerived(left, right) {
    if (left.success.burn !== right.success.burn) return left.success.burn - right.success.burn;
    const leftSteps = left.success.contactStep;
    const rightSteps = right.success.contactStep;
    if (leftSteps !== rightSteps) return leftSteps - rightSteps;
    return JSON.stringify(left.success.runs).localeCompare(JSON.stringify(right.success.runs));
}

function deriveTemplate(geometry) {
    let combinationsEvaluated = 0;
    const successes = [];
    for (const candidate of recipeCandidates(geometry.centerDelta)) {
        combinationsEvaluated += 1;
        if (combinationsEvaluated > MAX_COMBINATIONS) {
            throw new Error(`${geometry.templateId} exceeded the finite recipe budget`);
        }
        try {
            const success = replay(candidate, geometry.centerDelta, geometry.deckDelta);
            if (success.classification === "safe") successes.push({ candidate, success });
        } catch {
            // A candidate outside the safe recipe envelope is not a derived route.
        }
    }
    if (successes.length === 0) {
        throw new Error(`${geometry.templateId} has no safe route in ${RECIPE_VERSION}`);
    }
    successes.sort(compareDerived);
    const { success } = successes[0];
    const demonstratedMinimum = Math.ceil((success.burn - 1e-12) / FUEL_QUANTUM) * FUEL_QUANTUM;
    const smallerFailure = replay(success.runs, geometry.centerDelta, geometry.deckDelta, demonstratedMinimum - FUEL_QUANTUM);
    if (!("exhaustionStep" in smallerFailure)) {
        throw new Error(`${geometry.templateId} lower allowance did not exhaust before contact`);
    }
    return {
        ...geometry,
        demonstratedMinimum,
        runs: success.runs,
        scheduleDigest: scheduleDigest(success.runs),
        smallerFailure,
        success: {
            burn: success.burn,
            classification: success.classification,
            contactStep: success.contactStep,
            pose: success.pose,
        },
    };
}

function parseArguments(argumentsList) {
    const result = {};
    for (let index = 0; index < argumentsList.length; index += 2) {
        const flag = argumentsList[index];
        const value = argumentsList[index + 1];
        if (!["--geometry", "--output", "--verify"].includes(flag) || value === undefined) {
            throw new TypeError("Usage: derive_lander_routes.mjs --geometry PATH --output PATH [--verify PATH]");
        }
        result[flag.slice(2)] = value;
    }
    if (!result.geometry || !result.output) {
        throw new TypeError("Usage: derive_lander_routes.mjs --geometry PATH --output PATH [--verify PATH]");
    }
    return result;
}

async function main() {
    let options;
    try {
        options = parseArguments(process.argv.slice(2));
    } catch (error) {
        console.error(error.message);
        process.exitCode = 2;
        return;
    }
    try {
        const geometry = JSON.parse(await readFile(options.geometry, "utf8"));
        if (geometry.schema !== "agw-lander-route-geometry/v1" || geometry.templates.length !== 9) {
            throw new Error("Unsupported or incomplete geometry fixture");
        }
        const output = {
            deriverVersion: DERIVER_VERSION,
            geometryDigest: digest(geometry),
            physicsDigest: digest({ commands: COMMANDS, constants: CONSTANTS }),
            routes: geometry.templates.map(deriveTemplate),
            schema: "agw-lander-route-derived/v1",
        };
        output.outputDigest = digest(output);
        const serialized = `${canonicalBytes(output)}\n`;
        await writeFile(options.output, serialized, "utf8");
        if (options.verify) {
            const expected = await readFile(options.verify, "utf8");
            if (expected !== serialized) {
                throw new Error(`Derived routes differ from ${options.verify}`);
            }
        }
    } catch (error) {
        console.error(error.message);
        process.exitCode = 1;
    }
}

await main();
