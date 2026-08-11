#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, rename, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const DERIVER_VERSION = "agw-lander-route-deriver/v4";
const RECIPE_VERSION = "agw-lander-route-recipes/v3";
const REPLAY_POSE_DECIMAL_PLACES = 9;
export const MAX_RECIPE_COMBINATIONS = 256;
export const MAX_RECIPE_PHASES = 64;
export const MAX_RECIPE_STEPS = 2880;
const STEP_SECONDS = 1 / 120;
const FUEL_QUANTUM = 0.05;
const TERRAIN_SAMPLE_SPACING = 10;
const PLATFORM_WIDTH = 9.6;
const PLATFORM_CLEARANCE = 2.4;
const PLATFORM_THICKNESS = 0.35;
const MOTIFS = Object.freeze([
    Object.freeze([0, 2.4, -1.5, 1.8, -1.1, 0]),
    Object.freeze([0, -2.1, -0.8, 2.2, 1, 0]),
    Object.freeze([0, 0.9, 2.5, 0.6, -1.9, 0]),
    Object.freeze([0, -1.4, 1.3, 2.4, -0.5, 0]),
]);
const WORLD_SEEDS = Object.freeze([11, 39, 41]);
const WORLD_TRANSLATIONS = Object.freeze([
    Object.freeze({ originCenter: 36 }),
    Object.freeze({ originCenter: 117 }),
    Object.freeze({ originCenter: -42 }),
]);
const WORLD_WITNESS_CASES = Object.freeze(WORLD_SEEDS.flatMap((seed) =>
    WORLD_TRANSLATIONS.map((translation) => Object.freeze({ seed, ...translation }))));
const CONSTANTS = Object.freeze({
    ANGULAR_ASSIST_DIFFERENTIAL: 0.12,
    ANGULAR_ASSIST_FULL_SPEED: 15,
    COLLISION_MARGIN: 0.02,
    ENGINE_ACCELERATION: 9,
    FUEL_FLOW: 1,
    FUEL_QUANTUM,
    GRAVITY: 3,
    MAX_LANDING_ANGLE: 15,
    MAX_LANDING_ANGULAR_SPEED: 22,
    MAX_LANDING_DESCENT_SPEED: 3.2,
    MAX_LANDING_HORIZONTAL_SPEED: 2,
    MAX_PLAYABLE_Y: 56,
    MAX_THRUST_VECTOR: 30,
    STEP_SECONDS,
    TORQUE_ACCELERATION: 80,
    TURN_DIFFERENTIAL: 0.375,
    TURNING_TOTAL: 0.8,
});
const EXPECTED_SITE_GEOMETRY = Object.freeze({
    member: { cap: "butt", join: "round", width: 0.2 },
    noc: { mastHeight: 3.2, mastWidth: 0.5, roofOffset: 7.2, width: 7 },
    platform: { clearance: 2.4, deckTiers: [8.3, 9.1, 9.9], thickness: 0.35, width: 9.6 },
    pylons: {
        collisionExpansion: 0.1,
        count: 3,
        foot: "native-terrain-interpolation",
        positions: [0, 9.3, 18.6],
        top: "platform-bottom",
        width: 0.2,
    },
    truss: {
        alternation: "top-left-to-bottom-right-first",
        bayCount: 12,
        bayHeight: 0.75,
        bayWidth: 1.55,
        chordCount: 2,
        collisionEnvelope: { bottom: -1.2, left: -4.9, right: 13.9, top: -0.25 },
        clearApertures: {
            half: { count: 4, diagonalSquared: 2.965, height: 0.75, width: 1.55 },
            full: { count: 10, diagonalSquared: 10.1725, height: 0.75, width: 3.1 },
        },
        diagonalsPerBay: 1,
        span: 18.6,
    },
});
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
// Each phase is [reachable command index, inclusive minimum steps, inclusive maximum steps].
// These reviewed recipes describe a constructive family, not a copied output schedule.
const fixedPhases = (runs) => runs.map(([command, count]) => [command, count, count]);
const rangedAttitudeFamily = (prefix, left, separator, right, suffix) => [
    ...fixedPhases(prefix),
    [3, left, left + 1],
    ...fixedPhases([separator]),
    [3, right, right + 1],
    ...fixedPhases(suffix),
];
const RECIPE_PHASE_RANGES = new Map([
    [78, rangedAttitudeFamily(
        [[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,30],[1,82],[0,250],
            [1,76],[0,259],[1,103],[0,497],[1,81],[2,16],[3,1],[1,53],[4,1],[0,486],[1,87]],
        35, [0,1], 51, [[4,46]],
    )],
    [81, rangedAttitudeFamily(
        [[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,30],[1,82],[0,255],
            [1,80],[0,259],[1,103],[0,497],[1,79],[2,16],[1,53],[4,2],[0,486],[1,88]],
        37, [0,1], 54, [[1,3],[2,59]],
    )],
    [84, rangedAttitudeFamily(
        [[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,30],[1,82],[0,250],
            [1,76],[0,259],[1,103],[0,500],[1,82],[2,15],[4,1],[1,53],[5,2],[0,486],[1,73]],
        39, [0,1], 56, [[4,70]],
    )],
    [87, rangedAttitudeFamily(
        [[1,90],[0,1],[1,20],[0,7],[4,15],[5,4],[3,44],[0,138],[2,21],[0,359],[2,27],[1,82],[0,250],
            [1,78],[0,271],[1,101],[0,487],[1,81],[2,21],[1,45],[4,1],[0,480],[1,81]],
        37, [0,1], 54, [[4,123]],
    )],
    [90, rangedAttitudeFamily(
        [[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,28],[1,82],[0,250],
            [1,76],[0,259],[1,103],[0,497],[1,81],[2,19],[4,1],[1,53],[4,1],[0,486],[1,81]],
        37, [0,1], 54, [[4,62]],
    )],
    [93, rangedAttitudeFamily(
        [[1,90],[0,1],[1,27],[0,7],[2,15],[5,4],[3,44],[0,160],[2,21],[0,352],[2,30],[1,82],[0,250],
            [1,76],[0,259],[1,100],[0,497],[1,81],[2,16],[5,1],[1,54],[4,2],[0,493],[1,81]],
        45, [4,1], 35, [[4,56]],
    )],
    [96, rangedAttitudeFamily(
        [[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,26],[1,82],[0,250],
            [1,77],[0,259],[1,100],[0,487],[1,82],[2,20],[4,5],[1,53],[4,1],[0,480],[1,79]],
        46, [0,1], 57, [[4,123]],
    )],
    [99, rangedAttitudeFamily(
        [[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,141],[2,21],[0,359],[2,27],[1,85],[0,252],
            [1,77],[0,251],[1,100],[0,487],[1,81],[2,20],[4,7],[1,53],[4,1],[0,480],[1,67]],
        55, [0,1], 51, [[4,125]],
    )],
    [102, rangedAttitudeFamily(
        [[1,90],[0,1],[1,14],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,27],[1,82],[0,242],
            [1,77],[0,223],[1,104],[0,500],[1,81],[2,20],[0,1],[1,53],[4,1],[0,480],[1,53]],
        45, [4,1], 50, [[4,137]],
    )],
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

function canonicalReplayNumber(value) {
    if (!Number.isFinite(value)) throw new TypeError("Replay poses must contain finite numbers");
    return Number(value.toFixed(REPLAY_POSE_DECIMAL_PLACES));
}

function canonicalReplayPose(pose) {
    return Object.fromEntries(["x", "y", "vx", "vy", "angle", "angularVelocity"]
        .map((key) => [key, canonicalReplayNumber(pose[key])]));
}

function normalizeDegrees(degrees) {
    return ((((degrees + 180) % 360) + 360) % 360) - 180;
}

function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
}

