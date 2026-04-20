const APP_ID = "quick-lock";
const DEFAULT_DOOR_NAME = "Door";
const PENDING_TIMEOUT_MS = 60000;
const STARTING_FRAME_MS = 1000; // how long to linger on the green "starting" frame

// User-facing colour semantics for THIS app:
//   green  = open    (starting / pre-action)
//   red    = closed  (target state after lock command succeeds)
//   orange = closing (transitional / pending)
const STATE_IMG = {
  open: "./open.png",
  closed: "./closed.png",
  closing: "./opening.png", // reuse the orange transitional image
};
const STATE_LABEL = {
  open: "Open",
  closed: "Closed",
  closing: "Closing…",
};

// Bot replies are locked|unlocked|unknown|error -- map into our scheme.
// Anything that isn't a confirmed lock keeps the slider on green so the
// user knows the lock action did NOT take effect and can retry.
const RESP_TO_STATE = {
  locked: "closed",
  unlocked: "open",
  error: "open",
  unknown: "open",
};

const slider = document.getElementById("slider");
const sliderImg = document.getElementById("sliderImg");
const statusEl = document.getElementById("status");
const statusText = document.getElementById("statusText");
const doorNameEl = document.getElementById("doorName");
const deviceNameEl = document.getElementById("deviceName");

let _pendingTimer = null;
let _pendingLabel = ""; // shown once the bot acks the request
let _currentState = "open";
// On app load the bot's status-update queue may already contain stale
// ack/response payloads from previous sessions. We want the visual to
// start at green regardless and only follow updates that belong to the
// CURRENT auto-lock / tap, so we discard ack/response updates until
// the auto-lock has been kicked off.
let _ready = false;

function setState(state) {
  if (_pendingTimer !== null) {
    clearTimeout(_pendingTimer);
    _pendingTimer = null;
  }
  if (!STATE_IMG[state]) state = "open";
  _currentState = state;
  sliderImg.src = STATE_IMG[state];
  slider.className = "slider " + state;
  statusEl.className = "status " + state;
  statusText.textContent = STATE_LABEL[state];
}

function setPending() {
  // Called when the bot's ack arrives -- swap in the orange transitional
  // visual. The pending timer set by send() keeps running so it still
  // covers the rest of the BLE round-trip.
  sliderImg.src = STATE_IMG.closing;
  slider.className = "slider closing";
  statusEl.className = "status closing";
  statusText.textContent = _pendingLabel || STATE_LABEL.closing;
}

function setDoorName(name) {
  doorNameEl.textContent = (name && String(name).trim()) || DEFAULT_DOOR_NAME;
}

function send(command, label) {
  // Don't change the visual yet -- keep the green "still open" state
  // showing until the bot acks. Priority of THIS app is that the user
  // can clearly see whether the lock has been engaged or not.
  _pendingLabel = label || STATE_LABEL.closing;
  if (_pendingTimer !== null) clearTimeout(_pendingTimer);
  _pendingTimer = setTimeout(() => {
    _pendingTimer = null;
    // Bot didn't ack/reply -- fall back to green so the user knows
    // the lock did NOT engage and can tap to retry.
    sliderImg.src = STATE_IMG.open;
    slider.className = "slider open";
    statusEl.className = "status open";
    statusText.textContent = "Timeout (no response) — tap to retry";
    _currentState = "open";
  }, PENDING_TIMEOUT_MS);
  window.webxdc.sendUpdate(
    {
      payload: {
        request: {
          name: window.webxdc.selfName,
          text: command,
          app: APP_ID,
          ts: Math.floor(Date.now() / 1000),
        },
      },
    },
    "",
  );
}

// Apply defaults BEFORE registering the listener (some clients deliver
// queued callbacks synchronously, which would otherwise overwrite the
// just-set state).
setDoorName(DEFAULT_DOOR_NAME);
setState("open"); // green "starting point"
deviceNameEl.textContent = "You are: " + window.webxdc.selfName;

window.webxdc.setUpdateListener((update) => {
  const payload = update.payload || {};
  // door_name updates are always safe to apply.
  if (payload.config && typeof payload.config.door_name === "string") {
    setDoorName(payload.config.door_name);
  }
  // Skip stale ack/response updates from previous sessions until the
  // initial auto-lock has fired. They don't reflect our current intent.
  if (!_ready) return;
  if (payload.ack && _pendingTimer !== null) {
    setPending();
  }
  const resp = payload.response;
  if (resp) {
    const text = (resp.text || "").trim().toLowerCase();
    const mapped = RESP_TO_STATE[text];
    if (mapped) setState(mapped);
  }
});

// Tap behaviour for THIS app: retry the lock if it isn't yet closed.
// There is NO 'open' direction here by design -- the priority is that
// after touching the app the lock is closed. Taps while a request is
// in flight are ignored to avoid racing the bot.
slider.addEventListener("click", () => {
  if (_pendingTimer !== null) return;
  if (_currentState !== "closed") {
    send("lock", "Closing…");
  }
  // Already closed -> nothing to do; priority satisfied.
});

// One-tap UX on app open: linger on the green "starting" frame so the
// transition reads, then auto-fire the lock command.
setTimeout(() => {
  _ready = true;
  send("lock", "Closing…");
}, STARTING_FRAME_MS);
