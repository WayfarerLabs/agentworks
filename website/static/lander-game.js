import {
    agentInstalled,
    advanceCue,
    advanceMissionSequence,
    advanceSimulation,
    clearSimulationInput,
    createCueState,
    createPreflightModel,
    createSimulationClock,
    enqueueInputEdge,
    fuelGaugeLevel,
    GRAVITY,
    mixDigitalInput,
    mixEngineRequests,
    plumeForThrust,
    pointerEngineRequests,
    removeQueuedInputEdges,
    resetSimulationAccumulator,
    settleCue,
    TURN_DIFFERENTIAL,
    transitionMission,
    updateRetention,
} from "./lander-model.js";
import {
    cameraLeftForPose,
    CHUNK_WIDTH,
    mixUint32,
    siteScaffoldPath,
    siteStructure,
    skyProjectionForCamera,
    skyProjectionIdentityForCamera,
    terrainFillPath,
    terrainSurfacePath,
    terrainVerticesForRange,
    targetDirectionForViewport,
} from "./lander-world.js";

const SVG_NAMESPACE = document.querySelector("#lander-scene")?.namespaceURI;
const ACTIVE_STATES = new Set([
    "flying",
    "landed",
    "deploying",
    "powering",
    "launching",
    "crashing",
    "failed",
]);
const FLIGHT_CODES = new Set(["Space", "ArrowUp", "ArrowLeft", "ArrowRight", "KeyH", "KeyL"]);
const ZERO_INPUT = Object.freeze({ left: 0, right: 0, vectorAngle: 0 });
const INTERACTIVE_POINTER_TARGET =
    'a[href],button,input,select,textarea,summary,[contenteditable]:not([contenteditable="false"])';
const INTERACTIVE_KEY_TARGET =
    'a[href],button,input,select,textarea,summary,[contenteditable]:not([contenteditable="false"]),' +
    '[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="switch"],[role="menuitem"],' +
    '[role="option"],[role="slider"],[role="spinbutton"],[role="textbox"],' +
    '[tabindex]:not([tabindex="-1"]):not(#lander-scene-shell)';
const INSTALLED_AGENT_PATH =
    "M -4 -9 H 4 A 1 1 0 0 1 5 -8 V 1 A 1 1 0 0 1 4 2 " +
    "H -4 A 1 1 0 0 1 -5 1 V -8 A 1 1 0 0 1 -4 -9 Z " +
    "M -3 2 V 9 M 3 2 V 9 M -2 -5 L 0 -3 L -2 -1 M 1 -1 H 3";

