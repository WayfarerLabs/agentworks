#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, rename, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { classifyRouteSweep, routeHull } from "./lander_route_collision.mjs";
import { canonical, canonicalBytes, fixtureDigest as digest, fixturePose } from "./lander_route_fixture.mjs";

const DERIVER_VERSION = "agw-lander-route-deriver/v9";
const SYNTHESIZER_VERSION = "agw-lander-corridor-synthesizer/v1";
const DERIVED_SCHEMA = "agw-lander-route-derived/v8";
const BOOTSTRAP_SCHEMA = "agw-lander-route-derived/v7";
const BOOTSTRAP_SHA = "c5800497182045dbf664fd50abd6cfd79cc4293bdadbfd4afa526e72f7d71b12";
const BOOTSTRAP_OUTPUT = "dea7263fe5b01ea1c0a442a1f2fefb3f4dad472cbe8668b8bf00793cde5afef7";
const BOOTSTRAP_PROOF = "ca09ed720e3e752745af046cbb2013c99c36227963e9799b1f1cd8961b49f354";
const COLLISION_VERSION = "agw-lander-swept-collision/v2";
const POSE_DECIMALS = 9;
const STATIC_WORLD_SEED = 0x41475731;
const WORLD_SEEDS = [11, 39, 41, STATIC_WORLD_SEED];
const STEP_SECONDS = 1 / 120;
const FUEL_QUANTUM = 0.05;
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
const SEARCH_COMMANDS = [0, 1, 2, 3, 4, 5];
const CONSTANTS = Object.freeze({
    ANGULAR_ASSIST_DIFFERENTIAL: 0.12,
    ANGULAR_ASSIST_FULL_SPEED: 15,
    COLLISION_MARGIN: 0.02,
    COLLISION_ANGLE_KNOT_DEGREES: 1,
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
const PROFILE_ORDER = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"];
const OPENING_COUNTS = Object.freeze({
    S0: [162, 72],
    S1: [348, 102],
    S2: [378, 114],
    S3: [264, 72],
    S4: [168, 72],
    S5: [378, 102],
    S6: [258, 72],
    S7: [378, 114],
});

const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));
const positiveModulo = (value, modulus) => ((value % modulus) + modulus) % modulus;
const normalizeDegrees = (value) => positiveModulo(value + 180, 360) - 180;
function mixUint32(input) {
    let value = Number(input) >>> 0;
    value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
    value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
    return (value ^ (value >>> 16)) >>> 0;
}
const normalizeSeed = (seed) => Number(seed) >>> 0 || 0x6d2b79f5;
function sampleUnit(seed, stream, index) {
    return (
        mixUint32(
            normalizeSeed(seed) ^ Math.imul(stream, 0x9e3779b9) ^ Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b),
        ) /
        2 ** 32
    );
}
export const quantumCeil = (value) => Math.ceil(value / FUEL_QUANTUM) * FUEL_QUANTUM;
export function millimeterDeltaCensus(assignments) {
    return new Set(assignments.map(({ originMillimeters, targetMillimeters }) => targetMillimeters - originMillimeters))
        .size;
}

