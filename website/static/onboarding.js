const SUCCESS_MESSAGE = "Prompt copied.";
const FAILURE_MESSAGE = "Copy failed. Select the prompt and copy it manually.";
const MANUAL_MESSAGE = "Select the prompt and copy it manually.";

const TAB_ENTRIES = [
    ["via-agent-tab", "via-agent-panel"],
    ["manual-tab", "manual-panel"],
];

export async function copyPrompt(prompt, status, clipboard) {
    try {
        await clipboard.writeText(prompt.textContent);
        status.textContent = SUCCESS_MESSAGE;
    } catch {
        status.textContent = FAILURE_MESSAGE;
    }
}

export function initializeCopy(documentObject, clipboard) {
    const prompt = documentObject.getElementById("onboarding-prompt");
    const button = documentObject.getElementById("copy-onboarding-prompt");
    const status = documentObject.getElementById("copy-status");
    if (!prompt || !button || !status) {
        return;
    }
    if (!clipboard || typeof clipboard.writeText !== "function") {
        status.textContent = MANUAL_MESSAGE;
        return;
    }
    button.hidden = false;
    button.addEventListener("click", () => copyPrompt(prompt, status, clipboard));
}

function selectTab(entries, selectedIndex, focus) {
    entries.forEach(({ panel, tab }, index) => {
        const selected = index === selectedIndex;
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
        panel.hidden = !selected;
    });
    if (focus) {
        entries[selectedIndex].tab.focus();
    }
}

export function initializeTabs(documentObject) {
    const tabList = documentObject.getElementById("onboarding-tab-list");
    const entries = TAB_ENTRIES.map(([tabId, panelId]) => ({
        panel: documentObject.getElementById(panelId),
        panelId,
        tab: documentObject.getElementById(tabId),
        tabId,
    }));
    if (!tabList || entries.some(({ panel, tab }) => !panel || !tab)) {
        return;
    }

    tabList.setAttribute("role", "tablist");
    entries.forEach(({ panel, panelId, tab, tabId }, index) => {
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-controls", panelId);
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", tabId);
        tab.addEventListener("click", () => selectTab(entries, index, false));
        tab.addEventListener("keydown", (event) => {
            const destinations = {
                ArrowLeft: (index - 1 + entries.length) % entries.length,
                ArrowRight: (index + 1) % entries.length,
                End: entries.length - 1,
                Home: 0,
            };
            if (!(event.key in destinations)) {
                return;
            }
            event.preventDefault();
            selectTab(entries, destinations[event.key], true);
        });
    });
    tabList.hidden = false;
    selectTab(entries, 0, false);
}

export function initializeOnboarding(documentObject, clipboard) {
    initializeTabs(documentObject);
    initializeCopy(documentObject, clipboard);
}

if (typeof document !== "undefined") {
    initializeOnboarding(document, typeof navigator === "undefined" ? undefined : navigator.clipboard);
}
