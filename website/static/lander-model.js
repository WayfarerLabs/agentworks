import {
    cameraLeftForPose,
    createFirstSite,
    instantiateTemplateSite,
    mixUint32,
    normalizeSeed,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    sampleUnit,
    selectTemplate,
    STATIC_WORLD_SEED,
    terrainHeightAt,
    terrainVerticesForWindow,
} from "./lander-world.js";

export const STEP_SECONDS = 1 / 120;
export const MAX_FRAME_SECONDS = 0.1;
export const MAX_CATCH_UP_STEPS = 12;
export const GRAVITY = 3;
export const ENGINE_ACCELERATION = 8.4;
export const TORQUE_ACCELERATION = 70;
export const FUEL_FLOW = 1;
export const FUEL_QUANTUM = 0.05;
export const MAX_PLAYABLE_Y = 56;
export const MAX_LANDING_HORIZONTAL_SPEED = 1.4;
export const MAX_LANDING_DESCENT_SPEED = 2.2;
export const MAX_LANDING_ANGLE = 8;
export const MAX_LANDING_ANGULAR_SPEED = 12;
export const COLLISION_MARGIN = 0.02;

export const FAILURE_STATUS = "Landing unsuccessful. Press R to restart or Escape to exit.";
export const GENERATION_ERROR_STATUS = "Mission generation failed. Use Exit mission to start a new run.";
export const SUCCESS_STATUS = "Agent deployed. Mission continues.";

export const REFERENCE_COMMANDS = Object.freeze([
    Object.freeze([0, 0]),
    Object.freeze([0.72, 0.72]),
    Object.freeze([0, 0.45]),
    Object.freeze([0.45, 0]),
    Object.freeze([0.72, 1]),
    Object.freeze([1, 0.72]),
    Object.freeze([0.45, 0.45]),
    Object.freeze([1, 1]),
]);