function validateGeometry(geometry) {
    if (
        geometry.schema !== "agw-lander-route-geometry/v8" ||
        geometry.synthesizer.recipe !== SYNTHESIZER_VERSION ||
        geometry.terrain.superblockWidth !== 512 ||
        geometry.terrain.epochSuperblocks !== 8 ||
        geometry.terrain.cadence !== 16 ||
        geometry.sites.formula !== "36+96*i" ||
        geometry.sites.spacing !== 96 ||
        geometry.siteGeometry.platform.clearance !== 2.5 ||
        canonicalBytes(geometry.synthesizer.prefix) !== canonicalBytes(PREFIX) ||
        geometry.synthesizer.beamWidth !== 6000 ||
        geometry.synthesizer.maxLayers !== 269 ||
        geometry.synthesizer.macroSteps !== 12 ||
        geometry.synthesizer.maxContactStep !== 4320 ||
        canonicalBytes(Object.keys(geometry.terrain.profiles).sort()) !== canonicalBytes([...PROFILE_ORDER].sort())
    ) {
        throw new Error("Unsupported or incomplete geometry-v8 fixture");
    }
}
function worldY(geometry, normalized) {
    return geometry.terrain.mapping.worldScale * normalized + geometry.terrain.mapping.worldOffset;
}
function assignedHeight(geometry, assignment, x) {
    const blockIndex = Math.floor(x / 512);
    const profile = blockIndex === assignment.leftBlock ? assignment.leftProfile : assignment.rightProfile;
    if (profile === undefined) throw new RangeError(`Assignment ${assignment.assignmentId} misses block ${blockIndex}`);
    const samples = geometry.terrain.profiles[`S${profile}`];
    const local = x - blockIndex * 512;
    const segment = Math.min(31, Math.floor(local / 16));
    const fraction = (local - segment * 16) / 16;
    return worldY(geometry, samples[segment] + (samples[segment + 1] - samples[segment]) * fraction);
}
function seededBlock(geometry, seed, blockIndex) {
    const epoch = Math.floor(blockIndex / 8),
        slot = positiveModulo(blockIndex, 8),
        offset = Math.floor(8 * sampleUnit(seed, 15, 0)),
        first = positiveModulo(offset + epoch, 8),
        last = positiveModulo(first + 2, 8),
        middle = Array.from({ length: 8 }, (_, index) => index).filter((index) => index !== first && index !== last);
    for (let index = 5; index >= 1; index -= 1) {
        const exchange = Math.floor((index + 1) * sampleUnit(seed, 16, (Math.imul(epoch, 6) + 5 - index) >>> 0));
        [middle[index], middle[exchange]] = [middle[exchange], middle[index]];
    }
    const profile = [first, ...middle, last][slot],
        samples = geometry.terrain.profiles[`S${profile}`];
    return {
        epoch,
        index: blockIndex,
        profile,
        slot,
        vertices: samples.map((height, index) => [blockIndex * 512 + index * 16, worldY(geometry, height)]),
    };
}
function seededHeight(geometry, seed, x) {
    const block = seededBlock(geometry, seed, Math.floor(x / 512));
    const segment = Math.min(31, Math.floor((x - block.index * 512) / 16));
    const samples = geometry.terrain.profiles[`S${block.profile}`];
    const fraction = (x - block.index * 512 - segment * 16) / 16;
    return worldY(geometry, samples[segment] + (samples[segment + 1] - samples[segment]) * fraction);
}
function siteDescriptor(heightAt, index, center) {
    const closedFootprint = [center - 4.8, center + 13.8];
    const xs = [...closedFootprint];
    for (let x = Math.ceil(closedFootprint[0] / 16) * 16; x <= closedFootprint[1]; x += 16) xs.push(x);
    const localNativeMaximum = Math.max(...xs.map(heightAt));
    const platformTop = localNativeMaximum + 2.5;
    const supportXs = [
        closedFootprint[0],
        closedFootprint[0] + 1,
        closedFootprint[0] + 8.8,
        closedFootprint[0] + 9.8,
        closedFootprint[0] + 17.6,
        closedFootprint[0] + 18.6,
    ];
    return { index, center, closedFootprint, localNativeMaximum, platformTop, supportFeet: supportXs.map(heightAt) };
}
function millimeters(deck) {
    const result = Math.round(deck * 1000);
    if (Math.abs(result / 1000 - deck) > 1e-12) throw new Error(`Non-millimetre deck ${deck}`);
    return result;
}

function assignmentsFor(geometry) {
    const assignments = [];
    for (const phase of geometry.sites.spatialPhases) {
        const leftBlock = Math.floor((phase - 4.8) / 512),
            rightBlock = Math.floor((phase + 109.8) / 512);
        for (let leftProfile = 0; leftProfile < 8; leftProfile += 1)
            for (let rightProfile = 0; rightProfile < 8; rightProfile += 1) {
                if ((leftBlock === rightBlock) !== (leftProfile === rightProfile)) continue;
                const assignmentId = `p${phase}-a${leftProfile}-b${rightProfile}`;
                const partial = { assignmentId, phase, leftBlock, rightBlock, leftProfile, rightProfile };
                const heightAt = (x) => assignedHeight(geometry, partial, x);
                const originDeck = siteDescriptor(heightAt, 0, phase).platformTop;
                const targetDeck = siteDescriptor(heightAt, 1, phase + 96).platformTop;
                const originMillimeters = millimeters(originDeck),
                    targetMillimeters = millimeters(targetDeck);
                assignments.push({
                    ...partial,
                    originDeck,
                    targetDeck,
                    originMillimeters,
                    targetMillimeters,
                    deckDelta: targetDeck - originDeck,
                    pairKey: `d:${originMillimeters}:${targetMillimeters}`,
                });
            }
    }
    if (assignments.length !== 320) throw new Error(`Expected 320 assignments, got ${assignments.length}`);
    return assignments;
}

