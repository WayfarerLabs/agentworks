export const STEP_SECONDS = 1 / 120;
export const MAX_FRAME_SECONDS = 0.1;
export const MAX_CATCH_UP_STEPS = 12;
export const GRAVITY = 3.0;
export const ENGINE_ACCELERATION = 4.2;
export const TORQUE_ACCELERATION = 70.0;
export const FUEL_CAPACITY = 30.0;
export const FUEL_FLOW = 1.0;
export const INITIAL_X = 30.0;
export const INITIAL_Y = 32.0;
export const INITIAL_VX = 0.8;
export const INITIAL_VY = -0.4;
export const INITIAL_ANGLE = 0.0;
export const INITIAL_ANGULAR_VELOCITY = 0.0;
export const MAX_PLAYABLE_Y = 48.0;
export const MAX_LANDING_HORIZONTAL_SPEED = 1.4;
export const MAX_LANDING_DESCENT_SPEED = 2.2;
export const MAX_LANDING_ANGLE = 8.0;
export const MAX_LANDING_ANGULAR_SPEED = 12.0;

export const FAILURE_STATUS = "Landing unsuccessful. Press R to restart or Escape to exit.";
export const SUCCESS_STATUS = "Agent deployed. Mission continues.";

const STEP_MILLISECONDS = STEP_SECONDS * 1000;
const EPSILON_SECONDS = 1e-12;
const EDGE_EPSILON_MILLISECONDS = 1e-9;
const HULL = [
    [-1.6, 0],
    [1.6, 0],
    [1.6, 6.5],
    [-1.6, 6.5],
];
const NOC_MODULES = [
    [54, 59, 0, 4.2],
    [60, 66, 0, 6.8],
    [67, 72, 0, 4.8],
];

function flightPose() {
    return {
        x: INITIAL_X,
        y: INITIAL_Y,
        vx: INITIAL_VX,
        vy: INITIAL_VY,
        angle: INITIAL_ANGLE,
        angularVelocity: INITIAL_ANGULAR_VELOCITY,
    };
}

function baseModel(state) {
    return {
        state,
        pose: flightPose(),
        fuel: FUEL_CAPACITY,
        commanded: { left: 0, right: 0 },
        nocPower: false,
        nocStage: 0,
        bayOpen: false,
        agentVisible: false,
        agentPosition: null,
        landerVisible: true,
        touchdownPose: null,
        sequenceSeconds: 0,
        failureCause: null,
        status: "",
    };
}

export function createPreflightModel() {
    return baseModel("preflight");
}

export function createFlightModel() {
    return baseModel("flying");
}

export function normalizeDegrees(degrees) {
    return ((((degrees + 180) % 360) + 360) % 360) - 180;
}

export function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
}

export function transformLocalPoint(pose, localX, localY) {
    const radians = (pose.angle * Math.PI) / 180;
    const cosine = Math.cos(radians);
    const sine = Math.sin(radians);
    return {
        x: pose.x + localX * cosine + localY * sine,
        y: pose.y - localX * sine + localY * cosine,
    };
}

export function plumeForThrust(thrust) {
    const command = clamp(thrust, 0, 1);
    return {
        scaleY: 0.08 + 0.92 * command,
        opacity: 0.25 + 0.75 * command,
    };
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
    return {
        left: Math.max(keyboard.left, pointer.left),
        right: Math.max(keyboard.right, pointer.right),
    };
}

export function pointerEngineRequests(displacement, sceneWidth) {
    const deadZone = Math.max(10, sceneWidth * 0.01);
    const fullBiasDistance = Math.max(56, sceneWidth * 0.18);
    const magnitude = clamp((Math.abs(displacement) - deadZone) / (fullBiasDistance - deadZone), 0, 1);
    const bias = Math.sign(displacement) * magnitude;
    return {
        left: clamp(0.72 + 0.28 * bias, 0, 1),
        right: clamp(0.72 - 0.28 * bias, 0, 1),
    };
}

export function effectiveThrust(requested, fuel, seconds = STEP_SECONDS) {
    const left = clamp(requested.left, 0, 1);
    const right = clamp(requested.right, 0, 1);
    const requestedBurn = FUEL_FLOW * (left + right) * seconds;
    const scale = requestedBurn > fuel && requestedBurn > 0 ? fuel / requestedBurn : 1;
    return {
        left: left * scale,
        right: right * scale,
        fuel: Math.max(0, fuel - requestedBurn * scale),
    };
}

