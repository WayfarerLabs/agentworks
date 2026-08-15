#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, rename, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { classifyRouteSweep, routeHull } from "./lander_route_collision.mjs";
import { canonical, canonicalBytes, fixtureDigest as digest, fixturePose } from "./lander_route_fixture.mjs";
import {
    CANDIDATE_ORDERS,
    PROFILE_ORDER,
    REVIEW_SEEDS,
    assignmentWorld,
    assignmentsFor,
    envelopeHeight,
    envelopeWorld,
    groupAssignments,
    openingWorld,
    validateNumberBound,
    validateGeometry,
    validatePredecessor,
    worldWitness,
} from "./lander_route_geometry.mjs";

const DERIVER_VERSION = "agw-lander-route-deriver/v10";
const SYNTHESIZER_VERSION = "agw-lander-corridor-synthesizer/v2";
const DERIVED_SCHEMA = "agw-lander-route-derived/v9";
const PREDECESSOR_SCHEMA = "agw-lander-route-geometry/v8";
const PREDECESSOR_SHA = "257da30dbbaa9af6910ad2beb344162f0321760169cafb29a8e80164f4507248";
const BOOTSTRAP_SCHEMA = "agw-lander-route-derived/v8";
const BOOTSTRAP_SHA = "ebaa368a38b262bb7839b621fd9785a379347e57e2217a6a2dc66466f9fa5c88";
const BOOTSTRAP_OUTPUT = "562de8434513923e150a5db0198afeed168b992f877372e171de4a00fb1ed2cd";
const BOOTSTRAP_PROOF = "f49f06e1353684df36604e6f476380a9a58d5d97ddb478b24d35c69b4dd4e93f";
const COLLISION_VERSION = "agw-lander-swept-collision/v2";
const POSE_DECIMALS = 9;
const STEP_SECONDS = 1 / 120;
const FUEL_QUANTUM = 0.05;
const APPROVED_BASE = 13.4;
const MAX_CONTACT_STEP = 4332;
const COMMANDS = Object.freeze([
    [0, 0],
    [0.72, 0.72],
    [0, 0.375],
    [0.375, 0],
    [0.2125, 0.5875],
    [0.5875, 0.2125],
    [0, 0],
    [0.72, 0.72],
]);
const SEARCH_COMMANDS = Object.freeze([0, 1, 2, 3, 4, 5]);
const CONSTANTS = Object.freeze({
    ANGULAR_ASSIST_DIFFERENTIAL: 0.12,
    ANGULAR_ASSIST_FULL_SPEED: 15,
    COLLISION_ANGLE_KNOT_DEGREES: 1,
    COLLISION_MARGIN: 0.02,
    ENGINE_ACCELERATION: 9,
    FUEL_FLOW: 1,
    FUEL_QUANTUM,
    GRAVITY: 3,
    MAX_LANDING_ANGLE: 18,
    MAX_LANDING_ANGULAR_SPEED: 26,
    MAX_LANDING_DESCENT_SPEED: 3.6,
    MAX_LANDING_HORIZONTAL_SPEED: 2.2,
    MAX_THRUST_VECTOR: 30,
    STEP_SECONDS,
    TERMINUS_WIDTH: 0.2,
    TORQUE_ACCELERATION: 80,
    TURN_DIFFERENTIAL: 0.375,
    TURNING_TOTAL: 0.8,
    WORLD_MAX_X: 393216,
    WORLD_MIN_X: -393216,
});
const PREFIX = Object.freeze([
    [1, 90],
    [0, 218],
    [1, 21],
    [0, 94],
    [2, 12],
    [5, 6],
    [3, 41],
    [0, 119],
    [2, 23],
    [0, 180],
    [1, 129],
    [0, 120],
    [2, 34],
]);

const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));
const normalizeDegrees = (value) => ((((value + 180) % 360) + 360) % 360) - 180;
export const quantumCeil = (value) => Math.ceil(value / FUEL_QUANTUM) * FUEL_QUANTUM;