function affineEnvelope(originDeck, targetDeck, x) {
    const origin = originDeck - 2.5;
    const target = targetDeck - 2.5;
    if (x <= 13.8) return origin;
    if (x >= 91.2) return target;
    return Math.min(29.2, origin + 0.36 * (x - 13.8), target + 0.36 * (91.2 - x));
}
function envelopeVertices(originDeck, targetDeck) {
    const origin = originDeck - 2.5;
    const target = targetDeck - 2.5;
    const xs = new Set([5, 13.8, 91.2, 107]);
    const candidates = [
        13.8 + (29.2 - origin) / 0.36,
        91.2 - (29.2 - target) / 0.36,
        (target - origin + 0.36 * (91.2 + 13.8)) / 0.72,
    ];
    for (const x of candidates) if (x > 13.8 && x < 91.2) xs.add(x);
    return [...xs]
        .sort((a, b) => a - b)
        .map((x) => [x, affineEnvelope(originDeck, targetDeck, x)])
        .filter((point, index, values) => index === 0 || canonicalBytes(point) !== canonicalBytes(values[index - 1]));
}
function groupAssignments(assignments) {
    const groups = new Map();
    for (const assignment of assignments) {
        const group = groups.get(assignment.pairKey) ?? { assignmentIds: [], assignments: [] };
        group.assignmentIds.push(assignment.assignmentId);
        group.assignments.push(assignment);
        groups.set(assignment.pairKey, group);
    }
    const groupsOrdered = [...groups].sort(
        ([, left], [, right]) =>
            left.assignments[0].originMillimeters - right.assignments[0].originMillimeters ||
            left.assignments[0].targetMillimeters - right.assignments[0].targetMillimeters,
    );
    if (groupsOrdered.length !== 243) throw new Error(`Expected 243 pair keys, got ${groupsOrdered.length}`);
    if (millimeterDeltaCensus(groupsOrdered.map(([, group]) => group.assignments[0])) !== 224) {
        throw new Error("Expected 224 exact deck deltas");
    }
    return groupsOrdered;
}