const ROUTES = [
    ["route-78-flat",78,0,[[4.8,-0.65],[39,-0.65],[73.2,-0.65]],8.25,179067976,[[1,90],[3,200],[2,200],[1,20],[2,274],[3,274],[1,44],[3,189],[2,190],[0,362],[1,118]],1961,8.240250000000083,77.7263339065049,0.010064611143606045,-0.35013822715732584,-2.021594243536191,0.36053161621327945,-0.26249999999999035,1958,77.73271551523135,0.04735678880958487,-0.35176655442395444,-2.2229535179362707],
    ["route-81-rise",81,1.6,[[4.8,-0.65],[40.5,-0.65],[76.2,-0.65]],8.75,1875239339,[[1,90],[3,202],[2,202],[1,20],[2,262],[3,262],[1,79],[3,165],[2,164],[0,597],[1,146]],2189,8.733750000000082,81.46176696617067,1.607768874554987,0.0291235543158249,-1.7755267163904782,0.27829582213621507,0.26250000000000967,2187,81.46146758849471,1.6251824224337825,0.028139806261924763,-1.9499721583302025],
    ["route-84-fall",84,-0.8,[[4.8,-0.65],[42,-0.65],[79.2,-0.65]],8.450000000000001,2185442569,[[1,90],[3,189],[2,189],[1,35],[2,272],[3,272],[1,20],[3,195],[2,194],[0,472],[1,148]],2076,8.432250000000092,83.84184336983078,-0.776669796259028,0.6648482426313611,-1.8880694420343425,0.8355849456709166,0.26250000000000967,2074,83.83086743256571,-0.7454588478196159,0.6609437944802974,-2.107553170786723],
    ["route-87-rise",87,0.8,[[4.8,-0.65],[43.5,-0.65],[82.2,-0.65]],8,2725764839,[[1,90],[3,205],[2,205],[1,20],[2,290],[3,290],[1,20],[3,204],[2,203],[1,98],[0,78]],1703,7.9747500000000935,87.37147575289124,0.8173063604438984,-0.22427215355164115,-1.2510453351082702,-0.6198197937048917,0.26250000000000967,1623,87.52033263327817,0.9959263874973471,-0.2213945662053693,0.5324197953791863],
    ["route-90-fall",90,-1.6,[[4.8,-0.65],[45,-0.65],[85.2,-0.65]],8.950000000000001,1915623439,[[1,90],[3,211],[2,211],[1,23],[2,271],[3,271],[1,92],[3,171],[2,170],[0,364],[1,131],[0,51]],2056,8.925750000000104,90.39617230208658,-1.5470614107578358,0.09983235175511208,-0.8863612416793984,1.8961208343440603,0.26250000000000967,2003,90.3522699830073,-1.441221843984177,0.09310802670949096,0.21739569898607944],
    ["route-93-flat",93,0,[[4.8,-0.65],[46.5,-0.65],[88.2,-0.65]],7.65,250511621,[[1,90],[3,208],[2,209],[1,20],[2,284],[3,284],[1,34],[3,190],[2,189],[0,65],[1,66]],1639,7.635000000000097,89.8577042899191,0.13994947378347053,-0.4903117355121642,-2.1656702951540683,-5.018125000001646,9.658940314238862e-15,1637,89.86358003699574,0.1650096439683111,-0.46966683213351734,-2.36516584285813],
    ["route-96-fall",96,-0.8,[[4.8,-0.65],[48,-0.65],[91.2,-0.65]],8.200000000000001,1877669739,[[1,90],[3,219],[2,219],[1,20],[2,276],[3,276],[1,102],[3,168],[2,168],[0,140],[1,55]],1733,8.176500000000079,96.00765307776732,-0.7994520937865548,-0.2501129108406806,-1.205352472242873,0.019687499998553903,9.658940314238862e-15,1731,96.0097652790254,-0.789447499675277,-0.2501552278282772,-1.3031702872389384],
    ["route-99-rise",99,0.8,[[4.8,-0.65],[49.5,-0.65],[94.2,-0.65]],8.700000000000001,874838527,[[1,90],[3,207],[2,207],[1,42],[2,273],[3,273],[1,93],[3,180],[2,179],[0,264],[1,86]],1894,8.678250000000107,98.99636535293325,0.865303954524589,-0.09502947367377342,-1.9503707769743535,2.339180908197932,0.26250000000000967,1892,98.99741823413149,0.8868561749182591,-0.10199011896417977,-2.0874890035909046],
    ["route-102-flat",102,0,[[4.8,-0.65],[51,-0.65],[97.2,-0.65]],8.25,2841405082,[[1,90],[3,209],[2,209],[1,38],[2,279],[3,279],[1,70],[3,183],[2,184],[0,93],[1,68]],1702,8.228250000000095,102.06788448406428,0.05085559595482135,0.012973123945688402,-1.293307622839421,-1.8214633178716895,-0.26249999999999035,1700,102.06770154310719,0.06882788295895807,0.019443288475113202,-1.4554110107722478],
];
const FAILURE_LITERALS = [
    [8.2,8.19999999999996,0.3653125000023465,-0.26249999999999035],
    [8.7,8.699999999999912,0.27562499999271495,0.26250000000000967],
    [8.4,8.399999999999944,0.8312499999922238,0.26250000000000967],
    [7.95,7.949999999999993,-0.794062500003065,0.26250000000000967],
    [8.9,8.899999999999926,1.780624999993961,0.26250000000000967],
    [7.6000000000000005,7.600000000000018,-5.018125000001646,9.658940314238862e-15],
    [8.15,8.149999999999988,0.019687499998553903,9.658940314238862e-15],
    [8.65,8.649999999999984,2.336249999994834,0.26250000000000967],
    [8.2,8.200000000000008,-1.8178125000006276,-0.26249999999999035],
];

