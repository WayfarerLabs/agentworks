#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, rename, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const DERIVER_VERSION = "agw-lander-route-deriver/v8";
const SYNTHESIZER_VERSION = "agw-lander-corridor-synthesizer/v1";
const DERIVED_SCHEMA = "agw-lander-route-derived/v7";
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
    ENGINE_ACCELERATION: 9,
    FUEL_FLOW: 1,
    FUEL_QUANTUM,
    GRAVITY: 3,
    MAX_LANDING_ANGLE: 18,
    MAX_LANDING_ANGULAR_SPEED: 26,
    MAX_LANDING_DESCENT_SPEED: 3.6,
    MAX_LANDING_HORIZONTAL_SPEED: 2.2,
    MAX_PLAYABLE_Y: 56,
    MAX_THRUST_VECTOR: 30,
    STEP_SECONDS,
    TORQUE_ACCELERATION: 80,
    TURN_DIFFERENTIAL: 0.375,
    TURNING_TOTAL: 0.8,
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
const PROFILE_ORDER = ["H0", "H1", "H2", "H3", "L0", "L1", "L2", "L3"];
const OPENING_COUNTS = Object.freeze({
    H0: [126, 72],
    H1: [114, 72],
    H2: [132, 72],
    H3: [102, 72],
    L0: [396, 120],
    L1: [402, 120],
    L2: [384, 114],
    L3: [408, 120],
});

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
        return Object.fromEntries(
            Object.keys(value)
                .sort()
                .map((key) => [key, canonical(value[key])]),
        );
    }
    return value;
}
function canonicalBytes(value) {
    return JSON.stringify(canonical(value));
}
function digest(value) {
    return createHash("sha256").update(canonicalBytes(value), "utf8").digest("hex");
}
function poseForFixture(pose) {
    return Object.fromEntries(
        ["x", "y", "vx", "vy", "angle", "angularVelocity"].map((key) => [
            key,
            Number(pose[key].toFixed(POSE_DECIMALS)),
        ]),
    );
}
function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
}
function positiveModulo(value, modulus) {
    return ((value % modulus) + modulus) % modulus;
}
function normalizeDegrees(value) {
    return positiveModulo(value + 180, 360) - 180;
}
function mixUint32(input) {
    let value = Number(input) >>> 0;
    value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
    value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
    return (value ^ (value >>> 16)) >>> 0;
}
function normalizeSeed(seed) {
    return Number(seed) >>> 0 || 0x6d2b79f5;
}
function sampleUnit(seed, stream, index) {
    return (
        mixUint32(
            normalizeSeed(seed) ^ Math.imul(stream, 0x9e3779b9) ^ Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b),
        ) /
        2 ** 32
    );
}
function quantumCeil(value) {
    return Math.ceil((value - 1e-12) / FUEL_QUANTUM) * FUEL_QUANTUM;
}