function integrate(pose, engines, fuel = Infinity) {
    const totalRequest = engines[0] + engines[1];
    const manualSteer = clamp((engines[0] - engines[1]) / CONSTANTS.TURN_DIFFERENTIAL, -1, 1);
    let assistedLeft = engines[0];
    let assistedRight = engines[1];
    if (manualSteer === 0 && totalRequest > 0) {
        const raw =
            CONSTANTS.ANGULAR_ASSIST_DIFFERENTIAL *
            clamp(-pose.angularVelocity / CONSTANTS.ANGULAR_ASSIST_FULL_SPEED, -1, 1);
        const assist = clamp(raw, -Math.min(totalRequest, 2 - totalRequest), Math.min(totalRequest, 2 - totalRequest));
        assistedLeft = (totalRequest + assist) / 2;
        assistedRight = (totalRequest - assist) / 2;
    }
    const requestedBurn = (assistedLeft + assistedRight) * STEP_SECONDS;
    const exhausts = requestedBurn >= fuel;
    const scale = exhausts && requestedBurn > 0 ? fuel / requestedBurn : 1;
    const left = assistedLeft * scale;
    const right = assistedRight * scale;
    const vectorAngle = left + right > 0 ? CONSTANTS.MAX_THRUST_VECTOR * manualSteer : 0;
    const radians = ((pose.angle + vectorAngle) * Math.PI) / 180;
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
function siteSolids(site, isOrigin) {
    const platformLeft = site.center - 4.8,
        platformRight = site.center + 4.8,
        bottom = site.platformTop - 0.35;
    const buildingLeft = platformRight + 2,
        buildingRight = buildingLeft + 7,
        roof = site.platformTop + 7.2;
    const supports = [
        [0, 1],
        [8.8, 9.8],
        [17.6, 18.6],
    ].map(([a, b], index) => ({
        left: platformLeft + a,
        right: platformLeft + b,
        leftFoot: site.supportFeet[index * 2],
        rightFoot: site.supportFeet[index * 2 + 1],
    }));
    return {
        isOrigin,
        platform: { left: platformLeft, right: platformRight, bottom, top: site.platformTop },
        truss: { left: platformLeft - 0.1, right: buildingRight + 0.1, bottom: bottom - 0.85, top: bottom + 0.1 },
        supports: supports.map((s) => ({
            left: s.left - 0.1,
            right: s.right + 0.1,
            bottom: Math.min(s.leftFoot, s.rightFoot) - 0.1,
            top: bottom + 0.1,
        })),
        noc: { left: buildingLeft, right: buildingRight, bottom, top: roof },
        mast: { left: buildingLeft + 3.25, right: buildingLeft + 3.75, bottom: roof, top: roof + 3.2 },
    };
}
function collisionCandidates(world, ignoreOriginTop) {
    const result = [],
        target = world.sites[1] ?? world.sites[0];
    for (const site of world.sites) {
        const s = siteSolids(site, site.index === world.originSiteId);
        const p = s.platform;
        if (site === target || (s.isOrigin && ignoreOriginTop))
            result.push(
                {
                    cause: "platform",
                    priority: 1,
                    segment: [
                        { x: p.left, y: p.top },
                        { x: p.left, y: p.bottom },
                    ],
                },
                {
                    cause: "platform",
                    priority: 1,
                    segment: [
                        { x: p.left, y: p.bottom },
                        { x: p.right, y: p.bottom },
                    ],
                },
                {
                    cause: "platform",
                    priority: 1,
                    segment: [
                        { x: p.right, y: p.bottom },
                        { x: p.right, y: p.top },
                    ],
                },
            );
        else result.push({ cause: "platform", priority: 1, polygon: rectangle(p.left, p.right, p.bottom, p.top) });
        for (const box of [s.truss, ...s.supports, s.noc, s.mast])
            result.push({
                cause: box === s.noc ? "noc" : box === s.mast ? "mast" : box === s.truss ? "truss" : "column",
                priority: box === s.noc || box === s.mast ? 0 : 2,
                polygon: rectangle(box.left, box.right, box.bottom, box.top),
            });
    }
    for (let index = 1; index < world.vertices.length; index += 1)
        result.push({
            cause: "terrain",
            priority: 4,
            segment: [
                { x: world.vertices[index - 1][0], y: world.vertices[index - 1][1] },
                { x: world.vertices[index][0], y: world.vertices[index][1] },
            ],
            solidBelow: true,
        });
    result.push({
        cause: "target",
        priority: 5,
        segment: [
            { x: target.center - 4.8, y: target.platformTop },
            { x: target.center + 4.8, y: target.platformTop },
        ],
        target: true,
    });
    return {
        candidates: result,
        target: { ...target, platformLeft: target.center - 4.8, platformRight: target.center + 4.8 },
    };
}

function mergeRun(runs, command, count) {
    if (count <= 0) return;
    if (runs.at(-1)?.[0] === command) runs.at(-1)[1] += count;
    else runs.push([command, count]);
}
function replay(runs, world, initialPose, fuel, maxSteps = 4320) {
    let pose = { ...initialPose },
        remaining = fuel,
        burn = 0,
        stepIndex = 0,
        launchCleared = initialPose.y > world.sites[0].platformTop + 0.05;
    let maxHullTop = Math.max(...routeHull(pose).map((p) => p.y));
    const used = [],
        ordinaryCollision = collisionCandidates(world, false),
        launchCollision = collisionCandidates(world, true);
    for (const [command, count] of runs) {
        let runCount = 0;
        for (let i = 0; i < count; i += 1) {
            if (stepIndex >= maxSteps) break;
            const previous = pose,
                result = integrate(pose, COMMANDS[command], remaining);
            pose = result.pose;
            remaining = result.fuel;
            burn += result.burn;
            stepIndex += 1;
            runCount += 1;
            maxHullTop = Math.max(maxHullTop, ...routeHull(pose).map((p) => p.y));
            const ignoreOriginTop = !launchCleared;
            const collision = ignoreOriginTop ? launchCollision : ordinaryCollision,
                contact = classifyRouteSweep(
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
                    contactStep: stepIndex,
                    pose: contact.pose,
                    burn,
                    reserve: remaining,
                    maxHullTop,
                    runs: used,
                };
            }
            launchCleared ||= routeHull(pose)
                .slice(0, 2)
                .every((f) => f.y > world.sites[0].platformTop + 0.05);
        }
        mergeRun(used, command, runCount);
    }
    return {
        classification: "incomplete",
        contactStep: stepIndex,
        pose,
        burn,
        reserve: remaining,
        maxHullTop,
        runs: used,
    };
}