function rangesOverlap(aMin, aMax, bMin, bMax) {
    return aMax >= bMin && bMax >= aMin;
}

function project(points, axis) {
    const values = points.map((point) => point.x * axis.x + point.y * axis.y);
    return [Math.min(...values), Math.max(...values)];
}

function polygonIntersectsRectangle(polygon, rectangle) {
    const [left, right, bottom, top] = rectangle;
    const box = [
        { x: left, y: bottom },
        { x: right, y: bottom },
        { x: right, y: top },
        { x: left, y: top },
    ];
    const axes = [
        { x: 1, y: 0 },
        { x: 0, y: 1 },
    ];
    for (let index = 0; index < polygon.length; index += 1) {
        const start = polygon[index];
        const end = polygon[(index + 1) % polygon.length];
        axes.push({ x: -(end.y - start.y), y: end.x - start.x });
    }
    return axes.every((axis) => {
        const polygonRange = project(polygon, axis);
        const boxRange = project(box, axis);
        return rangesOverlap(...polygonRange, ...boxRange);
    });
}

export function contactForPose(pose) {
    const feet = [transformLocalPoint(pose, -1.6, 0), transformLocalPoint(pose, 1.6, 0)];
    const hull = HULL.map(([x, y]) => transformLocalPoint(pose, x, y));
    const ground = hull.some((point) => point.y <= 0);
    const noc = NOC_MODULES.some((module) => polygonIntersectsRectangle(hull, module));
    const safe =
        ground &&
        feet.every((foot) => foot.x >= 18 && foot.x <= 42) &&
        pose.vy <= 0 &&
        Math.abs(pose.vy) <= MAX_LANDING_DESCENT_SPEED &&
        Math.abs(pose.vx) <= MAX_LANDING_HORIZONTAL_SPEED &&
        Math.abs(normalizeDegrees(pose.angle)) <= MAX_LANDING_ANGLE &&
        Math.abs(pose.angularVelocity) <= MAX_LANDING_ANGULAR_SPEED;
    return { feet, hull, ground, noc, safe };
}

function settleTouchdown(model, contact) {
    const lowerFoot = Math.min(...contact.feet.map((foot) => foot.y));
    const pose = {
        ...model.pose,
        y: model.pose.y - lowerFoot,
        vx: 0,
        vy: 0,
        angularVelocity: 0,
    };
    return {
        ...model,
        state: "landed",
        pose,
        commanded: { left: 0, right: 0 },
        touchdownPose: pose,
        sequenceSeconds: 0,
        status: "Touchdown confirmed. Deploying agent.",
    };
}

function fail(model, cause, pose) {
    return {
        ...model,
        state: "failed",
        pose: { ...pose, vx: 0, vy: 0, angularVelocity: 0 },
        commanded: { left: 0, right: 0 },
        nocPower: false,
        nocStage: 0,
        failureCause: cause,
        status: FAILURE_STATUS,
    };
}

function succeedImmediately(model, contact) {
    const touchdown = settleTouchdown(model, contact);
    return {
        ...touchdown,
        state: "succeeded",
        nocPower: true,
        nocStage: 4,
        bayOpen: true,
        agentVisible: false,
        landerVisible: false,
        status: SUCCESS_STATUS,
    };
}

export function stepFlight(model, requested, options = {}) {
    if (model.state !== "flying") {
        return model;
    }
    const seconds = options.seconds ?? STEP_SECONDS;
    const thrust = effectiveThrust(requested, model.fuel, seconds);
    const radians = (model.pose.angle * Math.PI) / 180;
    const totalAcceleration = ENGINE_ACCELERATION * (thrust.left + thrust.right);
    const vx = model.pose.vx + totalAcceleration * Math.sin(radians) * seconds;
    const vy = model.pose.vy + (totalAcceleration * Math.cos(radians) - GRAVITY) * seconds;
    const angularVelocity = model.pose.angularVelocity + TORQUE_ACCELERATION * (thrust.left - thrust.right) * seconds;
    const rawPose = {
        x: model.pose.x + vx * seconds,
        y: model.pose.y + vy * seconds,
        vx,
        vy,
        angle: normalizeDegrees(model.pose.angle + angularVelocity * seconds),
        angularVelocity,
    };
    const contact = contactForPose(rawPose);
    const outOfBounds = rawPose.x < 7 || rawPose.x > 93 || rawPose.y > MAX_PLAYABLE_Y;
    const stepped = {
        ...model,
        pose: rawPose,
        fuel: thrust.fuel,
        commanded: { left: thrust.left, right: thrust.right },
    };
    const frozenPose = {
        ...rawPose,
        x: clamp(rawPose.x, 7, 93),
        y: clamp(rawPose.y, 0, MAX_PLAYABLE_Y),
    };
    if (contact.noc) {
        return fail(stepped, "noc", frozenPose);
    }
    if (contact.ground) {
        return contact.safe
            ? options.reducedMotion
                ? succeedImmediately(stepped, contact)
                : settleTouchdown(stepped, contact)
            : fail(stepped, "surface", frozenPose);
    }
    if (outOfBounds) {
        return fail(stepped, "bounds", frozenPose);
    }
    return stepped;
}

