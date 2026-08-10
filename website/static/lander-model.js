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
    ["route-78-flat",78,0,[[4.8,-0.65],[39,-0.65],[73.2,-0.65]],8.25,2144885575,[[1,90],[3,200],[2,200],[1,20],[2,274],[3,274],[1,44],[3,189],[2,189],[0,362],[1,118]],1960,8.236500000000081,77.81635462654064,0,-0.16132550005166613,-2.0400613638522276,1.4109374999979991,9.658940314238862e-15,1957,77.81915738522775,0.035815642788041835,-0.16656397706546264,-2.201021022692424],
    ["route-81-rise",81,1.6,[[4.8,-0.65],[40.5,-0.65],[76.2,-0.65]],8.8,2142277263,[[1,90],[3,202],[2,202],[1,20],[2,262],[3,262],[1,79],[3,165],[2,165],[0,597],[1,151]],2195,8.797500000000085,81.22313214146925,1.6,-0.3571484940191654,-1.4011609662143851,-1.3453125000005457,9.658940314238862e-15,2192,81.22946350909285,1.6247718554458646,-0.3498431538373557,-1.6590187792730884],
    ["route-84-fall",84,-0.8,[[4.8,-0.65],[42,-0.65],[79.2,-0.65]],8.55,2534735720,[[1,90],[3,189],[2,189],[1,35],[2,272],[3,272],[1,20],[3,195],[2,195],[0,471],[1,157],[0,8]],2093,8.544000000000096,83.70286133633655,-0.8,0.3456508750859214,-1.3498503744245758,-0.5206250000021555,9.658940314238862e-15,2082,83.673778669356,-0.6940811274803341,0.34900924377718046,-1.4671731413865226],
    ["route-87-rise",87,0.8,[[4.8,-0.65],[43.5,-0.65],[82.2,-0.65]],8.05,2668170190,[[1,90],[3,205],[2,205],[1,20],[2,290],[3,290],[1,20],[3,204],[2,204],[1,100],[0,86]],1714,8.002500000000094,87.33008153729982,0.8,-0.2466460903023193,-1.2970841910551256,-1.0040625000021919,9.658940314238862e-15,1628,87.50644892711453,0.9693146125224776,-0.24627810101080405,0.8270694757215198],
    ["route-90-fall",90,-1.6,[[4.8,-0.65],[45,-0.65],[85.2,-0.65]],8.9,2650405298,[[1,90],[3,211],[2,211],[1,23],[2,271],[3,271],[1,92],[3,171],[2,171],[0,362],[1,127],[0,67]],2067,8.881500000000102,90.18155353749594,-1.6,-0.120953984240069,-1.5272002017182151,0.7021874999984448,9.658940314238862e-15,1998,90.25084606793465,-1.209883781054864,-0.12419670408867468,-0.07322586179573665],
    ["route-93-flat",93,0,[[4.8,-0.65],[46.5,-0.65],[88.2,-0.65]],7.95,4118756405,[[1,90],[3,209],[2,209],[1,20],[2,284],[3,284],[1,34],[3,190],[2,190],[0,64],[1,91],[0,76]],1741,7.942500000000086,93.11203111281411,0,0.437731695217983,-1.658749652139103,-1.914062500001819,9.658940314238862e-15,1662,92.82611074235642,0.45379218514534153,0.449655666355321,-0.05646409890942916],
    ["route-96-fall",96,-0.8,[[4.8,-0.65],[48,-0.65],[91.2,-0.65]],8.25,2222144006,[[1,90],[3,219],[2,219],[1,20],[2,276],[3,276],[1,102],[3,168],[2,168],[0,138],[1,58],[0,10]],1744,8.21250000000008,95.98350674461238,-0.8,-0.24997483169118467,-1.0936723576008238,0.019687499998553903,9.658940314238862e-15,1733,96.00562967512104,-0.7138620102438289,-0.2500109109186034,-0.9331703120334541],
    ["route-99-rise",99,0.8,[[4.8,-0.65],[49.5,-0.65],[94.2,-0.65]],8.75,4183148461,[[1,90],[3,207],[2,207],[1,42],[2,273],[3,273],[1,93],[3,180],[2,180],[0,264],[1,91]],1900,8.74200000000011,98.95316347247866,0.8,-0.1810513427453735,-1.568342902278645,1.5749999999979991,9.658940314238862e-15,1897,98.95658064988923,0.8298488921582128,-0.18867696830427538,-1.7893700851808665],
    ["route-102-flat",102,0,[[4.8,-0.65],[51,-0.65],[97.2,-0.65]],8.3,3649007746,[[1,90],[3,209],[2,209],[1,38],[2,279],[3,279],[1,70],[3,183],[2,183],[0,91],[1,71],[0,18]],1720,8.260500000000095,102.0788892026972,0,0.038326945004470384,-1.438879686216837,-1.4678125000020827,9.658940314238862e-15,1702,102.07325610778827,0.18069713376632007,0.040586219656034796,-1.0846526729402146],
];