function integrate(pose, engines, fuel = Infinity) {
    const totalRequest = engines[0] + engines[1];
    const manualSteer = clamp((engines[0] - engines[1]) / CONSTANTS.TURN_DIFFERENTIAL, -1, 1);
    let left = engines[0];
    let right = engines[1];
    if (manualSteer === 0 && totalRequest > 0) {
        const raw =
            CONSTANTS.ANGULAR_ASSIST_DIFFERENTIAL *
            clamp(-pose.angularVelocity / CONSTANTS.ANGULAR_ASSIST_FULL_SPEED, -1, 1);
        const limit = Math.min(totalRequest, 2 - totalRequest);
        const assist = clamp(raw, -limit, limit);
        left = (totalRequest + assist) / 2;
        right = (totalRequest - assist) / 2;
    }
    const requestedBurn = (left + right) * STEP_SECONDS;
    const exhausts = requestedBurn >= fuel;
    const scale = exhausts && requestedBurn > 0 ? fuel / requestedBurn : 1;
    left *= scale;
    right *= scale;
    const radians = ((pose.angle + CONSTANTS.MAX_THRUST_VECTOR * manualSteer) * Math.PI) / 180;
    const total = CONSTANTS.ENGINE_ACCELERATION * (left + right);
    const vx = pose.vx + total * Math.sin(radians) * STEP_SECONDS;
    const vy = pose.vy + (total * Math.cos(radians) - CONSTANTS.GRAVITY) * STEP_SECONDS;
    const angularVelocity = pose.angularVelocity + CONSTANTS.TORQUE_ACCELERATION * (left - right) * STEP_SECONDS;
    const angularTravel = angularVelocity * STEP_SECONDS;
    return {
        pose: {
            x: pose.x + vx * STEP_SECONDS,
            y: pose.y + vy * STEP_SECONDS,
            vx,
            vy,
            angle: normalizeDegrees(pose.angle + angularTravel),
            angularVelocity,
        },
        angularTravel,
        fuel: exhausts ? 0 : Math.max(0, fuel - requestedBurn),
        burn: requestedBurn * scale,
    };
}

const rectangle = (left, right, bottom, top) => [
    { x: left, y: bottom },
    { x: right, y: bottom },
    { x: right, y: top },
    { x: left, y: top },
];

function collisionCandidates(world, ignoreOriginTop) {
    const candidates = [];
    const target = world.sites[1] ?? world.sites[0];
    for (const site of world.sites) {
        const platformLeft = site.center - 4.8;
        const platformRight = site.center + 4.8;
        const bottom = site.platformTop - 0.35;
        const buildingLeft = platformRight + 2;
        const buildingRight = buildingLeft + 7;
        const roof = site.platformTop + 7.2;
        const origin = site.index === world.originSiteId;
        if (site === target || (origin && ignoreOriginTop)) {
            candidates.push(
                {
                    cause: "platform",
                    priority: 1,
                    segment: [
                        { x: platformLeft, y: site.platformTop },
                        { x: platformLeft, y: bottom },
                    ],
                },
                {
                    cause: "platform",
                    priority: 1,
                    segment: [
                        { x: platformLeft, y: bottom },
                        { x: platformRight, y: bottom },
                    ],
                },
                {
                    cause: "platform",
                    priority: 1,
                    segment: [
                        { x: platformRight, y: bottom },
                        { x: platformRight, y: site.platformTop },
                    ],
                },
            );
        } else {
            candidates.push({
                cause: "platform",
                priority: 1,
                polygon: rectangle(platformLeft, platformRight, bottom, site.platformTop),
            });
        }
        const supports = [
            [0, 1],
            [8.8, 9.8],
            [17.6, 18.6],
        ].map(([left, right], index) => ({
            left: platformLeft + left - 0.1,
            right: platformLeft + right + 0.1,
            bottom: Math.min(site.supportFeet[index * 2], site.supportFeet[index * 2 + 1]) - 0.1,
            top: bottom + 0.1,
        }));
        const boxes = [
            [
                "truss",
                2,
                { left: platformLeft - 0.1, right: buildingRight + 0.1, bottom: bottom - 0.85, top: bottom + 0.1 },
            ],
            ...supports.map((box) => ["column", 2, box]),
            ["noc", 0, { left: buildingLeft, right: buildingRight, bottom, top: roof }],
            ["mast", 0, { left: buildingLeft + 3.25, right: buildingLeft + 3.75, bottom: roof, top: roof + 3.2 }],
        ];
        for (const [cause, priority, box] of boxes) {
            candidates.push({ cause, priority, polygon: rectangle(box.left, box.right, box.bottom, box.top) });
        }
    }
    for (let index = 1; index < world.vertices.length; index += 1) {
        candidates.push({
            cause: "terrain",
            priority: 4,
            segment: [
                { x: world.vertices[index - 1][0], y: world.vertices[index - 1][1] },
                { x: world.vertices[index][0], y: world.vertices[index][1] },
            ],
            solidBelow: true,
        });
    }
    candidates.push({
        cause: "target",
        priority: 5,
        target: true,
        segment: [
            { x: target.center - 4.8, y: target.platformTop },
            { x: target.center + 4.8, y: target.platformTop },
        ],
    });
    return {
        candidates,
        target: { ...target, platformLeft: target.center - 4.8, platformRight: target.center + 4.8 },
    };
}

function mergeRun(runs, command, count) {
    if (count <= 0) return;
    if (runs.at(-1)?.[0] === command) runs.at(-1)[1] += count;
    else runs.push([command, count]);
}