function svg(name, attributes = {}) {
    if (!SVG_NAMESPACE) throw new Error("SVG namespace is unavailable");
    const element = document.createElementNS(SVG_NAMESPACE, name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    return element;
}

function eventTime(event) {
    return Number.isFinite(event.timeStamp) ? event.timeStamp : performance.now();
}

function unmodified(event, allowShift = false) {
    return !event.ctrlKey && !event.altKey && !event.metaKey && (allowShift || !event.shiftKey);
}

function physicalControl(event) {
    if (["Space", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(event.code)) return event.code;
    if (event.key.toLowerCase() === "h") return "KeyH";
    if (event.key.toLowerCase() === "l") return "KeyL";
    return null;
}

function isEditable(target) {
    return target instanceof Element && Boolean(target.closest(INTERACTIVE_POINTER_TARGET));
}

export function isInteractivePointerEvent(event) {
    return event
        .composedPath()
        .some((node) => node instanceof Element && Boolean(node.closest(INTERACTIVE_POINTER_TARGET)));
}

function eventElementPath(event) {
    if (typeof event.composedPath === "function") {
        const path = event.composedPath();
        if (path.length) return path;
    }
    const path = [];
    let node = event.target;
    while (node instanceof Element) {
        path.push(node);
        node = node.parentElement;
    }
    return path;
}

export function isInteractiveKeyEvent(event) {
    const composedPath = typeof event.composedPath === "function" ? event.composedPath() : [];
    const path = composedPath.length ? composedPath : [event.target];
    return path.some((node) => node instanceof Element && Boolean(node.closest(INTERACTIVE_KEY_TARGET)));
}

function freshSeed() {
    if (globalThis.crypto?.getRandomValues) {
        const words = new Uint32Array(2);
        globalThis.crypto.getRandomValues(words);
        const rotated = ((words[1] << 13) | (words[1] >>> 19)) >>> 0;
        return mixUint32(words[0] ^ rotated);
    }
    const now = Date.now() >>> 0;
    const fine = Math.floor(performance.now() * 1000) >>> 0;
    return mixUint32(now ^ fine);
}

function createSiteGroup(site) {
    const group = svg("g", { class: "lander-site", "data-site-id": site.id });
    group.append(
        svg("rect", { class: "landing-platform" }),
        svg("path", { class: "helipad-mark" }),
        svg("path", {
            class: "site-scaffold",
            fill: "none",
            stroke: "#4b4e55",
            "stroke-width": 2,
            "stroke-linecap": "butt",
            "stroke-linejoin": "round",
        }),
        svg("path", { class: "gas-can", d: "M-7 0h14v15H-7Zm4-5h7v5h-7Zm10 7h4v8H7" }),
        svg("path", { class: "noc-building" }),
        svg("path", { class: "noc-building noc-entry" }),
    );
    const battery = svg("g", { class: "noc-battery" });
    battery.append(svg("rect"));
    for (let index = 1; index <= 4; index += 1)
        battery.append(svg("path", { class: `battery-bar battery-bar-${index}` }));
    group.append(battery);
    group.append(
        svg("path", { class: "noc-antenna antenna-mast" }),
        svg("circle", { class: "noc-antenna antenna-head", r: 4 }),
    );
    for (let index = 1; index <= 3; index += 1) {
        group.append(svg("path", { class: `noc-antenna antenna-signal antenna-signal-${index}` }));
    }
    return group;
}

function projectSiteState(
    group,
    site,
    buildingLeft = siteStructure(site).buildingLeft * 10,
    top = 548 - site.platformTop * 10,
) {
    group.dataset.can = site.canCollected ? "collected" : "present";
    group.dataset.power = site.powered ? "on" : "off";
    group.dataset.agent = agentInstalled(site) ? "installed" : "absent";
    group.dataset.nocStage = String(site.nocStage ?? (site.powered ? 7 : 0));
    const entry = group.querySelector(".noc-entry");
    if (agentInstalled(site)) {
        entry.setAttribute("d", INSTALLED_AGENT_PATH);
        entry.setAttribute("transform", `translate(${buildingLeft + 6.5} ${top - 9}) scale(0.75)`);
    } else {
        entry.setAttribute("d", `M ${buildingLeft} ${top - 18} H ${buildingLeft + 13} V ${top} H ${buildingLeft} Z`);
        entry.removeAttribute("transform");
    }
}

function positionSite(group, site) {
    const left = site.platformLeft * 10;
    const right = site.platformRight * 10;
    const top = 548 - site.platformTop * 10;
    const bottom = 548 - site.platformBottom * 10;
    const center = site.center * 10;
    const deck = group.querySelector(".landing-platform");
    deck.setAttribute("x", left);
    deck.setAttribute("y", top);
    deck.setAttribute("width", right - left);
    deck.setAttribute("height", bottom - top);
    group.querySelector(".helipad-mark").setAttribute("d", `M${center - 9} ${top + 1.7}v-20m18 20v-20m-18 10h18`);
    group.querySelector(".site-scaffold").setAttribute("d", siteScaffoldPath(site));
    group.querySelector(".gas-can").setAttribute("transform", `translate(${center + 30} ${top - 15})`);
    const structure = siteStructure(site);
    const buildingLeft = structure.buildingLeft * 10;
    const roof = Math.round((548 - structure.roof * 10) * 1e9) / 1e9;
    const buildingBottom = 548 - structure.noc.bottom * 10;
    group
        .querySelector(".noc-building")
        .setAttribute("d", `M${buildingLeft} ${buildingBottom}V${roof}h70V${buildingBottom}Z`);
    projectSiteState(group, site, buildingLeft, top);
    const battery = group.querySelector(".noc-battery");
    battery.querySelector("rect").setAttribute("x", buildingLeft + 24);
    battery.querySelector("rect").setAttribute("y", roof + 16);
    battery.querySelector("rect").setAttribute("width", 22);
    battery.querySelector("rect").setAttribute("height", 40);
    const barTops = [46, 38, 30, 22];
    for (let index = 1; index <= 4; index += 1) {
        battery
            .querySelector(`.battery-bar-${index}`)
            .setAttribute("d", `M${buildingLeft + 29} ${roof + barTops[index - 1]}h12v5h-12Z`);
    }
    group.querySelector(".antenna-mast").setAttribute("d", `M${buildingLeft + 35} ${roof}v-32`);
    group.querySelector(".antenna-head").setAttribute("cx", buildingLeft + 35);
    group.querySelector(".antenna-head").setAttribute("cy", roof - 34);
    const centerX = buildingLeft + 35;
    const antennaY = roof - 34;
    const signals = [
        [8, 4, 12],
        [15, 5, 20],
        [23, 6, 29],
    ];
    signals.forEach(([x, y, rise], index) =>
        group
            .querySelector(`.antenna-signal-${index + 1}`)
            .setAttribute(
                "d",
                `M${centerX - x} ${antennaY - y}Q${centerX} ${antennaY - rise} ${centerX + x} ${antennaY - y}`,
            ),
    );
}

export class LanderGameController {
    constructor(root, cleanups = [], snapshot = root?.cloneNode(true), dependencies = {}) {
        if (!root) throw new Error("Lunar deployment game root is missing");
        this.root = root;
        this.pristine = snapshot;
        for (const id of [
            "lander-scene-shell",
            "lander-scene",
            "lander-start",
            "lander-fuel",
            "lander-fuel-value",
            "lander-fuel-gauge",
            "lander-fuel-gauge-fill",
            "lander-target-direction",
            "lander-controls",
            "lander-scene-stage",
            "lander-outcome",
            "lander-controls-rail",
            "lander-exit",
            "lander-restart",
            "lander-status",
            "lander-sky-world",
            "scene-stars",
            "scene-landmarks",
            "terrain-layer",
            "site-layer",
            "debris-layer",
            "mission-agent",
        ]) {
            const name = id.replaceAll("-", "_");
            this[name] = root.querySelector(`#${id}`);
            if (!this[name]) throw new Error(`Missing #${id}`);
        }
        this.model = createPreflightModel();
        this.clock = createSimulationClock();
        this.heldKeys = new Set();
        this.pointerInput = ZERO_INPUT;
        this.pointer = null;
        this.pointerToken = 0;
        this.collectivePulse = { active: false, token: null, deadline: null };
        this.releasedCapture = null;
        this.pulseTimer = null;
        this.frameId = null;
        this.previousFrame = null;
        this.worldWindowKey = null;
        this.skyWindowKey = null;
        this.freshSeed = dependencies.freshSeed ?? freshSeed;
        this.skyProjectionForCamera = dependencies.skyProjectionForCamera ?? skyProjectionForCamera;
        this.paused = document.hidden;
        this.destroyed = false;
        this.cleanups = cleanups;
        this.motion = matchMedia("(prefers-reduced-motion: reduce)");
        this.cue = createCueState(this.motion.matches);
        this.cleanups.push(() => {
            if (this.frameId !== null) cancelAnimationFrame(this.frameId);
            if (this.pulseTimer !== null) clearTimeout(this.pulseTimer);
        });
        this.installListeners();
        this.render();
        if (!this.paused) this.requestFrame();
        this.lander_start.hidden = false;
        this.lander_start.disabled = false;
    }

    listen(target, type, listener, options) {
        target.addEventListener(type, listener, options);
        this.cleanups.push(() => target.removeEventListener(type, listener, options));
    }

    installListeners() {
        this.listen(this.lander_start, "click", () => this.start(false, performance.now()));
        this.listen(this.lander_exit, "click", () => this.exit());
        this.listen(this.lander_restart, "click", () => this.restart());
        this.listen(document, "keydown", (event) => this.onKeyDown(event));
        this.listen(document, "keyup", (event) => this.onKeyUp(event));
        this.listen(window, "blur", () => this.clearAllInput(performance.now()));
        this.listen(this.lander_scene_shell, "focusout", () => this.clearAllInput(performance.now()));
        for (const type of ["pointerdown", "pointermove", "pointerup", "pointercancel", "lostpointercapture"]) {
            this.listen(this.lander_scene_stage, type, (event) => this.onPointer(event));
        }
        this.listen(document, "visibilitychange", () => this.onVisibilityChange());
        this.listen(this.motion, "change", (event) => this.onMotionChange(event));
        this.listen(window, "resize", () => this.renderRefuel(cameraLeftForPose(this.model.pose)));
    }

    queueInput(timestamp, token = null) {
        const launchReadyIdle = this.model.state === "launching" && !this.model.launchStarted && this.frameId === null;
        if (launchReadyIdle) {
            this.clock = resetSimulationAccumulator(this.clock, timestamp);
            this.previousFrame = timestamp;
        }
        const held = Object.fromEntries([...this.heldKeys].map((key) => [key, true]));
        const collective = this.pointer
            ? this.pointerInput
            : this.collectivePulse.active
              ? { left: 0.72, right: 0.72 }
              : ZERO_INPUT;
        const request = mixEngineRequests(mixDigitalInput(held), collective);
        const physical = Object.freeze({
            heldCodes: Object.freeze([...this.heldKeys].sort()),
            pointer: this.pointer
                ? Object.freeze({
                      active: true,
                      id: this.pointer.id,
                      anchorX: this.pointer.x,
                      currentX: this.pointer.currentX,
                      token: this.pointer.token,
                  })
                : Object.freeze({ active: false }),
            collectivePulse: Object.freeze({ ...this.collectivePulse }),
        });
        this.clock = enqueueInputEdge(this.clock, { timestamp, ...request, token, physical });
        this.requestFrame();
    }

    start(holdSpace, timestamp) {
        if (this.model.state !== "preflight") return;
        this.cue = settleCue();
        this.model = updateRetention(
            transitionMission(this.model, "START", {
                seed: this.freshSeed(),
                reducedMotion: this.motion.matches,
            }),
        );
        this.clock = createSimulationClock();
        this.previousFrame = null;
        this.heldKeys.clear();
        if (holdSpace) {
            this.heldKeys.add("Space");
            this.queueInput(timestamp);
        }
        this.lander_start.hidden = true;
        this.lander_start.disabled = true;
        this.lander_fuel.hidden = false;
        this.lander_controls_rail.hidden = false;
        this.lander_outcome.hidden = false;
        this.lander_exit.disabled = false;
        this.lander_scene_shell.tabIndex = 0;
        this.lander_scene_shell.setAttribute("role", "application");
        this.lander_scene_shell.setAttribute("aria-label", "Lunar deployment game");
        this.lander_scene.setAttribute("aria-hidden", "true");
        this.render();
        this.lander_scene_shell.focus({ preventScroll: true });
        this.requestFrame();
    }

    exit() {
        if (this.model.state === "preflight") return;
        this.clearAllInput(performance.now());
        this.model = transitionMission(this.model, "EXIT");
        this.clock = createSimulationClock();
        this.previousFrame = null;
        this.lander_scene_shell.tabIndex = -1;
        for (const attribute of ["role", "aria-label", "aria-describedby"])
            this.lander_scene_shell.removeAttribute(attribute);
        this.lander_scene.removeAttribute("aria-hidden");
        this.lander_fuel.hidden = true;
        this.lander_target_direction.hidden = true;
        this.lander_controls_rail.hidden = true;
        this.lander_outcome.hidden = true;
        this.lander_exit.disabled = true;
        this.lander_restart.hidden = true;
        this.lander_restart.disabled = true;
        this.lander_start.hidden = false;
        this.lander_start.disabled = false;
        this.lander_status.textContent = "";
        const staticTerrain = this.pristine.querySelector("#terrain-layer");
        const staticSites = this.pristine.querySelector("#site-layer");
        this.terrain_layer.replaceChildren(...[...staticTerrain.children].map((node) => node.cloneNode(true)));
        this.site_layer.replaceChildren(...[...staticSites.children].map((node) => node.cloneNode(true)));
        this.debris_layer.replaceChildren();
        this.worldWindowKey = null;
        this.scene_stars.setAttribute("d", this.pristine.querySelector("#scene-stars").getAttribute("d"));
        this.scene_landmarks.setAttribute("d", this.pristine.querySelector("#scene-landmarks").getAttribute("d"));
        this.skyWindowKey = null;
        for (const property of ["--agent-x", "--agent-y", "--crash-x", "--crash-y", "--crash-progress"]) {
            this.root.style.removeProperty(property);
        }
        this.render();
        this.lander_start.focus({ preventScroll: true });
        this.requestFrame();
    }

    restart() {
        if (this.model.state !== "failed") return;
        this.clearAllInput(performance.now());
        this.model = updateRetention(transitionMission(this.model, "RESTART"));
        this.clock = createSimulationClock();
        this.previousFrame = null;
        this.lander_restart.hidden = true;
        this.lander_restart.disabled = true;
        this.render();
        this.lander_scene_shell.focus({ preventScroll: true });
        this.requestFrame();
    }

    activeShellEventPath(event) {
        return ACTIVE_STATES.has(this.model.state) && eventElementPath(event).includes(this.lander_scene_shell);
    }

    onKeyDown(event) {
        if (this.model.state === "preflight") {
            const acceptedTargets = [document.body, this.root, this.lander_scene_shell, this.lander_scene];
            if (
                event.code === "Space" &&
                !event.repeat &&
                unmodified(event) &&
                acceptedTargets.includes(event.target) &&
                !isEditable(event.target)
            ) {
                event.preventDefault();
                this.start(true, eventTime(event));
            }
            return;
        }
        if (!this.activeShellEventPath(event)) return;
        if (event.key === "Escape" && unmodified(event)) {
            event.preventDefault();
            this.exit();
            return;
        }
        if (event.key.toLowerCase() === "r" && unmodified(event) && this.model.state === "failed") {
            event.preventDefault();
            this.restart();
            return;
        }
        if (isInteractiveKeyEvent(event)) return;
        const control = physicalControl(event);
        if (!["flying", "launching"].includes(this.model.state) || !control || !unmodified(event, true)) return;
        event.preventDefault();
        if (event.repeat || this.heldKeys.has(control)) return;
        this.heldKeys.add(control);
        this.queueInput(eventTime(event));
    }

    onKeyUp(event) {
        if (!this.activeShellEventPath(event)) return;
        const control = physicalControl(event);
        if (!control || !this.heldKeys.delete(control)) return;
        event.preventDefault();
        if (["flying", "launching"].includes(this.model.state)) this.queueInput(eventTime(event));
    }

    onPointer(event) {
        if (isInteractivePointerEvent(event)) return;
        if (event.type === "lostpointercapture") {
            const released = this.releasedCapture;
            if (!released || released.pointerId !== event.pointerId) {
                this.finishPointer(this.pointer?.token ?? this.collectivePulse.token, eventTime(event), true);
                return;
            }
            this.releasedCapture = null;
            if (
                this.collectivePulse.active &&
                this.collectivePulse.token === released.token &&
                eventTime(event) < this.collectivePulse.deadline
            )
                return;
            this.finishPointer(released.token, eventTime(event), true);
            return;
        }
        if (event.type === "pointerdown") {
            if (
                !["flying", "launching"].includes(this.model.state) ||
                !event.isPrimary ||
                event.button !== 0 ||
                this.pointer
            )
                return;
            event.preventDefault();
            const timestamp = eventTime(event);
            this.endCollectivePulse(timestamp);
            const token = ++this.pointerToken;
            this.pointer = {
                id: event.pointerId,
                x: event.clientX,
                y: event.clientY,
                currentX: event.clientX,
                started: timestamp,
                token,
            };
            this.lander_scene_stage.setPointerCapture(event.pointerId);
            this.pointerInput = { left: 0.72, right: 0.72 };
            this.queueInput(timestamp, token);
            return;
        }
        if (!this.pointer || event.pointerId !== this.pointer.id) return;
        event.preventDefault();
        if (event.type === "pointermove") {
            this.pointer.currentX = event.clientX;
            this.pointerInput = pointerEngineRequests(
                event.clientX - this.pointer.x,
                this.lander_scene.getBoundingClientRect().width,
            );
            this.queueInput(eventTime(event), this.pointer.token);
            return;
        }
        if (event.type === "pointerup") {
            const pointer = this.pointer;
            const elapsed = eventTime(event) - pointer.started;
            const distance = Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y);
            if (elapsed <= 180 && distance <= 10) {
                const deadline = pointer.started + 140;
                this.releasedCapture = { pointerId: pointer.id, token: pointer.token };
                this.pointer = null;
                this.pointerInput = ZERO_INPUT;
                this.beginCollectivePulse(pointer.token, eventTime(event), deadline);
                if (this.lander_scene_stage.hasPointerCapture(pointer.id)) {
                    this.lander_scene_stage.releasePointerCapture(pointer.id);
                }
                return;
            }
        }
        this.finishPointer(this.pointer.token, eventTime(event), true);
    }

    finishPointer(token, timestamp, prune = false) {
        if (token === undefined || token === null) return;
        if (
            this.pointer?.token !== token &&
            this.collectivePulse.token !== token &&
            this.releasedCapture?.token !== token
        )
            return;
        if (this.pointer?.token === token) this.pointer = null;
        if (this.releasedCapture?.token === token) this.releasedCapture = null;
        this.pointerInput = ZERO_INPUT;
        if (this.collectivePulse.token === token) {
            this.endCollectivePulse(timestamp, prune);
            return;
        }
        if (prune) this.clock = removeQueuedInputEdges(this.clock, token);
        if (["flying", "launching"].includes(this.model.state)) this.queueInput(timestamp);
    }

    beginCollectivePulse(token, timestamp, deadline) {
        this.endCollectivePulse(timestamp);
        this.collectivePulse = { active: true, token, deadline };
        this.queueInput(timestamp, token);
        if (deadline <= timestamp) {
            this.endCollectivePulse(timestamp, false, timestamp);
            return;
        }
        this.pulseTimer = setTimeout(
            () => {
                if (this.collectivePulse.token === token && this.collectivePulse.deadline === deadline) {
                    this.endCollectivePulse(deadline);
                }
            },
            Math.max(0, deadline - performance.now()),
        );
    }

    endCollectivePulse(timestamp, prune = false, immediateTimestamp = null) {
        if (!this.collectivePulse.active) return;
        const token = this.collectivePulse.token;
        const endTimestamp = immediateTimestamp ?? Math.min(timestamp, this.collectivePulse.deadline);
        if (this.pulseTimer !== null) {
            clearTimeout(this.pulseTimer);
            this.pulseTimer = null;
        }
        this.collectivePulse = { active: false, token: null, deadline: null };
        if (prune) this.clock = removeQueuedInputEdges(this.clock, token);
        this.queueInput(endTimestamp, token);
    }

    clearAllInput(timestamp, quiesce = false) {
        this.heldKeys.clear();
        const pointer = this.pointer;
        const token = pointer?.token ?? this.collectivePulse.token;
        this.pointer = null;
        this.releasedCapture = null;
        if (this.pulseTimer !== null) {
            clearTimeout(this.pulseTimer);
            this.pulseTimer = null;
        }
        this.pointerInput = ZERO_INPUT;
        if (token !== undefined) this.clock = removeQueuedInputEdges(this.clock, token);
        this.collectivePulse = { active: false, token: null, deadline: null };
        if (pointer && this.lander_scene_stage.hasPointerCapture(pointer.id)) {
            this.lander_scene_stage.releasePointerCapture(pointer.id);
        }
        this.clock = quiesce
            ? { ...this.clock, timestamp, queue: [], input: { ...ZERO_INPUT }, accumulator: 0 }
            : clearSimulationInput(this.clock, timestamp);
        this.model = { ...this.model, commanded: { ...ZERO_INPUT } };
    }

    onVisibilityChange() {
        this.paused = document.hidden;
        this.root.dataset.paused = String(this.paused);
        if (this.paused) {
            if (this.frameId !== null) cancelAnimationFrame(this.frameId);
            this.frameId = null;
            this.clearAllInput(performance.now());
            this.clock = resetSimulationAccumulator(this.clock, null);
            this.previousFrame = null;
        } else {
            this.clock = createSimulationClock();
            this.previousFrame = null;
            this.requestFrame();
        }
    }

    onMotionChange(event) {
        this.root.dataset.reducedMotion = String(event.matches);
        this.model = { ...this.model, reducedMotion: event.matches };
        if (event.matches) {
            this.cue = settleCue();
            this.model = advanceMissionSequence(this.model, 3.1, true);
            this.render();
        }
    }

    requestFrame() {
        if (this.frameId === null && !this.paused && !this.destroyed)
            this.frameId = requestAnimationFrame((time) => this.frame(time));
    }

    frame(timestamp) {
        this.frameId = null;
        const elapsed =
            this.previousFrame === null ? 0 : Math.max(0, Math.min(0.1, (timestamp - this.previousFrame) / 1000));
        this.previousFrame = timestamp;
        const previousState = this.model.state;
        if (this.model.state === "preflight") this.cue = advanceCue(this.cue, elapsed);
        else if (["flying", "launching"].includes(this.model.state)) {
            const result = advanceSimulation(this.clock, this.model, timestamp);
            this.clock = result.clock;
            this.model = result.model;
            if (result.discarded) this.clearAllInput(timestamp);
        } else if (["landed", "deploying", "powering", "crashing"].includes(this.model.state)) {
            this.model = updateRetention(advanceMissionSequence(this.model, elapsed, this.motion.matches));
        }
        const reachedLaunchReady =
            previousState === "flying" && this.model.state === "launching" && !this.model.launchStarted;
        if (
            reachedLaunchReady ||
            (["flying", "launching"].includes(previousState) && !["flying", "launching"].includes(this.model.state))
        )
            this.clearAllInput(timestamp, reachedLaunchReady);
        this.render();
        const launchReady = this.model.state === "launching" && !this.model.launchStarted;
        const pendingLaunchInput =
            launchReady &&
            (this.clock.queue.length > 0 || this.clock.input.left + this.clock.input.right > TURN_DIFFERENTIAL);
        if (
            ["flying", "landed", "deploying", "powering", "crashing"].includes(this.model.state) ||
            (this.model.state === "launching" && (!launchReady || pendingLaunchInput)) ||
            (this.model.state === "preflight" && this.cue.state === "running")
        )
            this.requestFrame();
    }

    reconcileWorld() {
        if (!this.model.terrainVertices) return;
        if (this.worldWindowKey === this.model.retentionKey) {
            for (const site of this.model.retainedSites) {
                const group = this.site_layer.querySelector(`[data-site-id="${site.id}"]`);
                if (group) {
                    projectSiteState(group, site);
                }
            }
            return;
        }
        const indexes = this.model.retainedChunks;
        const left = indexes[0] * CHUNK_WIDTH;
        const right = (indexes.at(-1) + 1) * CHUNK_WIDTH;
        const vertices = terrainVerticesForRange(this.model.terrainVertices, left, right);
        let fill = this.terrain_layer.querySelector(".terrain-fill");
        let surface = this.terrain_layer.querySelector(".terrain-surface");
        let termini = this.terrain_layer.querySelector(".world-termini");
        if (!fill) {
            fill = svg("path", { class: "terrain-fill" });
            this.terrain_layer.append(fill);
        }
        if (!surface) {
            surface = svg("path", { class: "terrain-surface" });
            this.terrain_layer.append(surface);
        }
        if (!termini) {
            termini = svg("path", { class: "world-termini" });
            this.terrain_layer.append(termini);
        }
        fill.setAttribute("d", terrainFillPath(vertices));
        surface.setAttribute("d", terrainSurfacePath(vertices));
        const { left: terminusLeft, right: terminusRight } = this.model.termini;
        termini.setAttribute(
            "d",
            `M${terminusLeft.x * 10} 0V${548 - terminusLeft.foot * 10}` +
                `M${terminusRight.x * 10} 0V${548 - terminusRight.foot * 10}`,
        );
        for (const node of [...this.terrain_layer.children]) {
            if (node !== fill && node !== surface && node !== termini) node.remove();
        }
        const existingSites = new Map([...this.site_layer.children].map((node) => [Number(node.dataset.siteId), node]));
        for (const site of this.model.retainedSites) {
            let group = existingSites.get(site.id);
            if (!group) {
                group = createSiteGroup(site);
                this.site_layer.append(group);
            }
            positionSite(group, site);
            existingSites.delete(site.id);
        }
        for (const node of existingSites.values()) node.remove();
        this.worldWindowKey = this.model.retentionKey;
    }

    reconcileSky(cameraLeft) {
        const seed = this.model.seed ?? 0x41475731;
        const identity = skyProjectionIdentityForCamera(seed, cameraLeft);
        if (identity === this.skyWindowKey) return;
        const projection = this.skyProjectionForCamera(seed, cameraLeft);
        this.scene_stars.setAttribute("d", projection.starsPath);
        this.scene_landmarks.setAttribute("d", projection.landmarksPath);
        this.skyWindowKey = identity;
    }

    renderCrash(cameraLeft) {
        if (this.model.state !== "crashing" || !this.model.crash) {
            if (this.debris_layer.children.length) this.debris_layer.replaceChildren();
            return;
        }
        const time = this.model.sequenceSeconds;
        const existing = new Map(
            [...this.debris_layer.children].map((node) => [Number(node.dataset.fragmentId), node]),
        );
        for (const fragment of this.model.crash.fragments) {
            const x = fragment.x + fragment.vx * time;
            const y = fragment.y + fragment.vy * time - 0.5 * GRAVITY * time * time;
            let node = existing.get(fragment.id);
            if (!node) {
                node = svg("rect", { width: 8, height: 8, fill: fragment.color, "data-fragment-id": fragment.id });
                this.debris_layer.append(node);
            }
            node.setAttribute("x", x * 10 - 4);
            node.setAttribute("y", 548 - y * 10 - 4);
            node.setAttribute("transform", `rotate(${fragment.angularVelocity * time} ${x * 10} ${548 - y * 10})`);
            existing.delete(fragment.id);
        }
        for (const node of existing.values()) node.remove();
        this.root.style.setProperty("--crash-x", `${(this.model.crash.pose.x - cameraLeft) * 10}px`);
        this.root.style.setProperty("--crash-y", `${548 - this.model.crash.pose.y * 10}px`);
        this.root.style.setProperty("--crash-progress", String(Math.min(1, time / 0.14)));
    }

    renderRefuel(cameraLeft) {
        const refuel = this.model.refuel;
        const active = refuel && this.model.retainedSites?.find((site) => site.id === refuel.siteId);
        const enabled = Boolean(refuel && active && !this.motion.matches);
        this.root.dataset.refueling = String(enabled);
        this.root.style.setProperty("--refuel-progress", String(refuel?.progress ?? 0));
        if (!enabled) return;
        const stageRect = this.lander_scene_stage.getBoundingClientRect();
        const gaugeRect = this.lander_fuel_gauge.getBoundingClientRect();
        const scaleX = stageRect.width / 1000;
        const scaleY = stageRect.height / 640;
        const sceneCanX = (active.center + 3) * 10 - cameraLeft * 10;
        const sceneCanY = 548 - (active.platformTop + 1.5) * 10;
        const startX = stageRect.left + sceneCanX * scaleX - stageRect.left;
        const startY = stageRect.top + sceneCanY * scaleY - stageRect.top;
        const targetX = gaugeRect.left + gaugeRect.width / 2 - stageRect.left;
        const targetY = gaugeRect.top + gaugeRect.height / 2 - stageRect.top;
        const progress = refuel.progress;
        this.root.style.setProperty("--fuel-transfer-x", `${startX + (targetX - startX) * progress}px`);
        this.root.style.setProperty("--fuel-transfer-y", `${startY + (targetY - startY) * progress}px`);
    }

    render() {
        const pose = this.model.pose;
        const activeMission = ACTIVE_STATES.has(this.model.state);
        this.lander_fuel.hidden = !activeMission;
        this.lander_outcome.hidden = !activeMission;
        this.lander_controls_rail.hidden = !activeMission;
        this.lander_exit.disabled = !activeMission;
        const cameraLeft = cameraLeftForPose(pose);
        const left = plumeForThrust(this.model.commanded.left);
        const right = plumeForThrust(this.model.commanded.right);
        this.root.dataset.missionState = this.model.state;
        this.root.dataset.cue = this.cue.state;
        this.root.dataset.paused = String(this.paused);
        this.root.dataset.reducedMotion = String(this.motion.matches);
        this.root.style.setProperty("--camera-x", `${-cameraLeft * 10}px`);
        this.root.style.setProperty("--sky-camera-x", `${-cameraLeft * 10 * 0.24}px`);
        this.root.style.setProperty("--lander-x", `${pose.x * 10}px`);
        this.root.style.setProperty("--lander-y", `${548 - pose.y * 10}px`);
        this.root.style.setProperty("--lander-angle", `${pose.angle}deg`);
        this.root.style.setProperty("--thrust-vector-angle", `${this.model.commanded.vectorAngle ?? 0}deg`);
        for (const [name, value] of [
            ["left-plume-scale", left.scaleY],
            ["right-plume-scale", right.scaleY],
            ["left-plume-opacity", left.opacity],
            ["right-plume-opacity", right.opacity],
        ])
            this.root.style.setProperty(`--${name}`, String(value));
        this.reconcileWorld();
        this.reconcileSky(cameraLeft);
        this.renderCrash(cameraLeft);
        const active = this.model.retainedSites?.find((site) => site.id === this.model.activeSiteId);
        if (this.model.agent && active) {
            const startX = this.model.touchdownPose.x + 1.1;
            const endX = active.platformRight + 2;
            const progress = this.model.agent.progress;
            this.root.style.setProperty("--agent-x", `${(startX + (endX - startX) * progress) * 10}px`);
            this.root.style.setProperty("--agent-y", `${548 - (active.platformTop + 0.2) * 10}px`);
        }
        const target = this.model.retainedSites?.find((site) => site.id === this.model.targetSiteId);
        const targetDirection = targetDirectionForViewport(target, cameraLeft);
        const offscreen = targetDirection !== null;
        this.root.dataset.targetOffscreen = String(offscreen);
        this.root.dataset.targetDirection = targetDirection ?? "none";
        this.lander_target_direction.hidden = !offscreen;
        if (offscreen) {
            this.lander_target_direction.textContent =
                targetDirection === "right" ? "Next site is to the right." : "Next site is to the left.";
        }
        if (this.model.state !== "preflight") {
            const descriptions = [
                "lander-scene-description",
                "lander-controls",
                "lander-fuel-label",
                "lander-fuel-value",
            ];
            if (offscreen) descriptions.push("lander-target-direction");
            descriptions.push("lander-status");
            this.lander_scene_shell.setAttribute("aria-describedby", descriptions.join(" "));
        }
        const fuel = this.model.state === "preflight" ? "0.0" : this.model.fuel.toFixed(1);
        if (this.lander_fuel_value.textContent !== fuel) this.lander_fuel_value.textContent = fuel;
        const gauge = fuelGaugeLevel(this.model);
        const gaugeText = String(gauge);
        if (this.root.style.getPropertyValue("--fuel-gauge-level") !== gaugeText) {
            this.root.style.setProperty("--fuel-gauge-level", gaugeText);
        }
        const fuelLevel = this.model.fuel === 0 ? "empty" : gauge > 0.5 ? "ready" : gauge > 0.2 ? "caution" : "danger";
        const fuelColor = { empty: "#ff5a36", danger: "#ff5a36", caution: "#ffb000", ready: "#2ed49b" }[fuelLevel];
        this.root.dataset.fuelLevel = fuelLevel;
        this.root.style.setProperty("--fuel-level-color", fuelColor);
        this.renderRefuel(cameraLeft);
        if (this.lander_status.textContent !== this.model.status) this.lander_status.textContent = this.model.status;
        const launchReady = this.model.state === "launching" && !this.model.launchStarted;
        this.root.dataset.launchReady = String(launchReady);
        const failed = this.model.state === "failed";
        this.lander_restart.hidden = !failed;
        this.lander_restart.disabled = !failed;
        this.root.dataset.banner = launchReady ? "deployed" : failed ? "crashed" : "none";
    }

    destroy() {
        if (this.destroyed) return;
        this.destroyed = true;
        if (this.frameId !== null) {
            cancelAnimationFrame(this.frameId);
            this.frameId = null;
        }
        this.clearAllInput(performance.now());
        while (this.cleanups.length) {
            try {
                this.cleanups.pop()();
            } catch (error) {
                console.error(error);
            }
        }
        this.model = createPreflightModel();
        this.clock = createSimulationClock();
        this.cue = settleCue();
        this.previousFrame = null;
        this.paused = false;
        const restored = this.pristine.cloneNode(true);
        this.root.replaceWith(restored);
        this.root = restored;
    }
}

export function initializeLanderGame(root = document.querySelector("#lander-game")) {
    if (!root) return null;
    const snapshot = root.cloneNode(true);
    const cleanups = [];
    try {
        return new LanderGameController(root, cleanups, snapshot);
    } catch (error) {
        console.error(error);
        while (cleanups.length) {
            try {
                cleanups.pop()();
            } catch (cleanupError) {
                console.error(cleanupError);
            }
        }
        root.replaceWith(snapshot);
        return null;
    }
}

export const landerGameController = initializeLanderGame();