export function transitionMission(model, event, options = {}) {
    if (event === "EXIT" && model.state !== "preflight") {
        return createPreflightModel();
    }
    if (event === "START" && model.state === "preflight") {
        return createFlightModel();
    }
    if (event === "RESTART" && ["failed", "succeeded"].includes(model.state)) {
        return createFlightModel();
    }
    if (event === "SAFE_CONTACT" && model.state === "flying") {
        const contact = contactForPose(model.pose);
        return options.reducedMotion ? succeedImmediately(model, contact) : settleTouchdown(model, contact);
    }
    if (event === "UNSAFE_CONTACT" && model.state === "flying") {
        return fail(model, options.cause ?? "surface", model.pose);
    }
    if (event === "OUT_OF_BOUNDS" && model.state === "flying") {
        return fail(model, "bounds", model.pose);
    }
    const transitions = {
        landed: ["LANDING_SETTLED", "deploying"],
        deploying: ["AGENT_ENTERED", "powering"],
        powering: ["NOC_POWERED", "departing"],
        departing: ["LANDER_DEPARTED", "succeeded"],
    };
    const transition = transitions[model.state];
    if (!transition || event !== transition[0]) {
        return model;
    }
    const next = { ...model, state: transition[1], sequenceSeconds: 0 };
    if (event === "LANDING_SETTLED") {
        return { ...next, bayOpen: true, agentVisible: true };
    }
    if (event === "AGENT_ENTERED") {
        return { ...next, agentVisible: false };
    }
    if (event === "NOC_POWERED") {
        return {
            ...next,
            nocPower: true,
            nocStage: 4,
            commanded: { left: 0.82, right: 0.82 },
        };
    }
    return {
        ...next,
        commanded: { left: 0, right: 0 },
        landerVisible: false,
        status: SUCCESS_STATUS,
    };
}

export class DeploymentMission {
    constructor(model = createPreflightModel()) {
        this.model = model;
    }

    send(event, options = {}) {
        this.model = transitionMission(this.model, event, options);
        return this.model;
    }
}

function agentPosition(model, elapsed) {
    const start = transformLocalPoint(model.touchdownPose, 1.136, 2.8);
    const surface = { x: start.x, y: 0 };
    const entry = { x: 54, y: 1.1 };
    if (elapsed <= 0.35) {
        const progress = elapsed / 0.35;
        return { x: start.x, y: start.y + (surface.y - start.y) * progress };
    }
    const progress = clamp((elapsed - 0.35) / 1.65, 0, 1);
    return {
        x: surface.x + (entry.x - surface.x) * progress,
        y: surface.y + (entry.y - surface.y) * progress,
    };
}

function advanceOneSequence(model, seconds) {
    const elapsed = model.sequenceSeconds + seconds;
    if (model.state === "landed") {
        const next = { ...model, bayOpen: true, sequenceSeconds: Math.min(elapsed, 0.3) };
        return elapsed >= 0.3
            ? [{ ...next, state: "deploying", sequenceSeconds: 0, agentVisible: true }, elapsed - 0.3]
            : [next, 0];
    }
    if (model.state === "deploying") {
        const next = {
            ...model,
            sequenceSeconds: Math.min(elapsed, 2.2),
            agentVisible: elapsed < 2.2,
            agentPosition: agentPosition(model, Math.min(elapsed, 2.2)),
        };
        return elapsed >= 2.2
            ? [{ ...next, state: "powering", sequenceSeconds: 0, agentVisible: false }, elapsed - 2.2]
            : [next, 0];
    }
    if (model.state === "powering") {
        const stage = [0.2, 0.4, 0.6, 0.8].filter((limit) => elapsed >= limit).length;
        const next = { ...model, sequenceSeconds: Math.min(elapsed, 1), nocStage: stage };
        return elapsed >= 1
            ? [
                  {
                      ...next,
                      state: "departing",
                      sequenceSeconds: 0,
                      nocPower: true,
                      nocStage: 4,
                  },
                  elapsed - 1,
              ]
            : [next, 0];
    }
    if (model.state === "departing") {
        const progress = clamp(elapsed / 1.8, 0, 1);
        const touchdown = model.touchdownPose;
        const angleDelta = normalizeDegrees(-touchdown.angle);
        const next = {
            ...model,
            sequenceSeconds: Math.min(elapsed, 1.8),
            commanded: { left: 0.82, right: 0.82 },
            pose: {
                ...model.pose,
                x: touchdown.x + 6 * progress,
                y: touchdown.y + (62 - touchdown.y) * progress,
                angle: normalizeDegrees(touchdown.angle + angleDelta * progress),
            },
        };
        return elapsed >= 1.8
            ? [
                  {
                      ...next,
                      state: "succeeded",
                      commanded: { left: 0, right: 0 },
                      landerVisible: false,
                      status: SUCCESS_STATUS,
                  },
                  0,
              ]
            : [next, 0];
    }
    return [model, 0];
}

