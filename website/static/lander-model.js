import {
    cameraLeftForPose,
    CHUNK_WIDTH,
    createFirstSite,
    createSiteForIndex,
    instantiateTemplateSite,
    mixUint32,
    normalizeSeed,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    sampleUnit,
    selectTemplate,
    siteStructure,
    STATIC_WORLD_SEED,
    terrainVerticesForWindow,
} from "./lander-world.js";

export const STEP_SECONDS = 1 / 120;
export const MAX_FRAME_SECONDS = 0.1;
export const MAX_CATCH_UP_STEPS = 12;
export const GRAVITY = 3;
export const ENGINE_ACCELERATION = 9;
export const TORQUE_ACCELERATION = 80;
export const FUEL_FLOW = 1;
export const FUEL_QUANTUM = 0.05;
export const TURN_DIFFERENTIAL = 0.375;
export const TURNING_TOTAL = 0.8;
export const MAX_THRUST_VECTOR = 30;
export const ANGULAR_ASSIST_DIFFERENTIAL = 0.12;
export const ANGULAR_ASSIST_FULL_SPEED = 15;
export const MAX_PLAYABLE_Y = 56;
export const MAX_LANDING_HORIZONTAL_SPEED = 2.2;
export const MAX_LANDING_DESCENT_SPEED = 3.6;
export const MAX_LANDING_ANGLE = 18;
export const MAX_LANDING_ANGULAR_SPEED = 26;
export const COLLISION_MARGIN = 0.02;

export const FAILURE_STATUS = "Crashed!";
export const GENERATION_ERROR_STATUS = "Mission generation failed. Use Exit mission to start a new run.";
export const SUCCESS_STATUS = "Agent Deployed!";

export const REFERENCE_COMMANDS = Object.freeze([
    Object.freeze([0, 0]),
    Object.freeze([0.72, 0.72]),
    Object.freeze([0, 0.375]),
    Object.freeze([0.375, 0]),
    Object.freeze([0.2125, 0.5875]),
    Object.freeze([0.5875, 0.2125]),
    Object.freeze([0, 0]),
    Object.freeze([0.72, 0.72]),
]);

const ROUTES = [
    ["route-81-rise",81,1.6,[[4.8,-0.65],[40.5,-0.65],[76.2,-0.65]],8.05,3092760020,[[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,30],[1,82],[0,255],[1,80],[0,259],[1,103],[0,497],[1,79],[2,16],[1,53],[4,2],[0,486],[1,88],[3,37],[0,1],[3,54],[1,3],[2,47]],2868,8.040999999999864,79.421926035,1.851087286,1.028229664,-1.356072587,-9.028764212,8.29562548,2855,79.301727742,1.992954297,1.258513636,-1.316413152],
    ["route-84-fall",84,-0.8,[[4.8,-0.65],[42,-0.65],[79.2,-0.65]],8.15,4090953076,[[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,30],[1,82],[0,250],[1,76],[0,259],[1,103],[0,500],[1,82],[2,15],[4,1],[1,53],[5,2],[0,486],[1,73],[3,40],[0,1],[3,57],[4,64]],2870,8.115083333333196,80.931435924,-0.763465946,1.310117565,-0.853895223,-1.308440848,6.422033604,2868,80.92011176,-0.756152512,1.350911826,-0.894709565],
    ["route-96-fall",96,-0.8,[[4.8,-0.65],[48,-0.65],[91.2,-0.65]],8.25,3993330447,[[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,26],[1,82],[0,250],[1,77],[0,259],[1,100],[0,487],[1,82],[2,20],[4,5],[1,53],[4,1],[0,480],[1,79],[3,47],[0,1],[3,58],[4,65]],2868,8.217874999999847,98.622580048,-0.589564363,1.514588327,-0.608774039,-7.55759122,6.579816613,2866,98.60290418,-0.581820577,1.597005723,-0.676420503],
    ["route-87-rise",87,0.8,[[4.8,-0.65],[43.5,-0.65],[82.2,-0.65]],8.15,2261406657,[[1,90],[0,1],[1,20],[0,7],[4,15],[5,4],[3,44],[0,138],[2,21],[0,359],[2,27],[1,82],[0,250],[1,78],[0,271],[1,101],[0,487],[1,81],[2,21],[1,45],[4,1],[0,480],[1,81],[3,38],[0,1],[3,55],[4,62]],2860,8.126416666666524,89.202574148,1.047391253,1.201599927,-1.122183673,-8.894760551,4.7549347,2857,89.17836901,1.069730089,1.328469896,-1.218981232],
    ["route-99-rise",99,0.8,[[4.8,-0.65],[49.5,-0.65],[94.2,-0.65]],8.200000000000001,1325717841,[[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,141],[2,21],[0,359],[2,27],[1,85],[0,252],[1,77],[0,251],[1,100],[0,487],[1,81],[2,20],[4,7],[1,53],[4,1],[0,480],[1,67],[3,56],[0,1],[3,52],[4,70]],2869,8.157041666666526,97.35548535,1.062614507,1.049537591,-1.369955881,-9.446976587,5.489183854,2868,97.347924627,1.072511907,1.084902845,-1.391108322],
    ["route-90-fall",90,-1.6,[[4.8,-0.65],[45,-0.65],[85.2,-0.65]],8.1,1875581733,[[1,90],[0,1],[1,20],[0,7],[2,15],[5,4],[3,44],[0,138],[2,21],[0,356],[2,28],[1,82],[0,250],[1,76],[0,259],[1,103],[0,497],[1,81],[2,19],[4,1],[1,53],[4,1],[0,486],[1,81],[3,38],[0,1],[3,55],[4,47]],2854,8.072833333333197,89.185590304,-1.337207001,1.4033889,-1.134631358,-9.453460036,8.710088456,2851,89.156082366,-1.313541745,1.514785145,-1.207101066],
];
const FAILURE_LITERALS = [
    [8,8.000000000000005,-10.064366902,11.509402336],
    [8.1,8.100000000000009,-1.363740541,6.748522861],
    [8.2,8.199999999999989,-7.642799015,7.142035851],
    [8.1,8.099999999999998,-8.991046857,5.59523011],
    [8.15,8.150000000000002,-9.486448987,5.720897721],
    [8.049999999999999,8.050000000000013,-9.636744041,9.438286699],
];