export const ROUTE_DIGESTS = Object.freeze({
    geometryDigest: "a45465787699a9b737b22bb32e0f40ae50913ce14cc3c6c2aeb9300f287ed8d8",
    outputDigest: "724957dc0a3d0ba0845eb600db2a6794eb3f1d1ab218d8ea0a4cd73c4e7ae26f",
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

function routeRecord(row) {
    const [templateId, centerDelta, deckDelta, clearanceKnots, demonstratedMinimum, scheduleDigest, runs,
        contactStep, burn, x, y, vx, vy, angle, angularVelocity, exhaustionStep, failureX, failureY,
        failureVx, failureVy] = row;
    return freeze({
        templateId, centerDelta, deckDelta, clearanceKnots, demonstratedMinimum, scheduleDigest, runs,
        success: { contactStep, burn, classification: "safe", pose: { x, y, vx, vy, angle, angularVelocity } },
        smallerFailure: {
            allowance: demonstratedMinimum - FUEL_QUANTUM,
            burn: demonstratedMinimum - FUEL_QUANTUM,
            exhaustionStep,
            pose: { x: failureX, y: failureY, vx: failureVx, vy: failureVy, angle, angularVelocity },
        },
    });
}

export const REFERENCE_TEMPLATES = freeze(ROUTES.map(routeRecord));

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

function targetTopSweptContact(previous, next, target) {
    const radius = Math.hypot(1.6, 6.5);
    function search(leftPose, rightPose, leftTime, rightTime, depth) {
        const leftBounds = hullBounds(leftPose);
        const rightBounds = hullBounds(rightPose);
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
            return { pose: hit, time: hitTime, grazing: false };
        }
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

function unsafeAtPose(model, pose, target) {
    const hull = hullForPose(pose);
    const minimumY = Math.min(...hull.map((point) => point.y));
    let terrain = terrainHeightAt(model.seed, pose.x);
    const vertices = model.terrainVertices;
    if (vertices) {
        for (let index = 1; index < vertices.length; index += 1) {
            const left = vertices[index - 1];
            const right = vertices[index];
            if (pose.x >= left[0] && pose.x <= right[0]) {
                terrain = left[1] + (right[1] - left[1]) * ((pose.x - left[0]) / (right[0] - left[0]));
                break;
            }
        }
    }
    if (minimumY <= terrain + COLLISION_MARGIN && !(target && pose.x >= target.platformLeft && pose.x <= target.platformRight)) {
        return "terrain";
    }
    for (const site of model.retainedSites) {
        const inPlatformX = hull.some((point) => point.x >= site.platformLeft - COLLISION_MARGIN && point.x <= site.platformRight + COLLISION_MARGIN);
        const platformSide = inPlatformX && minimumY <= site.platformBottom + COLLISION_MARGIN;
        if (platformSide) return "platform";
        const buildingLeft = site.platformRight + 2;
        const buildingRight = buildingLeft + 7;
        const buildingTop = site.platformTop + 7.2;
        if (hull.some((point) => point.x >= buildingLeft - COLLISION_MARGIN && point.x <= buildingRight + COLLISION_MARGIN && point.y <= buildingTop + COLLISION_MARGIN)) {
            return "noc";
        }
    }
    return null;
}

export function classifySweptContact(model, previous, next) {
    const target = siteById(model, model.targetSiteId);
    const topContact = target ? targetTopSweptContact(previous, next, target) : null;
    if (topContact) {
        const pose = topContact.pose;
        const feet = [transformLocalPoint(pose, -1.6, 0), transformLocalPoint(pose, 1.6, 0)];
        const safe = !topContact.grazing && pose.vy <= 0 &&
            feet.every((foot) => foot.x >= target.platformLeft && foot.x <= target.platformRight) &&
            Math.abs(pose.vx) <= MAX_LANDING_HORIZONTAL_SPEED && Math.abs(pose.vy) <= MAX_LANDING_DESCENT_SPEED &&
            Math.abs(normalizeDegrees(pose.angle)) <= MAX_LANDING_ANGLE &&
            Math.abs(pose.angularVelocity) <= MAX_LANDING_ANGULAR_SPEED;
        return { kind: safe ? "safe" : "unsafe", cause: safe ? "target" : topContact.grazing ? "grazing" : "target-envelope", pose };
    }
    const radius = Math.hypot(1.6, 6.5);
    const travel = Math.hypot(next.x - previous.x, next.y - previous.y) + radius * Math.abs(normalizeDegrees(next.angle - previous.angle) * Math.PI / 180);
    const intervals = Math.max(1, Math.ceil(travel / COLLISION_MARGIN));
    if (intervals > 64) return { kind: "unsafe", cause: "overspeed", pose: next };
    for (let index = 0; index <= intervals; index += 1) {
        const pose = interpolatePose(previous, next, index / intervals);
        const cause = unsafeAtPose(model, pose, target);
        if (cause) return { kind: "unsafe", cause, pose };
    }
    return null;
}

function replayTemplate(template, fuel) {
    let pose = { x: 0, y: 0, vx: 0, vy: 0, angle: 0, angularVelocity: 0 };
    let reserve = fuel;
    let step = 0;
    for (const [commandIndex, count] of template.runs) {
        for (let index = 0; index < count; index += 1) {
            const previous = pose;
            const result = integratePose(pose, { left: REFERENCE_COMMANDS[commandIndex][0], right: REFERENCE_COMMANDS[commandIndex][1] }, reserve);
            pose = result.pose;
            reserve = result.thrust.fuel;
            step += 1;
            if (previous.y > template.deckDelta && pose.y <= template.deckDelta && pose.x >= template.centerDelta - 4.8 && pose.x <= template.centerDelta + 4.8) {
                return { kind: "contact", step, pose: interpolatePose(previous, pose, (previous.y - template.deckDelta) / (previous.y - pose.y)) };
            }
            if (reserve === 0) return { kind: "exhausted", step, pose };
        }
    }
    return { kind: "incomplete", step, pose };
}

export function proveTemplate(template) {
    const successful = replayTemplate(template, template.demonstratedMinimum);
    const smaller = replayTemplate(template, template.demonstratedMinimum - FUEL_QUANTUM);
    if (successful.kind !== "contact" || successful.step !== template.success.contactStep ||
        smaller.kind !== "exhausted" || smaller.step !== template.smallerFailure.exhaustionStep) {
        throw new Error(`Route proof mismatch for ${template.templateId}`);
    }
    return freeze({ templateId: template.templateId, demonstratedMinimum: template.demonstratedMinimum,
        quantum: FUEL_QUANTUM, scheduleDigest: template.scheduleDigest, burn: template.success.burn,
        success: template.success, smallerFailure: template.smallerFailure });
}

function generationError(model) {
    return { ...model, state: "generation-error", commanded: { ...ZERO }, status: GENERATION_ERROR_STATUS };
}

function prepareService(model, contactPose) {
    const contacted = siteById(model, model.targetSiteId);
    try {
        const template = selectTemplate(model.seed, model.generatorCursor, contacted, REFERENCE_TEMPLATES);
        const nextSite = instantiateTemplateSite(model.seed, model.generatorCursor, contacted, template);
        const proof = proveTemplate(template);
        const award = proof.demonstratedMinimum * model.awardRatio;
        const serviced = { ...contacted, canCollected: true };
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
        const cleared = model.launchCleared || (active && result.pose.y > active.platformTop + 0.05);
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
    return { ...model, retainedChunks: chunks, retainedSites: sites,
        terrainVertices: terrainVerticesForWindow(model.seed, sites, cameraLeft - 40, cameraLeft + 140) };
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
    const queue = clock.queue.length >= 64 ? [{ timestamp: edge.timestamp, left: queued.left, right: queued.right,
        sequence: clock.sequence, snapshot: true }] : [...clock.queue, queued].sort((a, b) => a.timestamp - b.timestamp || a.sequence - b.sequence);
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
