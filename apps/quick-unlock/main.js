const APP_ID = "quick-unlock";
const DEFAULT_DOOR_NAME = "Door";
const PENDING_TIMEOUT_MS = 60000;
const STARTING_FRAME_MS = 1000; // how long to linger on the red "starting" frame

// User-facing colour semantics (NOT lock-state semantics):
//   green = open       (target / success)
//   red   = closed     (starting / failure)
//   yellow= opening    (transitional / pending)
const STATE_IMG = {
  open: "./open.png",
  closed: "./closed.png",
  opening: "./opening.png",
};
const STATE_LABEL = {
  open: "Open",
  closed: "Closed",
  opening: "Opening…",
};

// Bot replies arrive as locked|unlocked|unknown|error -- map them into
// our colour semantics.
const RESP_TO_STATE = {
  unlocked: "open",
  locked: "closed",
  error: "closed",
  unknown: "closed",
};

const slider = document.getElementById("slider");
const sliderImg = document.getElementById("sliderImg");
const statusEl = document.getElementById("status");
const statusText = document.getElementById("statusText");
const doorNameEl = document.getElementById("doorName");
const deviceNameEl = document.getElementById("deviceName");

let _pendingTimer = null;

function setState(state) {
  if (_pendingTimer !== null) {
    clearTimeout(_pendingTimer);
    _pendingTimer = null;
  }
  if (!STATE_IMG[state]) state = "closed";
  sliderImg.src = STATE_IMG[state];
  slider.className = "slider " + state;
  statusEl.className = "status " + state;
  statusText.textContent = STATE_LABEL[state];
}

function setPending(label) {
  // Show the yellow transitional and arm a timeout fallback so the UI
  // can't get stuck if the bot never replies.
  sliderImg.src = STATE_IMG.opening;
  slider.className = "slider opening";
  statusEl.className = "status opening";
  statusText.textContent = label || STATE_LABEL.opening;
  if (_pendingTimer !== null) clearTimeout(_pendingTimer);
  _pendingTimer = setTimeout(() => {
    _pendingTimer = null;
    sliderImg.src = STATE_IMG.closed;
    slider.className = "slider closed";
    statusEl.className = "status closed";
    statusText.textContent = "Timeout (no response)";
  }, PENDING_TIMEOUT_MS);
}

function setDoorName(name) {
  doorNameEl.textContent = (name && String(name).trim()) || DEFAULT_DOOR_NAME;
}

function send(command, label) {
  setPending(label);
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

// Apply defaults BEFORE registering the listener. Some clients deliver
// queued-update callbacks synchronously; setting defaults first means
// any queued updates apply on top of them, not the other way round.
setDoorName(DEFAULT_DOOR_NAME);
setState("closed"); // red "starting point"
deviceNameEl.textContent = "You are: " + window.webxdc.selfName;

window.webxdc.setUpdateListener((update) => {
  const payload = update.payload || {};
  if (payload.config && typeof payload.config.door_name === "string") {
    setDoorName(payload.config.door_name);
  }
  const resp = payload.response;
  if (resp) {
    const text = (resp.text || "").trim().toLowerCase();
    const mapped = RESP_TO_STATE[text];
    if (mapped) setState(mapped);
  }
});

// Tap the slider to refresh the current state on demand.
slider.addEventListener("click", () => send("status", "Checking…"));

// One-tap UX: linger on the red "starting" frame for a beat so the
// transition reads, then flip to the yellow "opening" frame and fire
// the actual open command. The bot's response will switch us to green.
setTimeout(() => send("open", "Opening…"), STARTING_FRAME_MS);