function replay(runs, world, initialPose, fuel, maxSteps = 4320) {
    let pose = { ...initialPose };
    let remaining = fuel;
    let burn = 0;
    let step = 0;
    let launchCleared = world.originSiteId === null || initialPose.y > world.sites[0].platformTop + 0.05;
    let maxHullTop = Math.max(...routeHull(pose).map((point) => point.y));
    const used = [];
    const ordinary = collisionCandidates(world, false);
    const launch = collisionCandidates(world, true);
    for (const [command, count] of runs) {
        let runCount = 0;
        for (let index = 0; index < count && step < maxSteps; index += 1) {
            const previous = pose;
            const result = integrate(pose, COMMANDS[command], remaining);
            pose = result.pose;
            remaining = result.fuel;
            burn += result.burn;
            step += 1;
            runCount += 1;
            maxHullTop = Math.max(maxHullTop, ...routeHull(pose).map((point) => point.y));
            const collision = launchCleared ? ordinary : launch;
            const contact = classifyRouteSweep(
                previous,
                pose,
                result.angularTravel,
                collision.candidates,
                collision.target,
            );
            if (contact) {
                mergeRun(used, command, runCount);
                return {
                    classification: contact.classification,
                    cause: contact.cause,
                    contactStep: step,
                    pose: contact.pose,
                    burn,
                    reserve: remaining,
                    maxHullTop,
                    runs: used,
                };
            }
            launchCleared ||= routeHull(pose)
                .slice(0, 2)
                .every((foot) => foot.y > world.sites[0].platformTop + 0.05);
        }
        mergeRun(used, command, runCount);
    }
    return { classification: "incomplete", contactStep: step, pose, burn, reserve: remaining, maxHullTop, runs: used };
}

function advanceMacro(pose, fuel, command) {
    let current = pose;
    let remaining = fuel;
    let maxTop = -Infinity;
    let minY = Infinity;
    for (let index = 0; index < 12; index += 1) {
        const result = integrate(current, COMMANDS[command], remaining);
        current = result.pose;
        remaining = result.fuel;
        maxTop = Math.max(maxTop, current.y + 6.5);
        minY = Math.min(minY, current.y);
    }
    return { pose: current, fuel: remaining, maxTop, minY };
}

function runPrefix() {
    let pose = { x: 0, y: 0, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    let fuel = 30;
    for (const [command, count] of PREFIX) {
        for (let index = 0; index < count; index += 1) {
            const result = integrate(pose, COMMANDS[command], fuel);
            pose = result.pose;
            fuel = result.fuel;
        }
    }
    return { pose, fuel };
}

function quantizedKey(pose, shallowReleaseLayer = -1) {
    return `${[
        Math.round(pose.x * 2),
        Math.round(pose.y * 2),
        Math.round(pose.vx * 2),
        Math.round(pose.vy * 2),
        Math.round(pose.angle / 5),
        Math.round(pose.angularVelocity / 5),
    ].join(":")}:${shallowReleaseLayer}`;
}

function compareIntegers(left, right) {
    for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
        if (left[index] !== right[index]) return left[index] - right[index];
    }
    return left.length - right.length;
}

function compareStates(left, right, runsForPath) {
    return (
        left.cost - right.cost ||
        right.fuel - left.fuel ||
        compareIntegers(runsForPath(left.path).flat(), runsForPath(right.path).flat()) ||
        (left.shallowReleaseLayer ?? -1) - (right.shallowReleaseLayer ?? -1) ||
        left.pose.x - right.pose.x ||
        left.pose.y - right.pose.y ||
        left.pose.vx - right.pose.vx ||
        left.pose.vy - right.pose.vy ||
        left.pose.angle - right.pose.angle ||
        left.pose.angularVelocity - right.pose.angularVelocity
    );
}

function routeRuns(path) {
    const runs = PREFIX.map((run) => [...run]);
    for (const digit of path) mergeRun(runs, Number(digit), 12);
    return runs;
}

function desiredRoute(layer, delta, distance, prefixX, shallowReleaseLayer) {
    const cruise = Math.max(11.5, delta + 11.5);
    let x;
    let y;
    let vx = 4.2;
    let vy = 0;
    if (layer <= 190) {
        x = prefixX + ((distance - 6 - prefixX) * layer) / 190;
        y = 11.5 + (cruise - 11.5) * Math.min(1, layer / 120);
    } else if (layer <= 210) {
        const progress = (layer - 190) / 20;
        x = distance - 6 + 4 * progress;
        vx = 4.2 - 3.2 * progress;
        y = cruise;
    } else {
        const progress = Math.min(1, (layer - 210) / 50);
        x = distance - 2;
        vx = 1 - progress;
        y = cruise - (cruise - (delta + 0.7)) * progress;
        vy = -1.5;
    }
    if (delta < -10) {
        const progress = clamp((layer - 120) / 140, 0, 1);
        y = cruise + (delta + 0.7 - cruise) * progress;
        if (progress < 1) vy = (delta + 0.7 - cruise) / 14;
    } else if (delta < 0) {
        if (shallowReleaseLayer >= 0) {
            const duration = Math.max(1, Math.min(110, 269 - shallowReleaseLayer));
            const progress = clamp((layer - shallowReleaseLayer) / duration, 0, 1);
            y = cruise + (delta + 0.7 - cruise) * progress;
            if (progress < 1) vy = (delta + 0.7 - cruise) / (duration / 10);
        } else {
            y = cruise;
            vy = 0;
        }
    }
    return { x, y, vx, vy };
}

