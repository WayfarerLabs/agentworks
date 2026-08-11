import {
    cameraLeftForPose,
    CHUNK_WIDTH,
    createFirstSite,
    instantiateTemplateSite,
    mixUint32,
    normalizeSeed,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    sampleUnit,
    selectTemplate,
    siteFoundationBottom,
    STATIC_WORLD_SEED,
    terrainHeightAt,
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
export const TURNING_TOTAL = 1.2;
export const MAX_THRUST_VECTOR = 18;
export const ANGULAR_ASSIST_DIFFERENTIAL = 0.12;
export const ANGULAR_ASSIST_FULL_SPEED = 15;
export const MAX_PLAYABLE_Y = 56;
export const MAX_LANDING_HORIZONTAL_SPEED = 1.6;
export const MAX_LANDING_DESCENT_SPEED = 2.5;
export const MAX_LANDING_ANGLE = 10;
export const MAX_LANDING_ANGULAR_SPEED = 15;
export const COLLISION_MARGIN = 0.02;

export const FAILURE_STATUS = "Landing unsuccessful. Press R to restart or Escape to exit.";
export const GENERATION_ERROR_STATUS = "Mission generation failed. Use Exit mission to start a new run.";
export const SUCCESS_STATUS = "Agent deployed. Mission continues.";

export const REFERENCE_COMMANDS = Object.freeze([
    Object.freeze([0, 0]),
    Object.freeze([0.72, 0.72]),
    Object.freeze([0, 0.375]),
    Object.freeze([0.375, 0]),
    Object.freeze([0.4125, 0.7875]),
    Object.freeze([0.7875, 0.4125]),
    Object.freeze([0, 0]),
    Object.freeze([0.72, 0.72]),
]);

const ROUTES = [
    ["route-78-flat",78,0,[[4.8,-0.65],[39,-0.65],[73.2,-0.65]],8.05,923803459,[[1,90],[3,209],[2,206],[1,23],[2,292],[3,291],[1,56],[3,200],[2,200],[0,441],[1,136]],2144,8.028749999999738,79.04459382379311,0.07287586878902372,-1.296994012354091,-1.494553028125373,2.6106823892487228,0.14845993943307867,2142,79.0598999954952,0.09028817708840904,-1.3059059334637533,-1.6547104441557619],
    ["route-81-rise",81,1.6,[[4.8,-0.65],[40.5,-0.65],[76.2,-0.65]],8.65,4114519305,[[1,90],[3,230],[2,231],[2,285],[3,283],[1,127],[3,177],[2,176],[0,535],[1,143],[0,69]],2346,8.638749999999751,81.17653823536064,1.674198105495886,-1.4499915931945246,-1.3115973405240193,2.6579930147863706,-0.06064485752957691,2274,82.04095683700136,1.9330190104055316,-1.466381740363527,0.12825822977920567],
    ["route-84-fall",84,-0.8,[[4.8,-0.65],[42,-0.65],[79.2,-0.65]],8.15,2489108368,[[1,90],[3,216],[2,220],[2,281],[3,280],[1,55],[3,204],[2,205],[0,570],[1,166],[0,30]],2317,8.12574999999973,83.52765562743207,-0.6823407769016312,1.3272273268625225,-0.5357033346134922,4.217185031982581,-0.4862996814707733,2285,83.1767540954956,-0.6414773938683115,1.3096728196644345,0.026504714298839535],
    ["route-87-rise",87,0.8,[[4.8,-0.65],[43.5,-0.65],[82.2,-0.65]],6.9,4214969873,[[1,90],[3,220],[2,222],[2,313],[3,315],[3,229],[2,227],[0,75],[1,87],[0,17]],1795,6.892749999999758,89.79798234367756,0.9119066425993931,-0.050284385798704964,-1.7488314466062156,4.010651564623231,0.31399163581085,1775,89.80651443265766,1.1659760957063663,-0.07685578489088285,-1.632681130159711],
    ["route-90-fall",90,-1.6,[[4.8,-0.65],[45,-0.65],[85.2,-0.65]],8.55,1263542395,[[1,90],[3,235],[2,234],[2,292],[3,295],[1,129],[3,169],[2,165],[0,409],[1,130],[0,95]],2243,8.531749999999747,92.53436411733836,-1.3997502763281917,-0.4748346930720241,-1.318307723748483,7.189810733738341,0.7492965009489674,2146,92.91483375264889,-1.2960493811831821,-0.5076298298627254,0.8014009987074923],
    ["route-93-flat",93,0,[[4.8,-0.65],[46.5,-0.65],[88.2,-0.65]],6.95,2196063131,[[1,90],[3,224],[2,222],[2,318],[3,320],[1,3],[3,213],[2,212],[0,18],[1,93],[0,71]],1784,6.947624999999757,95.3098401363413,0.1136137727865326,-1.1366806363269895,-1.9812211219181608,-4.071975024861786,0.7505136528519546,1710,96.00714806617961,0.7740482627147528,-1.1028321324849137,-0.5677482434139624],
    ["route-96-fall",96,-0.8,[[4.8,-0.65],[48,-0.65],[91.2,-0.65]],7.25,2745118013,[[1,90],[3,227],[2,228],[2,305],[3,305],[1,36],[3,210],[2,210],[0,103],[1,90],[0,42]],1846,7.232624999999748,97.65771517089763,-0.7157549160169445,0.2762672730053483,-1.381915008270586,3.018282400376961,-0.12744214803325146,1802,97.55655601705077,-0.4060118360379878,0.26056825430977165,-0.5764811082711578],
    ["route-99-rise",99,0.8,[[4.8,-0.65],[49.5,-0.65],[94.2,-0.65]],8.05,1532869400,[[1,90],[3,239],[2,239],[2,293],[3,292],[1,133],[3,171],[2,171],[0,285],[1,81]],1994,8.038624999999728,101.62169615266731,0.8045559072979263,-1.4611030284593036,-0.5181765490091961,0.1631699616793867,-0.08003091181803634,1991,101.64606771526877,0.8136943119797628,-1.4617905064783077,-0.7079221455016917],
    ["route-102-flat",102,0,[[4.8,-0.65],[51,-0.65],[97.2,-0.65]],7.25,2735733026,[[1,90],[3,236],[2,239],[2,298],[3,297],[1,94],[3,201],[2,201],[0,35],[1,35],[0,13]],1739,7.227999999999749,101.04340045276406,0.23461017311679824,-0.9564614706226314,-1.9004685166351518,8.43177853046609,-0.5016557175782874,1724,101.16110852551178,0.44778369246206906,-0.9936759203581832,-1.7807846229980606],
];
const FAILURE_LITERALS = [
    [8,8.000000000000004,2.608930830353529,0.1499061417548924],
    [8.6,8.599999999999975,2.6941493143406205,-0.06170134210926019],
    [8.1,8.100000000000005,4.3457961455543455,-0.4919130226648554],
    [6.8500000000000005,6.849999999999999,3.9582873960259803,0.3200319112625077],
    [8.5,8.499999999999963,6.589470105893042,0.7599760438169482],
    [6.9,6.899999999999999,-4.532547787041267,0.7666125987035216],
    [7.2,7.199999999999983,3.064954925725715,-0.12930891267000144],
    [8,7.999999999999937,0.16450757772918223,-0.08098696516765456],
    [7.2,7.199999999999963,8.493483819708104,-0.5079560815831491],
];

export const ROUTE_DIGESTS = Object.freeze({
    geometryDigest: "a45465787699a9b737b22bb32e0f40ae50913ce14cc3c6c2aeb9300f287ed8d8",
    outputDigest: "e0c672669bb089a6b33501dbfccb1c834e342982aa0125236933e7531e052240",
    physicsDigest: "d54c0ecbd0f62d48cea3ca4a506f3287eaa42b8f793632212361e2ffaf5c9039",
    worldDigest: "9ab22205ef9fbdad86112d1d411b2836ce15f24f234029f441cc52167bd69d73",
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
        templateId, centerDelta, deckDelta, clearanceKnots, combinationsEvaluated: 81,
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

export function mixDigitalInput(held) {
    const collective = Boolean(held.Space || held.ArrowUp);
    const left = Boolean(held.ArrowLeft || held.KeyH);
    const right = Boolean(held.ArrowRight || held.KeyL);
    const steer = left === right ? 0 : left ? -1 : 1;
    if (collective) {
        if (steer < 0) return { left: 0.4125, right: 0.7875 };
        if (steer > 0) return { left: 0.7875, right: 0.4125 };
        return { left: 0.72, right: 0.72 };
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
    const base = 0.72 - 0.12 * Math.abs(steer);
    const halfDifference = 0.1875 * steer;
    return { left: base + halfDifference, right: base - halfDifference };
}

export function pointerEngineRequests(displacement, sceneWidth) {
    const deadZone = Math.max(10, sceneWidth * 0.01);
    const fullBiasDistance = Math.max(56, sceneWidth * 0.18);
    const magnitude = clamp((Math.abs(displacement) - deadZone) / (fullBiasDistance - deadZone), 0, 1);
    const bias = Math.sign(displacement) * magnitude;
    const base = 0.72 - 0.12 * Math.abs(bias);
    return { left: base + 0.1875 * bias, right: base - 0.1875 * bias };
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

export function nextAwardRatio(current) {
    const floor = 1 + Number.EPSILON;
    if (current <= floor) return floor;
    const raw = 1 + (current - 1) * 0.82;
    return Math.max(floor, Math.min(raw, current - Number.EPSILON));
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
    return { state: "preflight", pose: initialPose(), fuel: 0, commanded: { ...ZERO }, status: "" };
}

export function createRun({ seed, reducedMotion = false } = {}) {
    const runSeed = normalizeSeed(seed);
    const firstSite = createFirstSite(runSeed);
    return {
        state: "flying", seed: runSeed, reducedMotion, missionSeconds: 0, completedSites: 0,
        awardRatio: 3, pose: initialPose(), commanded: { ...ZERO }, fuel: 30,
        generatorCursor: 1, retainedChunks: retainedChunkIndexes(0), retainedSites: [firstSite],
        activeSiteId: null, targetSiteId: 0, targetRouteProof: null, touchdownPose: null,
        sequenceSeconds: 0, agent: null, nocStage: 0, checkpoint: null, failureCause: null,
        crashOrdinal: 0, crash: null, status: "Mission underway.", launchCleared: false,
    };
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
    let vertices = model.terrainVertices;
    if (!vertices) {
        const first = Math.floor((((bounds.left + bounds.right) / 2) - 10) / 4);
        vertices = Array.from({ length: 7 }, (_, index) => {
            const x = (first + index) * 4;
            return [x, terrainHeightAt(model.seed, x)];
        });
    }
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
        const padBase = site.platformTop - 0.8;
        features.push({ cause: "riser", priority: 2,
            polygon: rectangle(site.platformLeft, site.platformRight, padBase, site.platformBottom) });
        const buildingLeft = site.platformRight + 2;
        const buildingRight = buildingLeft + 7;
        const foundationBottom = site.foundationBottom ?? padBase;
        const roof = site.platformTop + 7.2;
        features.push({ cause: "noc", priority: 1,
            polygon: rectangle(buildingLeft, buildingRight, foundationBottom, roof) });
        features.push({ cause: "mast", priority: 1,
            polygon: rectangle(buildingLeft + 3.25, buildingLeft + 3.75, roof, roof + 3.2) });
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
    const originSite = freeze({ id: 0, center: 0, platformLeft: -4.8, platformRight: 4.8,
        platformTop: 0, platformBottom: -0.35, canCollected: true, powered: true, nocStage: 4 });
    return { seed: STATIC_WORLD_SEED, originSite,
        targetSite: instantiateTemplateSite(STATIC_WORLD_SEED, 1, originSite, template) };
}

function replayTemplate(template, fuel, suppliedContext = null) {
    const context = routeContext(template, suppliedContext);
    const { originSite, targetSite } = context;
    let pose = context.pose ?? uprightPose(originSite);
    let reserve = fuel;
    let step = 0;
    let launchCleared = false;
    const rawSites = [originSite, targetSite];
    const terrainVertices = context.terrainVertices ?? terrainVerticesForWindow(context.seed, rawSites,
        originSite.center - 20, targetSite.center + 20);
    const retainedSites = rawSites.map((site) => ({ ...site,
        foundationBottom: siteFoundationBottom(terrainVertices, site) }));
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
    return { ...model, state: "generation-error", commanded: { ...ZERO }, status: GENERATION_ERROR_STATUS };
}

function provisionalProofContext(model, originSite, targetSite, contactPose) {
    const poweredOrigin = { ...originSite, canCollected: true, powered: true, nocStage: 5 };
    const retainedSites = model.retainedSites.filter((site) => site.id !== originSite.id)
        .concat(poweredOrigin, targetSite).sort((left, right) => left.id - right.id);
    return freeze({ seed: model.seed, completedSites: model.completedSites + 1,
        awardRatio: nextAwardRatio(model.awardRatio), generatorCursor: model.generatorCursor + 1,
        pose: checkpointPoseForContact(originSite, contactPose), fuel: null, activeSiteId: originSite.id,
        targetSiteId: targetSite.id, retainedSites, originSite: poweredOrigin, targetSite });
}

function prepareService(model, contactPose) {
    const contacted = siteById(model, model.targetSiteId);
    try {
        const template = selectTemplate(model.seed, model.generatorCursor, contacted, REFERENCE_TEMPLATES);
        const nextSite = instantiateTemplateSite(model.seed, model.generatorCursor, contacted, template);
        const serviced = { ...contacted, canCollected: true };
        const proof = proveTemplate(template, provisionalProofContext(model, serviced, nextSite, contactPose));
        const award = proof.demonstratedMinimum * model.awardRatio;
        const sites = model.retainedSites.filter((site) => site.id !== contacted.id).concat(serviced, nextSite).sort((a, b) => a.id - b.id);
        return {
            ...model, state: "landed", pose: checkpointPoseForContact(contacted, contactPose), commanded: { ...ZERO },
            fuel: model.fuel + award, completedSites: model.completedSites + 1,
            awardRatio: nextAwardRatio(model.awardRatio), generatorCursor: model.generatorCursor + 1,
            activeSiteId: contacted.id, targetSiteId: nextSite.id, targetRouteProof: proof,
            retainedSites: sites, touchdownPose: checkpointPoseForContact(contacted, contactPose), sequenceSeconds: 0,
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
            crashOrdinal: ordinal, crash: null, status: FAILURE_STATUS };
    }
    return { ...model, state: "crashing", pose, commanded: { ...ZERO }, failureCause: cause,
        crashOrdinal: ordinal, sequenceSeconds: 0, crash: freeze({ pose, fragments: crashFragments(model, pose, ordinal) }), status: "" };
}

export function stepFlight(model, requested, options = {}) {
    if (model.state !== "flying" && model.state !== "launching") return model;
    const request = model.state === "launching" ? { left: 0.72, right: 0.72 } : requested;
    const previous = model.pose;
    const result = integratePose(previous, request, model.fuel, options.seconds ?? STEP_SECONDS);
    let stepped = { ...model, pose: result.pose, fuel: result.thrust.fuel,
        commanded: { left: result.thrust.left, right: result.thrust.right,
            vectorAngle: result.thrust.vectorAngle }, missionSeconds: model.missionSeconds + (options.seconds ?? STEP_SECONDS) };
    if (model.state === "launching") {
        const active = siteById(model, model.activeSiteId);
        const ignoreTopSiteId = !model.launchCleared && result.pose.vy > 0 ? active?.id ?? null : null;
        const contact = classifySweptContact(model, previous, result.pose, { ignoreTopSiteId });
        if (contact) return beginCrash(stepped, contact.cause, contact.pose);
        const feet = [transformLocalPoint(result.pose, -1.6, 0), transformLocalPoint(result.pose, 1.6, 0)];
        const cleared = model.launchCleared || (active && feet.every((foot) => foot.y > active.platformTop + 0.05));
        stepped = { ...stepped, launchCleared: cleared };
        if (stepped.sequenceSeconds + STEP_SECONDS + 1e-12 >= 0.75) {
            return { ...stepped, state: "flying", sequenceSeconds: 0, launchCleared: false, status: SUCCESS_STATUS };
        }
        return { ...stepped, sequenceSeconds: stepped.sequenceSeconds + STEP_SECONDS };
    }
    const contact = classifySweptContact(model, previous, result.pose);
    if (contact?.kind === "safe") {
        const serviced = prepareService(stepped, contact.pose);
        return model.reducedMotion ? advanceMissionSequence(serviced, 3.1, true) : serviced;
    }
    const active = siteById(model, model.activeSiteId);
    const target = siteById(model, model.targetSiteId);
    const lowerBound = active ? active.center - 45 : -5;
    const upperBound = target ? target.center + 65 : 101;
    if (contact || result.pose.x < lowerBound || result.pose.x > upperBound || result.pose.y > MAX_PLAYABLE_Y) {
        return beginCrash(stepped, contact?.cause ?? "bounds", contact?.pose ?? result.pose);
    }
    return stepped;
}

function freezeCheckpoint(model) {
    return freeze({ seed: model.seed, completedSites: model.completedSites, awardRatio: model.awardRatio,
        generatorCursor: model.generatorCursor, pose: { ...model.touchdownPose }, fuel: model.fuel,
        activeSiteId: model.activeSiteId, targetSiteId: model.targetSiteId, targetRouteProof: model.targetRouteProof,
        retainedChunks: [...model.retainedChunks], retainedSites: model.retainedSites.map((site) => ({ ...site })) });
}

function restoreCheckpoint(model) {
    if (!model.checkpoint) return { ...createRun({ seed: model.seed, reducedMotion: model.reducedMotion }), crashOrdinal: model.crashOrdinal };
    const checkpoint = structuredClone(model.checkpoint);
    return { ...model, ...checkpoint, state: "launching", commanded: { ...ZERO }, sequenceSeconds: 0,
        failureCause: null, crash: null, status: SUCCESS_STATUS, launchCleared: false };
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
        return elapsed >= 0.6 ? { ...model, state: "failed", sequenceSeconds: 0, crash: null, status: FAILURE_STATUS }
            : { ...model, sequenceSeconds: elapsed };
    }
    if (reducedMotion && ["landed", "deploying", "powering"].includes(model.state)) {
        const active = siteById(model, model.activeSiteId);
        const sites = model.retainedSites.map((site) => site.id === active.id ? { ...site, powered: true, nocStage: 5 } : site);
        const powered = { ...model, state: "launching", retainedSites: sites, nocStage: 5,
            agent: null, sequenceSeconds: 0, status: SUCCESS_STATUS, launchCleared: false };
        return { ...powered, checkpoint: freezeCheckpoint(powered) };
    }
    let elapsed = model.sequenceSeconds + seconds;
    if (model.state === "landed" && elapsed >= 0.3) {
        return { ...model, state: "deploying", sequenceSeconds: elapsed - 0.3, agent: { progress: 0 } };
    }
    if (model.state === "deploying") {
        const progress = clamp(elapsed / 1.8, 0, 1);
        if (elapsed < 1.8) return { ...model, sequenceSeconds: elapsed, agent: { progress } };
        return { ...model, state: "powering", sequenceSeconds: elapsed - 1.8, agent: null };
    }
    if (model.state === "powering") {
        const stage = Math.min(5, Math.floor((elapsed + 1e-12) / 0.2));
        if (elapsed < 1) {
            const sites = model.retainedSites.map((site) => site.id === model.activeSiteId ? { ...site, nocStage: stage } : site);
            return { ...model, sequenceSeconds: elapsed, retainedSites: sites, nocStage: stage };
        }
        const sites = model.retainedSites.map((site) => site.id === model.activeSiteId ? { ...site, powered: true, nocStage: 5 } : site);
        const powered = { ...model, state: "launching", retainedSites: sites, nocStage: 5,
            sequenceSeconds: 0, status: SUCCESS_STATUS, launchCleared: false };
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
    const terrainLeft = Math.min(chunks[0] * CHUNK_WIDTH, ...sites.map((site) => site.center - CHUNK_WIDTH));
    const terrainRight = Math.max((chunks.at(-1) + 1) * CHUNK_WIDTH,
        ...sites.map((site) => site.center + CHUNK_WIDTH));
    const terrainVertices = retentionKey === model.retentionKey && model.terrainVertices ? model.terrainVertices :
        terrainVerticesForWindow(model.seed, sites, terrainLeft, terrainRight);
    const sitesWithFoundations = sites.map((site) => Object.freeze({ ...site,
        foundationBottom: siteFoundationBottom(terrainVertices, site) }));
    return { ...model, retainedChunks: chunks, retainedSites: sitesWithFoundations, retentionKey, terrainVertices };
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
        current = stepFlight(current, input, options);
        accumulator -= STEP_SECONDS;
        cursor += 1;
        steps += 1;
        if (!["flying", "launching"].includes(current.state)) { input = { ...ZERO }; queue = []; break; }
    }
    return { clock: { ...clock, timestamp, accumulator: Math.abs(accumulator) < 1e-12 ? 0 : accumulator,
        cursor, queue, input }, model: updateRetention(current), steps, discarded: false };
}
