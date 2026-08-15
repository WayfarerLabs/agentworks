import {
    cameraLeftForPose,
    classifySweptContact,
    classifySweptContactFromBounds,
    COLLISION_MARGIN,
    CHUNK_WIDTH,
    createFirstSite,
    createSiteForIndex,
    hullBounds,
    hullForPose,
    LANDER_HULL,
    MAX_LANDING_ANGLE,
    MAX_LANDING_ANGULAR_SPEED,
    MAX_LANDING_DESCENT_SPEED,
    MAX_LANDING_HORIZONTAL_SPEED,
    MAX_SITE_INDEX,
    mixUint32,
    normalizeDegrees,
    normalizeSeed,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    sampleUnit,
    selectRouteProof,
    siteStructure,
    STATIC_WORLD_SEED,
    terrainVerticesForWindow,
    transformLocalPoint,
    unsafeFeatures,
    worldTermini,
} from "./lander-world.js";
import { REFERENCE_PROOF_CATALOG } from "./lander-route-proofs.generated.js";

export {
    classifySweptContact,
    COLLISION_MARGIN,
    MAX_LANDING_ANGLE,
    MAX_LANDING_ANGULAR_SPEED,
    MAX_LANDING_DESCENT_SPEED,
    MAX_LANDING_HORIZONTAL_SPEED,
    normalizeDegrees,
    transformLocalPoint,
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
export const BASE_ROUTE_ALLOWANCE = 13.4;

export const FAILURE_STATUS = "Crashed!";
export const GENERATION_ERROR_STATUS = "Mission generation failed. Use Exit mission to start a new run.";
export const SUCCESS_STATUS = "Agent Deployed!";

function freeze(value) {
    if (Array.isArray(value)) value.forEach(freeze);
    else if (value && typeof value === "object") Object.values(value).forEach(freeze);
    return Object.freeze(value);
}

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

const STEP_MILLISECONDS = STEP_SECONDS * 1000;
const ZERO = Object.freeze({ left: 0, right: 0, vectorAngle: 0 });
const STRAIGHT_ENGINE_REQUEST = 0.72;
const STRAIGHT_COLLECTIVE_TOTAL = STRAIGHT_ENGINE_REQUEST * 2;

export function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
}

export function plumeForThrust(thrust) {
    const command = clamp(thrust, 0, 1);
    return { scaleY: 0.08 + 0.92 * command, opacity: 0.25 + 0.75 * command };
}

export function collectiveRequestForSteer(steer, turningTotal, turnDifferential) {
    const normalized = clamp(steer, -1, 1);
    const total = STRAIGHT_COLLECTIVE_TOTAL - (STRAIGHT_COLLECTIVE_TOTAL - turningTotal) * Math.abs(normalized);
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
        const rawAssist = ANGULAR_ASSIST_DIFFERENTIAL * clamp(-angularVelocity / ANGULAR_ASSIST_FULL_SPEED, -1, 1);
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
    return {
        left,
        right,
        fuel: exhausts ? 0 : Math.max(0, fuel - requestedBurn),
        vectorAngle: left + right > 0 ? MAX_THRUST_VECTOR * manualSteer : 0,
    };
}