function routeCost(pose, layer, fuel, delta, distance, prefixX, shallowReleaseLayer) {
    const target = desiredRoute(layer, delta, distance, prefixX, shallowReleaseLayer);
    const terminalBias = Math.max(0, layer - 210) * 0.08;
    return (
        1.8 * Math.abs(pose.x - target.x) +
        2.4 * Math.abs(pose.y - target.y) +
        (2 + terminalBias) * Math.abs(pose.vx - target.vx) +
        (2 + terminalBias) * Math.abs(pose.vy - target.vy) +
        0.045 * Math.abs(pose.angle) +
        0.035 * Math.abs(pose.angularVelocity) +
        0.5 * (30 - fuel)
    );
}

function routeTerminal(pose, delta, distance) {
    return (
        pose.x >= distance - 3.2 &&
        pose.x <= distance + 3.2 &&
        pose.y >= delta &&
        pose.y <= delta + 0.3 &&
        Math.abs(pose.vx) <= 2.2 &&
        pose.vy <= 0 &&
        Math.abs(pose.vy) <= 3.6 &&
        Math.abs(pose.angle) <= 18 &&
        Math.abs(pose.angularVelocity) <= 26
    );
}

function synthesizeRoute(geometry, originDeck, targetDeck, distance) {
    const delta = targetDeck - originDeck;
    const origin = runPrefix();
    const world = envelopeWorld(geometry, originDeck, targetDeck, distance);
    const compare = (left, right) => compareStates(left, right, routeRuns);
    let beam = [
        {
            pose: origin.pose,
            fuel: origin.fuel,
            path: "",
            cost: 0,
            maxHullTop: originDeck + 6.5,
            shallowReleaseLayer: -1,
        },
    ];
    let macroExpansions = 0;
    let terminalReplays = 0;
    for (let layer = 1; layer <= 269; layer += 1) {
        const byKey = new Map();
        for (const state of beam) {
            for (const command of SEARCH_COMMANDS) {
                macroExpansions += 1;
                const advanced = advanceMacro(state.pose, state.fuel, command);
                const pose = advanced.pose;
                if (
                    !Object.values(pose).every(Number.isFinite) ||
                    advanced.fuel < 0 ||
                    advanced.minY < Math.min(-0.5, delta - 0.5) ||
                    pose.x < 5 ||
                    pose.x > distance + 11 ||
                    pose.y < envelopeHeight(originDeck, targetDeck, distance, pose.x) - originDeck + 1.65 ||
                    (pose.x > distance - 12 && pose.x < distance - 4.8 && pose.y < delta + 11)
                )
                    continue;
                let shallowReleaseLayer = state.shallowReleaseLayer;
                if (delta >= -10 && delta < 0 && shallowReleaseLayer < 0 && pose.x >= distance - 4.8)
                    shallowReleaseLayer = layer;
                const candidate = {
                    pose,
                    fuel: advanced.fuel,
                    path: state.path + command,
                    cost: routeCost(pose, layer, advanced.fuel, delta, distance, origin.pose.x, shallowReleaseLayer),
                    maxHullTop: Math.max(state.maxHullTop, originDeck + advanced.maxTop),
                    shallowReleaseLayer,
                };
                const key = quantizedKey(pose, shallowReleaseLayer);
                const existing = byKey.get(key);
                if (!existing || compare(candidate, existing) < 0) byKey.set(key, candidate);
            }
        }
        beam = [...byKey.values()].sort(compare).slice(0, 6000);
        for (const state of beam) {
            if (!routeTerminal(state.pose, delta, distance)) continue;
            terminalReplays += 1;
            const runs = routeRuns(state.path);
            mergeRun(runs, 0, MAX_CONTACT_STEP - runs.reduce((sum, [, count]) => sum + count, 0));
            const replayed = replay(
                runs,
                world,
                { x: 0, y: originDeck, vx: 0, vy: 0, angle: 0, angularVelocity: 0 },
                30,
                MAX_CONTACT_STEP,
            );
            if (replayed.classification === "safe") {
                return {
                    runs: replayed.runs,
                    replayed,
                    world,
                    search: { selectedLayer: layer, macroExpansions, terminalReplays },
                };
            }
        }
    }
    throw new Error(
        `No exact route for ${distance}/${originDeck}/${targetDeck}; beam=${beam.length} expansions=${macroExpansions} terminalReplays=${terminalReplays}`,
    );
}

function scheduleDigest(runs) {
    let value = 2166136261;
    for (const [command, count] of runs) {
        for (const byte of [command, count & 255, (count >>> 8) & 255]) {
            value = Math.imul(value ^ byte, 16777619) >>> 0;
        }
    }
    return value;
}