function mixUint32(input) {
    let value = Number(input) >>> 0;
    value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
    value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
    return (value ^ (value >>> 16)) >>> 0;
}

function sampleUnit(seed, stream, index) {
    const normalized = (Number(seed) >>> 0) || 0x6d2b79f5;
    return mixUint32(normalized ^ Math.imul(stream, 0x9e3779b9) ^
        Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b)) / 2 ** 32;
}

function positiveModulo(value, modulus) {
    return ((value % modulus) + modulus) % modulus;
}

function motifSelection(seed) {
    return {
        direction: sampleUnit(seed, 2, 1) < 0.5 ? 1 : 3,
        offset: Math.floor(4 * sampleUnit(seed, 2, 0)),
    };
}

function motifIndex(seed, chunkIndex) {
    const selection = motifSelection(seed);
    return positiveModulo(selection.offset + selection.direction * chunkIndex, 4);
}

function terrainSample(seed, sampleIndex) {
    const chunk = Math.floor(sampleIndex / 5); const local = sampleIndex - chunk * 5;
    const boundary = (index) => 1.5 + 4.5 * sampleUnit(seed, 1, index);
    if (local === 0) return boundary(chunk);
    const base = boundary(chunk) + (boundary(chunk + 1) - boundary(chunk)) * (local / 5);
    return Math.max(0.5, Math.min(7.5, base + MOTIFS[motifIndex(seed, chunk)][local]));
}

function terrainHeightAt(seed, x) {
    const leftIndex = Math.floor(x / TERRAIN_SAMPLE_SPACING);
    const leftX = leftIndex * TERRAIN_SAMPLE_SPACING;
    const fraction = (x - leftX) / TERRAIN_SAMPLE_SPACING;
    const left = terrainSample(seed, leftIndex);
    const right = terrainSample(seed, leftIndex + 1);
    return left + (right - left) * fraction;
}

function maximumNativeTerrain(seed, left, right) {
    const xs = [left, right];
    for (let index = Math.ceil(left / TERRAIN_SAMPLE_SPACING);
        index * TERRAIN_SAMPLE_SPACING < right; index += 1) xs.push(index * TERRAIN_SAMPLE_SPACING);
    return Math.max(...xs.map((x) => terrainHeightAt(seed, x)));
}