function validateGeometry(geometry) {
    if (
        geometry.schema !== "agw-lander-route-geometry/v7" ||
        geometry.synthesizer.recipe !== SYNTHESIZER_VERSION ||
        geometry.terrain.blockWidth !== 128 ||
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
        throw new Error("Unsupported or incomplete geometry-v7 fixture");
    }
}
function worldY(geometry, normalized) {
    return geometry.terrain.mapping.worldScale * normalized + geometry.terrain.mapping.worldOffset;
}
function profileFor(geometry, parity, blockIndex, variant) {
    const family = positiveModulo(blockIndex + parity, 2) === 0 ? "H" : "L";
    const profile = `${family}${variant}`;
    return { profile, samples: geometry.terrain.profiles[profile] };
}
function assignedHeight(geometry, assignment, x) {
    const blockIndex = Math.floor(x / 128);
    const variant = assignment.variants[blockIndex + 1];
    if (variant === undefined) throw new RangeError(`Assignment ${assignment.assignmentId} misses block ${blockIndex}`);
    const samples = profileFor(geometry, assignment.parity, blockIndex, variant).samples;
    const local = x - blockIndex * 128;
    const segment = Math.min(7, Math.floor(local / 16));
    const fraction = (local - segment * 16) / 16;
    return worldY(geometry, samples[segment] + (samples[segment + 1] - samples[segment]) * fraction);
}
function seededBlock(geometry, seed, blockIndex) {
    const parity = Math.floor(2 * sampleUnit(seed, geometry.terrain.parityStream, 0));
    const variant = Math.floor(4 * sampleUnit(seed, geometry.terrain.variantStream, blockIndex >>> 0));
    const selected = profileFor(geometry, parity, blockIndex, variant);
    return {
        index: blockIndex,
        variant,
        profile: selected.profile,
        vertices: selected.samples.map((height, index) => [blockIndex * 128 + index * 16, worldY(geometry, height)]),
    };
}
function seededHeight(geometry, seed, x) {
    const block = seededBlock(geometry, seed, Math.floor(x / 128));
    const segment = Math.min(7, Math.floor((x - block.index * 128) / 16));
    const left = block.vertices[segment];
    const right = block.vertices[segment + 1];
    return left[1] + ((right[1] - left[1]) * (x - left[0])) / 16;
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
    for (const phase of [4, 36, 68, 100])
        for (const parity of [0, 1]) {
            for (let vm1 = 0; vm1 < 4; vm1 += 1)
                for (let v0 = 0; v0 < 4; v0 += 1)
                    for (let v1 = 0; v1 < 4; v1 += 1)
                        for (let v2 = 0; v2 < 4; v2 += 1) {
                            const variants = [vm1, v0, v1, v2];
                            const assignmentId = `p${phase}-q${parity}-v${variants.join("")}`;
                            const partial = { assignmentId, phase, parity, variants };
                            const heightAt = (x) => assignedHeight(geometry, partial, x);
                            const originDeck = siteDescriptor(heightAt, 0, phase).platformTop;
                            const targetDeck = siteDescriptor(heightAt, 1, phase + 96).platformTop;
                            const originMillimeters = millimeters(originDeck);
                            const targetMillimeters = millimeters(targetDeck);
                            assignments.push({
                                assignmentId,
                                phase,
                                parity,
                                variants,
                                originDeck,
                                targetDeck,
                                originMillimeters,
                                targetMillimeters,
                                deckDelta: targetDeck - originDeck,
                                pairKey: `d:${originMillimeters}:${targetMillimeters}`,
                            });
                        }
        }
    if (assignments.length !== 2048) throw new Error(`Expected 2048 assignments, got ${assignments.length}`);
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
    if (groupsOrdered.length !== 100) throw new Error(`Expected 100 pair keys, got ${groupsOrdered.length}`);
    if (new Set(groupsOrdered.map(([, g]) => g.assignments[0].deckDelta.toFixed(12))).size !== 75) {
        throw new Error("Expected 75 exact deck deltas");
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
    return {
        pose: {
            x: pose.x + vx * STEP_SECONDS,
            y: pose.y + vy * STEP_SECONDS,
            vx,
            vy,
            angle: normalizeDegrees(pose.angle + angularVelocity * STEP_SECONDS),
            angularVelocity,
        },
        fuel: exhausts ? 0 : Math.max(0, fuel - requestedBurn),
        burn: requestedBurn * scale,
    };
}
function transform(pose, x, y) {
    const radians = (pose.angle * Math.PI) / 180;
    return {
        x: pose.x + x * Math.cos(radians) + y * Math.sin(radians),
        y: pose.y - x * Math.sin(radians) + y * Math.cos(radians),
    };
}
function hull(pose) {
    return [transform(pose, -1.6, 0), transform(pose, 1.6, 0), transform(pose, 1.6, 6.5), transform(pose, -1.6, 6.5)];
}
function hullBottom(pose) {
    return Math.min(...hull(pose).map((point) => point.y));
}
function interpolatePose(left, right, fraction) {
    const lerp = (a, b) => a + (b - a) * fraction;
    return {
        x: lerp(left.x, right.x),
        y: lerp(left.y, right.y),
        vx: lerp(left.vx, right.vx),
        vy: lerp(left.vy, right.vy),
        angle: normalizeDegrees(left.angle + normalizeDegrees(right.angle - left.angle) * fraction),
        angularVelocity: lerp(left.angularVelocity, right.angularVelocity),
    };
}
function segmentDistanceSquared(a, b, c, d) {
    const pointDistance = (p, s, e) => {
        const dx = e.x - s.x,
            dy = e.y - s.y,
            length = dx * dx + dy * dy;
        const t = length === 0 ? 0 : clamp(((p.x - s.x) * dx + (p.y - s.y) * dy) / length, 0, 1);
        return (p.x - s.x - t * dx) ** 2 + (p.y - s.y - t * dy) ** 2;
    };
    const cross = (p, q, r) => (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);
    const on = (p, q, r) =>
        q.x >= Math.min(p.x, r.x) &&
        q.x <= Math.max(p.x, r.x) &&
        q.y >= Math.min(p.y, r.y) &&
        q.y <= Math.max(p.y, r.y);
    const v = [cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)];
    if (
        (v[0] === 0 && on(a, c, b)) ||
        (v[1] === 0 && on(a, d, b)) ||
        (v[2] === 0 && on(c, a, d)) ||
        (v[3] === 0 && on(c, b, d)) ||
        (v[0] > 0 !== v[1] > 0 && v[2] > 0 !== v[3] > 0)
    )
        return 0;
    return Math.min(pointDistance(a, c, d), pointDistance(b, c, d), pointDistance(c, a, b), pointDistance(d, a, b));
}
function polygonSegmentDistanceSquared(polygon, start, end) {
    let minimum = Infinity;
    for (let i = 0; i < polygon.length; i += 1)
        minimum = Math.min(minimum, segmentDistanceSquared(polygon[i], polygon[(i + 1) % polygon.length], start, end));
    return minimum;
}
function rectangleSegments(left, right, bottom, top) {
    const p = [
        { x: left, y: bottom },
        { x: right, y: bottom },
        { x: right, y: top },
        { x: left, y: top },
    ];
    return p.map((point, index) => [point, p[(index + 1) % 4]]);
}
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
function solidSegments(world, ignoreOriginTop) {
    const result = [];
    for (const site of world.sites) {
        const s = siteSolids(site, site.index === world.originSiteId);
        const p = s.platform;
        if (s.isOrigin && !ignoreOriginTop)
            result.push([
                { x: p.left, y: p.top },
                { x: p.right, y: p.top },
            ]);
        result.push(
            [
                { x: p.left, y: p.top },
                { x: p.left, y: p.bottom },
            ],
            [
                { x: p.left, y: p.bottom },
                { x: p.right, y: p.bottom },
            ],
            [
                { x: p.right, y: p.bottom },
                { x: p.right, y: p.top },
            ],
        );
        for (const r of [s.truss, ...s.supports, s.noc, s.mast])
            result.push(...rectangleSegments(r.left, r.right, r.bottom, r.top));
    }
    return result;
}
function terrainSegments(world) {
    return world.vertices.slice(1).map((right, index) => [
        { x: world.vertices[index][0], y: world.vertices[index][1] },
        { x: right[0], y: right[1] },
    ]);
}
function collides(pose, world, ignoreOriginTop) {
    const margin2 = CONSTANTS.COLLISION_MARGIN ** 2,
        polygon = hull(pose);
    const bounds = {
        left: Math.min(...polygon.map((p) => p.x)),
        right: Math.max(...polygon.map((p) => p.x)),
        bottom: Math.min(...polygon.map((p) => p.y)),
        top: Math.max(...polygon.map((p) => p.y)),
    };
    const near = ([a, b]) =>
        Math.max(a.x, b.x) >= bounds.left - 0.02 &&
        Math.min(a.x, b.x) <= bounds.right + 0.02 &&
        Math.max(a.y, b.y) >= bounds.bottom - 0.02 &&
        Math.min(a.y, b.y) <= bounds.top + 0.02;
    if (
        solidSegments(world, ignoreOriginTop).some(
            ([a, b]) => near([a, b]) && polygonSegmentDistanceSquared(polygon, a, b) <= margin2,
        )
    )
        return true;
    return terrainSegments(world).some(
        ([a, b]) =>
            (near([a, b]) && polygonSegmentDistanceSquared(polygon, a, b) <= margin2) ||
            polygon.some((p) => p.x >= a.x && p.x <= b.x && p.y <= a.y + ((b.y - a.y) * (p.x - a.x)) / (b.x - a.x)),
    );
}
function sweptCollision(previous, next, world, ignoreOriginTop) {
    const rotation = Math.hypot(1.6, 6.5) * Math.abs((normalizeDegrees(next.angle - previous.angle) * Math.PI) / 180);
    const intervals = Math.ceil((Math.hypot(next.x - previous.x, next.y - previous.y) + rotation) / 0.02);
    if (intervals > 64) return true;
    for (let i = 0; i <= Math.max(1, intervals); i += 1)
        if (collides(interpolatePose(previous, next, i / Math.max(1, intervals)), world, ignoreOriginTop)) return true;
    return false;
}
function contactPose(previous, next, deck) {
    let clear = previous,
        hit = next,
        a = 0,
        b = 1;
    for (let i = 0; i < 12; i += 1) {
        const m = (a + b) / 2,
            p = interpolatePose(previous, next, m);
        if (hullBottom(p) <= deck) {
            hit = p;
            b = m;
        } else {
            clear = p;
            a = m;
        }
    }
    void clear;
    return hit;
}
function safeContact(pose, site) {
    const feet = [transform(pose, -1.6, 0), transform(pose, 1.6, 0)];
    return (
        feet.every((f) => f.x >= site.center - 4.8 && f.x <= site.center + 4.8) &&
        pose.vy <= 0 &&
        Math.abs(pose.vx) <= 2.2 &&
        Math.abs(pose.vy) <= 3.6 &&
        Math.abs(normalizeDegrees(pose.angle)) <= 18 &&
        Math.abs(pose.angularVelocity) <= 26
    );
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
    let maxHullTop = Math.max(...hull(pose).map((p) => p.y));
    const used = [];
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
            maxHullTop = Math.max(maxHullTop, ...hull(pose).map((p) => p.y));
            const ignoreOriginTop = !launchCleared && pose.vy > 0;
            if (sweptCollision(previous, pose, world, ignoreOriginTop))
                return {
                    classification: "unsafe",
                    cause: "collision",
                    contactStep: stepIndex,
                    pose,
                    burn,
                    reserve: remaining,
                    maxHullTop,
                    runs: used,
                };
            launchCleared ||= [transform(pose, -1.6, 0), transform(pose, 1.6, 0)].every(
                (f) => f.y > world.sites[0].platformTop + 0.05,
            );
            const target = world.sites[1] ?? world.sites[0];
            if (
                hullBottom(previous) > target.platformTop &&
                hullBottom(pose) <= target.platformTop &&
                Math.max(transform(pose, -1.6, 0).x, transform(pose, 1.6, 0).x) >= target.center - 4.8 &&
                Math.min(transform(pose, -1.6, 0).x, transform(pose, 1.6, 0).x) <= target.center + 4.8
            ) {
                mergeRun(used, command, runCount);
                const contact = contactPose(previous, pose, target.platformTop);
                return {
                    classification: safeContact(contact, target) ? "safe" : "unsafe",
                    contactStep: stepIndex,
                    pose: contact,
                    burn,
                    reserve: remaining,
                    maxHullTop,
                    runs: used,
                };
            }
            if (pose.y > 56)
                return {
                    classification: "unsafe",
                    cause: "ceiling",
                    contactStep: stepIndex,
                    pose,
                    burn,
                    reserve: remaining,
                    maxHullTop,
                    runs: used,
                };
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
    const vertices = [];
    for (let x = 5; x <= 107; x += 2) vertices.push([x + 36, at(x)]);
    if (vertices.at(-1)[0] < 143) vertices.push([143, at(107)]);
    return {
        relativeEnvelope: relative,
        originSiteId: 0,
        sites: [make(0, 36, originDeck), make(1, 132, targetDeck)],
        vertices,
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
    } else if (poseX >= 91.2 && delta < 0) {
        const q = clamp((layer - 150) / 110, 0, 1);
        y = cruiseY + (delta + 0.7 - cruiseY) * q;
        if (q < 1) vy = (delta + 0.7 - cruiseY) / 11;
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
                    advanced.maxTop > 56 - originDeck ||
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
    throw new Error(`No exact route for ${originDeck}/${targetDeck}`);
}

function scheduleDigest(runs) {
    let value = 2166136261;
    for (const [command, count] of runs)
        for (const byte of [command, count & 255, (count >>> 8) & 255]) value = Math.imul(value ^ byte, 16777619) >>> 0;
    return value;
}
function recordFor(group) {
    const first = group.assignments[0],
        originDeck = first.originDeck,
        targetDeck = first.targetDeck;
    const result = synthesize(originDeck, targetDeck),
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
            pose: poseForFixture({
                ...result.replayed.pose,
                x: result.replayed.pose.x - 36,
                y: result.replayed.pose.y - originDeck,
            }),
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
            const local = positiveModulo(x, 128),
                segment = Math.min(7, Math.floor(local / 16)),
                q = (local - segment * 16) / 16;
            return worldY(geometry, samples[segment] + (samples[segment + 1] - samples[segment]) * q);
        };
    const site = siteDescriptor(at, 0, 36),
        vertices = [];
    for (let x = 0; x <= 128; x += 16) vertices.push([x, at(x)]);
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
        pose: poseForFixture(result.pose),
        burn: result.burn,
        reserve: result.reserve,
        classification: "safe",
    };
}
function worldWitness(geometry, seed, siteIndex) {
    const center = 36 + 96 * siteIndex,
        left = center - 4.8,
        right = center + 13.8;
    const firstBlock = Math.floor(left / 128),
        lastBlock = Math.floor(right / 128),
        blocks = [];
    for (let index = firstBlock; index <= lastBlock; index += 1) blocks.push(seededBlock(geometry, seed, index));
    const site = siteDescriptor((x) => seededHeight(geometry, seed, x), siteIndex, center);
    const descriptor = {
        seed: normalizeSeed(seed),
        siteIndex,
        directionlessPhase: positiveModulo(center, 128),
        terrainParity: Math.floor(2 * sampleUnit(seed, geometry.terrain.parityStream, 0)),
        blocks,
        site,
    };
    return { descriptor, digest: digest(descriptor) };
}