function envelopeWorld(originDeck, targetDeck) {
    const relative = envelopeVertices(originDeck, targetDeck),
        at = (x) => affineEnvelope(originDeck, targetDeck, x);
    const make = (index, center, deck) => {
        const left = center - 4.8;
        return {
            index,
            center,
            platformTop: deck,
            supportFeet: [left, left + 1, left + 8.8, left + 9.8, left + 17.6, left + 18.6].map((x) => at(x - 36)),
        };
    };
    return {
        relativeEnvelope: relative,
        originSiteId: 0,
        sites: [make(0, 36, originDeck), make(1, 132, targetDeck)],
        vertices: relative.map(([x, y]) => [x + 36, y]),
    };
}
function assignmentWorld(geometry, assignment) {
    const at = (x) => assignedHeight(geometry, assignment, x);
    const origin = siteDescriptor(at, 0, assignment.phase),
        target = siteDescriptor(at, 1, assignment.phase + 96);
    const xs = new Set([assignment.phase - 31, assignment.phase + 127]);
    for (let x = Math.ceil((assignment.phase - 31) / 16) * 16; x <= assignment.phase + 127; x += 16) xs.add(x);
    for (const site of [origin, target])
        for (const x of [
            site.closedFootprint[0],
            site.closedFootprint[0] + 1,
            site.closedFootprint[0] + 8.8,
            site.closedFootprint[0] + 9.8,
            site.closedFootprint[0] + 17.6,
            site.closedFootprint[0] + 18.6,
        ])
            xs.add(x);
    return { originSiteId: 0, sites: [origin, target], vertices: [...xs].sort((a, b) => a - b).map((x) => [x, at(x)]) };
}

function advanceMacro(pose, fuel, command) {
    let p = pose,
        f = fuel,
        maxTop = -Infinity,
        minY = Infinity;
    for (let i = 0; i < 12; i += 1) {
        const result = integrate(p, COMMANDS[command], f);
        p = result.pose;
        f = result.fuel;
        maxTop = Math.max(maxTop, p.y + 6.5);
        minY = Math.min(minY, p.y);
    }
    return { pose: p, fuel: f, maxTop, minY };
}
function runPrefix() {
    let pose = { x: 0, y: 0, vx: 0, vy: 0, angle: 0, angularVelocity: 0 },
        fuel = 30;
    for (const [c, n] of PREFIX)
        for (let i = 0; i < n; i += 1) {
            const result = integrate(pose, COMMANDS[c], fuel);
            pose = result.pose;
            fuel = result.fuel;
        }
    return { pose, fuel };
}
function quantizedKey(p) {
    return [
        Math.round(p.x * 2),
        Math.round(p.y * 2),
        Math.round(p.vx * 2),
        Math.round(p.vy * 2),
        Math.round(p.angle / 5),
        Math.round(p.angularVelocity / 5),
    ].join(":");
}
function runsFromPath(path) {
    const runs = PREFIX.map((r) => [...r]);
    for (const digit of path) mergeRun(runs, Number(digit), 12);
    return runs;
}
function compareIntegers(left, right) {
    for (let i = 0; i < Math.min(left.length, right.length); i += 1)
        if (left[i] !== right[i]) return left[i] - right[i];
    return left.length - right.length;
}
function flattened(path) {
    return runsFromPath(path).flat();
}
function compareStates(a, b) {
    return (
        a.cost - b.cost ||
        b.fuel - a.fuel ||
        compareIntegers(flattened(a.path), flattened(b.path)) ||
        a.pose.x - b.pose.x ||
        a.pose.y - b.pose.y ||
        a.pose.vx - b.pose.vx ||
        a.pose.vy - b.pose.vy ||
        a.pose.angle - b.pose.angle ||
        a.pose.angularVelocity - b.pose.angularVelocity
    );
}
function desired(layer, delta, prefixX, poseX) {
    const cruiseY = Math.max(11.5, delta + 11.5);
    let x,
        y,
        vx = 4.2,
        vy = 0;
    if (layer <= 190) {
        x = prefixX + ((90 - prefixX) * layer) / 190;
        y = 11.5 + (cruiseY - 11.5) * Math.min(1, layer / 120);
    } else if (layer <= 210) {
        const q = (layer - 190) / 20;
        x = 90 + 4 * q;
        vx = 4.2 - 3.2 * q;
        y = cruiseY;
    } else {
        const q = Math.min(1, (layer - 210) / 50);
        x = 94;
        vx = 1 - q;
        y = cruiseY - (cruiseY - (delta + 0.7)) * q;
        vy = -1.5;
    }
    if (delta < -10) {
        const q = clamp((layer - 120) / 140, 0, 1);
        y = cruiseY + (delta + 0.7 - cruiseY) * q;
        if (q < 1) vy = (delta + 0.7 - cruiseY) / 14;
    } else if (delta < 0) {
        if (poseX >= 91.2) {
            const q = clamp((layer - 150) / 110, 0, 1);
            y = cruiseY + (delta + 0.7 - cruiseY) * q;
            if (q < 1) vy = (delta + 0.7 - cruiseY) / 11;
        } else {
            y = cruiseY;
            vy = 0;
        }
    }
    return { x, y, vx, vy };
}
function stateCost(pose, layer, fuel, delta, prefixX) {
    const target = desired(layer, delta, prefixX, pose.x),
        bias = Math.max(0, layer - 210) * 0.08;
    return (
        1.8 * Math.abs(pose.x - target.x) +
        2.4 * Math.abs(pose.y - target.y) +
        (2 + bias) * Math.abs(pose.vx - target.vx) +
        (2 + bias) * Math.abs(pose.vy - target.vy) +
        0.045 * Math.abs(pose.angle) +
        0.035 * Math.abs(pose.angularVelocity) +
        0.08 * (30 - fuel)
    );
}
function terminal(p, delta) {
    return (
        p.x >= 92.8 &&
        p.x <= 99.2 &&
        p.y >= delta &&
        p.y <= delta + 0.3 &&
        Math.abs(p.vx) <= 2.2 &&
        p.vy <= 0 &&
        Math.abs(p.vy) <= 3.6 &&
        Math.abs(p.angle) <= 18 &&
        Math.abs(p.angularVelocity) <= 26
    );
}

