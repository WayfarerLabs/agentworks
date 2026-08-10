#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";

const DERIVER_VERSION = "agw-lander-route-deriver/v1";
const RECIPE_VERSION = "agw-lander-route-recipes/v1";
const MAX_COMBINATIONS = 2_000_000;
const STEP_SECONDS = 1 / 120;
const FUEL_QUANTUM = 0.05;
const MOTIF = Object.freeze([0, 1.2, -0.8, 1, -0.6, 0]);
const WORLD_SEEDS = Object.freeze([1, 0x12345678, 0xffffffff]);
const WORLD_TRANSLATIONS = Object.freeze([
    Object.freeze({ originCenter: 36, originTop: 3.5 }),
    Object.freeze({ originCenter: 117, originTop: 5 }),
    Object.freeze({ originCenter: -42, originTop: 6.5 }),
]);
const WORLD_TRIALS = Object.freeze(WORLD_SEEDS.map((seed, index) =>
    Object.freeze({ seed, ...WORLD_TRANSLATIONS[index] })));
const WORLD_WITNESS_CASES = Object.freeze(WORLD_SEEDS.flatMap((seed) =>
    WORLD_TRANSLATIONS.map((translation) => Object.freeze({ seed, ...translation }))));
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
// Each phase is [reachable command index, inclusive minimum steps, inclusive maximum steps].
// These reviewed recipes describe a constructive family, not a copied output schedule.
const RECIPE_PHASE_RANGES = new Map([
    [78, [[1,90,90],[3,199,201],[2,200,200],[1,20,20],[2,274,274],[3,274,274],[1,44,44],[3,189,189],[2,188,190],[0,360,364],[1,187,187]]],
    [81, [[1,90,90],[3,201,203],[2,202,202],[1,20,20],[2,262,262],[3,262,262],[1,79,79],[3,165,165],[2,164,166],[0,595,599],[1,191,191],[0,120,120]]],
    [84, [[1,90,90],[3,188,190],[2,189,189],[1,35,35],[2,272,272],[3,272,272],[1,20,20],[3,195,195],[2,194,196],[0,469,473],[1,161,161],[0,30,30]]],
    [87, [[1,90,90],[3,204,206],[2,205,205],[1,20,20],[2,290,290],[3,290,290],[1,20,20],[3,204,204],[2,203,205],[1,98,102],[0,455,455]]],
    [90, [[1,90,90],[3,210,212],[2,211,211],[1,23,23],[2,271,271],[3,271,271],[1,92,92],[3,171,171],[2,170,172],[0,360,364],[1,131,131],[0,94,94]]],
    [93, [[1,90,90],[3,208,210],[2,209,209],[1,20,20],[2,284,284],[3,284,284],[1,34,34],[3,190,190],[2,189,191],[0,62,66],[1,95,95],[0,93,93]]],
    [96, [[1,90,90],[3,218,220],[2,219,219],[1,20,20],[2,276,276],[3,276,276],[1,102,102],[3,168,168],[2,167,169],[0,136,140],[1,62,62],[0,357,357]]],
    [99, [[1,90,90],[3,206,208],[2,207,207],[1,42,42],[2,273,273],[3,273,273],[1,93,93],[3,180,180],[2,179,181],[0,262,266],[1,182,182],[0,202,202]]],
    [102, [[1,90,90],[3,208,210],[2,209,209],[1,38,38],[2,279,279],[3,279,279],[1,70,70],[3,183,183],[2,182,184],[0,89,93],[1,75,75],[0,261,261]]],
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

function terrainSample(seed, sampleIndex) {
    const chunk = Math.floor(sampleIndex / 5); const local = sampleIndex - chunk * 5;
    const boundary = (index) => 2 + 3 * sampleUnit(seed, 1, index >>> 0);
    if (local === 0) return boundary(chunk);
    const base = boundary(chunk) + (boundary(chunk + 1) - boundary(chunk)) * local / 5;
    const sign = sampleUnit(seed, 2, chunk >>> 0) >= 0.5 ? 1 : -1;
    return Math.max(0.75, Math.min(7.5, base + sign * MOTIF[local]));
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
    const origin = { center: trial.originCenter, top: trial.originTop };
    const target = { center: origin.center + geometry.centerDelta, top: origin.top + geometry.deckDelta };
    const originRight = origin.center + 4.8; const targetLeft = target.center - 4.8;
    const targetRight = target.center + 4.8; const vertices = [[originRight, origin.top - 0.8]];
    const corridorSamples = [];
    const first = Math.floor(originRight / 4) + 1; const last = Math.ceil(targetLeft / 4) - 1;
    for (let index = first; index <= last; index += 1) {
        const x = index * 4; const raw = terrainSample(trial.seed, index);
        const cap = origin.top + interpolateKnots(geometry.clearanceKnots, x - origin.center);
        const reliefUnit = sampleUnit(trial.seed, 4, index >>> 0);
        const y = raw > cap ? Math.max(0.75, cap - 0.15 * reliefUnit) : raw;
        corridorSamples.push({ cap, index, raw, reliefUnit, relieved: raw > cap, x, y });
        vertices.push([x, y]);
    }
    vertices.push([targetLeft, target.top - 0.8], [targetRight, target.top - 0.8]);
    const resume = Math.floor(targetRight / 4) + 1;
    const nativeResumeSamples = [];
    for (let index = resume; index * 4 <= targetRight + 20; index += 1) {
        const x = index * 4; const y = terrainSample(trial.seed, index);
        nativeResumeSamples.push({ index, x, y }); vertices.push([x, y]);
    }
    const world = { corridorSamples, geometry, nativeResumeSamples, origin, target, seed: trial.seed, vertices };
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

function terrainHeight(world, x) {
    for (const [left, right] of terrainSegments(world)) {
        if (x < left.x || x > right.x) continue;
        return left.y + (right.y - left.y) * (x - left.x) / (right.x - left.x);
    }
    throw new Error(`Independent terrain does not cover ${x}`);
}

const SOLID_CACHE = new WeakMap();
const SITE_SOLID_CACHE = new WeakMap();
function siteSolids(world) {
    const cached = SITE_SOLID_CACHE.get(world);
    if (cached) return cached;
    const descriptors = [[world.origin.center,world.origin.top,true],[world.target.center,world.target.top,false]]
        .map(([center, top, isOrigin]) => {
            const left = center - 4.8; const right = center + 4.8; const bottom = top - 0.35;
            const buildingLeft = right + 2; const roof = top + 7.2;
            const foundation = Math.min(terrainHeight(world, buildingLeft), terrainHeight(world, buildingLeft + 7));
            return {
                isOrigin,
                mast: { bottom: roof, left: buildingLeft + 3.25, right: buildingLeft + 3.75, top: roof + 3.2 },
                noc: { bottom: foundation, left: buildingLeft, right: buildingLeft + 7, top: roof },
                platform: { bottom, left, right, top },
                pylons: [left + 1.4, right - 1.4].map((pylon) =>
                    ({ bottom: top - 0.8, left: pylon - 0.3, right: pylon + 0.3, top: bottom })),
            };
        });
    SITE_SOLID_CACHE.set(world, descriptors);
    return descriptors;
}

function solidSegments(world, launchCleared) {
    const cached = SOLID_CACHE.get(world) ?? {};
    const key = launchCleared ? "cleared" : "launch";
    if (cached[key]) return cached[key];
    const segments = [];
    for (const site of siteSolids(world)) {
        const { bottom, left, right, top } = site.platform;
        if (site.isOrigin && launchCleared) segments.push([{x:left,y:top},{x:right,y:top}]);
        segments.push([{x:left,y:top},{x:left,y:bottom}], [{x:left,y:bottom},{x:right,y:bottom}],
            [{x:right,y:bottom},{x:right,y:top}]);
        for (const pylon of site.pylons) {
            segments.push(...rectangleSegments(pylon.left,pylon.right,pylon.bottom,pylon.top));
        }
        segments.push(...rectangleSegments(site.noc.left,site.noc.right,site.noc.bottom,site.noc.top));
        segments.push(...rectangleSegments(site.mast.left,site.mast.right,site.mast.bottom,site.mast.top));
    }
    cached[key] = segments; SOLID_CACHE.set(world, cached); return segments;
}

function worldWitness(geometry, trial) {
    const world = constructWorld(geometry, trial);
    const descriptor = {
        blendSegments: {
            left: [world.vertices[world.vertices.length - world.nativeResumeSamples.length - 3],
                world.vertices[world.vertices.length - world.nativeResumeSamples.length - 2]],
            right: [world.vertices[world.vertices.length - world.nativeResumeSamples.length - 1],
                world.vertices[world.vertices.length - world.nativeResumeSamples.length]],
        },
        corridorSamples: world.corridorSamples,
        nativeResumeSamples: world.nativeResumeSamples,
        origin: world.origin,
        seed: world.seed,
        sites: siteSolids(world),
        target: world.target,
        templateId: geometry.templateId,
        vertices: world.vertices,
    };
    return { descriptor, digest: digest(descriptor) };
}

function collidesWithUnsafe(pose, world, launchCleared) {
    const marginSquared = CONSTANTS.COLLISION_MARGIN ** 2;
    const polygon = hull(pose);
    const bounds = { left: Math.min(...polygon.map((point) => point.x)), right: Math.max(...polygon.map((point) => point.x)),
        bottom: Math.min(...polygon.map((point) => point.y)), top: Math.max(...polygon.map((point) => point.y)) };
    const near = ([left,right]) => Math.max(left.x,right.x) >= bounds.left - CONSTANTS.COLLISION_MARGIN &&
        Math.min(left.x,right.x) <= bounds.right + CONSTANTS.COLLISION_MARGIN &&
        Math.max(left.y,right.y) >= bounds.bottom - CONSTANTS.COLLISION_MARGIN &&
        Math.min(left.y,right.y) <= bounds.top + CONSTANTS.COLLISION_MARGIN;
    if (solidSegments(world, launchCleared).some(([left, right]) => near([left,right]) &&
        polygonSegmentDistanceSquared(polygon, left, right) <= marginSquared)) return true;
    return terrainSegments(world).some(([left, right]) =>
        near([left,right]) && polygonSegmentDistanceSquared(polygon, left, right) <= marginSquared || polygon.some((point) => {
            if (point.x < left.x || point.x > right.x) return false;
            const terrain = left.y + (right.y - left.y) * (point.x - left.x) / (right.x - left.x);
            return point.y <= terrain;
        }));
}

function sweptUnsafeCollision(previous, next, world, launchCleared) {
    const rotation = Math.hypot(1.6,6.5) * Math.abs(normalizeDegrees(next.angle - previous.angle) * Math.PI / 180);
    const intervals = Math.ceil((Math.hypot(next.x - previous.x, next.y - previous.y) + rotation) /
        CONSTANTS.COLLISION_MARGIN);
    if (intervals > 64) return true;
    for (let index = 0; index <= Math.max(1, intervals); index += 1) {
        if (collidesWithUnsafe(interpolatePose(previous, next, index / Math.max(1, intervals)), world, launchCleared)) return true;
    }
    return false;
}

function replay(fullRuns, geometry, allowance = Infinity, trial = null) {
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
            const previous = pose;
            const result = step(pose, COMMANDS[commandIndex], fuel);
            pose = result.pose;
            fuel = result.fuel;
            burn += result.burn;
            stepIndex += 1;
            used += 1;
            if (trial && sweptUnsafeCollision(previous, pose, world, launchCleared)) {
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
    let combinationsEvaluated = 0;
    let firstFailure = null;
    const firstFailureByTrial = WORLD_TRIALS.map(() => null);
    const safeByTrial = WORLD_TRIALS.map(() => 0);
    let translatedSafe = 0;
    const successes = [];
    for (const candidate of recipeCandidates(geometry.centerDelta)) {
        combinationsEvaluated += 1;
        if (combinationsEvaluated > MAX_COMBINATIONS) {
            throw new Error(`${geometry.templateId} exceeded the finite recipe budget`);
        }
        let canonicalSuccess;
        try {
            canonicalSuccess = replay(candidate, geometry);
        } catch (error) {
            firstFailure ??= error.message;
            continue;
        }
        if (canonicalSuccess.classification !== "safe") continue;
        const trialSuccesses = WORLD_TRIALS.map((trial, index) => {
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
                const trialFailures = WORLD_TRIALS.map((trial) =>
                    replay(canonicalSuccess.runs, geometry, demonstratedMinimum - FUEL_QUANTUM, trial));
                const smallerFailure = replay(canonicalSuccess.runs, geometry, demonstratedMinimum - FUEL_QUANTUM);
                if ("exhaustionStep" in smallerFailure && trialFailures.every((result) =>
                    result.exhaustionStep === smallerFailure.exhaustionStep &&
                    ["x","y","vx","vy","angle","angularVelocity"].every(
                        (key) => Math.abs(result.pose[key] - smallerFailure.pose[key]) <= 1e-9,
                    ))) {
                    successes.push({ candidate, demonstratedMinimum, smallerFailure, success: canonicalSuccess });
                }
            } catch (error) {
                firstFailure ??= error.message;
                // A candidate outside the safe recipe envelope is not a derived route.
            }
        }
    }
    if (successes.length === 0) {
        throw new Error(`${geometry.templateId} has no safe route in ${RECIPE_VERSION} after ${combinationsEvaluated} combinations ` +
            `(${safeByTrial.join("/")} per trial, ${translatedSafe} translated): ${JSON.stringify(firstFailureByTrial)}; ${firstFailure}`);
    }
    successes.sort(compareDerived);
    const { demonstratedMinimum, smallerFailure, success } = successes[0];
    const { runs: unusedFailureRuns, ...failureVector } = smallerFailure;
    void unusedFailureRuns;
    return {
        ...geometry,
        demonstratedMinimum,
        runs: success.runs,
        scheduleDigest: scheduleDigest(success.runs),
        smallerFailure: failureVector,
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
        const worldWitnesses = geometry.templates.flatMap((template) =>
            WORLD_WITNESS_CASES.map((trial) => worldWitness(template, trial)));
        const output = {
            deriverVersion: DERIVER_VERSION,
            geometryDigest: digest(geometry),
            physicsDigest: digest({ commands: COMMANDS, constants: CONSTANTS }),
            routes: geometry.templates.map(deriveTemplate),
            schema: "agw-lander-route-derived/v1",
            worldDigest: digest(worldWitnesses),
            worldWitnesses,
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