function interpolateKnots(knots, x) {
    if (x <= knots[0][0]) return knots[0][1];
    for (let index = 1; index < knots.length; index += 1) {
        const left = knots[index - 1]; const right = knots[index];
        if (x <= right[0]) return left[1] + (right[1] - left[1]) * (x - left[0]) / (right[0] - left[0]);
    }
    return knots.at(-1)[1];
}

function step(pose, engines, fuel = Infinity) {
    const totalRequest = engines[0] + engines[1];
    const manualSteer = clamp((engines[0] - engines[1]) / CONSTANTS.TURN_DIFFERENTIAL, -1, 1);
    let assistedLeft = engines[0];
    let assistedRight = engines[1];
    if (manualSteer === 0 && totalRequest > 0) {
        const rawAssist = CONSTANTS.ANGULAR_ASSIST_DIFFERENTIAL *
            clamp(-pose.angularVelocity / CONSTANTS.ANGULAR_ASSIST_FULL_SPEED, -1, 1);
        const differenceLimit = Math.min(totalRequest, 2 - totalRequest);
        const assist = clamp(rawAssist, -differenceLimit, differenceLimit);
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
        burn: requestedBurn * scale,
        engines: { left, right, vectorAngle },
        fuel: exhausts ? 0 : Math.max(0, fuel - requestedBurn),
    };
}

function interpolatePose(left, right, fraction) {
    const lerp = (a, b) => a + (b - a) * fraction;
    return { x: lerp(left.x, right.x), y: lerp(left.y, right.y), vx: lerp(left.vx, right.vx),
        vy: lerp(left.vy, right.vy), angle: normalizeDegrees(left.angle + normalizeDegrees(right.angle - left.angle) * fraction),
        angularVelocity: lerp(left.angularVelocity, right.angularVelocity) };
}

function transform(pose, x, y) {
    const radians = pose.angle * Math.PI / 180;
    return { x: pose.x + x * Math.cos(radians) + y * Math.sin(radians),
        y: pose.y - x * Math.sin(radians) + y * Math.cos(radians) };
}

function hullBottom(pose) {
    return Math.min(transform(pose, -1.6, 0).y, transform(pose, 1.6, 0).y,
        transform(pose, -1.6, 6.5).y, transform(pose, 1.6, 6.5).y);
}

function contactPose(previous, next, deckTop) {
    let clear = previous;
    let hit = next;
    let clearTime = 0;
    let hitTime = 1;
    for (let iteration = 0; iteration < 12; iteration += 1) {
        const middleTime = (clearTime + hitTime) / 2;
        const middle = interpolatePose(previous, next, middleTime);
        if (hullBottom(middle) <= deckTop) { hit = middle; hitTime = middleTime; }
        else { clear = middle; clearTime = middleTime; }
    }
    void clear;
    return hit;
}

function safe(pose, center) {
    const feet = [transform(pose, -1.6, 0), transform(pose, 1.6, 0)];
    return (
        feet.every((foot) => foot.x >= center - 4.8 && foot.x <= center + 4.8) &&
        pose.vy <= 0 &&
        Math.abs(pose.vx) <= CONSTANTS.MAX_LANDING_HORIZONTAL_SPEED &&
        Math.abs(pose.vy) <= CONSTANTS.MAX_LANDING_DESCENT_SPEED &&
        Math.abs(normalizeDegrees(pose.angle)) <= CONSTANTS.MAX_LANDING_ANGLE &&
        Math.abs(pose.angularVelocity) <= CONSTANTS.MAX_LANDING_ANGULAR_SPEED
    );
}

function segmentDistanceSquared(a, b, c, d) {
    const pointDistance = (point, start, end) => {
        const dx = end.x - start.x; const dy = end.y - start.y;
        const length = dx * dx + dy * dy;
        const t = length === 0 ? 0 : Math.max(0, Math.min(1,
            ((point.x - start.x) * dx + (point.y - start.y) * dy) / length));
        return (point.x - start.x - t * dx) ** 2 + (point.y - start.y - t * dy) ** 2;
    };
    const cross = (p, q, r) => (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);
    const on = (p, q, r) => q.x >= Math.min(p.x, r.x) && q.x <= Math.max(p.x, r.x) &&
        q.y >= Math.min(p.y, r.y) && q.y <= Math.max(p.y, r.y);
    const values = [cross(a,b,c), cross(a,b,d), cross(c,d,a), cross(c,d,b)];
    if ((values[0] === 0 && on(a,c,b)) || (values[1] === 0 && on(a,d,b)) ||
        (values[2] === 0 && on(c,a,d)) || (values[3] === 0 && on(c,b,d)) ||
        ((values[0] > 0) !== (values[1] > 0) && (values[2] > 0) !== (values[3] > 0))) return 0;
    return Math.min(pointDistance(a,c,d), pointDistance(b,c,d), pointDistance(c,a,b), pointDistance(d,a,b));
}