function synthesize(originDeck, targetDeck) {
    const delta = targetDeck - originDeck,
        origin = runPrefix(),
        prefixX = origin.pose.x,
        world = envelopeWorld(originDeck, targetDeck);
    let beam = [{ pose: origin.pose, fuel: origin.fuel, path: "", cost: 0, maxHullTop: originDeck + 6.5 }],
        macroExpansions = 0,
        terminalReplays = 0;
    for (let layer = 1; layer <= 269; layer += 1) {
        const byKey = new Map();
        for (const state of beam)
            for (const command of SEARCH_COMMANDS) {
                macroExpansions += 1;
                const advanced = advanceMacro(state.pose, state.fuel, command),
                    p = advanced.pose;
                if (
                    !Object.values(p).every(Number.isFinite) ||
                    advanced.fuel < 0 ||
                    advanced.minY < Math.min(-0.5, delta - 0.5) ||
                    p.x < 5 ||
                    p.x > 107 ||
                    p.y < affineEnvelope(originDeck, targetDeck, p.x) - originDeck + 1.65 ||
                    (p.x > 84 && p.x < 91.2 && p.y < delta + 11)
                )
                    continue;
                const candidate = {
                    pose: p,
                    fuel: advanced.fuel,
                    path: state.path + command,
                    cost: stateCost(p, layer, advanced.fuel, delta, prefixX),
                    maxHullTop: Math.max(state.maxHullTop, originDeck + advanced.maxTop),
                };
                const key = quantizedKey(p),
                    existing = byKey.get(key);
                if (!existing || compareStates(candidate, existing) < 0) byKey.set(key, candidate);
            }
        beam = [...byKey.values()].sort(compareStates).slice(0, 6000);
        for (const state of beam) {
            if (!terminal(state.pose, delta)) continue;
            terminalReplays += 1;
            const runs = runsFromPath(state.path);
            mergeRun(runs, 0, 4320 - runs.reduce((sum, [, count]) => sum + count, 0));
            const replayed = replay(
                runs,
                world,
                { x: 36, y: originDeck, vx: 0, vy: 0, angle: 0, angularVelocity: 0 },
                30,
            );
            if (replayed.classification === "safe")
                return {
                    runs: replayed.runs,
                    search: { selectedLayer: layer, macroExpansions, terminalReplays },
                    replayed,
                    world,
                };
        }
    }
    throw new Error(
        `No exact route for ${originDeck}/${targetDeck}; beam=${beam.length} expansions=${macroExpansions} terminalReplays=${terminalReplays} sample=${JSON.stringify(beam.slice(0, 3).map(({ pose }) => pose))}`,
    );
}

function scheduleDigest(runs) {
    let value = 2166136261;
    for (const [command, count] of runs)
        for (const byte of [command, count & 255, (count >>> 8) & 255]) value = Math.imul(value ^ byte, 16777619) >>> 0;
    return value;
}
let bootstrapSchedules;

function replayBootstrap(originDeck, targetDeck) {
    const world = envelopeWorld(originDeck, targetDeck);
    let terminalReplays = 0;
    for (const certified of bootstrapSchedules) {
        terminalReplays += 1;
        const replayed = replay(
            certified.runs,
            world,
            { x: 36, y: originDeck, vx: 0, vy: 0, angle: 0, angularVelocity: 0 },
            30,
        );
        if (replayed.classification === "safe") {
            return {
                runs: replayed.runs,
                search: { selectedLayer: certified.selectedLayer, macroExpansions: 0, terminalReplays },
                replayed,
                world,
            };
        }
    }
    return null;
}