function compareCatalog(left, right) {
    return (
        left.scheduleDigest - right.scheduleDigest ||
        left.sourceOrigin - right.sourceOrigin ||
        left.sourceTarget - right.sourceTarget
    );
}

function insertCatalog(catalog, candidate) {
    const key = canonicalBytes(candidate.runs);
    const existingIndex = catalog.findIndex((entry) => canonicalBytes(entry.runs) === key);
    if (existingIndex >= 0) {
        if (compareCatalog(candidate, catalog[existingIndex]) < 0) catalog[existingIndex] = candidate;
    } else {
        catalog.push(candidate);
    }
    catalog.sort(compareCatalog);
}

function selectCatalog(catalog, geometry, first) {
    const delta = first.targetDeck - first.originDeck;
    const world = envelopeWorld(geometry, first.originDeck, first.targetDeck, first.distance);
    let terminalReplays = 0;
    for (const candidate of catalog) {
        if (candidate.relativeY < delta || candidate.relativeY > delta + 0.3) continue;
        terminalReplays += 1;
        const replayed = replay(
            candidate.runs,
            world,
            { x: 0, y: first.originDeck, vx: 0, vy: 0, angle: 0, angularVelocity: 0 },
            30,
            MAX_CONTACT_STEP,
        );
        if (replayed.classification === "safe") {
            return {
                kind: candidate.bootstrap ? "bootstrap" : "reuse",
                runs: replayed.runs,
                replayed,
                world,
                search: { selectedLayer: candidate.selectedLayer, macroExpansions: 0, terminalReplays },
            };
        }
    }
    return null;
}

function recordFor(geometry, group, catalogs, selectionCensus, class96Census) {
    const first = group.assignments[0];
    const catalog = catalogs.get(first.distance) ?? [];
    catalogs.set(first.distance, catalog);
    let result = selectCatalog(catalog, geometry, first);
    if (!result) {
        result = {
            ...synthesizeRoute(geometry, first.originDeck, first.targetDeck, first.distance),
            kind: "synthesis",
        };
        insertCatalog(catalog, {
            bootstrap: false,
            scheduleDigest: scheduleDigest(result.runs),
            sourceOrigin: first.originMillimeters,
            sourceTarget: first.targetMillimeters,
            runs: result.runs.map((run) => [...run]),
            relativeY: result.replayed.pose.y - first.originDeck,
            selectedLayer: result.search.selectedLayer,
        });
    }
    selectionCensus[result.kind] += 1;
    if (first.distance === 96) class96Census[result.kind] += 1;
    for (const assignment of group.assignments) {
        const concrete = assignmentWorld(geometry, assignment);
        const replayed = replay(
            result.runs,
            concrete,
            {
                x: assignment.originCenter,
                y: assignment.originDeck,
                vx: 0,
                vy: 0,
                angle: 0,
                angularVelocity: 0,
            },
            30,
            MAX_CONTACT_STEP,
        );
        if (replayed.classification !== "safe" || replayed.contactStep !== result.replayed.contactStep) {
            throw new Error(`Concrete replay mismatch ${assignment.assignmentId}`);
        }
    }
    const climbSurcharge = Math.max(0, first.deckDelta) / 3;
    const controllerBurn = result.replayed.burn;
    return {
        pairKey: first.pairKey,
        envelope: result.world.relativeEnvelope,
        assignmentIds: group.assignmentIds,
        assignmentMembershipDigest: digest(group.assignmentIds),
        runs: result.runs,
        scheduleDigest: scheduleDigest(result.runs),
        search: result.search,
        success: {
            classification: "safe",
            contactStep: result.replayed.contactStep,
            pose: fixturePose(
                {
                    ...result.replayed.pose,
                    x: result.replayed.pose.x,
                    y: result.replayed.pose.y - first.originDeck,
                },
                POSE_DECIMALS,
            ),
        },
        controllerBurn,
        baseBurn: controllerBurn - climbSurcharge,
        climbSurcharge,
        allowance: quantumCeil(APPROVED_BASE + climbSurcharge),
        maxHullTop: result.replayed.maxHullTop,
    };
}

function openingRuns(path) {
    const runs = [];
    for (const digit of path) mergeRun(runs, Number(digit), 12);
    return runs;
}

function openingTarget(layer, site) {
    const cruise = Math.max(32, site.platformTop + 11.5);
    if (layer <= 120) {
        return {
            x: 30 + ((site.center - 36) * layer) / 120,
            y: 32 + (cruise - 32) * Math.min(1, layer / 60),
            vx: Math.max(0.8, (site.center - 36) / 12),
            vy: 0,
        };
    }
    if (layer <= 150) {
        const progress = (layer - 120) / 30;
        return { x: site.center - 6 + 4 * progress, y: cruise, vx: 4.2 - 3.2 * progress, vy: 0 };
    }
    const progress = Math.min(1, (layer - 150) / 80);
    return {
        x: site.center - 2,
        y: cruise - (cruise - site.platformTop - 0.7) * progress,
        vx: 1 - progress,
        vy: -1.5,
    };
}