function hull(pose) {
    return [transform(pose,-1.6,0), transform(pose,1.6,0), transform(pose,1.6,6.5), transform(pose,-1.6,6.5)];
}

function polygonSegmentDistanceSquared(polygon, start, end) {
    let minimum = Infinity;
    for (let index = 0; index < polygon.length; index += 1) {
        minimum = Math.min(minimum, segmentDistanceSquared(polygon[index], polygon[(index + 1) % polygon.length], start, end));
    }
    return minimum;
}

function rectangleSegments(left, right, bottom, top) {
    const points = [{x:left,y:bottom},{x:right,y:bottom},{x:right,y:top},{x:left,y:top}];
    return points.map((point,index) => [point, points[(index + 1) % points.length]]);
}

const WORLD_CACHE = new WeakMap();
function constructWorld(geometry, trial) {
    const cached = WORLD_CACHE.get(geometry) ?? new Map();
    if (cached.has(trial)) return cached.get(trial);
    const originLevel = geometry.deckDelta === 1.6 ? 83 : geometry.deckDelta === 0.8 ? 91 : 99;
    const originTop = originLevel / 10;
    const site = (center, top) => {
        const left = center - PLATFORM_WIDTH / 2;
        return {
            center,
            deckLevel: Math.round(top * 10),
            minimumDeckTop: maximumNativeTerrain(trial.seed, left, left + 18.6) + PLATFORM_CLEARANCE,
            top,
        };
    };
    const origin = site(trial.originCenter, originTop);
    const target = site(origin.center + geometry.centerDelta,
        (originLevel + Math.round(geometry.deckDelta * 10)) / 10);
    const originShelfLeft = origin.center - PLATFORM_WIDTH / 2;
    const originShelfRight = origin.center + PLATFORM_WIDTH / 2 + 9;
    const targetShelfLeft = target.center - PLATFORM_WIDTH / 2;
    const targetShelfRight = target.center + PLATFORM_WIDTH / 2 + 9;
    const firstIndex = Math.floor((originShelfLeft - 10) / TERRAIN_SAMPLE_SPACING);
    const lastIndex = Math.ceil((targetShelfRight + 50) / TERRAIN_SAMPLE_SPACING);
    const xs = new Set();
    for (let index = firstIndex; index <= lastIndex; index += 1) xs.add(index * TERRAIN_SAMPLE_SPACING);
    for (const descriptor of [origin, target]) {
        const left = descriptor.center - PLATFORM_WIDTH / 2;
        const right = left + 18.6;
        xs.add(left); xs.add(left + 9.3); xs.add(left + 9.6); xs.add(left + 11.6); xs.add(right);
    }
    const corridorSamples = [];
    const vertices = [...xs].sort((a,b) => a-b).map((x) => {
        const raw = terrainHeightAt(trial.seed, x);
        if (x <= originShelfRight || x >= targetShelfLeft || x % TERRAIN_SAMPLE_SPACING !== 0) return [x, raw];
        const index = x / TERRAIN_SAMPLE_SPACING;
        const cap = origin.top + interpolateKnots(geometry.clearanceKnots, x - origin.center);
        const reliefUnit = sampleUnit(trial.seed, 4, index >>> 0);
        const y = raw > cap ? Math.max(0.5, cap - 0.15 * reliefUnit) : raw;
        corridorSamples.push({ cap, index, motifIndex: motifIndex(trial.seed, Math.floor(index / 5)),
            raw, reliefUnit, relieved: raw > cap, x, y });
        return [x, y];
    });
    const world = { corridorSamples, geometry, origin,
        originShelfLeft, originShelfRight, seed: trial.seed, target, targetShelfLeft, targetShelfRight, vertices };
    cached.set(trial, world); WORLD_CACHE.set(geometry, cached);
    return world;
}

const TERRAIN_SEGMENT_CACHE = new WeakMap();
function terrainSegments(world) {
    const cached = TERRAIN_SEGMENT_CACHE.get(world);
    if (cached) return cached;
    const segments = world.vertices.slice(1).map((right, index) => [
        { x: world.vertices[index][0], y: world.vertices[index][1] }, { x: right[0], y: right[1] },
    ]);
    TERRAIN_SEGMENT_CACHE.set(world, segments);
    return segments;
}