function recordFor(group) {
    const first = group.assignments[0],
        originDeck = first.originDeck,
        targetDeck = first.targetDeck;
    const result = replayBootstrap(originDeck, targetDeck) ?? synthesize(originDeck, targetDeck),
        delta = targetDeck - originDeck;
    for (const assignment of group.assignments) {
        const concrete = assignmentWorld(currentGeometry, assignment);
        const translated = result.runs;
        const check = replay(
            translated,
            concrete,
            { x: assignment.phase, y: originDeck, vx: 0, vy: 0, angle: 0, angularVelocity: 0 },
            30,
        );
        if (check.classification !== "safe" || check.contactStep !== result.replayed.contactStep)
            throw new Error(`Concrete replay mismatch ${assignment.assignmentId}`);
    }
    const climbSurcharge = Math.max(0, delta) / 3,
        controllerBurn = result.replayed.burn;
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
                    x: result.replayed.pose.x - 36,
                    y: result.replayed.pose.y - originDeck,
                },
                POSE_DECIMALS,
            ),
        },
        controllerBurn,
        baseBurn: controllerBurn - climbSurcharge,
        climbSurcharge,
        maxHullTop: result.replayed.maxHullTop,
    };
}

function openingWorld(geometry, profile) {
    const samples = geometry.terrain.profiles[profile],
        at = (x) => {
            const local = positiveModulo(x, 512),
                segment = Math.min(31, Math.floor(local / 16)),
                q = (local - segment * 16) / 16;
            return worldY(geometry, samples[segment] + (samples[segment + 1] - samples[segment]) * q);
        };
    const site = siteDescriptor(at, 0, 36),
        vertices = [];
    for (let x = 0; x <= 160; x += 16) vertices.push([x, at(x)]);
    return { originSiteId: null, sites: [site], vertices };
}
function openingFor(geometry, profile) {
    const world = openingWorld(geometry, profile),
        [off, on] = OPENING_COUNTS[profile],
        runs = [
            [0, off],
            [1, on],
            [0, 720],
        ],
        result = replay(runs, world, { x: 30, y: 32, vx: 0.8, vy: -0.4, angle: 0, angularVelocity: 0 }, 15, 720);
    if (result.classification !== "safe") throw new Error(`Opening ${profile} failed: ${JSON.stringify(result)}`);
    return {
        profile,
        deck: world.sites[0].platformTop,
        runs: result.runs,
        contactStep: result.contactStep,
        pose: fixturePose(result.pose, POSE_DECIMALS),
        burn: result.burn,
        reserve: result.reserve,
        classification: "safe",
    };
}
function worldWitness(geometry, seed, siteIndex) {
    const center = 36 + 96 * siteIndex,
        left = center - 4.8,
        right = center + 13.8;
    const firstBlock = Math.floor(left / 512),
        lastBlock = Math.floor(right / 512),
        superblocks = [];
    for (let index = firstBlock; index <= lastBlock; index += 1) superblocks.push(seededBlock(geometry, seed, index));
    const site = siteDescriptor((x) => seededHeight(geometry, seed, x), siteIndex, center);
    const descriptor = {
        seed: normalizeSeed(seed),
        siteIndex,
        directionlessPhase: positiveModulo(center, 512),
        superblocks,
        site,
    };
    return { descriptor, digest: digest(descriptor) };
}