function synthesizeOpening(geometry, profile, candidateOrder) {
    const world = openingWorld(geometry, profile, candidateOrder);
    const site = world.sites[0];
    const initial = { x: 30, y: 32, vx: 0.8, vy: -0.4, angle: 0, angularVelocity: 0 };
    const compare = (left, right) => compareStates(left, right, openingRuns);
    let beam = [{ pose: initial, fuel: 15, path: "", cost: 0 }];
    for (let layer = 1; layer <= 269; layer += 1) {
        const byKey = new Map();
        for (const state of beam) {
            for (const command of SEARCH_COMMANDS) {
                const advanced = advanceMacro(state.pose, state.fuel, command);
                const pose = advanced.pose;
                if (
                    !Object.values(pose).every(Number.isFinite) ||
                    advanced.fuel < 0 ||
                    advanced.minY < -2.5 ||
                    pose.x < 5 ||
                    pose.x > site.center + 11 ||
                    pose.y < -3.3
                )
                    continue;
                const target = openingTarget(layer, site);
                const terminalBias = Math.max(0, layer - 190) * 0.08;
                const candidate = {
                    pose,
                    fuel: advanced.fuel,
                    path: state.path + command,
                    cost:
                        1.8 * Math.abs(pose.x - target.x) +
                        2.4 * Math.abs(pose.y - target.y) +
                        (2 + terminalBias) * Math.abs(pose.vx - target.vx) +
                        (2 + terminalBias) * Math.abs(pose.vy - target.vy) +
                        0.045 * Math.abs(pose.angle) +
                        0.035 * Math.abs(pose.angularVelocity) +
                        0.5 * (15 - advanced.fuel),
                };
                const key = quantizedKey(pose);
                const existing = byKey.get(key);
                if (!existing || compare(candidate, existing) < 0) byKey.set(key, candidate);
            }
        }
        beam = [...byKey.values()].sort(compare).slice(0, 6000);
        for (const state of beam) {
            const pose = state.pose;
            if (
                pose.x < site.center - 3.2 ||
                pose.x > site.center + 3.2 ||
                pose.y < site.platformTop ||
                pose.y > site.platformTop + 0.3 ||
                Math.abs(pose.vx) > 2.2 ||
                pose.vy > 0 ||
                Math.abs(pose.vy) > 3.6 ||
                Math.abs(pose.angle) > 18 ||
                Math.abs(pose.angularVelocity) > 26
            )
                continue;
            const runs = openingRuns(state.path);
            mergeRun(runs, 0, 4320 - runs.reduce((sum, [, count]) => sum + count, 0));
            const result = replay(runs, world, initial, 15);
            if (result.classification === "safe") {
                return {
                    profile: `S${profile}`,
                    candidateOrder,
                    center: site.center,
                    candidateOrdinal: site.candidateOrdinal,
                    offsetIndex: site.offsetIndex,
                    deck: site.platformTop,
                    runs: result.runs,
                    contactStep: result.contactStep,
                    pose: fixturePose(result.pose, POSE_DECIMALS),
                    burn: result.burn,
                    reserve: result.reserve,
                    classification: "safe",
                };
            }
        }
    }
    throw new Error(`Opening synthesis failed S${profile}/${candidateOrder}`);
}

function parsePair(pairKey) {
    const match = /^d:(-?\d+):(-?\d+)$/.exec(pairKey);
    if (!match) throw new Error(`Malformed bootstrap pair ${pairKey}`);
    return [Number(match[1]), Number(match[2])];
}

async function readPinnedJson(path, expectedSha) {
    const bytes = await readFile(path);
    const sha = createHash("sha256").update(bytes).digest("hex");
    if (sha !== expectedSha) throw new Error(`Pinned input digest mismatch ${path}`);
    return { sha, value: JSON.parse(bytes.toString("utf8")) };
}