const SOLID_CACHE = new WeakMap();
const SITE_SOLID_CACHE = new WeakMap();
function siteSolids(world) {
    const cached = SITE_SOLID_CACHE.get(world);
    if (cached) return cached;
    const descriptors = [[world.origin.center,world.origin.top,true],[world.target.center,world.target.top,false]]
        .map(([center, top, isOrigin]) => {
            const left = center - PLATFORM_WIDTH / 2; const right = center + PLATFORM_WIDTH / 2;
            const bottom = top - PLATFORM_THICKNESS;
            const buildingLeft = right + 2; const roof = top + 7.2;
            const trussTop = bottom; const trussBottom = trussTop - 0.75;
            const pylonXs = [left, left + 9.3, buildingLeft + 7];
            return {
                deckLevel: isOrigin ? world.origin.deckLevel : world.target.deckLevel,
                isOrigin,
                mast: { bottom: roof, left: buildingLeft + 3.25, right: buildingLeft + 3.75, top: roof + 3.2 },
                noc: { bottom, left: buildingLeft, right: buildingLeft + 7, top: roof },
                platform: { bottom, left, right, top },
                minimumDeckTop: isOrigin ? world.origin.minimumDeckTop : world.target.minimumDeckTop,
                pylons: pylonXs.map((x, index) => ({ bottom: terrainHeightAt(world.seed, x) - 0.1,
                    index, left: x - 0.1, right: x + 0.1, top: trussTop + 0.1, x,
                    footY: terrainHeightAt(world.seed, x) })),
                truss: { bottom: trussBottom - 0.1, left: left - 0.1,
                    right: buildingLeft + 7.1, top: trussTop + 0.1 },
            };
        });
    SITE_SOLID_CACHE.set(world, descriptors);
    return descriptors;
}

function solidSegments(world, ignoreOriginTop) {
    const cached = SOLID_CACHE.get(world) ?? {};
    const key = ignoreOriginTop ? "ignore-origin-top" : "include-origin-top";
    if (cached[key]) return cached[key];
    const segments = [];
    for (const site of siteSolids(world)) {
        const { bottom, left, right, top } = site.platform;
        if (site.isOrigin && !ignoreOriginTop) segments.push([{x:left,y:top},{x:right,y:top}]);
        segments.push([{x:left,y:top},{x:left,y:bottom}], [{x:left,y:bottom},{x:right,y:bottom}],
            [{x:right,y:bottom},{x:right,y:top}]);
        segments.push(...rectangleSegments(site.truss.left,site.truss.right,
            site.truss.bottom,site.truss.top));
        for (const pylon of site.pylons) segments.push(...rectangleSegments(
            pylon.left,pylon.right,pylon.bottom,pylon.top));
        segments.push(...rectangleSegments(site.noc.left,site.noc.right,site.noc.bottom,site.noc.top));
        segments.push(...rectangleSegments(site.mast.left,site.mast.right,site.mast.bottom,site.mast.top));
    }
    cached[key] = segments; SOLID_CACHE.set(world, cached); return segments;
}

function scaffoldMembers(site) {
    const left = site.platform.left; const right = site.noc.right;
    const top = site.platform.bottom; const bottom = top - 0.75;
    const bayWidth = (right - left) / 12;
    const segments = [];
    const segment = (x1,y1,x2,y2) => segments.push({ cap: "butt", join: "round", start: [x1,y1], end: [x2,y2] });
    segment(left, top, right, top);
    segment(left, bottom, right, bottom);
    for (let bay = 0; bay < 12; bay += 1) {
        const x1 = left + bayWidth * bay; const x2 = x1 + bayWidth;
        if (bay % 2 === 0) segment(x1, top, x2, bottom);
        else segment(x1, bottom, x2, top);
    }
    for (const pylon of site.pylons) segment(pylon.x, top, pylon.x, pylon.footY);
    return segments;
}

function worldWitness(geometry, trial) {
    const world = constructWorld(geometry, trial);
    const selection = motifSelection(world.seed);
    const insertedSiteSamples = [world.origin, world.target].map((site) => {
        const left = site.center - PLATFORM_WIDTH / 2;
        return [left, left + 9.3, left + 9.6, left + 11.6, left + 18.6]
            .map((x) => [x, terrainHeightAt(world.seed, x)]);
    });
    const descriptor = {
        capRelief: 0.15,
        corridorSamples: world.corridorSamples,
        insertedSiteSamples,
        motifSelection: {
            direction: selection.direction,
            indexes: Array.from({ length: 4 }, (_, index) => motifIndex(world.seed, index)),
            offset: selection.offset,
        },
        origin: world.origin,
        seed: world.seed,
        sites: siteSolids(world).map((site) => ({ ...site,
            clearApertures: {
                trussFull: { count: 10, diagonalSquared: 10.1725, triangleBase: 3.1, triangleHeight: 0.75 },
                trussHalf: { count: 4, diagonalSquared: 2.965, triangleBase: 1.55, triangleHeight: 0.75 },
            },
            scaffoldMembers: scaffoldMembers(site),
        })),
        target: world.target,
        templateId: geometry.templateId,
        terrainRange: [world.vertices[0][0], world.vertices.at(-1)[0]],
        vertices: world.vertices,
    };
    return { descriptor, digest: digest(descriptor) };
}