let currentGeometry;
function parseArguments(args) {
    const result = {};
    for (let i = 0; i < args.length; i += 2) {
        if (!["--geometry", "--bootstrap", "--output", "--verify"].includes(args[i]) || args[i + 1] === undefined)
            throw new TypeError(
                "Usage: derive_lander_routes.mjs --geometry PATH --bootstrap PATH --output PATH [--verify PATH]",
            );
        result[args[i].slice(2)] = args[i + 1];
    }
    if (
        !result.geometry ||
        !result.bootstrap ||
        !result.output ||
        (result.verify && resolve(result.output) === resolve(result.verify))
    )
        throw new TypeError(
            "Usage: derive_lander_routes.mjs --geometry PATH --bootstrap PATH --output PATH [--verify PATH]",
        );
    return result;
}
async function readBootstrap(path) {
    const bytes = await readFile(path),
        bootstrapDigest = createHash("sha256").update(bytes).digest("hex"),
        source = JSON.parse(bytes.toString("utf8"));
    if (
        bootstrapDigest !== BOOTSTRAP_SHA ||
        source.schema !== BOOTSTRAP_SCHEMA ||
        source.outputDigest !== BOOTSTRAP_OUTPUT ||
        source.proofDigest !== BOOTSTRAP_PROOF ||
        source.records?.length !== 100
    )
        throw new Error("Bootstrap fixture authority mismatch");
    const distinct = new Map();
    for (const record of source.records) {
        const match = /^d:(-?\d+):(-?\d+)$/.exec(record.pairKey),
            runs = record.runs,
            steps = Array.isArray(runs) ? runs.reduce((sum, run) => sum + (run?.[1] ?? NaN), 0) : NaN;
        if (
            !match ||
            !Array.isArray(runs) ||
            runs.length === 0 ||
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
            scheduleDigest(runs) !== record.scheduleDigest
        )
            throw new Error(`Malformed bootstrap schedule ${record.pairKey}`);
        const candidate = {
                digest: record.scheduleDigest >>> 0,
                origin: Number(match[1]),
                target: Number(match[2]),
                runs: runs.map((run) => [...run]),
                selectedLayer: record.search.selectedLayer,
            },
            key = canonicalBytes(candidate.runs),
            existing = distinct.get(key);
        if (
            !existing ||
            candidate.origin < existing.origin ||
            (candidate.origin === existing.origin && candidate.target < existing.target)
        )
            distinct.set(key, candidate);
    }
    const schedules = [...distinct.values()].sort(
        (a, b) => a.digest - b.digest || a.origin - b.origin || a.target - b.target,
    );
    if (schedules.length !== 75) throw new Error(`Expected 75 bootstrap schedules, got ${schedules.length}`);
    return { bootstrapDigest, schedules };
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
        currentGeometry = JSON.parse(await readFile(options.geometry, "utf8"));
        validateGeometry(currentGeometry);
        const bootstrap = await readBootstrap(options.bootstrap);
        bootstrapSchedules = bootstrap.schedules;
        const assignments = assignmentsFor(currentGeometry),
            groups = groupAssignments(assignments),
            provisionalRecords = [];
        for (let index = 0; index < groups.length; index += 1) {
            const [, group] = groups[index];
            provisionalRecords.push(recordFor(group));
            console.error(`derived ${index + 1}/243 ${provisionalRecords.at(-1).pairKey}`);
        }
        const maximumBaseRecord = provisionalRecords.reduce((left, right) =>
                right.baseBurn > left.baseBurn ? right : left,
            ),
            controllerBase = quantumCeil(maximumBaseRecord.baseBurn);
        console.error(
            `diagnostic controllerBase=${controllerBase} maximum=${maximumBaseRecord.baseBurn} key=${maximumBaseRecord.pairKey} bootstrap=${provisionalRecords.filter((record) => record.search.macroExpansions === 0).length}`,
        );
        if (
            provisionalRecords.filter((record) => record.search.macroExpansions === 0).length !== 205 ||
            provisionalRecords.filter((record) => record.search.macroExpansions !== 0).length !== 38 ||
            maximumBaseRecord.pairKey !== "d:12364:9460" ||
            maximumBaseRecord.baseBurn !== 12.50091666666676 ||
            controllerBase !== 12.55
        )
            throw new Error("Route feasibility authority mismatch");
        const approvedBase = 12.55;
        const records = provisionalRecords.map((record) => ({
            ...record,
            allowance: quantumCeil(approvedBase + record.climbSurcharge),
        }));
        const openings = PROFILE_ORDER.map((profile) => openingFor(currentGeometry, profile));
        const terminal = {
            siteIndex: 4095,
            deckDelta: 0,
            allowance: approvedBase,
            ratio: 1,
            award: approvedBase,
            completedSites: 4096,
            generatorCursor: 4096,
            activeSiteId: 4095,
            targetSiteId: null,
            targetRouteProof: null,
            cueDirection: null,
        };
        const worldWitnesses = [];
        for (const seed of WORLD_SEEDS)
            for (const direction of [-1, 1])
                for (let ordinal = 0; ordinal <= 100; ordinal += 1)
                    worldWitnesses.push(worldWitness(currentGeometry, seed, direction * ordinal));
        const output = {
            schema: DERIVED_SCHEMA,
            deriverVersion: DERIVER_VERSION,
            synthesizerVersion: SYNTHESIZER_VERSION,
            collisionVersion: COLLISION_VERSION,
            canonicalPoseDecimals: POSE_DECIMALS,
            bootstrapDigest: bootstrap.bootstrapDigest,
            geometryDigest: digest(currentGeometry),
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
        const serialized = `${JSON.stringify(canonical(output))}\n`,
            temporary = `${options.output}.tmp-${process.pid}`;
        await writeFile(temporary, serialized, "utf8");
        await rename(temporary, options.output);
        if (options.verify && (await readFile(options.verify, "utf8")) !== serialized)
            throw new Error(`Derived routes differ from ${options.verify}`);
    } catch (error) {
        console.error(error.stack ?? error.message);
        process.exitCode = 1;
    }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