let currentGeometry;
function parseArguments(args) {
    const result = {};
    for (let i = 0; i < args.length; i += 2) {
        if (!["--geometry", "--output", "--verify"].includes(args[i]) || args[i + 1] === undefined)
            throw new TypeError("Usage: derive_lander_routes.mjs --geometry PATH --output PATH [--verify PATH]");
        result[args[i].slice(2)] = args[i + 1];
    }
    if (!result.geometry || !result.output || (result.verify && resolve(result.output) === resolve(result.verify)))
        throw new TypeError("Usage: derive_lander_routes.mjs --geometry PATH --output PATH [--verify PATH]");
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
        currentGeometry = JSON.parse(await readFile(options.geometry, "utf8"));
        validateGeometry(currentGeometry);
        const assignments = assignmentsFor(currentGeometry),
            groups = groupAssignments(assignments),
            provisionalRecords = [];
        for (let index = 0; index < groups.length; index += 1) {
            const [, group] = groups[index];
            provisionalRecords.push(recordFor(group));
            console.error(`derived ${index + 1}/100 ${provisionalRecords.at(-1).pairKey}`);
        }
        const controllerBase = quantumCeil(Math.max(...provisionalRecords.map((record) => record.baseBurn)));
        if (controllerBase !== 12.65) throw new Error(`Controller base drift: ${controllerBase}`);
        const records = provisionalRecords.map((record) => ({
            ...record,
            allowance: quantumCeil(controllerBase + record.climbSurcharge),
        }));
        const openings = PROFILE_ORDER.map((profile) => openingFor(currentGeometry, profile));
        const worldWitnesses = [];
        for (const seed of WORLD_SEEDS)
            for (const direction of [-1, 1])
                for (let ordinal = 0; ordinal <= 100; ordinal += 1)
                    worldWitnesses.push(worldWitness(currentGeometry, seed, direction * ordinal));
        const output = {
            schema: DERIVED_SCHEMA,
            deriverVersion: DERIVER_VERSION,
            synthesizerVersion: SYNTHESIZER_VERSION,
            canonicalPoseDecimals: POSE_DECIMALS,
            geometryDigest: digest(currentGeometry),
            physicsDigest: digest({ commands: COMMANDS, constants: CONSTANTS }),
            assignments,
            assignmentDigest: digest(assignments),
            records,
            openings,
            proofDigest: digest({ records, openings }),
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
await main();