export function advanceMissionSequence(model, seconds, reducedMotion = false) {
    if (reducedMotion && ["landed", "deploying", "powering", "departing"].includes(model.state)) {
        return {
            ...model,
            state: "succeeded",
            commanded: { left: 0, right: 0 },
            nocPower: true,
            nocStage: 4,
            bayOpen: true,
            agentVisible: false,
            landerVisible: false,
            status: SUCCESS_STATUS,
        };
    }
    let current = model;
    let remaining = Math.max(0, seconds);
    do {
        [current, remaining] = advanceOneSequence(current, remaining);
    } while (remaining > 0 && ["landed", "deploying", "powering", "departing"].includes(current.state));
    return current;
}

export function createCueState(reducedMotion = false) {
    return { state: reducedMotion ? "settled" : "running", elapsed: 0 };
}

export function advanceCue(cue, seconds) {
    if (cue.state === "settled") {
        return cue;
    }
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
        input: { left: 0, right: 0 },
    };
}

export function enqueueInputEdge(clock, edge) {
    const queued = {
        timestamp: edge.timestamp,
        left: clamp(edge.left, 0, 1),
        right: clamp(edge.right, 0, 1),
        sequence: clock.sequence,
        token: edge.token ?? null,
    };
    return {
        ...clock,
        sequence: clock.sequence + 1,
        queue: [...clock.queue, queued].sort(
            (left, right) => left.timestamp - right.timestamp || left.sequence - right.sequence,
        ),
    };
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
        originTimestamp: timestamp - clock.cursor * STEP_MILLISECONDS,
        accumulator: 0,
    };
}

export function advanceSimulation(clock, model, timestamp, options = {}) {
    if (clock.timestamp === null) {
        return {
            clock: { ...clock, timestamp, originTimestamp: timestamp },
            model,
            steps: 0,
            discarded: false,
        };
    }
    const frameSeconds = (timestamp - clock.timestamp) / 1000;
    if (frameSeconds < 0 || frameSeconds > MAX_FRAME_SECONDS) {
        return {
            clock: resetSimulationAccumulator(clock, timestamp),
            model,
            steps: 0,
            discarded: true,
        };
    }
    let accumulator = clock.accumulator + frameSeconds;
    let cursor = clock.cursor;
    let queue = clock.queue;
    let input = clock.input;
    let current = model;
    let steps = 0;
    while (accumulator + EPSILON_SECONDS >= STEP_SECONDS && steps < MAX_CATCH_UP_STEPS) {
        const stepEnd = clock.originTimestamp + (cursor + 1) * STEP_MILLISECONDS;
        const ready = queue.filter((edge) => edge.timestamp <= stepEnd + EDGE_EPSILON_MILLISECONDS);
        if (ready.length > 0) {
            input = { left: ready.at(-1).left, right: ready.at(-1).right };
            queue = queue.slice(ready.length);
        }
        current = stepFlight(current, input, options);
        accumulator -= STEP_SECONDS;
        cursor += 1;
        steps += 1;
        if (current.state !== "flying") {
            input = { left: 0, right: 0 };
            queue = [];
            break;
        }
    }
    if (Math.abs(accumulator) <= EPSILON_SECONDS) {
        accumulator = 0;
    }
    return {
        clock: { ...clock, timestamp, accumulator, cursor, queue, input },
        model: current,
        steps,
        discarded: false,
    };
}