function collidesWithUnsafe(pose, world, ignoreOriginTop) {
    const marginSquared = CONSTANTS.COLLISION_MARGIN ** 2;
    const polygon = hull(pose);
    const bounds = { left: Math.min(...polygon.map((point) => point.x)), right: Math.max(...polygon.map((point) => point.x)),
        bottom: Math.min(...polygon.map((point) => point.y)), top: Math.max(...polygon.map((point) => point.y)) };
    const near = ([left,right]) => Math.max(left.x,right.x) >= bounds.left - CONSTANTS.COLLISION_MARGIN &&
        Math.min(left.x,right.x) <= bounds.right + CONSTANTS.COLLISION_MARGIN &&
        Math.max(left.y,right.y) >= bounds.bottom - CONSTANTS.COLLISION_MARGIN &&
        Math.min(left.y,right.y) <= bounds.top + CONSTANTS.COLLISION_MARGIN;
    if (solidSegments(world, ignoreOriginTop).some(([left, right]) => near([left,right]) &&
        polygonSegmentDistanceSquared(polygon, left, right) <= marginSquared)) return true;
    return terrainSegments(world).some(([left, right]) =>
        near([left,right]) && polygonSegmentDistanceSquared(polygon, left, right) <= marginSquared || polygon.some((point) => {
            if (point.x < left.x || point.x > right.x) return false;
            const terrain = left.y + (right.y - left.y) * (point.x - left.x) / (right.x - left.x);
            return point.y <= terrain;
        }));
}

function sweptUnsafeCollision(previous, next, world, ignoreOriginTop) {
    const rotation = Math.hypot(1.6,6.5) * Math.abs(normalizeDegrees(next.angle - previous.angle) * Math.PI / 180);
    const intervals = Math.ceil((Math.hypot(next.x - previous.x, next.y - previous.y) + rotation) /
        CONSTANTS.COLLISION_MARGIN);
    if (intervals > 64) return true;
    for (let index = 0; index <= Math.max(1, intervals); index += 1) {
        if (collidesWithUnsafe(interpolatePose(previous, next, index / Math.max(1, intervals)), world, ignoreOriginTop)) return true;
    }
    return false;
}