export const ROUTE_DIGESTS = Object.freeze({
    geometryDigest: "d0393f958f4a8b2657dbf21ef5184f40527be2c8c8d5cf860e6e81e3d2971fa2",
    outputDigest: "ac653b9bbb909ca39a2175c8b26065a81701d375dab14468f928b558638cea93",
    physicsDigest: "e08f8260b723dd245db88de9ae2cdbac54bf9a97cb0bed1b6f170eda362c48dc",
    worldDigest: "502d34c5c1a447b50eebcb458b40bbf8169f7efc7774edc1322af76ab9d0f215",
});

function freeze(value) {
    if (Array.isArray(value)) {
        value.forEach(freeze);
    } else if (value && typeof value === "object") {
        Object.values(value).forEach(freeze);
    }
    return Object.freeze(value);
}

function routeRecord(row, failureLiteral) {
    const [templateId, centerDelta, deckDelta, clearanceKnots, demonstratedMinimum, scheduleDigest, runs,
        contactStep, burn, x, y, vx, vy, angle, angularVelocity, exhaustionStep, failureX, failureY,
        failureVx, failureVy] = row;
    const [failureAllowance, failureBurn, failureAngle, failureAngularVelocity] = failureLiteral;
    return freeze({
        templateId, centerDelta, deckDelta, clearanceKnots, combinationsEvaluated: 4,
        demonstratedMinimum, scheduleDigest, runs,
        success: { contactStep, burn, classification: "safe", pose: { x, y, vx, vy, angle, angularVelocity } },
        smallerFailure: {
            allowance: failureAllowance,
            burn: failureBurn,
            exhaustionStep,
            pose: { x: failureX, y: failureY, vx: failureVx, vy: failureVy,
                angle: failureAngle, angularVelocity: failureAngularVelocity },
        },
    });
}

export const REFERENCE_TEMPLATES = freeze(ROUTES.map((row, index) => routeRecord(row, FAILURE_LITERALS[index])));

const STEP_MILLISECONDS = STEP_SECONDS * 1000;
const HULL = Object.freeze([[-1.6, 0], [1.6, 0], [1.6, 6.5], [-1.6, 6.5]]);
const ZERO = Object.freeze({ left: 0, right: 0, vectorAngle: 0 });
const STRAIGHT_ENGINE_REQUEST = 0.72;
const STRAIGHT_COLLECTIVE_TOTAL = STRAIGHT_ENGINE_REQUEST * 2;

export function normalizeDegrees(degrees) {
    return ((((degrees + 180) % 360) + 360) % 360) - 180;
}

export function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
}

export function transformLocalPoint(pose, localX, localY) {
    const radians = (pose.angle * Math.PI) / 180;
    return {
        x: pose.x + localX * Math.cos(radians) + localY * Math.sin(radians),
        y: pose.y - localX * Math.sin(radians) + localY * Math.cos(radians),
    };
}

export function plumeForThrust(thrust) {
    const command = clamp(thrust, 0, 1);
    return { scaleY: 0.08 + 0.92 * command, opacity: 0.25 + 0.75 * command };
}

export function collectiveRequestForSteer(steer, turningTotal, turnDifferential) {
    const normalized = clamp(steer, -1, 1);
    const total = STRAIGHT_COLLECTIVE_TOTAL -
        (STRAIGHT_COLLECTIVE_TOTAL - turningTotal) * Math.abs(normalized);
    const difference = turnDifferential * normalized;
    return { left: (total + difference) / 2, right: (total - difference) / 2 };
}

export function mixDigitalInput(held) {
    const collective = Boolean(held.Space || held.ArrowUp);
    const left = Boolean(held.ArrowLeft || held.KeyH);
    const right = Boolean(held.ArrowRight || held.KeyL);
    const steer = left === right ? 0 : left ? -1 : 1;
    if (collective) {
        return collectiveRequestForSteer(steer, TURNING_TOTAL, TURN_DIFFERENTIAL);
    }
    if (steer < 0) return { left: 0, right: TURN_DIFFERENTIAL };
    if (steer > 0) return { left: TURN_DIFFERENTIAL, right: 0 };
    return { left: 0, right: 0 };
}

export function mixEngineRequests(keyboard, pointer) {
    const keyboardTotal = keyboard.left + keyboard.right;
    const keyboardSteer = clamp((keyboard.left - keyboard.right) / TURN_DIFFERENTIAL, -1, 1);
    const pointerTotal = pointer.left + pointer.right;
    const pointerSteer = pointerTotal > 0 ? clamp((pointer.left - pointer.right) / TURN_DIFFERENTIAL, -1, 1) : 0;
    const steer = keyboardSteer !== 0 ? keyboardSteer : pointerSteer;
    const collective = keyboardTotal > TURN_DIFFERENTIAL || pointerTotal > 0;
    if (!collective) {
        const total = TURN_DIFFERENTIAL * Math.abs(steer);
        return { left: steer > 0 ? total : 0, right: steer < 0 ? total : 0 };
    }
    return collectiveRequestForSteer(steer, TURNING_TOTAL, TURN_DIFFERENTIAL);
}

export function pointerEngineRequests(displacement, sceneWidth) {
    const deadZone = Math.max(10, sceneWidth * 0.01);
    const fullBiasDistance = Math.max(56, sceneWidth * 0.18);
    const magnitude = clamp((Math.abs(displacement) - deadZone) / (fullBiasDistance - deadZone), 0, 1);
    const bias = Math.sign(displacement) * magnitude;
    return collectiveRequestForSteer(bias, TURNING_TOTAL, TURN_DIFFERENTIAL);
}

export function fuelGaugeLevel(model) {
    const ordinary = model.fuelGaugeReference > 0 ? clamp(model.fuel / model.fuelGaugeReference, 0, 1) : 0;
    if (!model.refuel) return ordinary;
    return model.refuel.fromLevel + (1 - model.refuel.fromLevel) * model.refuel.progress;
}

export function agentInstalled(site) {
    return Boolean(site.powered || (site.nocStage ?? 0) >= 1);
}