export const ROUTE_DIGESTS = Object.freeze({
    geometryDigest: "a45465787699a9b737b22bb32e0f40ae50913ce14cc3c6c2aeb9300f287ed8d8",
    outputDigest: "a424370d4d928b814c8aeb17237b98cbefdc6d2732277697283864c938989c1e",
    physicsDigest: "390d39bcacade9ebf38e6c8715a9f09bd6aeae4dea9a9e426c6d2f5707499ec1",
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
        templateId, centerDelta, deckDelta, clearanceKnots, demonstratedMinimum, scheduleDigest, runs,
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
const ZERO = Object.freeze({ left: 0, right: 0 });

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
    const collective = held.Space || held.ArrowUp ? 0.72 : 0;
    const leftBias = held.ArrowLeft || held.KeyH ? 0.45 : 0;
    const rightBias = held.ArrowRight || held.KeyL ? 0.45 : 0;
    return {
        left: clamp(collective + rightBias, 0, 1),
        right: clamp(collective + leftBias, 0, 1),
    };
}

export function mixEngineRequests(keyboard, pointer) {
    return { left: Math.max(keyboard.left, pointer.left), right: Math.max(keyboard.right, pointer.right) };
}

export function pointerEngineRequests(displacement, sceneWidth) {
    const deadZone = Math.max(10, sceneWidth * 0.01);
    const fullBiasDistance = Math.max(56, sceneWidth * 0.18);
    const magnitude = clamp((Math.abs(displacement) - deadZone) / (fullBiasDistance - deadZone), 0, 1);
    const bias = Math.sign(displacement) * magnitude;
    return { left: clamp(0.72 + 0.28 * bias, 0, 1), right: clamp(0.72 - 0.28 * bias, 0, 1) };
}

export function effectiveThrust(requested, fuel, seconds = STEP_SECONDS) {
    const left = clamp(requested.left, 0, 1);
    const right = clamp(requested.right, 0, 1);
    const requestedBurn = FUEL_FLOW * (left + right) * seconds;
    const scale = requestedBurn > fuel && requestedBurn > 0 ? fuel / requestedBurn : 1;
    return { left: left * scale, right: right * scale, fuel: Math.max(0, fuel - requestedBurn * scale) };
}

export function integratePose(pose, requested, fuel, seconds = STEP_SECONDS) {
    const thrust = effectiveThrust(requested, fuel, seconds);
    const radians = (pose.angle * Math.PI) / 180;
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
        for (const center of [site.platformLeft + 1.4, site.platformRight - 1.4]) {
            const bottom = site.platformTop - 0.8;
            features.push({ cause: "pylon", priority: 2,
                polygon: rectangle(center - 0.3, center + 0.3, bottom, site.platformBottom) });
        }
        const buildingLeft = site.platformRight + 2;
        const buildingRight = buildingLeft + 7;
        const foundationBottom = Math.min(terrainHeightAt(model.seed, buildingLeft), terrainHeightAt(model.seed, buildingRight));
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
    let pose = uprightPose(originSite);
    let reserve = fuel;
    let step = 0;
    let launchCleared = false;
    const retainedSites = [originSite, targetSite];
    const collisionModel = {
        seed: context.seed, retainedSites, targetSiteId: targetSite.id,
        terrainVertices: context.terrainVertices ?? terrainVerticesForWindow(context.seed, retainedSites,
            originSite.center - 12, targetSite.center + 12),
    };
    const target = targetSite;
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
        pose: uprightPose(originSite, contactPose.x), fuel: null, activeSiteId: originSite.id,
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
            ...model, state: "landed", pose: uprightPose(contacted, contactPose.x), commanded: { ...ZERO },
            fuel: model.fuel + award, completedSites: model.completedSites + 1,
            awardRatio: nextAwardRatio(model.awardRatio), generatorCursor: model.generatorCursor + 1,
            activeSiteId: contacted.id, targetSiteId: nextSite.id, targetRouteProof: proof,
            retainedSites: sites, touchdownPose: uprightPose(contacted, contactPose.x), sequenceSeconds: 0,
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
        commanded: { left: result.thrust.left, right: result.thrust.right }, missionSeconds: model.missionSeconds + (options.seconds ?? STEP_SECONDS) };
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
        const stage = Math.min(5, Math.floor(elapsed / 0.2) + 1);
        if (elapsed < 1) return { ...model, sequenceSeconds: elapsed, nocStage: stage };
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
    const terrainVertices = retentionKey === model.retentionKey && model.terrainVertices ? model.terrainVertices :
        terrainVerticesForWindow(model.seed, sites, chunks[0] * 20, (chunks.at(-1) + 1) * 20);
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
        current = stepFlight(current, input, options);
        accumulator -= STEP_SECONDS;
        cursor += 1;
        steps += 1;
        if (!["flying", "launching"].includes(current.state)) { input = { ...ZERO }; queue = []; break; }
    }
    return { clock: { ...clock, timestamp, accumulator: Math.abs(accumulator) < 1e-12 ? 0 : accumulator,
        cursor, queue, input }, model: updateRetention(current), steps, discarded: false };
}