export function integratePose(pose, requested, fuel, seconds = STEP_SECONDS) {
    const thrust = effectiveThrust(requested, fuel, seconds, pose.angularVelocity);
    const radians = ((pose.angle + thrust.vectorAngle) * Math.PI) / 180;
    const total = ENGINE_ACCELERATION * (thrust.left + thrust.right);
    const vx = pose.vx + total * Math.sin(radians) * seconds;
    const vy = pose.vy + (total * Math.cos(radians) - GRAVITY) * seconds;
    const angularVelocity = pose.angularVelocity + TORQUE_ACCELERATION * (thrust.left - thrust.right) * seconds;
    const angularTravel = angularVelocity * seconds;
    return {
        pose: {
            x: pose.x + vx * seconds,
            y: pose.y + vy * seconds,
            vx,
            vy,
            angle: normalizeDegrees(pose.angle + angularTravel),
            angularVelocity,
        },
        thrust,
        angularTravel,
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
    return {
        x: clamp(x, site.platformLeft + 1.6, site.platformRight - 1.6),
        y: site.platformTop,
        vx: 0,
        vy: 0,
        angle: 0,
        angularVelocity: 0,
    };
}

export function checkpointPoseForContact(site, contactPose) {
    if (!Number.isFinite(contactPose?.x)) throw new TypeError("Touchdown pose is required");
    return uprightPose(site);
}

export function createPreflightModel() {
    return {
        state: "preflight",
        pose: initialPose(),
        fuel: 0,
        fuelGaugeReference: 0,
        commanded: { ...ZERO },
        refuel: null,
        status: "",
        launchStarted: false,
        launchCleared: false,
    };
}

export function createRun({ seed, reducedMotion = false } = {}) {
    const runSeed = normalizeSeed(seed);
    const firstSite = createFirstSite(runSeed);
    return updateRetention({
        state: "flying",
        seed: runSeed,
        reducedMotion,
        missionSeconds: 0,
        completedSites: 0,
        refuelRatio: refuelRatioForBase(1),
        pose: initialPose(),
        commanded: { ...ZERO },
        fuel: 15,
        fuelGaugeReference: 30,
        generatorCursor: 1,
        retainedChunks: retainedChunkIndexes(0),
        retainedSites: [firstSite],
        activeSiteId: null,
        targetSiteId: 0,
        targetRouteProof: null,
        touchdownPose: null,
        sequenceSeconds: 0,
        refuel: null,
        agent: null,
        nocStage: 0,
        checkpoint: null,
        failureCause: null,
        crashOrdinal: 0,
        crash: null,
        termini: worldTermini(runSeed),
        status: "Mission underway.",
        launchStarted: false,
        launchCleared: false,
    });
}

export function createFlightModel(options = {}) {
    return createRun(options);
}

function siteById(model, id) {
    return model.retainedSites.find((site) => site.id === id) ?? null;
}

function routeContext(proof, supplied = null) {
    if (supplied) return supplied;
    throw new Error(`Route proof ${proof.pairKey} requires an exact concrete context`);
}

function replayRouteProof(proof, fuel, suppliedContext = null) {
    const firstRun = proof.runs[0];
    const firstRequest = REFERENCE_COMMANDS[firstRun?.[0]];
    if (!firstRequest || firstRun[1] !== 90 || firstRequest[0] + firstRequest[1] <= TURN_DIFFERENTIAL) {
        throw new Error(`Route schedule ${proof.pairKey} must begin with exact [1,90] launch request`);
    }
    const context = routeContext(proof, suppliedContext);
    const { originSite, targetSite } = context;
    let pose = context.pose ?? uprightPose(originSite);
    let reserve = fuel;
    let step = 0;
    let launchCleared = false;
    let maxHullTop = Math.max(...hullForPose(pose).map((point) => point.y));
    let previousBounds = hullBounds(pose);
    const rawSites = [originSite, targetSite];
    const terrainVertices =
        context.terrainVertices ??
        terrainVerticesForWindow(context.seed, rawSites, originSite.center - 20, targetSite.center + 20);
    const retainedSites = rawSites;
    const collisionModel = {
        seed: context.seed,
        retainedSites,
        targetSiteId: targetSite.id,
        terrainAuthority: "context",
        terrainVertices,
    };
    const target = targetSite;
    const ordinaryFeatures = unsafeFeatures(collisionModel, pose, target);
    const launchFeatures = unsafeFeatures(collisionModel, pose, target, originSite.id);
    collisionModel.collisionMaximumTop = Math.max(
        29.2,
        target.platformTop,
        ...ordinaryFeatures.map((feature) => feature.bounds.top),
        ...launchFeatures.map((feature) => feature.bounds.top),
    );
    for (const [commandIndex, count] of proof.runs) {
        for (let index = 0; index < count; index += 1) {
            const previous = pose;
            const result = integratePose(
                pose,
                { left: REFERENCE_COMMANDS[commandIndex][0], right: REFERENCE_COMMANDS[commandIndex][1] },
                reserve,
            );
            pose = result.pose;
            maxHullTop = Math.max(maxHullTop, ...hullForPose(pose).map((point) => point.y));
            const nextBounds = hullBounds(pose);
            reserve = result.thrust.fuel;
            step += 1;
            const ignoreTopSiteId = !launchCleared ? originSite.id : null;
            const contact = classifySweptContactFromBounds(collisionModel, previous, pose, previousBounds, nextBounds, {
                angularTravel: result.angularTravel,
                ignoreTopSiteId,
                features: ignoreTopSiteId === null ? ordinaryFeatures : launchFeatures,
            });
            if (contact) {
                const relative = {
                    ...contact.pose,
                    x: contact.pose.x - originSite.center,
                    y: contact.pose.y - originSite.platformTop,
                };
                return {
                    kind: contact.kind === "safe" ? "contact" : "collision",
                    cause: contact.cause,
                    step,
                    pose: relative,
                    burn: fuel - reserve,
                    maxHullTop,
                };
            }
            if (!launchCleared) {
                const feet = [transformLocalPoint(pose, -1.6, 0), transformLocalPoint(pose, 1.6, 0)];
                launchCleared = feet.every((foot) => foot.y > originSite.platformTop + 0.05);
            }
            previousBounds = nextBounds;
            if (reserve === 0)
                return {
                    kind: "exhausted",
                    step,
                    pose: { ...pose, x: pose.x - originSite.center, y: pose.y - originSite.platformTop },
                    burn: fuel,
                };
        }
    }
    return { kind: "incomplete", step, pose };
}

function samePose(actual, expected) {
    return ["x", "y", "vx", "vy", "angle", "angularVelocity"].every(
        (key) => Math.abs(actual[key] - expected[key]) <= 1e-9,
    );
}

export function proveRouteProof(proof, suppliedContext = null) {
    const successful = replayRouteProof(proof, proof.allowance, suppliedContext);
    if (
        successful.kind !== "contact" ||
        successful.step !== proof.success.contactStep ||
        Math.abs(successful.burn - proof.controllerBurn) > 1e-9 ||
        Math.abs(successful.maxHullTop - proof.maxHullTop) > 1e-9 ||
        !samePose(successful.pose, proof.success.pose)
    ) {
        throw new Error(`Route proof mismatch for ${proof.pairKey}: ${JSON.stringify({ successful })}`);
    }
    return proof;
}

function generationError(model) {
    return {
        ...model,
        state: "generation-error",
        commanded: { ...ZERO },
        refuel: null,
        status: GENERATION_ERROR_STATUS,
    };
}

function provisionalProofContext(model, originSite, targetSite, contactPose) {
    const poweredOrigin = { ...originSite, canCollected: true, powered: true, nocStage: 7 };
    const retainedSites = model.retainedSites
        .filter((site) => site.id !== originSite.id)
        .concat(poweredOrigin, targetSite)
        .sort((left, right) => left.id - right.id);
    return freeze({
        seed: model.seed,
        completedSites: model.completedSites + 1,
        refuelRatio: refuelRatioForBase(model.completedSites + 2),
        generatorCursor: model.generatorCursor + 1,
        pose: checkpointPoseForContact(originSite, contactPose),
        fuel: null,
        activeSiteId: originSite.id,
        targetSiteId: targetSite.id,
        retainedSites,
        originSite: poweredOrigin,
        targetSite,
    });
}

function prepareService(model, contactPose) {
    const contacted = siteById(model, model.targetSiteId);
    try {
        const poweredBaseNumber = model.completedSites + 1;
        const ratio = refuelRatioForBase(poweredBaseNumber);
        if (model.refuelRatio !== ratio) throw new Error("Stored refuel ratio does not match mission progress");
        if (contacted.id === MAX_SITE_INDEX) {
            const serviced = { ...contacted, canCollected: true };
            const award = BASE_ROUTE_ALLOWANCE * ratio;
            const fromLevel = model.fuelGaugeReference > 0 ? clamp(model.fuel / model.fuelGaugeReference, 0, 1) : 0;
            const fuelGaugeReference = model.fuel + award;
            return {
                ...model,
                state: "landed",
                pose: checkpointPoseForContact(contacted, contactPose),
                commanded: { ...ZERO },
                fuel: fuelGaugeReference,
                fuelGaugeReference,
                completedSites: model.completedSites + 1,
                refuelRatio: refuelRatioForBase(poweredBaseNumber + 1),
                generatorCursor: MAX_SITE_INDEX + 1,
                activeSiteId: contacted.id,
                targetSiteId: null,
                targetRouteProof: null,
                retainedSites: model.retainedSites.map((site) => (site.id === contacted.id ? serviced : site)),
                touchdownPose: checkpointPoseForContact(contacted, contactPose),
                sequenceSeconds: 0,
                refuel: model.reducedMotion ? null : freeze({ siteId: contacted.id, fromLevel, progress: 0 }),
                nocStage: 0,
                agent: null,
                status: "Touchdown confirmed. Fuel collected. Deploying agent.",
            };
        }
        const candidate = createSiteForIndex(model.seed, model.generatorCursor);
        const selectedProof = selectRouteProof(contacted, candidate, REFERENCE_PROOF_CATALOG);
        const nextSite = freeze({ ...candidate, pairKey: selectedProof.pairKey, originSiteId: contacted.id });
        const serviced = { ...contacted, canCollected: true };
        const proof = proveRouteProof(selectedProof, provisionalProofContext(model, serviced, nextSite, contactPose));
        const award = proof.allowance * ratio;
        const sites = model.retainedSites
            .filter((site) => site.id !== contacted.id)
            .concat(serviced, nextSite)
            .sort((a, b) => a.id - b.id);
        const fromLevel = model.fuelGaugeReference > 0 ? clamp(model.fuel / model.fuelGaugeReference, 0, 1) : 0;
        const fuelGaugeReference = model.fuel + award;
        return {
            ...model,
            state: "landed",
            pose: checkpointPoseForContact(contacted, contactPose),
            commanded: { ...ZERO },
            fuel: fuelGaugeReference,
            fuelGaugeReference,
            completedSites: model.completedSites + 1,
            refuelRatio: refuelRatioForBase(poweredBaseNumber + 1),
            generatorCursor: model.generatorCursor + 1,
            activeSiteId: contacted.id,
            targetSiteId: nextSite.id,
            targetRouteProof: proof,
            retainedSites: sites,
            touchdownPose: checkpointPoseForContact(contacted, contactPose),
            sequenceSeconds: 0,
            refuel: model.reducedMotion ? null : freeze({ siteId: contacted.id, fromLevel, progress: 0 }),
            nocStage: 0,
            agent: null,
            status: "Touchdown confirmed. Fuel collected. Deploying agent.",
        };
    } catch (error) {
        console.error(error);
        return generationError(model);
    }
}

function crashFragments(model, pose, ordinal) {
    if (model.reducedMotion) return [];
    const colors = ["#292b30", "#d94a1e", "#ff7a00", "#ffe09a"];
    return LANDER_HULL.concat(LANDER_HULL).map(([x, y], index) => {
        const key = mixUint32(
            Math.imul((model.targetSiteId ?? 0) + 1, 0x85ebca6b) ^
                Math.imul(ordinal, 0xc2b2ae35) ^
                Math.imul(index + 1, 0x27d4eb2f),
        );
        const unit = (property) => sampleUnit(model.seed, 5, (key + Math.imul(property, 0x9e3779b9)) >>> 0);
        const origin = transformLocalPoint(pose, x, y);
        return freeze({
            id: index,
            x: origin.x,
            y: origin.y,
            vx: -8 + 16 * unit(0),
            vy: 2 + 9 * unit(1),
            angularVelocity: -240 + 480 * unit(2),
            color: colors[Math.floor(4 * unit(3))],
        });
    });
}

function beginCrash(model, cause, pose) {
    const ordinal = model.crashOrdinal + 1;
    if (model.reducedMotion) {
        return {
            ...model,
            state: "failed",
            pose,
            commanded: { ...ZERO },
            failureCause: cause,
            crashOrdinal: ordinal,
            crash: null,
            refuel: null,
            status: FAILURE_STATUS,
        };
    }
    return {
        ...model,
        state: "crashing",
        pose,
        commanded: { ...ZERO },
        failureCause: cause,
        crashOrdinal: ordinal,
        sequenceSeconds: 0,
        refuel: null,
        crash: freeze({ pose, fragments: crashFragments(model, pose, ordinal) }),
        status: "",
    };
}

export function stepFlight(model, requested, options = {}) {
    if (model.state !== "flying" && model.state !== "launching") return model;
    const rawTotal = requested.left + requested.right;
    const departureThrust =
        model.state === "launching" && !model.launchStarted
            ? effectiveThrust(requested, model.fuel, options.seconds ?? STEP_SECONDS, model.pose.angularVelocity)
            : null;
    if (departureThrust && (rawTotal <= TURN_DIFFERENTIAL || departureThrust.left + departureThrust.right === 0)) {
        return { ...model, commanded: { ...ZERO } };
    }
    const request = requested;
    const previous = model.pose;
    const result = integratePose(previous, request, model.fuel, options.seconds ?? STEP_SECONDS);
    let stepped = {
        ...model,
        pose: result.pose,
        fuel: result.thrust.fuel,
        commanded: { left: result.thrust.left, right: result.thrust.right, vectorAngle: result.thrust.vectorAngle },
        missionSeconds: model.missionSeconds + (options.seconds ?? STEP_SECONDS),
    };
    if (model.state === "launching") {
        stepped = { ...stepped, launchStarted: true, status: "" };
        const active = siteById(model, model.activeSiteId);
        const ignoreTopSiteId = !model.launchCleared ? (active?.id ?? null) : null;
        const contact = classifySweptContact(model, previous, result.pose, {
            angularTravel: result.angularTravel,
            ignoreTopSiteId,
        });
        if (contact) return beginCrash(stepped, contact.cause, contact.pose);
        const feet = [transformLocalPoint(result.pose, -1.6, 0), transformLocalPoint(result.pose, 1.6, 0)];
        const cleared = model.launchCleared || (active && feet.every((foot) => foot.y > active.platformTop + 0.05));
        stepped = { ...stepped, launchCleared: cleared };
        if (cleared) return { ...stepped, state: "flying", launchCleared: true };
        return stepped;
    }
    const contact = classifySweptContact(model, previous, result.pose, { angularTravel: result.angularTravel });
    if (contact?.kind === "safe") {
        const serviced = prepareService(stepped, contact.pose);
        return model.reducedMotion ? advanceMissionSequence(serviced, 3.1, true) : serviced;
    }
    if (contact) return beginCrash(stepped, contact.cause, contact.pose);
    return stepped;
}

function freezeCheckpoint(model) {
    if (model.refuelRatio !== refuelRatioForBase(model.completedSites + 1)) {
        throw new Error("Checkpoint refuel ratio does not match mission progress");
    }
    return freeze({
        seed: model.seed,
        completedSites: model.completedSites,
        refuelRatio: model.refuelRatio,
        generatorCursor: model.generatorCursor,
        pose: { ...model.touchdownPose },
        fuel: model.fuel,
        fuelGaugeReference: model.fuelGaugeReference,
        activeSiteId: model.activeSiteId,
        targetSiteId: model.targetSiteId,
        targetRouteProof: model.targetRouteProof,
        retainedChunks: [...model.retainedChunks],
        retainedSites: model.retainedSites.map((site) => ({ ...site })),
    });
}

function restoreCheckpoint(model) {
    if (!model.checkpoint)
        return {
            ...createRun({ seed: model.seed, reducedMotion: model.reducedMotion }),
            crashOrdinal: model.crashOrdinal,
        };
    const checkpoint = structuredClone(model.checkpoint);
    if (checkpoint.refuelRatio !== refuelRatioForBase(checkpoint.completedSites + 1)) {
        throw new Error("Checkpoint refuel ratio does not match mission progress");
    }
    return {
        ...model,
        ...checkpoint,
        state: "launching",
        commanded: { ...ZERO },
        sequenceSeconds: 0,
        refuel: null,
        failureCause: null,
        crash: null,
        status: SUCCESS_STATUS,
        launchStarted: false,
        launchCleared: false,
    };
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
        return elapsed >= 0.6
            ? { ...model, state: "failed", sequenceSeconds: 0, crash: null, refuel: null, status: FAILURE_STATUS }
            : { ...model, sequenceSeconds: elapsed };
    }
    if (reducedMotion && ["landed", "deploying", "powering"].includes(model.state)) {
        const active = siteById(model, model.activeSiteId);
        const sites = model.retainedSites.map((site) =>
            site.id === active.id ? { ...site, powered: true, nocStage: 7 } : site,
        );
        const powered = {
            ...model,
            state: "launching",
            retainedSites: sites,
            nocStage: 7,
            agent: null,
            refuel: null,
            sequenceSeconds: 0,
            status: SUCCESS_STATUS,
            launchStarted: false,
            launchCleared: false,
        };
        return { ...powered, checkpoint: freezeCheckpoint(powered) };
    }
    let elapsed = model.sequenceSeconds + seconds;
    if (model.state === "landed") {
        if (elapsed < 0.3) {
            return {
                ...model,
                sequenceSeconds: elapsed,
                refuel: model.refuel ? freeze({ ...model.refuel, progress: clamp(elapsed / 0.3, 0, 1) }) : null,
            };
        }
        return { ...model, state: "deploying", sequenceSeconds: elapsed - 0.3, refuel: null, agent: { progress: 0 } };
    }
    if (model.state === "deploying") {
        const progress = clamp(elapsed / 0.9, 0, 1);
        if (elapsed < 0.9) return { ...model, sequenceSeconds: elapsed, agent: { progress } };
        return { ...model, state: "powering", sequenceSeconds: elapsed - 0.9, agent: null };
    }
    if (model.state === "powering") {
        const stage = Math.min(7, Math.floor((elapsed + 1e-12) / 0.2));
        if (elapsed < 1.4) {
            const sites = model.retainedSites.map((site) =>
                site.id === model.activeSiteId ? { ...site, nocStage: stage } : site,
            );
            return { ...model, sequenceSeconds: elapsed, retainedSites: sites, nocStage: stage };
        }
        const sites = model.retainedSites.map((site) =>
            site.id === model.activeSiteId ? { ...site, powered: true, nocStage: 7 } : site,
        );
        const powered = {
            ...model,
            state: "launching",
            retainedSites: sites,
            nocStage: 7,
            refuel: null,
            sequenceSeconds: 0,
            status: SUCCESS_STATUS,
            launchStarted: false,
            launchCleared: false,
        };
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
    const terrainVertices =
        retentionKey === model.retentionKey && model.terrainVertices
            ? model.terrainVertices
            : terrainVerticesForWindow(model.seed, sites, terrainLeft, terrainRight);
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

export function settleCue() {
    return { state: "settled", elapsed: 2.4 };
}

export function createSimulationClock(timestamp = null) {
    return {
        timestamp,
        originTimestamp: timestamp,
        accumulator: 0,
        cursor: 0,
        sequence: 0,
        queue: [],
        input: { ...ZERO },
    };
}

export function enqueueInputEdge(clock, edge) {
    const queued = { ...edge, left: clamp(edge.left, 0, 1), right: clamp(edge.right, 0, 1), sequence: clock.sequence };
    const queue =
        clock.queue.length >= 64
            ? [{ ...queued, snapshot: true }]
            : [...clock.queue, queued].sort((a, b) => a.timestamp - b.timestamp || a.sequence - b.sequence);
    return { ...clock, sequence: clock.sequence + 1, queue };
}

export function removeQueuedInputEdges(clock, token) {
    return { ...clock, queue: clock.queue.filter((edge) => edge.token !== token) };
}

export function clearSimulationInput(clock, timestamp = clock.timestamp ?? 0) {
    return enqueueInputEdge({ ...clock, queue: [] }, { timestamp, left: 0, right: 0 });
}

export function resetSimulationAccumulator(clock, timestamp = clock.timestamp) {
    return {
        ...clock,
        timestamp,
        originTimestamp: timestamp === null ? null : timestamp - clock.cursor * STEP_MILLISECONDS,
        accumulator: 0,
    };
}

export function advanceSimulation(clock, model, timestamp, options = {}) {
    if (clock.timestamp === null)
        return { clock: { ...clock, timestamp, originTimestamp: timestamp }, model, steps: 0, discarded: false };
    const frameSeconds = (timestamp - clock.timestamp) / 1000;
    if (frameSeconds < 0 || frameSeconds > MAX_FRAME_SECONDS)
        return { clock: resetSimulationAccumulator(clock, timestamp), model, steps: 0, discarded: true };
    let accumulator = clock.accumulator + frameSeconds;
    let cursor = clock.cursor;
    let queue = clock.queue;
    let input = clock.input;
    let current = model;
    let steps = 0;
    while (accumulator + 1e-12 >= STEP_SECONDS && steps < MAX_CATCH_UP_STEPS) {
        const stepEnd = clock.originTimestamp + (cursor + 1) * STEP_MILLISECONDS;
        const ready = queue.filter((edge) => edge.timestamp <= stepEnd + 1e-9);
        if (ready.length) {
            input = { left: ready.at(-1).left, right: ready.at(-1).right };
            queue = queue.slice(ready.length);
        }
        const previousState = current.state;
        current = stepFlight(current, input, options);
        accumulator -= STEP_SECONDS;
        cursor += 1;
        steps += 1;
        if (previousState === "flying" && current.state === "launching" && !current.launchStarted) {
            input = { ...ZERO };
            queue = [];
        }
        if (!["flying", "launching"].includes(current.state)) {
            input = { ...ZERO };
            queue = [];
            break;
        }
    }
    return {
        clock: {
            ...clock,
            timestamp,
            accumulator: Math.abs(accumulator) < 1e-12 ? 0 : accumulator,
            cursor,
            queue,
            input,
        },
        model: updateRetention(current),
        steps,
        discarded: false,
    };
}