export function effectiveThrust(requested, fuel, seconds = STEP_SECONDS, angularVelocity = 0) {
    const rawLeft = clamp(requested.left, 0, 1);
    const rawRight = clamp(requested.right, 0, 1);
    const total = rawLeft + rawRight;
    const manualSteer = clamp((rawLeft - rawRight) / TURN_DIFFERENTIAL, -1, 1);
    let left = rawLeft;
    let right = rawRight;
    if (manualSteer === 0 && total > 0) {
        const rawAssist = ANGULAR_ASSIST_DIFFERENTIAL *
            clamp(-angularVelocity / ANGULAR_ASSIST_FULL_SPEED, -1, 1);
        const differenceLimit = Math.min(total, 2 - total);
        const assist = clamp(rawAssist, -differenceLimit, differenceLimit);
        left = (total + assist) / 2;
        right = (total - assist) / 2;
    }
    const requestedBurn = FUEL_FLOW * (left + right) * seconds;
    const exhausts = requestedBurn >= fuel;
    const scale = exhausts && requestedBurn > 0 ? fuel / requestedBurn : 1;
    left *= scale;
    right *= scale;
    return { left, right, fuel: exhausts ? 0 : Math.max(0, fuel - requestedBurn),
        vectorAngle: left + right > 0 ? MAX_THRUST_VECTOR * manualSteer : 0 };
}

export function integratePose(pose, requested, fuel, seconds = STEP_SECONDS) {
    const thrust = effectiveThrust(requested, fuel, seconds, pose.angularVelocity);
    const radians = ((pose.angle + thrust.vectorAngle) * Math.PI) / 180;
    const total = ENGINE_ACCELERATION * (thrust.left + thrust.right);
    const vx = pose.vx + total * Math.sin(radians) * seconds;
    const vy = pose.vy + (total * Math.cos(radians) - GRAVITY) * seconds;
    const angularVelocity = pose.angularVelocity + TORQUE_ACCELERATION * (thrust.left - thrust.right) * seconds;
    return {
        pose: {
            x: pose.x + vx * seconds,
            y: pose.y + vy * seconds,
            vx,
            vy,
            angle: normalizeDegrees(pose.angle + angularVelocity * seconds),
            angularVelocity,
        },
        thrust,
    };
}

export function refuelRatioForBase(baseNumber) {
    if (!Number.isInteger(baseNumber) || baseNumber < 1) {
        throw new RangeError("Powered base number must be a positive integer");
    }
    return 1 + 0.5 ** (baseNumber - 1);
}

function initialPose() {
    return { x: 30, y: 32, vx: 0.8, vy: -0.4, angle: 0, angularVelocity: 0 };
}