async function readInputs(options) {
    const predecessor = await readPinnedJson(options.predecessorGeometry, PREDECESSOR_SHA);
    if (predecessor.value.schema !== PREDECESSOR_SCHEMA) throw new Error("Predecessor schema mismatch");
    const bootstrap = await readPinnedJson(options.bootstrap, BOOTSTRAP_SHA);
    if (
        bootstrap.value.schema !== BOOTSTRAP_SCHEMA ||
        bootstrap.value.outputDigest !== BOOTSTRAP_OUTPUT ||
        bootstrap.value.proofDigest !== BOOTSTRAP_PROOF ||
        bootstrap.value.records?.length !== 243
    )
        throw new Error("Bootstrap fixture authority mismatch");
    const distinct = new Map();
    for (const record of bootstrap.value.records) {
        const [sourceOrigin, sourceTarget] = parsePair(record.pairKey);
        const runs = record.runs;
        const steps = Array.isArray(runs) ? runs.reduce((sum, run) => sum + (run?.[1] ?? NaN), 0) : NaN;
        if (
            !Array.isArray(runs) ||
            !runs.length ||
            !Number.isInteger(record.search?.selectedLayer) ||
            record.search.selectedLayer < 1 ||
            !runs.every(
                (run, index) =>
                    Array.isArray(run) &&
                    run.length === 2 &&
                    SEARCH_COMMANDS.includes(run[0]) &&
                    Number.isInteger(run[1]) &&
                    run[1] > 0 &&
                    (index === 0 || runs[index - 1][0] !== run[0]),
            ) ||
            steps > 4320 ||
            scheduleDigest(runs) !== record.scheduleDigest ||
            !Number.isFinite(record.success?.pose?.y)
        )
            throw new Error(`Malformed bootstrap schedule ${record.pairKey}`);
        const candidate = {
            bootstrap: true,
            scheduleDigest: record.scheduleDigest >>> 0,
            sourceOrigin,
            sourceTarget,
            runs: runs.map((run) => [...run]),
            relativeY: record.success.pose.y,
            selectedLayer: record.search.selectedLayer,
        };
        const key = canonicalBytes(candidate.runs);
        const existing = distinct.get(key);
        if (!existing || compareCatalog(candidate, existing) < 0) distinct.set(key, candidate);
    }
    const schedules = [...distinct.values()].sort(compareCatalog);
    if (schedules.length !== 225) throw new Error(`Expected 225 bootstrap schedules, got ${schedules.length}`);
    return { predecessor, bootstrap, schedules };
}

function validateFeasibility(assignments, records, selectionCensus, class96Census) {
    const maximumBase = records.reduce((left, right) => (right.baseBurn > left.baseBurn ? right : left));
    const maximumContact = Math.max(...records.map((record) => record.success.contactStep));
    const maximumBurn = Math.max(...records.map((record) => record.controllerBurn));
    const maximumTop = Math.max(...records.map((record) => record.maxHullTop));
    const branches = records.reduce(
        (result, record) => {
            const assignment = assignments.find((candidate) => candidate.pairKey === record.pairKey);
            result[assignment.deckDelta < -10 ? "deep" : assignment.deckDelta < 0 ? "shallow" : "flatRise"] += 1;
            return result;
        },
        { deep: 0, shallow: 0, flatRise: 0 },
    );
    const class96 = records.filter((record) => record.pairKey.startsWith("r:96000:"));
    const assignments96 = assignments.filter((assignment) => assignment.distance === 96);
    const synthesized = records.filter((record) => record.search.macroExpansions > 0);
    if (
        canonicalBytes(selectionCensus) !== canonicalBytes({ bootstrap: 70, reuse: 41, synthesis: 201 }) ||
        canonicalBytes(class96Census) !== canonicalBytes({ bootstrap: 70, reuse: 35, synthesis: 72 }) ||
        canonicalBytes(branches) !== canonicalBytes({ deep: 8, shallow: 121, flatRise: 183 }) ||
        maximumBase.pairKey !== "r:136000:18340:12828" ||
        maximumBase.baseBurn !== 13.368250000000229 ||
        quantumCeil(maximumBase.baseBurn) !== APPROVED_BASE ||
        maximumContact !== 4332 ||
        maximumBurn !== 13.368250000000229 ||
        maximumTop !== 44.50313531329338 ||
        Math.max(...synthesized.map((record) => record.search.selectedLayer)) !== 269 ||
        Math.max(...synthesized.map((record) => record.search.macroExpansions)) !== 9350310 ||
        Math.max(...synthesized.map((record) => record.search.terminalReplays)) !== 2 ||
        Math.max(
            ...records
                .filter((record) => !record.search.macroExpansions)
                .map((record) => record.search.terminalReplays),
        ) > 7 ||
        class96.length !== 177 ||
        assignments96.length !== 529 ||
        Math.max(...class96.map((record) => record.baseBurn)) !== 12.87975000000018 ||
        Math.max(...class96.map((record) => record.success.contactStep)) !== 4321
    )
        throw new Error("Phase 4T route feasibility authority mismatch");
}

function validateSignedWorld(geometry) {
    for (const seed of REVIEW_SEEDS) {
        let previous = null;
        for (let index = -4095; index <= 4095; index += 1) {
            const site = worldWitness(geometry, seed, index).descriptor.site;
            if (site.candidateOrdinal > 5 || site.normalizedDeck > 0.5)
                throw new Error(`Site bound mismatch ${seed}/${index}`);
            if (previous) {
                const spacing = site.center - previous.center;
                if (spacing < 56 || spacing > 136) throw new Error(`Site spacing mismatch ${seed}/${index}`);
            }
            previous = site;
        }
        if (previous.center !== 393156 || Math.round((393216 - previous.center - 13.8) * 1000) !== 46200) {
            throw new Error(`Final-site authority mismatch ${seed}`);
        }
    }
}

