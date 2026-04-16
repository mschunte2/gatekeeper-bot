const DEFAULT_DOOR_NAME = "Door";

const DOOR_ICONS = {
  locked:
    '<path d="M5 2v20h14V2H5Zm2 2h10v16H7V4Zm6 7h-2v3h2v-3Z"/>' +
    '<path d="M14.5 8V6.5a2.5 2.5 0 0 0-5 0V8H8.5v3.5h7V8h-1Zm-4-1.5a1.5 1.5 0 0 1 3 0V8h-3V6.5Z" fill="#c0392b"/>',
  unlocked:
    '<path d="M5 2v20h6v-3l-2-1V4l8 2v14l-2 1v3h6V2H5Zm12 11a1 1 0 1 1 0 2 1 1 0 0 1 0-2Z"/>',
  unknown:
    '<path d="M5 2v20h14V2H5Zm2 2h10v16H7V4Zm5 2.5a2.5 2.5 0 0 0-2.5 2.5h1.5a1 1 0 1 1 1.7.7l-.9.9c-.5.5-.8 1-.8 1.9h1.5c0-.6.2-1 .7-1.5l.7-.7a2.5 2.5 0 0 0-1.9-3.8Zm-.75 8h1.5v1.5h-1.5V14.5Z" fill="#666"/>',
  error:
    '<path d="M12 2 1 21h22L12 2Zm0 4.7L19.5 19h-15L12 6.7Zm-1 4.3v5h2v-5h-2Zm0 6v2h2v-2h-2Z" fill="#c0392b"/>',
};

const STATE_LABELS = {
  locked: "Locked",
  unlocked: "Unlocked",
  unknown: "Unknown",
  error: "Error",
};

const PENDING_VERB = {
  lock: "locking",
  unlock: "unlocking",
  open: "opening",
  status: "checking",
};

const PENDING_TIMEOUT_MS = 60000;

const doorBtn = document.getElementById("doorBtn");
const doorIcon = document.getElementById("doorIcon");
const statusEl = document.getElementById("status");
const statusText = document.getElementById("statusText");
const doorNameEl = document.getElementById("doorName");
const deviceNameEl = document.getElementById("deviceName");

let _pendingTimer = null;

function clearPending() {
  if (_pendingTimer !== null) {
    clearTimeout(_pendingTimer);
    _pendingTimer = null;
  }
  doorBtn.classList.remove("pending");
  statusEl.classList.remove("pending");
}

function setDoorState(state) {
  clearPending();
  if (!DOOR_ICONS[state]) state = "unknown";
  doorIcon.innerHTML = DOOR_ICONS[state];
  doorBtn.className = "round-btn door-pos " + state;
  statusEl.className = "status " + state;
  statusText.textContent = "Door: " + STATE_LABELS[state];
}

function setPending(command) {
  doorBtn.classList.add("pending");
  statusEl.classList.add("pending");
  statusText.textContent = "Door: " + (PENDING_VERB[command] || "working") + "…";
  if (_pendingTimer !== null) clearTimeout(_pendingTimer);
  _pendingTimer = setTimeout(() => {
    _pendingTimer = null;
    doorBtn.classList.remove("pending");
    statusEl.classList.remove("pending");
    statusText.textContent = "Door: timeout (no response)";
  }, PENDING_TIMEOUT_MS);
}

function setDoorName(name) {
  doorNameEl.textContent = (name && String(name).trim()) || DEFAULT_DOOR_NAME;
}

const APP_ID = "gatekeeper";

function send(command) {
  setPending(command);
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

document.getElementById("lockBtn").addEventListener("click", () => send("lock"));
document
  .getElementById("unlockBtn")
  .addEventListener("click", () => send("open"));
doorBtn.addEventListener("click", () => send("status"));

let _gotState = false;

// Apply defaults BEFORE registering the listener -- if the client fires
// queued-update callbacks synchronously (some do), the defaults would
// otherwise overwrite anything the listener just set.
setDoorState("unknown");
setDoorName(DEFAULT_DOOR_NAME);
deviceNameEl.textContent = "You are: " + window.webxdc.selfName;

window.webxdc.setUpdateListener((update) => {
  const payload = update.payload || {};
  if (payload.config && typeof payload.config.door_name === "string") {
    setDoorName(payload.config.door_name);
  }
  const resp = payload.response;
  if (resp) {
    const text = (resp.text || "").trim().toLowerCase();
    if (DOOR_ICONS[text]) {
      setDoorState(text);
      _gotState = true;
    }
  }
});

// On open, queued updates arrive synchronously through setUpdateListener.
// If none of them set a state (e.g. the bot didn't know about this app
// instance yet), ask for one. The bot recognises 'status' as read-only
// and won't post an audit line for it.
setTimeout(() => {
  if (!_gotState) send("status");
}, 1000);