function uprightPose(site, x = site.center) {
    return { x: clamp(x, site.platformLeft + 1.6, site.platformRight - 1.6), y: site.platformTop,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
}

export function checkpointPoseForContact(site, contactPose) {
    if (!Number.isFinite(contactPose?.x)) throw new TypeError("Touchdown pose is required");
    return uprightPose(site);
}

export function createPreflightModel() {
    return { state: "preflight", pose: initialPose(), fuel: 0, fuelGaugeReference: 0,
        commanded: { ...ZERO }, refuel: null, status: "", launchStarted: false, launchCleared: false };
}

export function createRun({ seed, reducedMotion = false } = {}) {
    const runSeed = normalizeSeed(seed);
    const firstSite = createFirstSite(runSeed);
    return updateRetention({
        state: "flying", seed: runSeed, reducedMotion, missionSeconds: 0, completedSites: 0,
        refuelRatio: refuelRatioForBase(1), pose: initialPose(), commanded: { ...ZERO }, fuel: 15,
        fuelGaugeReference: 30,
        generatorCursor: 1, retainedChunks: retainedChunkIndexes(0), retainedSites: [firstSite],
        activeSiteId: null, targetSiteId: 0, targetRouteProof: null, touchdownPose: null,
        sequenceSeconds: 0, refuel: null, agent: null, nocStage: 0, checkpoint: null, failureCause: null,
        crashOrdinal: 0, crash: null, status: "Mission underway.", launchStarted: false, launchCleared: false,
    });
}

export function createFlightModel(options = {}) {
    return createRun(options);
}

function siteById(model, id) {
    return model.retainedSites.find((site) => site.id === id) ?? null;
}

function interpolatePose(left, right, fraction) {
    const lerp = (a, b) => a + (b - a) * fraction;
    return {
        x: lerp(left.x, right.x), y: lerp(left.y, right.y), vx: lerp(left.vx, right.vx),
        vy: lerp(left.vy, right.vy), angle: normalizeDegrees(left.angle + normalizeDegrees(right.angle - left.angle) * fraction),
        angularVelocity: lerp(left.angularVelocity, right.angularVelocity),
    };
}

function hullForPose(pose) {
    return HULL.map(([x, y]) => transformLocalPoint(pose, x, y));
}

function hullBounds(pose) {
    const hull = hullForPose(pose);
    return {
        left: Math.min(...hull.map((point) => point.x)),
        right: Math.max(...hull.map((point) => point.x)),
        bottom: Math.min(...hull.map((point) => point.y)),
        top: Math.max(...hull.map((point) => point.y)),
    };
}

function segmentDistanceSquared(a, b, c, d) {
    function orientation(p, q, r) {
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);
    }
    function onSegment(p, q, r) {
        return q.x >= Math.min(p.x, r.x) && q.x <= Math.max(p.x, r.x) &&
            q.y >= Math.min(p.y, r.y) && q.y <= Math.max(p.y, r.y);
    }
    const o1 = orientation(a, b, c);
    const o2 = orientation(a, b, d);
    const o3 = orientation(c, d, a);
    const o4 = orientation(c, d, b);
    if (((o1 === 0 && onSegment(a, c, b)) || (o2 === 0 && onSegment(a, d, b)) ||
        (o3 === 0 && onSegment(c, a, d)) || (o4 === 0 && onSegment(c, b, d))) ||
        ((o1 > 0) !== (o2 > 0) && (o3 > 0) !== (o4 > 0))) return 0;
    function pointDistanceSquared(point, start, end) {
        const dx = end.x - start.x;
        const dy = end.y - start.y;
        const lengthSquared = dx * dx + dy * dy;
        const projection = lengthSquared === 0 ? 0 : clamp(((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared, 0, 1);
        const x = start.x + projection * dx;
        const y = start.y + projection * dy;
        return (point.x - x) ** 2 + (point.y - y) ** 2;
    }
    return Math.min(pointDistanceSquared(a, c, d), pointDistanceSquared(b, c, d),
        pointDistanceSquared(c, a, b), pointDistanceSquared(d, a, b));
}

function polygonSegmentDistanceSquared(polygon, start, end) {
    let minimum = Infinity;
    for (let index = 0; index < polygon.length; index += 1) {
        minimum = Math.min(minimum, segmentDistanceSquared(polygon[index], polygon[(index + 1) % polygon.length], start, end));
    }
    return minimum;
}

function rectangle(left, right, bottom, top) {
    return [{ x: left, y: bottom }, { x: right, y: bottom }, { x: right, y: top }, { x: left, y: top }];
}

function pointInPolygon(point, polygon) {
    let inside = false;
    for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index, index += 1) {
        const a = polygon[index];
        const b = polygon[previous];
        if ((a.y > point.y) !== (b.y > point.y) && point.x <= ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
    }
    return inside;
}

function polygonDistanceSquared(left, right) {
    if (left.some((point) => pointInPolygon(point, right)) || right.some((point) => pointInPolygon(point, left))) return 0;
    let minimum = Infinity;
    for (let index = 0; index < right.length; index += 1) {
        minimum = Math.min(minimum, polygonSegmentDistanceSquared(left, right[index], right[(index + 1) % right.length]));
    }
    return minimum;
}

function terrainSegments(model, bounds) {
    const vertices = model.terrainVertices;
    if (!vertices) throw new TypeError("Collision classification requires retained terrain vertices");
    const segments = [];
    for (let index = 1; index < vertices.length; index += 1) {
        const left = { x: vertices[index - 1][0], y: vertices[index - 1][1] };
        const right = { x: vertices[index][0], y: vertices[index][1] };
        if (right.x >= bounds.left - COLLISION_MARGIN && left.x <= bounds.right + COLLISION_MARGIN) segments.push([left, right]);
    }
    return segments;
}

function belowTerrain(hull, segment) {
    const [left, right] = segment;
    return hull.some((point) => {
        if (point.x < left.x || point.x > right.x) return false;
        const y = left.y + (right.y - left.y) * ((point.x - left.x) / (right.x - left.x));
        return point.y <= y;
    });
}

function unsafeFeatures(model, pose, target, ignoredTopSiteId = null) {
    const features = [];
    for (const site of model.retainedSites) {
        const topLeft = { x: site.platformLeft, y: site.platformTop };
        const topRight = { x: site.platformRight, y: site.platformTop };
        const bottomLeft = { x: site.platformLeft, y: site.platformBottom };
        const bottomRight = { x: site.platformRight, y: site.platformBottom };
        if (site.id === target?.id || site.id === ignoredTopSiteId) {
            features.push({ cause: "platform", priority: 2, segment: [topLeft, bottomLeft] });
            features.push({ cause: "platform", priority: 2, segment: [bottomLeft, bottomRight] });
            features.push({ cause: "platform", priority: 2, segment: [bottomRight, topRight] });
        } else {
            features.push({ cause: "platform", priority: 2,
                polygon: rectangle(site.platformLeft, site.platformRight, site.platformBottom, site.platformTop) });
        }
        const structure = siteStructure(site);
        features.push({ cause: "truss", priority: 2, polygon: rectangle(
            structure.truss.left, structure.truss.right, structure.truss.bottom, structure.truss.top) });
        for (const column of structure.supportColumns) {
            features.push({ cause: "column", priority: 2, polygon: rectangle(
                column.collider.left, column.collider.right, column.collider.bottom, column.collider.top) });
        }
        features.push({ cause: "noc", priority: 1,
            polygon: rectangle(structure.noc.left, structure.noc.right, structure.noc.bottom, structure.noc.top) });
        features.push({ cause: "mast", priority: 1,
            polygon: rectangle(structure.mast.left, structure.mast.right, structure.mast.bottom, structure.mast.top) });
    }
    return features.map((feature) => {
        const points = feature.polygon ?? feature.segment;
        return { ...feature, bounds: { left: Math.min(...points.map((point) => point.x)),
            right: Math.max(...points.map((point) => point.x)), bottom: Math.min(...points.map((point) => point.y)),
            top: Math.max(...points.map((point) => point.y)) } };
    });
}

function unsafeCauseAtPose(model, pose, target, ignoredTopSiteId = null, suppliedFeatures = null) {
    const hull = hullForPose(pose);
    const bounds = { left: Math.min(...hull.map((point) => point.x)), right: Math.max(...hull.map((point) => point.x)),
        bottom: Math.min(...hull.map((point) => point.y)), top: Math.max(...hull.map((point) => point.y)) };
    const marginSquared = COLLISION_MARGIN ** 2;
    const hits = [];
    for (const feature of suppliedFeatures ?? unsafeFeatures(model, pose, target, ignoredTopSiteId)) {
        const featureBounds = feature.bounds;
        if (featureBounds.right < bounds.left - COLLISION_MARGIN || featureBounds.left > bounds.right + COLLISION_MARGIN ||
            featureBounds.top < bounds.bottom - COLLISION_MARGIN || featureBounds.bottom > bounds.top + COLLISION_MARGIN) continue;
        const distance = feature.polygon ? polygonDistanceSquared(hull, feature.polygon) :
            polygonSegmentDistanceSquared(hull, feature.segment[0], feature.segment[1]);
        if (distance <= marginSquared) hits.push(feature);
    }
    for (const segment of terrainSegments(model, bounds)) {
        if (belowTerrain(hull, segment) || polygonSegmentDistanceSquared(hull, segment[0], segment[1]) <= marginSquared) {
            hits.push({ cause: "terrain", priority: 3 });
        }
    }
    hits.sort((left, right) => left.priority - right.priority);
    return hits[0]?.cause ?? null;
}

function targetTopSweptContact(previous, next, target) {
    const radius = Math.hypot(1.6, 6.5);
    const topLeft = { x: target.platformLeft, y: target.platformTop };
    const topRight = { x: target.platformRight, y: target.platformTop };
    function search(leftPose, rightPose, leftTime, rightTime, depth) {
        const leftBounds = hullBounds(leftPose);
        const rightBounds = hullBounds(rightPose);
        const translation = Math.hypot(rightPose.x - leftPose.x, rightPose.y - leftPose.y);
        const rotation = radius * Math.abs(normalizeDegrees(rightPose.angle - leftPose.angle) * Math.PI / 180);
        const bound = translation + rotation;
        const enclosure = {
            left: Math.min(leftBounds.left, rightBounds.left) - bound,
            right: Math.max(leftBounds.right, rightBounds.right) + bound,
            bottom: Math.min(leftBounds.bottom, rightBounds.bottom) - bound,
            top: Math.max(leftBounds.top, rightBounds.top) + bound,
        };
        if (enclosure.right < target.platformLeft || enclosure.left > target.platformRight ||
            enclosure.bottom > target.platformTop || enclosure.top < target.platformTop) return null;
        if (leftBounds.bottom > target.platformTop && rightBounds.bottom <= target.platformTop) {
            let clear = leftPose;
            let hit = rightPose;
            let clearTime = leftTime;
            let hitTime = rightTime;
            for (let iteration = 0; iteration < 12; iteration += 1) {
                const middleTime = (clearTime + hitTime) / 2;
                const middle = interpolatePose(previous, next, middleTime);
                if (hullBounds(middle).bottom <= target.platformTop) {
                    hit = middle;
                    hitTime = middleTime;
                } else {
                    clear = middle;
                    clearTime = middleTime;
                }
            }
            void clear;
            if (polygonSegmentDistanceSquared(hullForPose(hit), topLeft, topRight) <= Number.EPSILON) {
                return { pose: hit, time: hitTime, grazing: false };
            }
        }
        if (depth >= 20 || (rightTime - leftTime) * STEP_SECONDS <= 1e-7) {
            return { pose: interpolatePose(previous, next, (leftTime + rightTime) / 2),
                time: (leftTime + rightTime) / 2, grazing: true };
        }
        const middleTime = (leftTime + rightTime) / 2;
        const middle = interpolatePose(previous, next, middleTime);
        return search(leftPose, middle, leftTime, middleTime, depth + 1) ??
            search(middle, rightPose, middleTime, rightTime, depth + 1);
    }
    return search(previous, next, 0, 1, 0);
}

export function classifySweptContact(model, previous, next, options = {}) {
    const target = siteById(model, model.targetSiteId);
    const radius = Math.hypot(1.6, 6.5);
    const travel = Math.hypot(next.x - previous.x, next.y - previous.y) + radius * Math.abs(normalizeDegrees(next.angle - previous.angle) * Math.PI / 180);
    const intervals = Math.max(1, Math.ceil(travel / COLLISION_MARGIN));
    if (intervals > 64) return { kind: "unsafe", cause: "overspeed", pose: next };
    const previousBounds = hullBounds(previous); const nextBounds = hullBounds(next);
    const swept = { left: Math.min(previousBounds.left, nextBounds.left) - travel,
        right: Math.max(previousBounds.right, nextBounds.right) + travel,
        bottom: Math.min(previousBounds.bottom, nextBounds.bottom) - travel,
        top: Math.max(previousBounds.top, nextBounds.top) + travel };
    const overlaps = (bounds) => bounds.right >= swept.left - COLLISION_MARGIN &&
        bounds.left <= swept.right + COLLISION_MARGIN && bounds.top >= swept.bottom - COLLISION_MARGIN &&
        bounds.bottom <= swept.top + COLLISION_MARGIN;
    const features = options.features ?? unsafeFeatures(model, previous, target, options.ignoreTopSiteId);
    const topPossible = target && target.platformRight >= swept.left && target.platformLeft <= swept.right &&
        target.platformTop >= swept.bottom && target.platformTop <= swept.top;
    const featurePossible = features.some((feature) => overlaps(feature.bounds));
    const terrainPossible = terrainSegments(model, swept).some(([left, right]) => overlaps({
        left: left.x, right: right.x, bottom: Math.min(left.y, right.y), top: Math.max(left.y, right.y),
    }));
    if (!topPossible && !featurePossible && !terrainPossible) return null;
    const topContact = topPossible ? targetTopSweptContact(previous, next, target) : null;
    let unsafeContact = null;
    let clearPose = previous;
    let clearTime = 0;
    for (let index = 0; index <= intervals; index += 1) {
        const time = index / intervals;
        const pose = interpolatePose(previous, next, time);
        const cause = unsafeCauseAtPose(model, pose, target, options.ignoreTopSiteId, features);
        if (cause) {
            let hitPose = pose;
            let hitTime = time;
            let hitCause = cause;
            for (let iteration = 0; index > 0 && iteration < 12; iteration += 1) {
                const middleTime = (clearTime + hitTime) / 2;
                const middle = interpolatePose(previous, next, middleTime);
                const middleCause = unsafeCauseAtPose(model, middle, target, options.ignoreTopSiteId, features);
                if (middleCause) { hitPose = middle; hitTime = middleTime; hitCause = middleCause; }
                else { clearPose = middle; clearTime = middleTime; }
            }
            void clearPose;
            unsafeContact = { kind: "unsafe", cause: hitCause, pose: hitPose, time: hitTime };
            break;
        }
        clearPose = pose;
        clearTime = time;
    }
    if (unsafeContact && (!topContact || unsafeContact.time <= topContact.time + 1e-12)) return unsafeContact;
    if (topContact) {
        const pose = topContact.pose;
        const feet = [transformLocalPoint(pose, -1.6, 0), transformLocalPoint(pose, 1.6, 0)];
        const safe = !topContact.grazing && pose.vy <= 0 &&
            feet.every((foot) => foot.x >= target.platformLeft && foot.x <= target.platformRight) &&
            Math.abs(pose.vx) <= MAX_LANDING_HORIZONTAL_SPEED && Math.abs(pose.vy) <= MAX_LANDING_DESCENT_SPEED &&
            Math.abs(normalizeDegrees(pose.angle)) <= MAX_LANDING_ANGLE &&
            Math.abs(pose.angularVelocity) <= MAX_LANDING_ANGULAR_SPEED;
        return { kind: safe ? "safe" : "unsafe", cause: safe ? "target" : topContact.grazing ? "grazing" : "target-envelope", pose, time: topContact.time };
    }
    return null;
}

function routeContext(template, supplied = null) {
    if (supplied) return supplied;
    for (const seed of [11, 41]) {
        for (let index = 0; index < 3; index += 1) {
            const candidate = createSiteForIndex(seed, index, {
                canCollected: true, powered: true, nocStage: 7,
            });
            if (selectTemplate(seed, index + 1, candidate, REFERENCE_TEMPLATES) !== template) continue;
            return { seed, originSite: candidate,
                targetSite: instantiateTemplateSite(seed, index + 1, candidate, template) };
        }
    }
    throw new Error(`Route template ${template.templateId} has no finite cycle proof context`);
}

function replayTemplate(template, fuel, suppliedContext = null) {
    const firstRun = template.runs[0];
    const firstRequest = REFERENCE_COMMANDS[firstRun?.[0]];
    if (!firstRequest || firstRun[1] !== 90 || firstRequest[0] + firstRequest[1] <= TURN_DIFFERENTIAL) {
        throw new Error(`Route schedule ${template.templateId} must begin with exact [1,90] launch request`);
    }
    const context = routeContext(template, suppliedContext);
    const { originSite, targetSite } = context;
    let pose = context.pose ?? uprightPose(originSite);
    let reserve = fuel;
    let step = 0;
    let launchCleared = false;
    const rawSites = [originSite, targetSite];
    const terrainVertices = context.terrainVertices ?? terrainVerticesForWindow(context.seed, rawSites,
        originSite.center - 20, targetSite.center + 20);
    const retainedSites = rawSites;
    const collisionModel = {
        seed: context.seed, retainedSites, targetSiteId: targetSite.id,
        terrainVertices,
    };
    const target = retainedSites.find((site) => site.id === targetSite.id);
    const ordinaryFeatures = unsafeFeatures(collisionModel, pose, target);
    const launchFeatures = unsafeFeatures(collisionModel, pose, target, originSite.id);
    for (const [commandIndex, count] of template.runs) {
        for (let index = 0; index < count; index += 1) {
            const previous = pose;
            const result = integratePose(pose, { left: REFERENCE_COMMANDS[commandIndex][0], right: REFERENCE_COMMANDS[commandIndex][1] }, reserve);
            pose = result.pose;
            reserve = result.thrust.fuel;
            step += 1;
            const ignoreTopSiteId = !launchCleared && pose.vy > 0 ? originSite.id : null;
            const contact = classifySweptContact(collisionModel, previous, pose, { ignoreTopSiteId,
                features: ignoreTopSiteId === null ? ordinaryFeatures : launchFeatures });
            if (contact) {
                const relative = { ...contact.pose, x: contact.pose.x - originSite.center,
                    y: contact.pose.y - originSite.platformTop };
                return { kind: contact.kind === "safe" ? "contact" : "collision", cause: contact.cause,
                    step, pose: relative, burn: fuel - reserve };
            }
            const feet = [transformLocalPoint(pose, -1.6, 0), transformLocalPoint(pose, 1.6, 0)];
            launchCleared ||= feet.every((foot) => foot.y > originSite.platformTop + 0.05);
            if (reserve === 0) return { kind: "exhausted", step,
                pose: { ...pose, x: pose.x - originSite.center, y: pose.y - originSite.platformTop }, burn: fuel };
        }
    }
    return { kind: "incomplete", step, pose };
}

function samePose(actual, expected) {
    return ["x", "y", "vx", "vy", "angle", "angularVelocity"].every(
        (key) => Math.abs(actual[key] - expected[key]) <= 1e-9,
    );
}

export function proveTemplate(template, suppliedContext = null) {
    const successful = replayTemplate(template, template.demonstratedMinimum, suppliedContext);
    const smaller = replayTemplate(template, template.demonstratedMinimum - FUEL_QUANTUM, suppliedContext);
    if (successful.kind !== "contact" || successful.step !== template.success.contactStep ||
        Math.abs(successful.burn - template.success.burn) > 1e-9 || !samePose(successful.pose, template.success.pose) ||
        smaller.kind !== "exhausted" || smaller.step !== template.smallerFailure.exhaustionStep ||
        Math.abs(smaller.burn - template.smallerFailure.burn) > 1e-9 || !samePose(smaller.pose, template.smallerFailure.pose)) {
        throw new Error(`Route proof mismatch for ${template.templateId}: ${JSON.stringify({ successful, smaller })}`);
    }
    return freeze({ templateId: template.templateId, demonstratedMinimum: template.demonstratedMinimum,
        quantum: FUEL_QUANTUM, scheduleDigest: template.scheduleDigest, burn: template.success.burn,
        success: template.success, smallerFailure: template.smallerFailure });
}

function generationError(model) {
    return { ...model, state: "generation-error", commanded: { ...ZERO }, refuel: null,
        status: GENERATION_ERROR_STATUS };
}

function provisionalProofContext(model, originSite, targetSite, contactPose) {
    const poweredOrigin = { ...originSite, canCollected: true, powered: true, nocStage: 7 };
    const retainedSites = model.retainedSites.filter((site) => site.id !== originSite.id)
        .concat(poweredOrigin, targetSite).sort((left, right) => left.id - right.id);
    return freeze({ seed: model.seed, completedSites: model.completedSites + 1,
        refuelRatio: refuelRatioForBase(model.completedSites + 2), generatorCursor: model.generatorCursor + 1,
        pose: checkpointPoseForContact(originSite, contactPose), fuel: null, activeSiteId: originSite.id,
        targetSiteId: targetSite.id, retainedSites, originSite: poweredOrigin, targetSite });
}

function prepareService(model, contactPose) {
    const contacted = siteById(model, model.targetSiteId);
    try {
        const poweredBaseNumber = model.completedSites + 1;
        const ratio = refuelRatioForBase(poweredBaseNumber);
        if (model.refuelRatio !== ratio) throw new Error("Stored refuel ratio does not match mission progress");
        const template = selectTemplate(model.seed, model.generatorCursor, contacted, REFERENCE_TEMPLATES);
        const nextSite = instantiateTemplateSite(model.seed, model.generatorCursor, contacted, template);
        const serviced = { ...contacted, canCollected: true };
        const proof = proveTemplate(template, provisionalProofContext(model, serviced, nextSite, contactPose));
        const award = proof.demonstratedMinimum * ratio;
        const sites = model.retainedSites.filter((site) => site.id !== contacted.id).concat(serviced, nextSite).sort((a, b) => a.id - b.id);
        const fromLevel = model.fuelGaugeReference > 0 ? clamp(model.fuel / model.fuelGaugeReference, 0, 1) : 0;
        const fuelGaugeReference = model.fuel + award;
        return {
            ...model, state: "landed", pose: checkpointPoseForContact(contacted, contactPose), commanded: { ...ZERO },
            fuel: fuelGaugeReference, fuelGaugeReference, completedSites: model.completedSites + 1,
            refuelRatio: refuelRatioForBase(poweredBaseNumber + 1), generatorCursor: model.generatorCursor + 1,
            activeSiteId: contacted.id, targetSiteId: nextSite.id, targetRouteProof: proof,
            retainedSites: sites, touchdownPose: checkpointPoseForContact(contacted, contactPose), sequenceSeconds: 0,
            refuel: model.reducedMotion ? null : freeze({ siteId: contacted.id, fromLevel, progress: 0 }),
            nocStage: 0, agent: null, status: "Touchdown confirmed. Fuel collected. Deploying agent.",
        };
    } catch (error) {
        console.error(error);
        return generationError(model);
    }
}

function crashFragments(model, pose, ordinal) {
    if (model.reducedMotion) return [];
    const colors = ["#292b30", "#d94a1e", "#ff7a00", "#ffe09a"];
    return HULL.concat(HULL).map(([x, y], index) => {
        const key = mixUint32(Math.imul((model.targetSiteId ?? 0) + 1, 0x85ebca6b) ^ Math.imul(ordinal, 0xc2b2ae35) ^ Math.imul(index + 1, 0x27d4eb2f));
        const unit = (property) => sampleUnit(model.seed, 5, (key + Math.imul(property, 0x9e3779b9)) >>> 0);
        const origin = transformLocalPoint(pose, x, y);
        return freeze({ id: index, x: origin.x, y: origin.y, vx: -8 + 16 * unit(0), vy: 2 + 9 * unit(1),
            angularVelocity: -240 + 480 * unit(2), color: colors[Math.floor(4 * unit(3))] });
    });
}

function beginCrash(model, cause, pose) {
    const ordinal = model.crashOrdinal + 1;
    if (model.reducedMotion) {
        return { ...model, state: "failed", pose, commanded: { ...ZERO }, failureCause: cause,
            crashOrdinal: ordinal, crash: null, refuel: null, status: FAILURE_STATUS };
    }
    return { ...model, state: "crashing", pose, commanded: { ...ZERO }, failureCause: cause,
        crashOrdinal: ordinal, sequenceSeconds: 0, refuel: null,
        crash: freeze({ pose, fragments: crashFragments(model, pose, ordinal) }), status: "" };
}

export function stepFlight(model, requested, options = {}) {
    if (model.state !== "flying" && model.state !== "launching") return model;
    const rawTotal = requested.left + requested.right;
    const departureThrust = model.state === "launching" && !model.launchStarted ?
        effectiveThrust(requested, model.fuel, options.seconds ?? STEP_SECONDS, model.pose.angularVelocity) : null;
    if (departureThrust && (rawTotal <= TURN_DIFFERENTIAL || departureThrust.left + departureThrust.right === 0)) {
        return { ...model, commanded: { ...ZERO } };
    }
    const request = requested;
    const previous = model.pose;
    const result = integratePose(previous, request, model.fuel, options.seconds ?? STEP_SECONDS);
    let stepped = { ...model, pose: result.pose, fuel: result.thrust.fuel,
        commanded: { left: result.thrust.left, right: result.thrust.right,
            vectorAngle: result.thrust.vectorAngle }, missionSeconds: model.missionSeconds + (options.seconds ?? STEP_SECONDS) };
    if (model.state === "launching") {
        stepped = { ...stepped, launchStarted: true, status: "" };
        const active = siteById(model, model.activeSiteId);
        const ignoreTopSiteId = !model.launchCleared && result.pose.vy > 0 ? active?.id ?? null : null;
        const contact = classifySweptContact(model, previous, result.pose, { ignoreTopSiteId });
        if (contact) return beginCrash(stepped, contact.cause, contact.pose);
        const feet = [transformLocalPoint(result.pose, -1.6, 0), transformLocalPoint(result.pose, 1.6, 0)];
        const cleared = model.launchCleared || (active && feet.every((foot) => foot.y > active.platformTop + 0.05));
        stepped = { ...stepped, launchCleared: cleared };
        if (cleared) return { ...stepped, state: "flying", launchCleared: true };
        return stepped;
    }
    const contact = classifySweptContact(model, previous, result.pose);
    if (contact?.kind === "safe") {
        const serviced = prepareService(stepped, contact.pose);
        return model.reducedMotion ? advanceMissionSequence(serviced, 3.1, true) : serviced;
    }
    if (contact || result.pose.y > MAX_PLAYABLE_Y) {
        return beginCrash(stepped, contact?.cause ?? "ceiling", contact?.pose ?? result.pose);
    }
    return stepped;
}

function freezeCheckpoint(model) {
    if (model.refuelRatio !== refuelRatioForBase(model.completedSites + 1)) {
        throw new Error("Checkpoint refuel ratio does not match mission progress");
    }
    return freeze({ seed: model.seed, completedSites: model.completedSites, refuelRatio: model.refuelRatio,
        generatorCursor: model.generatorCursor, pose: { ...model.touchdownPose }, fuel: model.fuel,
        fuelGaugeReference: model.fuelGaugeReference,
        activeSiteId: model.activeSiteId, targetSiteId: model.targetSiteId, targetRouteProof: model.targetRouteProof,
        retainedChunks: [...model.retainedChunks], retainedSites: model.retainedSites.map((site) => ({ ...site })) });
}

function restoreCheckpoint(model) {
    if (!model.checkpoint) return { ...createRun({ seed: model.seed, reducedMotion: model.reducedMotion }), crashOrdinal: model.crashOrdinal };
    const checkpoint = structuredClone(model.checkpoint);
    if (checkpoint.refuelRatio !== refuelRatioForBase(checkpoint.completedSites + 1)) {
        throw new Error("Checkpoint refuel ratio does not match mission progress");
    }
    return { ...model, ...checkpoint, state: "launching", commanded: { ...ZERO }, sequenceSeconds: 0,
        refuel: null, failureCause: null, crash: null, status: SUCCESS_STATUS,
        launchStarted: false, launchCleared: false };
}

export function transitionMission(model, event, options = {}) {
    if (event === "EXIT" && model.state !== "preflight") return createPreflightModel();
    if (event === "START" && model.state === "preflight") return createRun(options);
    if (event === "RESTART" && model.state === "failed") return restoreCheckpoint(model);
    return model;
}

export function advanceMissionSequence(model, seconds, reducedMotion = model.reducedMotion) {
    if (model.state === "crashing") {
        const elapsed = model.sequenceSeconds + seconds;
        return elapsed >= 0.6 ? { ...model, state: "failed", sequenceSeconds: 0, crash: null,
            refuel: null, status: FAILURE_STATUS }
            : { ...model, sequenceSeconds: elapsed };
    }
    if (reducedMotion && ["landed", "deploying", "powering"].includes(model.state)) {
        const active = siteById(model, model.activeSiteId);
        const sites = model.retainedSites.map((site) => site.id === active.id ? { ...site, powered: true, nocStage: 7 } : site);
        const powered = { ...model, state: "launching", retainedSites: sites, nocStage: 7,
            agent: null, refuel: null, sequenceSeconds: 0, status: SUCCESS_STATUS,
            launchStarted: false, launchCleared: false };
        return { ...powered, checkpoint: freezeCheckpoint(powered) };
    }
    let elapsed = model.sequenceSeconds + seconds;
    if (model.state === "landed") {
        if (elapsed < 0.3) {
            return { ...model, sequenceSeconds: elapsed,
                refuel: model.refuel ? freeze({ ...model.refuel, progress: clamp(elapsed / 0.3, 0, 1) }) : null };
        }
        return { ...model, state: "deploying", sequenceSeconds: elapsed - 0.3,
            refuel: null, agent: { progress: 0 } };
    }
    if (model.state === "deploying") {
        const progress = clamp(elapsed / 0.9, 0, 1);
        if (elapsed < 0.9) return { ...model, sequenceSeconds: elapsed, agent: { progress } };
        return { ...model, state: "powering", sequenceSeconds: elapsed - 0.9, agent: null };
    }
    if (model.state === "powering") {
        const stage = Math.min(7, Math.floor((elapsed + 1e-12) / 0.2));
        if (elapsed < 1.4) {
            const sites = model.retainedSites.map((site) => site.id === model.activeSiteId ? { ...site, nocStage: stage } : site);
            return { ...model, sequenceSeconds: elapsed, retainedSites: sites, nocStage: stage };
        }
        const sites = model.retainedSites.map((site) => site.id === model.activeSiteId ? { ...site, powered: true, nocStage: 7 } : site);
        const powered = { ...model, state: "launching", retainedSites: sites, nocStage: 7,
            refuel: null, sequenceSeconds: 0, status: SUCCESS_STATUS,
            launchStarted: false, launchCleared: false };
        return { ...powered, checkpoint: freezeCheckpoint(powered) };
    }
    return { ...model, sequenceSeconds: elapsed };
}

export function updateRetention(model) {
    if (model.state === "preflight") return model;
    const cameraLeft = cameraLeftForPose(model.pose);
    const chunks = retainedChunkIndexes(cameraLeft);
    const sites = retainedSiteDescriptors(model.retainedSites, model.activeSiteId, model.targetSiteId);
    const retentionKey = `${chunks[0]}:${chunks.at(-1)}|${sites.map((site) => site.id).join(",")}`;
    const terrainLeft = chunks[0] * CHUNK_WIDTH;
    const terrainRight = (chunks.at(-1) + 1) * CHUNK_WIDTH;
    const terrainVertices = retentionKey === model.retentionKey && model.terrainVertices ? model.terrainVertices :
        terrainVerticesForWindow(model.seed, sites, terrainLeft, terrainRight);
    return { ...model, retainedChunks: chunks, retainedSites: sites, retentionKey, terrainVertices };
}

export function createCueState(reducedMotion = false) {
    return { state: reducedMotion ? "settled" : "running", elapsed: 0 };
}

export function advanceCue(cue, seconds) {
    if (cue.state === "settled") return cue;
    const elapsed = cue.elapsed + Math.max(0, seconds);
    return { state: elapsed >= 2.4 ? "settled" : "running", elapsed: Math.min(elapsed, 2.4) };
}

export function settleCue() { return { state: "settled", elapsed: 2.4 }; }

export function createSimulationClock(timestamp = null) {
    return { timestamp, originTimestamp: timestamp, accumulator: 0, cursor: 0, sequence: 0, queue: [], input: { ...ZERO } };
}

export function enqueueInputEdge(clock, edge) {
    const queued = { ...edge, left: clamp(edge.left, 0, 1), right: clamp(edge.right, 0, 1), sequence: clock.sequence };
    const queue = clock.queue.length >= 64 ? [{ ...queued, snapshot: true }] :
        [...clock.queue, queued].sort((a, b) => a.timestamp - b.timestamp || a.sequence - b.sequence);
    return { ...clock, sequence: clock.sequence + 1, queue };
}

export function removeQueuedInputEdges(clock, token) {
    return { ...clock, queue: clock.queue.filter((edge) => edge.token !== token) };
}

export function clearSimulationInput(clock, timestamp = clock.timestamp ?? 0) {
    return enqueueInputEdge({ ...clock, queue: [] }, { timestamp, left: 0, right: 0 });
}

export function resetSimulationAccumulator(clock, timestamp = clock.timestamp) {
    return { ...clock, timestamp, originTimestamp: timestamp === null ? null : timestamp - clock.cursor * STEP_MILLISECONDS, accumulator: 0 };
}

export function advanceSimulation(clock, model, timestamp, options = {}) {
    if (clock.timestamp === null) return { clock: { ...clock, timestamp, originTimestamp: timestamp }, model, steps: 0, discarded: false };
    const frameSeconds = (timestamp - clock.timestamp) / 1000;
    if (frameSeconds < 0 || frameSeconds > MAX_FRAME_SECONDS) return { clock: resetSimulationAccumulator(clock, timestamp), model, steps: 0, discarded: true };
    let accumulator = clock.accumulator + frameSeconds;
    let cursor = clock.cursor;
    let queue = clock.queue;
    let input = clock.input;
    let current = model;
    let steps = 0;
    while (accumulator + 1e-12 >= STEP_SECONDS && steps < MAX_CATCH_UP_STEPS) {
        const stepEnd = clock.originTimestamp + (cursor + 1) * STEP_MILLISECONDS;
        const ready = queue.filter((edge) => edge.timestamp <= stepEnd + 1e-9);
        if (ready.length) { input = { left: ready.at(-1).left, right: ready.at(-1).right }; queue = queue.slice(ready.length); }
        const previousState = current.state;
        current = stepFlight(current, input, options);
        accumulator -= STEP_SECONDS;
        cursor += 1;
        steps += 1;
        if (previousState === "flying" && current.state === "launching" && !current.launchStarted) {
            input = { ...ZERO }; queue = [];
        }
        if (!["flying", "launching"].includes(current.state)) { input = { ...ZERO }; queue = []; break; }
    }
    return { clock: { ...clock, timestamp, accumulator: Math.abs(accumulator) < 1e-12 ? 0 : accumulator,
        cursor, queue, input }, model: updateRetention(current), steps, discarded: false };
}
