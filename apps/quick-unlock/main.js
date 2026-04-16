const APP_ID = "quick-unlock";
const DEFAULT_DOOR_NAME = "Door";
const PENDING_TIMEOUT_MS = 60000;

const STATE_IMG = {
  locked: "./locked.png",
  unlocked: "./unlocked.png",
  unknown: "./locked.png",
  error: "./unlocked.png",
};
const STATE_LABEL = {
  locked: "Locked",
  unlocked: "Unlocked",
  unknown: "Unknown",
  error: "Error",
  pending: "Working…",
};

const slider = document.getElementById("slider");
const sliderImg = document.getElementById("sliderImg");
const statusEl = document.getElementById("status");
const statusText = document.getElementById("statusText");
const doorNameEl = document.getElementById("doorName");
const deviceNameEl = document.getElementById("deviceName");

let _gotState = false;
let _pendingTimer = null;

function setState(state) {
  if (_pendingTimer !== null) {
    clearTimeout(_pendingTimer);
    _pendingTimer = null;
  }
  if (!STATE_IMG[state]) state = "unknown";
  sliderImg.src = STATE_IMG[state];
  slider.className = "slider " + state;
  statusEl.className = "status " + state;
  statusText.textContent = STATE_LABEL[state];
}

function setPending(verb) {
  slider.classList.add("pending");
  statusEl.className = "status pending";
  statusText.textContent = verb || "Working…";
  if (_pendingTimer !== null) clearTimeout(_pendingTimer);
  _pendingTimer = setTimeout(() => {
    _pendingTimer = null;
    slider.classList.remove("pending");
    statusEl.className = "status unknown";
    statusText.textContent = "Timeout (no response)";
  }, PENDING_TIMEOUT_MS);
}

function setDoorName(name) {
  doorNameEl.textContent = (name && String(name).trim()) || DEFAULT_DOOR_NAME;
}

function send(command, verb) {
  setPending(verb);
  window.webxdc.sendUpdate(
    {
      payload: {
        request: {
          name: window.webxdc.selfName,
          text: command,
          app: APP_ID,
        },
      },
    },
    "",
  );
}

// Apply defaults BEFORE registering the listener -- some clients deliver
// queued-update callbacks synchronously, which would otherwise overwrite
// the just-set state.
setDoorName(DEFAULT_DOOR_NAME);
setState("unknown");
deviceNameEl.textContent = "You are: " + window.webxdc.selfName;

window.webxdc.setUpdateListener((update) => {
  const payload = update.payload || {};
  if (payload.config && typeof payload.config.door_name === "string") {
    setDoorName(payload.config.door_name);
  }
  const resp = payload.response;
  if (resp) {
    const text = (resp.text || "").trim().toLowerCase();
    if (STATE_IMG[text]) {
      setState(text);
      _gotState = true;
    }
  }
});

// Tap the slider to refresh the lock state on demand.
slider.addEventListener("click", () => send("status", "Refreshing…"));

// One-tap UX: opening the app immediately requests the door to open.
send("open", "Opening…");