function parseArguments(args) {
    const result = {};
    for (let index = 0; index < args.length; index += 2) {
        if (
            !["--geometry", "--predecessor-geometry", "--bootstrap", "--output", "--verify"].includes(args[index]) ||
            args[index + 1] === undefined
        )
            throw new TypeError(
                "Usage: derive_lander_routes.mjs --geometry PATH --predecessor-geometry PATH --bootstrap PATH --output PATH [--verify PATH]",
            );
        result[args[index].slice(2).replaceAll("-", "_")] = args[index + 1];
    }
    const options = {
        geometry: result.geometry,
        predecessorGeometry: result.predecessor_geometry,
        bootstrap: result.bootstrap,
        output: result.output,
        verify: result.verify,
    };
    if (
        !options.geometry ||
        !options.predecessorGeometry ||
        !options.bootstrap ||
        !options.output ||
        (options.verify && resolve(options.output) === resolve(options.verify))
    )
        throw new TypeError(
            "Usage: derive_lander_routes.mjs --geometry PATH --predecessor-geometry PATH --bootstrap PATH --output PATH [--verify PATH]",
        );
    return options;
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
        validateGeometry(geometry, SYNTHESIZER_VERSION);
        const inputs = await readInputs(options);
        validatePredecessor(inputs.predecessor.value, geometry);
        const assignments = assignmentsFor(geometry);
        const groups = groupAssignments(assignments);
        const selectionCensus = { bootstrap: 0, reuse: 0, synthesis: 0 };
        const class96Census = { bootstrap: 0, reuse: 0, synthesis: 0 };
        const catalogs = new Map([[96, inputs.schedules.map((candidate) => structuredClone(candidate))]]);
        const records = [];
        for (let index = 0; index < groups.length; index += 1) {
            records.push(recordFor(geometry, groups[index], catalogs, selectionCensus, class96Census));
            console.error(`derived ${index + 1}/312 ${records.at(-1).pairKey}`);
        }
        validateFeasibility(assignments, records, selectionCensus, class96Census);
        validateNumberBound(quantumCeil, APPROVED_BASE);
        const openings = [];
        for (let profile = 0; profile < PROFILE_ORDER.length; profile += 1) {
            for (let candidateOrder = 0; candidateOrder < CANDIDATE_ORDERS.length; candidateOrder += 1) {
                openings.push(synthesizeOpening(geometry, profile, candidateOrder));
            }
        }
        if (Math.min(...openings.map((opening) => Number(opening.reserve.toFixed(6)))) < 6.386) {
            throw new Error("Opening reserve authority mismatch");
        }
        validateSignedWorld(geometry);
        const terminal = {
            siteIndex: 4095,
            deckDelta: 0,
            allowance: APPROVED_BASE,
            ratio: 1,
            award: APPROVED_BASE,
            completedSites: 4096,
            generatorCursor: 4096,
            activeSiteId: 4095,
            targetSiteId: null,
            targetRouteProof: null,
            cueDirection: null,
        };
        const worldWitnesses = [];
        for (const seed of REVIEW_SEEDS) {
            for (const direction of [-1, 1]) {
                for (let ordinal = 0; ordinal <= 100; ordinal += 1) {
                    worldWitnesses.push(worldWitness(geometry, seed, direction * ordinal));
                }
            }
        }
        const output = {
            schema: DERIVED_SCHEMA,
            deriverVersion: DERIVER_VERSION,
            synthesizerVersion: SYNTHESIZER_VERSION,
            collisionVersion: COLLISION_VERSION,
            canonicalPoseDecimals: POSE_DECIMALS,
            predecessorGeometryDigest: inputs.predecessor.sha,
            bootstrapDigest: inputs.bootstrap.sha,
            geometryDigest: digest(geometry),
            physicsDigest: digest({ collisionVersion: COLLISION_VERSION, commands: COMMANDS, constants: CONSTANTS }),
            assignments,
            assignmentDigest: digest(assignments),
            records,
            openings,
            terminal,
            proofDigest: digest({ records, openings, terminal }),
            worldWitnesses,
            worldDigest: digest(worldWitnesses),
        };
        output.outputDigest = digest(output);
        const serialized = `${JSON.stringify(canonical(output))}\n`;
        const temporary = `${options.output}.tmp-${process.pid}`;
        await writeFile(temporary, serialized, "utf8");
        await rename(temporary, options.output);
        if (options.verify && (await readFile(options.verify, "utf8")) !== serialized) {
            throw new Error(`Derived routes differ from ${options.verify}`);
        }
        console.error(
            `derived assignments=${assignments.length} records=${records.length} openings=${openings.length} census=${JSON.stringify(selectionCensus)} output=${output.outputDigest}`,
        );
    } catch (error) {
        console.error(error.stack ?? error.message);
        process.exitCode = 1;
    }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