function replay(fullRuns, geometry, allowance = Infinity, trial = null) {
    const firstRequest = COMMANDS[fullRuns[0]?.[0]];
    if (!firstRequest || firstRequest[0] + firstRequest[1] <= CONSTANTS.TURN_DIFFERENTIAL) {
        throw new Error("Recipe first request must exceed the launch threshold");
    }
    const world = trial ? constructWorld(geometry, trial) : {
        origin: { center: 0, top: 0 },
        target: { center: geometry.centerDelta, top: geometry.deckDelta },
    };
    const center = world.target.center;
    const deckTop = world.target.top;
    let pose = { x: world.origin.center, y: world.origin.top, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    let fuel = allowance;
    let burn = 0;
    let stepIndex = 0;
    let launchCleared = false;
    const runs = [];
    for (const [commandIndex, count] of fullRuns) {
        let used = 0;
        for (let index = 0; index < count; index += 1) {
            if (stepIndex >= MAX_RECIPE_STEPS) {
                throw new Error(`Recipe exceeded ${MAX_RECIPE_STEPS} steps`);
            }
            const requested = COMMANDS[commandIndex];
            const previous = pose;
            const result = step(pose, requested, fuel);
            pose = result.pose;
            fuel = result.fuel;
            burn += result.burn;
            stepIndex += 1;
            used += 1;
            const ignoreOriginTop = !launchCleared && pose.vy > 0;
            if (trial && sweptUnsafeCollision(previous, pose, world, ignoreOriginTop)) {
                throw new Error(`Recipe collided with its clearance envelope at step ${stepIndex} ${JSON.stringify(pose)}`);
            }
            launchCleared ||= [transform(pose,-1.6,0),transform(pose,1.6,0)]
                .every((foot) => foot.y > world.origin.top + 0.05);
            if (hullBottom(previous) > deckTop && hullBottom(pose) <= deckTop &&
                Math.max(transform(pose, -1.6, 0).x, transform(pose, 1.6, 0).x) >= center - 4.8 &&
                Math.min(transform(pose, -1.6, 0).x, transform(pose, 1.6, 0).x) <= center + 4.8) {
                runs.push([commandIndex, used]);
                const contact = contactPose(previous, pose, deckTop);
                return { burn, classification: safe(contact, center) ? "safe" : "unsafe", contactStep: stepIndex,
                    pose: { ...contact, x: contact.x - world.origin.center, y: contact.y - world.origin.top }, runs };
            }
            if (Number.isFinite(allowance) && fuel === 0) {
                runs.push([commandIndex, used]);
                return { allowance, burn, exhaustionStep: stepIndex,
                    pose: { ...pose, x: pose.x - world.origin.center, y: pose.y - world.origin.top }, runs };
            }
            if (pose.y > CONSTANTS.MAX_PLAYABLE_Y) {
                throw new Error(`Recipe exceeded the playable ceiling at step ${stepIndex}`);
            }
        }
        runs.push([commandIndex, used]);
    }
    throw new Error(`Recipe ended before target contact or fuel exhaustion at step ${stepIndex} ${JSON.stringify(pose)}`);
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
    const reviewed = RECIPE_PHASE_RANGES.get(distance);
    if (!reviewed) return;
    const phases = reviewed.map(([commandIndex, minimum, maximum]) => ({ commandIndex, minimum, maximum }));
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

function declaredRecipeRanges(distance) {
    return JSON.stringify(RECIPE_PHASE_RANGES.get(distance) ?? null);
}

function validateRecipeRanges(geometry) {
    const reviewed = RECIPE_PHASE_RANGES.get(geometry.centerDelta);
    const declared = declaredRecipeRanges(geometry.centerDelta);
    if (!reviewed || reviewed.length === 0 || reviewed.length > MAX_RECIPE_PHASES) {
        throw new Error(`${geometry.templateId} invalid recipe phase count; ranges=${declared}`);
    }
    if (canonicalBytes(reviewed[0]) !== canonicalBytes([1, 90, 90])) {
        throw new Error(`${geometry.templateId} must begin with exact [1,90]; ranges=${declared}`);
    }
    if (reviewed.some(([command, minimum, maximum]) => !COMMANDS[command] ||
        !Number.isSafeInteger(minimum) || !Number.isSafeInteger(maximum) || minimum < 1 || maximum < minimum)) {
        throw new Error(`${geometry.templateId} invalid recipe phase; ranges=${declared}`);
    }
    const variableIndexes = reviewed.flatMap(([, minimum, maximum], index) => maximum > minimum ? [index] : []);
    if (variableIndexes.length < 2 || !variableIndexes.some((index) =>
        variableIndexes.some((other) => other > index + 1))) {
        throw new Error(`${geometry.templateId} needs two independently ranged phases; ranges=${declared}`);
    }
    const combinations = reviewed.reduce((product, [, minimum, maximum]) =>
        product * (maximum - minimum + 1), 1);
    if (combinations < 2 || combinations > MAX_RECIPE_COMBINATIONS) {
        throw new Error(`${geometry.templateId} finite recipe budget ${combinations}; ranges=${declared}`);
    }
    return { combinations, declared };
}

function compareDerived(left, right) {
    if (left.success.burn !== right.success.burn) return left.success.burn - right.success.burn;
    const leftSteps = left.success.contactStep;
    const rightSteps = right.success.contactStep;
    if (leftSteps !== rightSteps) return leftSteps - rightSteps;
    return JSON.stringify(left.success.runs).localeCompare(JSON.stringify(right.success.runs));
}

function sameVector(left, right) {
    return left.contactStep === right.contactStep && left.classification === right.classification &&
        ["x","y","vx","vy","angle","angularVelocity"].every(
            (key) => Math.abs(left.pose[key] - right.pose[key]) <= 1e-9,
        );
}

function deriveTemplate(geometry) {
    const { combinations: expectedCombinations, declared } = validateRecipeRanges(geometry);
    let combinationsEvaluated = 0;
    let firstFailure = null;
    let firstMinimumFailure = null;
    const firstFailureByTrial = WORLD_WITNESS_CASES.map(() => null);
    const safeByTrial = WORLD_WITNESS_CASES.map(() => 0);
    let translatedSafe = 0;
    const successes = [];
    for (const candidate of recipeCandidates(geometry.centerDelta)) {
        combinationsEvaluated += 1;
        if (combinationsEvaluated > MAX_RECIPE_COMBINATIONS) {
            throw new Error(`${geometry.templateId} exceeded the finite recipe budget; ranges=${declared}`);
        }
        let canonicalSuccess;
        try {
            canonicalSuccess = replay(candidate, geometry);
        } catch (error) {
            firstFailure ??= error.message;
            continue;
        }
        if (canonicalSuccess.classification !== "safe") continue;
        const trialSuccesses = WORLD_WITNESS_CASES.map((trial, index) => {
            try {
                const result = replay(candidate, geometry, Infinity, trial);
                if (result.classification === "safe") safeByTrial[index] += 1;
                return result;
            } catch (error) {
                firstFailure ??= error.message;
                firstFailureByTrial[index] ??= error.message;
                return null;
            }
        });
        const representative = trialSuccesses[0];
        if (representative?.classification === "safe" &&
            trialSuccesses.every((result) => result && sameVector(canonicalSuccess, result))) {
            try {
                translatedSafe += 1;
                const demonstratedMinimum = Math.ceil((canonicalSuccess.burn - 1e-12) / FUEL_QUANTUM) * FUEL_QUANTUM;
                const trialFailures = WORLD_WITNESS_CASES.map((trial) =>
                    replay(canonicalSuccess.runs, geometry, demonstratedMinimum - FUEL_QUANTUM, trial));
                const smallerFailure = replay(canonicalSuccess.runs, geometry, demonstratedMinimum - FUEL_QUANTUM);
                const matchingFailures = trialFailures.every((result) =>
                    result.exhaustionStep === smallerFailure.exhaustionStep &&
                    ["x","y","vx","vy","angle","angularVelocity"].every(
                        (key) => Math.abs(result.pose[key] - smallerFailure.pose[key]) <= 1e-9,
                    ));
                if ("exhaustionStep" in smallerFailure && matchingFailures) {
                    successes.push({ candidate, demonstratedMinimum, smallerFailure, success: canonicalSuccess });
                } else {
                    firstMinimumFailure ??= `minimum ${demonstratedMinimum}; non-exhaustion or translation mismatch: ${JSON.stringify(smallerFailure)} ` +
                        `${JSON.stringify(trialFailures)}`;
                }
            } catch (error) {
                firstFailure ??= error.message;
                firstMinimumFailure ??= error.message;
                // A candidate outside the safe recipe envelope is not a derived route.
            }
        }
    }
    if (combinationsEvaluated !== expectedCombinations || expectedCombinations > MAX_RECIPE_COMBINATIONS) {
        throw new Error(`${geometry.templateId} recipe enumeration mismatch: ${combinationsEvaluated}/${expectedCombinations}; ` +
            `ranges=${declared}`);
    }
    const distinctOutcomes = new Set(successes.map(({ success }) => canonicalBytes({
        burn: success.burn, contactStep: success.contactStep, pose: canonicalReplayPose(success.pose),
    })));
    if (successes.length < 2) {
        throw new Error(`${geometry.templateId} has no safe route in ${RECIPE_VERSION} after ${combinationsEvaluated} combinations ` +
            `(${safeByTrial.join("/")} per trial, ${translatedSafe} translated): ${JSON.stringify(firstFailureByTrial)}; ` +
            `candidate=${firstFailure}; minimum=${firstMinimumFailure}; ranges=${declared}`);
    }
    if (distinctOutcomes.size < 2) {
        throw new Error(`${geometry.templateId} needs distinct safe outcomes in ${RECIPE_VERSION}; ` +
            `${successes.length} safe candidates produced ${distinctOutcomes.size} outcomes; ranges=${declared}`);
    }
    successes.sort(compareDerived);
    const { demonstratedMinimum, smallerFailure, success } = successes[0];
    const { runs: unusedFailureRuns, ...failureVector } = smallerFailure;
    void unusedFailureRuns;
    return {
        ...geometry,
        combinationsEvaluated,
        demonstratedMinimum,
        runs: success.runs,
        scheduleDigest: scheduleDigest(success.runs),
        smallerFailure: { ...failureVector, pose: canonicalReplayPose(failureVector.pose) },
        success: {
            burn: success.burn,
            classification: success.classification,
            contactStep: success.contactStep,
            pose: canonicalReplayPose(success.pose),
        },
    };
}

function verifySelectedRoutes(routes, templates) {
    for (let index = 0; index < routes.length; index += 1) {
        const route = routes[index]; const geometry = templates[index];
        for (const trial of WORLD_WITNESS_CASES) {
            const success = replay(route.runs, geometry, route.demonstratedMinimum, trial);
            const failure = replay(route.runs, geometry, route.demonstratedMinimum - FUEL_QUANTUM, trial);
            if (success.classification !== "safe" || failure.exhaustionStep !== route.smallerFailure.exhaustionStep) {
                throw new Error(`${route.templateId} selected translated replay mismatch`);
            }
        }
    }
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
    if (result.verify && resolve(result.output) === resolve(result.verify)) {
        throw new TypeError("--output and --verify must resolve to different paths");
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
        if (geometry.schema !== "agw-lander-route-geometry/v3" || geometry.templates.length !== 9 ||
            canonicalBytes(geometry.siteGeometry) !== canonicalBytes(EXPECTED_SITE_GEOMETRY)) {
            throw new Error("Unsupported or incomplete geometry fixture");
        }
        const worldWitnesses = geometry.templates.flatMap((template) =>
            WORLD_WITNESS_CASES.map((trial) => worldWitness(template, trial)));
        const routes = geometry.templates.map(deriveTemplate);
        const combinationsEvaluated = routes.reduce((total, route) => total + route.combinationsEvaluated, 0);
        if (combinationsEvaluated > MAX_RECIPE_COMBINATIONS * geometry.templates.length) {
            throw new Error(`Route catalog exceeded the finite total recipe budget: ${combinationsEvaluated}`);
        }
        const output = {
            canonicalPoseDecimals: REPLAY_POSE_DECIMAL_PLACES,
            deriverVersion: DERIVER_VERSION,
            geometryDigest: digest(geometry),
            physicsDigest: digest({ commands: COMMANDS, constants: CONSTANTS }),
            recipeVersion: RECIPE_VERSION,
            routes,
            schema: "agw-lander-route-derived/v3",
            worldDigest: digest(worldWitnesses),
            worldWitnesses,
        };
        output.outputDigest = digest(output);
        const serialized = `${JSON.stringify(canonical(output))}\n`;
        const temporaryOutput = `${options.output}.tmp-${process.pid}`;
        await writeFile(temporaryOutput, serialized, "utf8");
        await rename(temporaryOutput, options.output);
        if (options.verify) {
            verifySelectedRoutes(routes, geometry.templates);
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
