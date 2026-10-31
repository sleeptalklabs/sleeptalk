import "./styles.css";
import { DEFAULT_ICE_SERVERS, parseIceServers } from "./ice";
import { Orb, type OrbState } from "./orb";
import { ScribbleOrb } from "./scribble-orb";
import { SILENCE, voiceState } from "./voice-activity";
import { VoiceMeter } from "./voice-meter";
import { Transcript, type Block } from "./transcript";
import { WorkerPanel, workerLink } from "./worker_panel";
import {
  agentWaiting,
  hostOrbState,
  parsePresentation,
  snapshotKey,
  userSpeakingHint,
  type PresentationLine,
  type WorkerLine,
  type PresentationSnapshot,
  type SettingsState,
  type VoiceState,
  type UserSpeakingHint,
} from "./presentation";
import {
  DEFAULT_NARRATION,
  DEFAULT_REPLY_LENGTH,
  DEFAULT_SPEED,
  NARRATION_STYLES,
  REPLY_LENGTHS,
  SPEED_STEPS,
  type NarrationStyle,
  type ReplyLength,
  type SpeedStep,
  callSettingPayload,
  narrationLabel,
  replyLengthLabel,
  speedLabel,
} from "./settings";
import { parseCallRoute } from "./routes";
import { validBrowserSignalUrl } from "./signal_url";
import { collectRtcStats } from "./telemetry";
import { browserRuntimePolicy } from "./webview";
import { STATE_COPY, uiStateForEndReason } from "./state_copy";
import { categorizeFailure, correlationKey } from "./failure_categories";
import { decideIceRestart } from "./ice_restart";
import { decideReconnect } from "./reconnect";

type SignalType =
  | "call.auth"
  | "peer.ready"
  | "webrtc.offer"
  | "webrtc.answer"
  | "webrtc.ice"
  | "webrtc.ice_complete"
  | "call.hangup"
  | "call.cancel"
  | "call.ended"
  | "call.error"
  | "call.telemetry"
  | "call.agent_state"
  | "call.presentation"
  | "call.interrupt"
  | "call.settings"
  | "ping"
  | "pong";

interface SignalEnvelope {
  v: 1;
  id: string;
  type: SignalType;
  payload: Record<string, unknown>;
}

interface TicketRedemption {
  browserSignalUrl: string;
  browserAccessKey: string;
  iceServers: unknown;
  sessionExpiresAt: string;
  sessionCapSeconds: number;
  inactivitySeconds: number;
  iceTransportPolicy: "all" | "relay";
}

type ViewName = "connecting" | "stage" | "card";

const CONNECT_STEPS = [
  "Connecting to your agent…",
  "Opening a secure channel…",
  "Finding the best audio route…",
  "Securing your audio connection…",
  "Connected — verifying access…",
  "You're live",
];
// Timed steps advance on their own; the last two are milestone-driven.
const LAST_TIMED_STEP = 3;
const STEP_CONNECTED = 4;
const STEP_LIVE = 5;

const views: Record<ViewName, HTMLElement> = {
  connecting: required("view-connecting"),
  stage: required("view-stage"),
  card: required("view-card"),
};
const title = required<HTMLHeadingElement>("title");
const message = required<HTMLParagraphElement>("message");
const talkButton = required<HTMLButtonElement>("talk");
const controls = required<HTMLDivElement>("controls");
const stopButton = required<HTMLButtonElement>("stop");
const muteButton = required<HTMLButtonElement>("mute");
const hangupButton = required<HTMLButtonElement>("hangup");
const settingsButton = required<HTMLButtonElement>("settings");
const settingsMenu = required<HTMLDivElement>("settings-menu");
const speedOptions = required<HTMLDivElement>("speed-options");
const narrationOptions = required<HTMLDivElement>("narration-options");
const replyOptions = required<HTMLDivElement>("reply-options");
const menuMain = required<HTMLDivElement>("menu-main");
const menuVoices = required<HTMLDivElement>("menu-voices");
const voicesRow = required<HTMLButtonElement>("voices-row");
const voicesCurrent = required<HTMLSpanElement>("voices-current");
const voicesBack = required<HTMLButtonElement>("voices-back");
const voicesTitle = required<HTMLSpanElement>("voices-title");
const voicesMore = required<HTMLButtonElement>("voices-more");
const advancedRow = required<HTMLButtonElement>("advanced-row");
const helpPanel = required<HTMLDetailsElement>("help");
const diagnosticsPanel = required<HTMLDetailsElement>("diagnostics");
const recordDiagnostics = required<HTMLInputElement>("record-diagnostics");
const settingsNotice = required<HTMLParagraphElement>("settings-notice");
const voiceOptions = required<HTMLDivElement>("voice-options");
const voiceNotice = required<HTMLParagraphElement>("voice-notice");
const stackEl = required<HTMLElement>("stack");
const orbStateEl = required<HTMLElement>("orb-state");
const sleepVideo = required<HTMLVideoElement>("sleep-video");
const connectTitle = required<HTMLHeadingElement>("connect-title");
const connectStep = required<HTMLParagraphElement>("connect-step");
const stageHint = required<HTMLParagraphElement>("stage-hint");
const feed = required<HTMLDivElement>("feed");
const transcript = new Transcript(feed, drawBlock);
const workerPanel = new WorkerPanel({
  onCancel: (jobId) => send("call.settings", callSettingPayload({ kind: "cancelJob", jobId })),
  onSelect: (jobId) => send("call.settings", callSettingPayload({ kind: "selectJob", jobId })),
});

const OrbRenderer = new URLSearchParams(location.search).get("orb") === "classic" ? Orb : ScribbleOrb;
const orb = new OrbRenderer(required<HTMLCanvasElement>("orbcv"));

// Module state, not a lazy declaration: buildSpeedOptions() runs during
// module init and reads this, so a later `let` would be in its TDZ and
// take the whole page down before boot.
let currentSpeed: SpeedStep = DEFAULT_SPEED;
let currentNarration: NarrationStyle = DEFAULT_NARRATION;
let currentReplyLength: ReplyLength = DEFAULT_REPLY_LENGTH;
let currentVoice: VoiceState | null = null;
let currentSettings: SettingsState = { available: false, notice: "", url: "" };
let renderSettingsMenu = (): void => undefined;
let socket: WebSocket | null = null;
let peer: RTCPeerConnection | null = null;
let microphone: MediaStream | null = null;
let remoteAudio: HTMLAudioElement | null = null;
let audioContext: AudioContext | null = null;
let localAnalyser: VoiceMeter | null = null;
let remoteAnalyser: VoiceMeter | null = null;
let offerSent = false;
let localDescriptionSent = false;
let iceConnectedOnce = false;
let iceRestartAttempts = 0;
let iceRestarting = false;
let iceRestartTimer: number | null = null;
const pendingLocalCandidates: Array<RTCIceCandidateInit | null> = [];
let activeSignalUrl: string | null = null;
let activeAccessKey: string | null = null;
let activeIceServers: RTCIceServer[] = DEFAULT_ICE_SERVERS;
let heartbeatTimer: number | null = null;
let statsTimer: number | null = null;
let ended = false;
const pageStartedAt = performance.now();
// Routing id of the active call (redacted correlation key component).
let activeRoutingId = "unknown";
const pendingTelemetry: Array<Record<string, unknown>> = [];

// Connecting-page step cycler.
let connectTimer: number | null = null;
let connectStepIndex = -1;
let connectFadeTimer: number | null = null;

// Guard against a double tap starting two calls.
let starting = false;

// Live-call orb state loop.
let orbStateTimer: number | null = null;
let lastLocalSpeechAt = 0;
let lastRemoteSpeechAt = 0;
let agentStateOverride: OrbState | null = null;
let agentStateOverrideUntil = 0;
let lastSnapshotKey = "";
let lastSnapshot: PresentationSnapshot | null = null;
let agentDotsShown = false;
let micMuted = false;

const callRoute = parseCallRoute(location.hash);
if (callRoute?.kind === "ticket") {
  history.replaceState(null, "", `${location.pathname}${location.search}`);
}
const browserPolicy = browserRuntimePolicy(navigator.userAgent);

talkButton.addEventListener("click", () => {
  void startCall();
});
stopButton.addEventListener("click", requestStop);
muteButton.addEventListener("click", toggleMute);
// User-initiated end is a cancel (r2-browser-delivery): call.cancel makes
// the worker end the session with user_cancel so BOTH peers observe the
// cancelled state rather than a peer drop.
hangupButton.addEventListener("click", () => endCall("you_hung_up", true, "cancel"));
renderSettingsMenu = buildSettingsMenu();
required<HTMLButtonElement>("help-row").addEventListener("click", () => {
  setSettingsOpen(false);
  helpPanel.open = true;
  diagnosticsPanel.open = true;
  helpPanel.querySelector("summary")?.focus();
});
recordDiagnostics.addEventListener("change", () => {
  send("call.settings", callSettingPayload({ kind: "diagnostics", value: recordDiagnostics.checked }));
});
settingsButton.addEventListener("click", toggleSettings);
document.addEventListener("click", closeSettingsOnOutsideClick);
window.addEventListener("keydown", onStageKey);
window.addEventListener("pagehide", () => endCall("page_closed", true));

/* ------------------------------------------------------------------ */
/* Views                                                               */
/* ------------------------------------------------------------------ */

function showView(name: ViewName): void {
  for (const [key, view] of Object.entries(views)) {
    view.hidden = key !== name;
  }
  if (name !== "connecting") stopConnecting();
  if (name === "stage" && orb.available) orb.start();
  if (name !== "stage") stopOrbStateLoop();
  if (name !== "stage") window.scrollTo(0, 0);
}

/* ------------------------------------------------------------------ */
/* Connecting sequence                                                 */
/* ------------------------------------------------------------------ */

function startConnecting(): void {
  showView("connecting");
  talkButton.hidden = true;
  connectTitle.textContent = "Connecting your agent to voice";
  // Clear the join subtext instantly — never crossfade it into the
  // connecting steps (reads as overlapping text).
  connectStep.textContent = "";
  connectStep.classList.remove("fading");
  connectStepIndex = -1;
  void sleepVideo.play().catch(() => undefined);
  setConnectStep(0);
  if (connectTimer !== null) window.clearInterval(connectTimer);
  connectTimer = window.setInterval(() => {
    if (connectStepIndex < LAST_TIMED_STEP) setConnectStep(connectStepIndex + 1);
  }, 1400);
}

function setConnectStep(index: number): void {
  if (index === connectStepIndex) return;
  connectStepIndex = index;
  connectStep.classList.add("fading");
  if (connectFadeTimer !== null) window.clearTimeout(connectFadeTimer);
  connectFadeTimer = window.setTimeout(() => {
    connectStep.textContent = CONNECT_STEPS[index] ?? "";
    connectStep.classList.remove("fading");
  }, 220);
}

function advanceConnectTo(index: number): void {
  if (index > connectStepIndex) setConnectStep(index);
}

function stopConnecting(): void {
  if (connectTimer !== null) window.clearInterval(connectTimer);
  connectTimer = null;
  if (connectFadeTimer !== null) window.clearTimeout(connectFadeTimer);
  connectFadeTimer = null;
  connectStepIndex = -1;
  sleepVideo.pause();
}

/* ------------------------------------------------------------------ */
/* Call setup                                                          */
/* ------------------------------------------------------------------ */

async function startCall(): Promise<void> {
  if (!activeSignalUrl || ended || starting) return;
  starting = true;
  const talkStartedAt = performance.now();
  telemetry("talk_tapped");
  talkButton.disabled = true;
  setPage("requesting microphone");
  startConnecting();

  try {
    audioContext = new AudioContext();
    await audioContext.resume();
    microphone = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    localAnalyser = makeAnalyser(microphone);
    setDiag("diag-mic", "allowed");
    telemetry("microphone_ready", {
      permissionMs: elapsedSince(talkStartedAt),
    });
  } catch (error) {
    starting = false;
    fail(STATE_COPY.permission.heading, STATE_COPY.permission.detail, errorName(error));
    return;
  }

  setPage("connecting");
  starting = false;
  iceConnectedOnce = false;
  iceRestartAttempts = 0;
  iceRestarting = false;
  clearIceRestartTimer();

  peer = new RTCPeerConnection({
    iceServers: activeIceServers,
    bundlePolicy: "max-bundle",
    iceTransportPolicy: browserPolicy.iceTransportPolicy,
  });
  for (const track of microphone.getAudioTracks()) peer.addTrack(track, microphone);
  preferOpus(peer);
  // Transcript and agent state come over this channel, end to end with the
  // host, never through the call service's signaling relay.
  const display = peer.createDataChannel(DISPLAY_CHANNEL_LABEL, { ordered: true });
  display.addEventListener("message", (event) => {
    if (typeof event.data === "string") receiveDisplay(event.data);
  });

  remoteAudio = document.createElement("audio");
  remoteAudio.autoplay = true;
  remoteAudio.setAttribute("playsinline", "");
  remoteAudio.hidden = true;
  document.body.append(remoteAudio);

  peer.addEventListener("track", (event) => {
    if (!remoteAudio) return;
    minimizeReceiverBuffer(event.receiver);
    const stream = event.streams[0] ?? new MediaStream([event.track]);
    remoteAudio.srcObject = stream;
    remoteAnalyser = makeAnalyser(stream);
    telemetry("remote_track");
    remoteAudio.addEventListener(
      "playing",
      () => telemetry("remote_audio_playing"),
      { once: true },
    );
    void remoteAudio.play().catch((error) => {
      setDiag("diag-error", `audio_playback:${errorName(error)}`);
    });
  });
  peer.addEventListener("icecandidate", (event) => {
    const candidate = event.candidate?.toJSON() ?? null;
    if (!localDescriptionSent) {
      pendingLocalCandidates.push(candidate);
      return;
    }
    sendLocalCandidate(candidate);
  });
  peer.addEventListener("iceconnectionstatechange", () => {
    handleIceStateChange();
  });
  peer.addEventListener("connectionstatechange", () => {
    if (!peer) return;
    setDiag("diag-peer", peer.connectionState);
    telemetry("connection_state", { connectionState: peer.connectionState });
    if (peer.connectionState === "connected") {
      iceConnectedOnce = true;
      iceRestartAttempts = 0;
      iceRestarting = false;
      clearIceRestartTimer();
      advanceConnectTo(STEP_CONNECTED);
      showLive();
      telemetry("peer_connected");
      void sendTransportStats();
      if (statsTimer === null) {
        statsTimer = window.setInterval(() => void sendTransportStats(), 5_000);
      }
    }
    if (peer.connectionState === "failed") {
      const decision = decideIceRestart({
        iceState: peer.iceConnectionState,
        connectedOnce: iceConnectedOnce,
        restartAttempts: iceRestartAttempts,
        ended,
      });
      if (decision.action === "restart") {
        void restartIce();
        return;
      }
      if (decision.action === "give_up" || !iceConnectedOnce) {
        fail(
          "The call connection failed.",
          "Check your connection and run the development command again.",
          "peer_failed",
        );
      }
    }
  });

  socket = new WebSocket(activeSignalUrl);
  socket.addEventListener("open", () => {
    setDiag("diag-signal", "connected");
    if (activeAccessKey) send("call.auth", { role: "browser", key: activeAccessKey });
    flushTelemetry();
    telemetry("signal_connected");
    heartbeatTimer = window.setInterval(() => send("ping"), 15_000);
  });
  socket.addEventListener("message", (event) => void receiveSignal(String(event.data)));
  socket.addEventListener("close", () => {
    setDiag("diag-signal", "closed");
    if (!ended && peer?.connectionState !== "closed") {
      scheduleSignalingReconnect();
    }
  });
  socket.addEventListener("error", () => {
    setDiag("diag-signal", "failed");
    setDiag("diag-error", "signal_failed");
  });
}

/* ------------------------------------------------------------------ */
/* Bounded signaling reconnect (r2-browser-connectivity)               */
/* ------------------------------------------------------------------ */

/** Reconnect is bounded and backoff-paced; exhausting attempts fails with
 * actionable copy. Cancelling the call (or a successful re-open) cancels
 * pending attempts. Policy lives in reconnect.ts (pure, tested). */
let signalReconnectAttempts = 0;
let signalReconnectTimer: number | null = null;

function cancelSignalingReconnect(): void {
  if (signalReconnectTimer !== null) {
    window.clearTimeout(signalReconnectTimer);
    signalReconnectTimer = null;
  }
}

function scheduleSignalingReconnect(): void {
  if (ended || signalReconnectTimer !== null) return;
  const decision = decideReconnect(signalReconnectAttempts);
  if (decision.action === "exhausted") {
    telemetry("signal_reconnect_exhausted", {
      attempts: decision.attempts,
      category: "signaling_unreachable",
    });
    fail(
      "The call connection was lost.",
      "We couldn't reconnect automatically. Check your internet connection, then ask for a new call link.",
      "signal_reconnect_exhausted",
    );
    return;
  }
  const { attempt, delayMs: delay } = decision;
  stageHint.textContent = STATE_COPY.reconnecting.detail;
  setDiag("diag-conn", "reconnecting");
  telemetry("signal_reconnect_scheduled", { attempt, delayMs: delay });
  signalReconnectTimer = window.setTimeout(() => {
    signalReconnectTimer = null;
    if (ended) return;
    signalReconnectAttempts = attempt;
    telemetry("signal_reconnect_attempt", { attempt });
    if (!activeSignalUrl) {
      scheduleSignalingReconnect();
      return;
    }
    try {
      socket = new WebSocket(activeSignalUrl);
      wireSignalSocket();
    } catch {
      scheduleSignalingReconnect();
    }
  }, delay);
}

function wireSignalSocket(): void {
  const active = socket;
  if (!active) return;
  active.addEventListener("open", () => {
    signalReconnectAttempts = 0;
    setDiag("diag-signal", "connected");
    if (activeAccessKey) send("call.auth", { role: "browser", key: activeAccessKey });
    flushTelemetry();
    telemetry("signal_connected");
    if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer);
    heartbeatTimer = window.setInterval(() => send("ping"), 15_000);
  });
  active.addEventListener("message", (event) => void receiveSignal(String(event.data)));
  active.addEventListener("close", () => {
    setDiag("diag-signal", "closed");
    if (!ended && peer?.connectionState !== "closed") {
      scheduleSignalingReconnect();
    }
  });
  active.addEventListener("error", () => {
    setDiag("diag-signal", "failed");
    setDiag("diag-error", "signal_failed");
  });
}

function makeAnalyser(stream: MediaStream): VoiceMeter | null {
  if (!audioContext) return null;
  try {
    const source = audioContext.createMediaStreamSource(stream);
    const analyser = new VoiceMeter(audioContext);
    source.connect(analyser.node);
    return analyser;
  } catch {
    return null;
  }
}

function minimizeReceiverBuffer(receiver: RTCRtpReceiver): void {
  const tunable = receiver as RTCRtpReceiver & {
    playoutDelayHint?: number;
    jitterBufferTarget?: number;
  };
  try {
    if ("playoutDelayHint" in tunable) tunable.playoutDelayHint = 0;
    if ("jitterBufferTarget" in tunable) tunable.jitterBufferTarget = 0;
  } catch {
    // Experimental receiver hints vary by browser. Continuous RTP silence
    // still prevents the large starvation-driven buffer if a setter rejects.
  }
}

async function receiveSignal(raw: string): Promise<void> {
  const envelope = parseEnvelope(raw);
  if (!envelope || !peer) return;
  try {
    if (envelope.type === "peer.ready" && !offerSent) {
      telemetry("peer_ready_received");
      offerSent = true;
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      send("webrtc.offer", { sdp: offer.sdp, type: offer.type });
      telemetry("offer_sent");
      localDescriptionSent = true;
      for (const candidate of pendingLocalCandidates.splice(0)) sendLocalCandidate(candidate);
    } else if (envelope.type === "webrtc.answer") {
      const sdp = String(envelope.payload.sdp ?? "");
      await peer.setRemoteDescription({ type: "answer", sdp });
      telemetry("answer_received");
    } else if (envelope.type === "webrtc.ice") {
      const candidate = envelope.payload.candidate as RTCIceCandidateInit | undefined;
      if (candidate) await peer.addIceCandidate(candidate);
    } else if (envelope.type === "webrtc.ice_complete") {
      await peer.addIceCandidate(null);
    } else if (envelope.type === "call.hangup") {
      finishCall(
        "The other side ended the call.",
        "You can return to the text conversation.",
        "peer_hung_up",
      );
    } else if (envelope.type === "call.cancel") {
      // Explicit user cancel (r2-browser-state-machine): distinct copy.
      finishCall(STATE_COPY.cancelled.heading, STATE_COPY.cancelled.detail, "user_cancel");
    } else if (envelope.type === "call.ended") {
      finishFromServer(String(envelope.payload.reason ?? "call_ended"));
    } else if (envelope.type === "call.error") {
      fail(
        "The other side couldn't start audio.",
        "Run the development command again and try a fresh call.",
        String(envelope.payload.code ?? "agent_error"),
      );
    }
  } catch (error) {
    fail(
      "The call couldn't finish connecting.",
      "Run the development command again and try a fresh call.",
      errorName(error),
    );
  }
}

function applyOrbOverride(state: string): void {
  const orbState = hostOrbState(state);
  if (!orbState) return;
  agentStateOverride = orbState;
  agentStateOverrideUntil = performance.now() + 8_000;
}

const DISPLAY_CHANNEL_LABEL = "sleeptalk-display";

function receiveDisplay(raw: string): void {
  const envelope = parseEnvelope(raw);
  if (!envelope) return;
  if (envelope.type === "call.agent_state") handleAgentState(envelope.payload);
  else if (envelope.type === "call.presentation") handlePresentation(envelope.payload);
}

function handleAgentState(payload: Record<string, unknown>): void {
  const state = payload.state;
  setDiag(
    "diag-agent",
    [typeof state === "string" ? state : "?", payload.task, payload.detail]
      .filter((part) => typeof part === "string" && part)
      .join(" · "),
  );
  if (typeof state === "string") applyOrbOverride(state);
}

function handlePresentation(payload: Record<string, unknown>): void {
  const snapshot = parsePresentation(payload);
  if (!snapshot) return;
  currentVoice = snapshot.voice;
  currentSettings = snapshot.settings;
  applyOrbOverride(snapshot.presence);
  lastSnapshot = snapshot;
  currentSpeed = snapshot.controls.ttsSpeed;
  currentNarration = snapshot.controls.narrationStyle;
  currentReplyLength = snapshot.controls.replyLength;
  recordDiagnostics.checked = snapshot.diagnostics === true;
  recordDiagnostics.disabled = ended;
  renderSettingsMenu();
  workerPanel.render(snapshot.jobs, snapshot.selectedJob);
  const nextKey = snapshotKey(snapshot);
  if (nextKey === lastSnapshotKey) return;
  lastSnapshotKey = nextKey;
  renderFeed(snapshot);
}

function renderHeader(stack: string[]): void {
  const shown = stack.filter(Boolean);
  stackEl.textContent = shown.join(" · ");
  stackEl.hidden = shown.length === 0;
}

function paintStateWord(state: OrbState): void {
  const word = micMuted ? "muted" : state;
  orbStateEl.textContent = word;
  orbStateEl.classList.toggle("live", word !== "idle");
}

function renderFeed(snapshot: PresentationSnapshot): void {
  renderHeader(snapshot.stack);
  paintStateWord(snapshot.presence);
  transcript.render(snapshot.lines, snapshot.speaker);
  paintFeedTail(performance.now());
  scrollLatestIntoView();
}

/** A block of the transcript other than a reply (those are drawn by Transcript). */
function drawBlock(block: Block): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = `block ${block.kind}`;
  block.lines.forEach((line, index) => {
    wrap.appendChild(line.kind === "worker" ? renderWorkerLine(line, index === 0) : renderLine(line));
  });
  return wrap;
}

function scrollLatestIntoView(): void {
  const last = feed.lastElementChild;
  if (last instanceof HTMLElement) {
    last.scrollIntoView({ block: "end" });
  }
}

/** The feed's last row: the caller's words or dots, or the agent's dots while
 * its answer is late. */
function paintFeedTail(now: number): void {
  const snapshot = lastSnapshot;
  agentDotsShown = snapshot !== null && agentWaiting({
    presence: snapshot.presence,
    hearing: snapshot.hearing,
    partial: snapshot.partial,
    lines: snapshot.lines,
    quietMs: lastRemoteSpeechAt > 0 ? now - lastRemoteSpeechAt : Number.POSITIVE_INFINITY,
    shown: agentDotsShown,
  });
  paintUserSpeaking(
    userSpeakingHint({
      partial: snapshot?.partial ?? "",
      hearing: snapshot?.hearing ?? false,
      agentWaiting: agentDotsShown,
      speaker: snapshot?.speaker,
    }),
  );
}

function speakingHintKey(hint: UserSpeakingHint): string {
  switch (hint.kind) {
    case "none":
      return "none";
    case "dots":
      return "dots";
    case "waiting":
      return `waiting:${hint.speaker}`;
    case "partial":
      return `partial:${hint.text}`;
    default: {
      const _exhaustive: never = hint;
      return _exhaustive;
    }
  }
}

function renderPartial(text: string): HTMLElement {
  const el = document.createElement("p");
  el.className = "line user partial";
  el.dataset.speakingHint = `partial:${text}`;
  el.appendChild(gutter("you"));
  el.appendChild(bodySpan(text));
  return el;
}

function renderSpeakingDots(): HTMLElement {
  const el = document.createElement("p");
  el.className = "line user speaking-hint";
  el.dataset.speakingHint = "dots";
  el.setAttribute("role", "status");
  el.setAttribute("aria-label", "Listening");
  el.appendChild(gutter("you"));
  el.appendChild(dotsBody());
  return el;
}

/** The agent's row while its answer is on the way. */
function renderAgentDots(speaker: string): HTMLElement {
  const el = document.createElement("p");
  el.className = "line assistant speaking-hint waiting";
  el.dataset.speakingHint = `waiting:${speaker}`;
  el.setAttribute("role", "status");
  el.setAttribute("aria-label", `${speaker} is working on it`);
  el.appendChild(gutter(speaker));
  el.appendChild(dotsBody());
  return el;
}

function dotsBody(): HTMLElement {
  const body = document.createElement("span");
  body.className = "line-body speaking-dots";
  body.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 3; index += 1) {
    body.appendChild(document.createElement("span"));
  }
  return body;
}

function paintUserSpeaking(hint: UserSpeakingHint): void {
  const existing = feed.querySelector("[data-speaking-hint]");
  const key = speakingHintKey(hint);
  if (existing instanceof HTMLElement && existing.dataset.speakingHint === key) return;
  existing?.remove();
  switch (hint.kind) {
    case "none":
      return;
    case "dots":
      feed.appendChild(renderSpeakingDots());
      scrollLatestIntoView();
      return;
    case "waiting":
      feed.appendChild(renderAgentDots(hint.speaker));
      scrollLatestIntoView();
      return;
    case "partial":
      feed.appendChild(renderPartial(hint.text));
      scrollLatestIntoView();
      return;
    default: {
      const _exhaustive: never = hint;
      return _exhaustive;
    }
  }
}

function renderLine(line: PresentationLine): HTMLElement {
  const el = document.createElement(line.kind === "tool" ? "div" : "p");
  el.className = `line ${line.kind}`;
  if (line.kind === "user") {
    el.appendChild(gutter("you"));
    el.appendChild(bodySpan(line.text));
    return el;
  }
  el.appendChild(gutter(""));
  el.appendChild(bodySpan(line.text));
  return el;
}

/** A job's milestone in the conversation (started, needs you, done):
 * tapping it opens the worker panel, where its working detail lives. */
function renderWorkerLine(line: WorkerLine, first: boolean): HTMLElement {
  const el = document.createElement("p");
  el.className = `line worker ${line.phase}`;
  el.appendChild(gutter(first ? "worker" : ""));
  const body = bodySpan(line.text);
  for (const link of line.links) {
    body.appendChild(document.createElement("br"));
    body.appendChild(workerLink(link));
  }
  el.appendChild(body);
  el.title = "Open the worker";
  el.addEventListener("click", (event) => {
    if (!(event.target instanceof HTMLAnchorElement)) workerPanel.open();
  });
  return el;
}

function gutter(label: string): HTMLElement {
  const el = document.createElement("span");
  el.className = "gutter";
  el.textContent = label;
  return el;
}

function bodySpan(text: string): HTMLElement {
  const el = document.createElement("span");
  el.className = "line-body";
  el.textContent = text;
  return el;
}

function isLocalPreview(): boolean {
  const host = location.hostname;
  if (host !== "localhost" && host !== "127.0.0.1") return false;
  const hash = location.hash || `#${location.href.split("#")[1] ?? ""}`;
  return hash === "#preview-frame" || hash.startsWith("#preview-frame?");
}

const PREVIEW_SNAPSHOT = {
  presence: "speaking",
  voice: {
    provider: "cartesia",
    voiceId: "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
    name: "Skylar",
    curated: [
      { name: "Skylar", description: "Approachable American female.", voice_id: "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4", default: true },
      { name: "Daniel", description: "Clear, crisp male voice.", voice_id: "47c38ca4-5f35-497b-b1a3-415245fb35e1", default: false },
      { name: "Gemma", description: "Confident British female.", voice_id: "preview-gemma", default: false },
    ],
    libraryUrl: "",
    notice: "",
  },
  settings: { available: true, notice: "", url: "" },
  jobs: [],
  muted: false,
  hearing: false,
  partial: "and also the stack",
  speaker: "agent",
  stack: ["pi/opus", "deepgram/nova-3", "cartesia/sonic-3"],
  lines: [
    { kind: "user", text: "show me the frame" },
    {
      kind: "assistant",
      text: "Here's the voice frame.",
      spokenIndex: 4,
      words: [
        { text: "Here's" },
        { text: "the" },
        { text: "voice" },
        { text: "frame." },
      ],
    },
    { kind: "tool", text: "read  presentation.py", phase: "completed" },
    {
      kind: "assistant",
      text: "New words stream in as the reply grows.",
      spokenIndex: 0,
      words: [
        { text: "New" },
        { text: "words" },
        { text: "stream" },
        { text: "in" },
        { text: "as" },
        { text: "the" },
        { text: "reply" },
        { text: "grows." },
      ],
    },
    { kind: "user", text: "keep talking so the page has to scroll" },
    {
      kind: "assistant",
      text: "The feed grows with the call. The page scrollbar is the one that moves.",
      spokenIndex: 14,
      words: [
        { text: "The" },
        { text: "feed" },
        { text: "grows" },
        { text: "with" },
        { text: "the" },
        { text: "call." },
        { text: "The" },
        { text: "page" },
        { text: "scrollbar" },
        { text: "is" },
        { text: "the" },
        { text: "one" },
        { text: "that" },
        { text: "moves." },
      ],
    },
    { kind: "user", text: "and the last line should sit above the orb" },
    {
      kind: "assistant",
      text: "Yes. New words land above the dock, not underneath it.",
      spokenIndex: 12,
      words: [
        { text: "Yes." },
        { text: "New" },
        { text: "words" },
        { text: "land" },
        { text: "above" },
        { text: "the" },
        { text: "dock," },
        { text: "not" },
        { text: "underneath" },
        { text: "it." },
      ],
    },
    { kind: "user", text: "one more so I can scroll back up" },
    {
      kind: "assistant",
      text: "Scroll the page. … The orb and the pills stay put.",
      // A reply shortened for speech: the fold holds the whole answer.
      full: "Scroll the page. Older lines move up under the header while new ones land above the dock. The orb and the pills stay put.",
      spokenIndex: 11,
      words: [
        { text: "Scroll" },
        { text: "the" },
        { text: "page." },
        { text: "…" },
        { text: "The" },
        { text: "orb" },
        { text: "and" },
        { text: "the" },
        { text: "pills" },
        { text: "stay" },
        { text: "put." },
      ],
    },
  ],
} satisfies Record<string, unknown>;

function previewFlag(name: string): boolean {
  const raw = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  const queryAt = raw.indexOf("?");
  if (queryAt < 0) return false;
  return new URLSearchParams(raw.slice(queryAt + 1)).has(name);
}

function previewListening(): boolean {
  return previewFlag("listening");
}

/** `#preview-frame?worker`: a job mid-run, to style the worker panel
 * without an agent. `&done` shows it finished. */
function previewWorker(done: boolean): Record<string, unknown> {
  const start = Date.now() - 74_000;
  const at = (seconds: number) => start + seconds * 1000;
  const entries: Record<string, unknown>[] = [
    { kind: "thinking", text: "A claw machine needs a cabinet, a claw on two axes and real physics. cannon-es for the prizes, Three.js to draw it.", startedAt: at(1) },
    { kind: "step", text: "write_file · claw-machine/index.html", steps: 1, startedAt: at(38) },
    { kind: "said", text: "Written. Let me open it and check the console for errors.", startedAt: at(52) },
    { kind: "step", text: "browser_navigate · file:///claw-machine/index.html", steps: 2, startedAt: at(53) },
    { kind: "step", text: "browser_console", steps: 3, startedAt: at(57) },
    { kind: "thinking", text: "THREE is undefined: r160 only ships ES modules, so a classic script tag gets nothing.", startedAt: at(59) },
    { kind: "step", text: "web_search · Searching the web for three.js r147 UMD build", steps: 4, startedAt: at(66) },
  ];
  if (done) {
    entries.push(
      { kind: "said", text: "The game runs: move the claw with the arrows, drop with space. Prizes stack and tumble with real physics.", startedAt: at(70) },
      { kind: "done", text: "Done.", links: ["http://localhost:8765/claw-machine/", "/Users/you/claw-machine/index.html"], startedAt: at(72) },
    );
  }
  return { jobId: "job-1", title: "claw machine game", phase: done ? "completed" : "running", startedAt: start, steps: 4, entries };
}

/** `#preview-frame?jobs`: several jobs as tabs — the first selected and
 * running, one waiting on the caller, one finished and unread, one queued. */
function previewJobs(): Record<string, unknown>[] {
  const start = Date.now() - 31 * 60_000;
  return [
    { ...previewWorker(false), jobId: "job-1", title: "latency run", startedAt: start },
    {
      jobId: "job-2", title: "six flags valuation", phase: "running", startedAt: Date.now() - 4 * 60_000, steps: 6,
      waiting: "Waiting for your OK: Run npm install",
      entries: [
        { kind: "step", text: "web_search · Six Flags 2025 annual report", steps: 6, startedAt: Date.now() - 200_000 },
        { kind: "ask", text: "Waiting for your OK: Run npm install", startedAt: Date.now() - 20_000 },
      ],
    },
    {
      jobId: "job-3", title: "summary bug", phase: "completed", startedAt: Date.now() - 9 * 60_000, steps: 12,
      entries: [{ kind: "done", text: "Done.", links: ["/Users/you/sleeptalk/sleeptalk/summarize.py"], startedAt: Date.now() - 60_000 }],
    },
    { jobId: "job-4", title: "todo app", phase: "queued", steps: 0, entries: [] },
  ];
}

function showPreviewFrame(): void {
  ended = false;
  lastSnapshotKey = "";
  transcript.reset();
  const listening = previewListening();
  setPage("preview");
  setDiag("diag-ticket", "local preview");
  showView("stage");
  talkButton.hidden = true;
  controls.hidden = false;
  orb.setState(listening ? "listening" : "speaking");
  startOrbStateLoop();
  const several = previewFlag("jobs");
  const worker = previewFlag("worker") || several;
  const done = previewFlag("done");
  const base = listening
    ? { ...PREVIEW_SNAPSHOT, presence: "listening", partial: "", hearing: true }
    : previewFlag("waiting")
      ? {
          ...PREVIEW_SNAPSHOT,
          presence: "thinking",
          partial: "",
          lines: [...PREVIEW_SNAPSHOT.lines, { kind: "user", text: "why is the build red" }, { kind: "progress", text: "Let me look." }],
        }
      : PREVIEW_SNAPSHOT;
  const snapshot = parsePresentation(
    worker
      ? {
          ...base,
          lines: [
            ...base.lines,
            { kind: "worker", text: "Working on claw machine game.", phase: "started", callId: "job-1" },
            ...(done ? [{ kind: "worker", text: "Done.", phase: "completed", callId: "job-1", links: ["http://localhost:8765/claw-machine/"] }] : []),
          ],
          jobs: several ? previewJobs() : [previewWorker(done)],
          ...(several ? { selectedJob: "job-1" } : {}),
        }
      : base,
  );
  if (!snapshot) return;
  lastSnapshot = snapshot;
  lastSnapshotKey = snapshotKey(snapshot);
  currentVoice = snapshot.voice;
  currentSettings = snapshot.settings;
  renderSettingsMenu();
  applyOrbOverride(snapshot.presence);
  renderFeed(snapshot);
  workerPanel.render(snapshot.jobs, snapshot.selectedJob);
  setupVoicePreview();
}

function setupVoicePreview(): void {
  if (document.getElementById("voice-preview")) return;
  const button = document.createElement("button");
  button.id = "voice-preview";
  button.className = "pill";
  button.textContent = "Play voice preview";
  const audio = document.createElement("audio");
  audio.src = "/orb-voice-preview.mp3";
  audio.preload = "auto";
  audio.hidden = true;
  document.body.append(audio);
  remoteAudio = audio;
  stageHint.replaceChildren(button);
  button.addEventListener("click", async () => {
    try {
      if (!audioContext || audioContext.state === "closed") {
        audioContext = new AudioContext();
        const source = audioContext.createMediaElementSource(audio);
        remoteAnalyser = new VoiceMeter(audioContext);
        source.connect(remoteAnalyser.node);
        remoteAnalyser.node.connect(audioContext.destination);
      }
      await audioContext.resume();
      if (audio.paused) await audio.play();
      else audio.pause();
    } catch (error) {
      setDiag("diag-error", `preview_audio:${errorName(error)}`);
    }
  });
  audio.addEventListener("play", () => { button.textContent = "Pause voice preview"; });
  audio.addEventListener("pause", () => { button.textContent = "Play voice preview"; });
  audio.addEventListener("ended", () => { button.textContent = "Replay voice preview"; });
}

function startOrbStateLoop(): void {
  if (orbStateTimer !== null || !orb.available) return;
  let lastPaint = 0;
  const frame = (now: number): void => {
    const preview = isLocalPreview();
    const playback = remoteAudio && !remoteAudio.paused && !remoteAudio.muted && remoteAudio.volume > 0
      ? remoteAnalyser?.read() ?? SILENCE : SILENCE;
    const remote = preview && previewListening() ? SILENCE : playback;
    const local = preview && previewListening() ? playback
      : micMuted ? SILENCE : localAnalyser?.read() ?? SILENCE;
    if (remote.level > 0.04) lastRemoteSpeechAt = now;
    if (local.level > 0.05) lastLocalSpeechAt = now;

    if (agentStateOverride && now > agentStateOverrideUntil) agentStateOverride = null;
    // A host that is still thinking sends nothing new while it waits on its
    // agent; its last word stands rather than lapsing to "idle".
    const hostState = agentStateOverride ?? (lastSnapshot?.presence === "thinking" ? "thinking" : null);
    const state = voiceState({
      host: preview ? null : hostState,
      remoteLevel: remote.level,
      localLevel: local.level,
      now,
      lastRemoteSpeechAt,
      lastLocalSpeechAt,
    });
    const signal = state === "speaking" ? remote : state === "listening" ? local : SILENCE;
    orb.setState(state);
    if (orb instanceof ScribbleOrb) orb.setVoice(signal);
    else orb.setLevel(signal.level);
    if (now - lastPaint >= 100) {
      lastPaint = now;
      paintStateWord(state);
      paintFeedTail(now);
    }
    orbStateTimer = requestAnimationFrame(frame);
  };
  orbStateTimer = requestAnimationFrame(frame);
}

function stopOrbStateLoop(): void {
  if (orbStateTimer !== null) cancelAnimationFrame(orbStateTimer);
  orbStateTimer = null;
  agentStateOverride = null;
  agentDotsShown = false;
  paintUserSpeaking({ kind: "none" });
  lastLocalSpeechAt = 0;
  lastRemoteSpeechAt = 0;
  orb.setState("idle");
  orb.setLevel(0);
}

/* ------------------------------------------------------------------ */
/* Live / end states                                                   */
/* ------------------------------------------------------------------ */

function showLive(): void {
  setPage("live");
  if (connectStepIndex >= 0) {
    // Still on the connecting page: flash "You're live" before the swap.
    advanceConnectTo(STEP_LIVE);
    window.setTimeout(() => enterLiveStage(), 900);
  } else {
    enterLiveStage();
  }
}

function enterLiveStage(): void {
  if (ended) return;
  showView("stage");
  talkButton.hidden = true;
  controls.hidden = false;
  stageHint.textContent = "";
  transcript.reset();
  lastSnapshot = null;
  lastSnapshotKey = "";
  orb.setState("idle");
  startOrbStateLoop();
}

function toggleMute(): void {
  const track = microphone?.getAudioTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  micMuted = !track.enabled;
  muteButton.classList.toggle("muted", micMuted);
  const muteLabel = micMuted ? "Unmute (M)" : "Mute (M)";
  const label = muteButton.querySelector(".ctl-label");
  if (label) label.textContent = micMuted ? "Unmute" : "Mute";
  muteButton.title = muteLabel;
  muteButton.setAttribute("aria-label", muteLabel);
  setDiag("diag-mic", micMuted ? "muted" : "live");
  paintStateWord(agentStateOverride ?? "listening");
}

function typingInField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable;
}

function onStageKey(event: KeyboardEvent): void {
  if (views.stage.hidden || ended || typingInField(event.target)) return;
  if (event.key === "Escape" && !settingsMenu.hidden) {
    // Dismiss the menu rather than the call: Escape ending a call from an
    // open settings panel would be an expensive surprise.
    event.preventDefault();
    setSettingsOpen(false);
    settingsButton.focus();
    return;
  }
  if (event.key === " " || event.key === "s" || event.key === "S") {
    event.preventDefault();
    requestStop();
    return;
  }
  if (event.key === "m" || event.key === "M") {
    event.preventDefault();
    toggleMute();
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    endCall("you_hung_up", true, "cancel");
  }
}

/**
 * One radio group in the settings menu.
 *
 * Playback speed, tool narration, and response length are the same control
 * three times over: a row of choices, one of them checked, one
 * `call.settings` key sent on click. Each button closes over its own value,
 * so the checked state compares against that value rather than re-parsing
 * it back out of the DOM.
 */
function buildSettingsGroup<T>(
  container: HTMLDivElement,
  values: readonly T[],
  label: (value: T) => string,
  selected: () => T,
  choose: (value: T) => void,
): () => void {
  const buttons: Array<readonly [T, HTMLButtonElement]> = [];
  function render(): void {
    for (const [value, button] of buttons) {
      button.setAttribute("aria-checked", String(value === selected()));
    }
  }
  for (const value of values) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "settings-option";
    button.setAttribute("role", "menuitemradio");
    button.textContent = label(value);
    button.addEventListener("click", () => {
      // The menu stays open: speed, narration and length are often set together.
      choose(value);
      render();
    });
    container.append(button);
    buttons.push([value, button]);
  }
  render();
  return render;
}

function buildSettingsMenu(): () => void {
  const renderSpeed = buildSettingsGroup(
    speedOptions,
    SPEED_STEPS,
    speedLabel,
    () => currentSpeed,
    (step) => {
      currentSpeed = step;
      // The host snaps this again, so an unreachable value cannot desync the menu.
      send("call.settings", callSettingPayload({ kind: "ttsSpeed", value: step }));
      telemetry("speed_changed", { ttsSpeed: step });
    },
  );
  const renderNarration = buildSettingsGroup(
    narrationOptions,
    NARRATION_STYLES,
    narrationLabel,
    () => currentNarration,
    (style) => {
      currentNarration = style;
      send(
        "call.settings",
        callSettingPayload({ kind: "narrationStyle", value: style }),
      );
      telemetry("narration_changed", { narrationStyle: style });
    },
  );
  const renderReply = buildSettingsGroup(
    replyOptions,
    REPLY_LENGTHS,
    replyLengthLabel,
    () => currentReplyLength,
    (length) => {
      currentReplyLength = length;
      send("call.settings", callSettingPayload({ kind: "replyLength", value: length }));
      telemetry("reply_length_changed", { replyLength: length });
    },
  );
  const renderVoice = buildVoiceGroup();
  return () => {
    renderSpeed();
    renderNarration();
    renderReply();
    renderVoice();
  };
}

/** Voices live in a submenu: Voices › lists the live provider's voices
 * (already scoped to it and to the call's language by the host), one tap
 * each, and "More voices" opens the full settings page on the host.
 * Provider switching is not offered here: a new provider needs a key, and
 * keys never enter a call. */
function buildVoiceGroup(): () => void {
  let renderedFor = "";
  const choose = (voiceId: string) => {
    send("call.settings", callSettingPayload({ kind: "ttsVoice", value: voiceId }));
    voiceNotice.hidden = false;
    voiceNotice.textContent = "Trying that voice…";
    telemetry("voice_changed", { curated: true });
  };
  const openAdvanced = () => {
    send("call.settings", callSettingPayload({ kind: "openSettings" }));
    settingsNotice.hidden = false;
    settingsNotice.textContent = "Getting your settings ready…";
    showMenuView("main");
    telemetry("settings_opened", {});
  };
  voicesRow.addEventListener("click", () => showMenuView("voices"));
  voicesBack.addEventListener("click", () => showMenuView("main"));
  voicesMore.addEventListener("click", openAdvanced);
  advancedRow.addEventListener("click", openAdvanced);
  return () => {
    const voice = currentVoice;
    voicesRow.hidden = voice === null;
    advancedRow.hidden = !currentSettings.available;
    voicesMore.hidden = !currentSettings.available;
    if (currentSettings.notice) {
      settingsNotice.hidden = false;
      settingsNotice.textContent = currentSettings.notice;
      if (currentSettings.url) {
        // A tap opens it (a link the host just made can't open itself: browsers block that).
        const open = document.createElement("a");
        open.className = "settings-open";
        open.href = currentSettings.url;
        open.target = "_blank";
        open.rel = "noopener noreferrer";
        open.textContent = "Open settings";
        settingsNotice.append(" ", open);
      }
    }
    if (!voice) {
      voiceNotice.hidden = true;
      if (!menuVoices.hidden) showMenuView("main");
      return;
    }
    voicesCurrent.textContent = voice.name;
    voicesTitle.textContent = "Voices";
    const key = `${voice.provider}:${voice.curated.map((v) => v.voiceId).join(",")}`;
    if (key !== renderedFor) {
      renderedFor = key;
      voiceOptions.replaceChildren();
      for (const option of voice.curated) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "settings-option";
        button.setAttribute("role", "menuitemradio");
        button.dataset.voiceId = option.voiceId;
        button.textContent = option.default ? `${option.name} (default)` : option.name;
        button.title = option.description;
        button.addEventListener("click", () => choose(option.voiceId));
        voiceOptions.append(button);
      }
    }
    for (const button of Array.from(voiceOptions.children) as HTMLButtonElement[]) {
      button.setAttribute("aria-checked", String(button.dataset.voiceId === voice.voiceId));
    }
    if (voice.notice) {
      voiceNotice.hidden = false;
      voiceNotice.textContent = voice.notice;
    } else {
      voiceNotice.hidden = true;
    }
  };
}

function showMenuView(view: "main" | "voices"): void {
  menuMain.hidden = view !== "main";
  menuVoices.hidden = view !== "voices";
  (view === "voices" ? voicesBack : voicesRow).focus();
}

function setSettingsOpen(open: boolean): void {
  if (open) {
    // Every opening starts on the main view.
    menuMain.hidden = false;
    menuVoices.hidden = true;
  }
  settingsMenu.hidden = !open;
  settingsButton.setAttribute("aria-expanded", String(open));
}

function toggleSettings(event: MouseEvent): void {
  event.stopPropagation();
  setSettingsOpen(settingsMenu.hidden);
}

function closeSettingsOnOutsideClick(event: MouseEvent): void {
  if (settingsMenu.hidden) return;
  // composedPath, not contains(): switching between the main and Voices
  // views detaches nothing, but a re-rendered option may already be gone.
  if (event.composedPath().includes(settingsMenu)) return;
  setSettingsOpen(false);
}

function requestStop(): void {
  if (ended || views.stage.hidden) return;
  if (isLocalPreview()) {
    remoteAudio?.pause();
    return;
  }
  send("call.interrupt");
}

function clearIceRestartTimer(): void {
  if (iceRestartTimer !== null) {
    window.clearTimeout(iceRestartTimer);
    iceRestartTimer = null;
  }
}

function handleIceStateChange(): void {
  if (!peer) return;
  const iceState = peer.iceConnectionState;
  if (iceState === "connected" || iceState === "completed") {
    iceConnectedOnce = true;
    iceRestartAttempts = 0;
    iceRestarting = false;
    clearIceRestartTimer();
  }
  if (iceState === "disconnected" && iceConnectedOnce && !ended) {
    stageHint.textContent = STATE_COPY.reconnecting.detail;
    setDiag("diag-conn", "reconnecting");
  }
  setDiag("diag-ice", iceState);
  telemetry("ice_state", {
    iceState,
    category: categorizeFailure(`ice_${iceState}`),
  });
  const decision = decideIceRestart({
    iceState,
    connectedOnce: iceConnectedOnce,
    restartAttempts: iceRestartAttempts,
    ended,
  });
  if (decision.action === "wait") {
    clearIceRestartTimer();
    iceRestartTimer = window.setTimeout(() => {
      iceRestartTimer = null;
      if (peer?.iceConnectionState === "disconnected") void restartIce();
    }, decision.delayMs);
    return;
  }
  if (decision.action === "restart") {
    void restartIce();
    return;
  }
  if (decision.action === "give_up" || (iceState === "failed" && !iceConnectedOnce && !ended)) {
    fail(
      "The call couldn't connect.",
      "Check your connection and run the development command again.",
      "ice_failed",
    );
  }
}

async function restartIce(): Promise<void> {
  if (!peer || ended || iceRestarting) return;
  iceRestarting = true;
  iceRestartAttempts += 1;
  clearIceRestartTimer();
  stageHint.textContent = STATE_COPY.reconnecting.detail;
  setDiag("diag-conn", "reconnecting");
  try {
    const offer = await peer.createOffer({ iceRestart: true });
    await peer.setLocalDescription(offer);
    send("webrtc.offer", { sdp: offer.sdp, type: offer.type });
    localDescriptionSent = true;
    telemetry("ice_restart", { attempt: iceRestartAttempts });
  } catch (error) {
    iceRestarting = false;
    fail(
      "The call connection failed.",
      "Check your connection and run the development command again.",
      errorName(error),
    );
  }
}

function endCall(
  reason: string,
  notify: boolean,
  endMessageType: "cancel" | "hangup" = "hangup",
): void {
  if (ended) return;
  cleanup(reason, notify, endMessageType === "cancel" ? "call.cancel" : "call.hangup");
  setPage("ended");
  showView("card");
  title.textContent = "Call ended";
  message.textContent = "This call is over. Ask for a new link when you want to talk again.";
  controls.hidden = true;
}

function finishCall(heading: string, detail: string, reason: string): void {
  if (ended) return;
  cleanup(reason, false);
  setDiag("diag-error", reason);
  setPage("ended");
  showView("card");
  title.textContent = heading;
  message.textContent = detail;
  controls.hidden = true;
}

function finishFromServer(reason: string): void {
  if (reason === "user_cancel") {
    finishCall(STATE_COPY.cancelled.heading, STATE_COPY.cancelled.detail, reason);
    return;
  }
  if (reason === "session_cap") {
    finishCall(
      "This hosted call reached its 30-minute limit.",
      "Calls on your own machine or over Tailscale have no time limit. Ask for a new link to keep talking.",
      reason,
    );
  } else if (reason === "inactivity_timeout") {
    finishCall(
      "This call ended after 90 seconds of silence.",
      "Ask for a new call link when you're ready to continue.",
      reason,
    );
  } else if (reason === "agent_disconnected" || reason === "connection_lost") {
    finishCall(
      "The agent left the call.",
      "Return to the text conversation and ask for a fresh link.",
      reason,
    );
  } else if (uiStateForEndReason(reason) === "expired") {
    const copy = STATE_COPY.expired;
    finishCall(copy.heading, copy.detail, reason);
  } else {
    finishCall(
      "The call has ended.",
      "You can return to the text conversation.",
      reason,
    );
  }
}

function fail(heading: string, detail: string, code: string): void {
  if (!ended && (peer || socket || microphone || audioContext)) cleanup(code, false);
  else ended = true;
  // Stable failure category + correlation key on every terminal failure
  // (r2-browser-ice-diagnostics): operators join browser events to worker
  // and host logs by these redacted fields.
  telemetry("failure_categorized", {
    category: categorizeFailure(code),
    correlation: correlationKey(activeRoutingId, pageStartedAt),
  });
  setDiag("diag-error", code);
  setPage("error");
  showView("card");
  title.textContent = heading;
  message.textContent = detail;
  controls.hidden = true;
}

let timeLimitTimers: number[] = [];

/** Tell the caller before a metered (hosted) call ends. A server that sends
 * no cap, such as a local or tailnet call, schedules nothing. */
function scheduleTimeLimitNotices(expiresAt: unknown, capSeconds: unknown): void {
  clearTimeLimitNotices();
  const cap = typeof capSeconds === "number" ? capSeconds : Number(capSeconds);
  if (!(cap > 0) || typeof expiresAt !== "string" || !expiresAt) return;
  const endsAt = Date.parse(expiresAt);
  if (!Number.isFinite(endsAt)) return;
  const minutes = Math.round(cap / 60);
  const notices: Array<[number, string]> = [
    [120_000, `2 minutes left on this hosted call (${minutes}-minute limit). Calls on your own machine or over Tailscale have no time limit.`],
    [30_000, "30 seconds left on this hosted call."],
  ];
  for (const [before, text] of notices) {
    const delay = endsAt - before - Date.now();
    if (delay <= 0) continue;
    timeLimitTimers.push(window.setTimeout(() => {
      if (ended || views.stage.hidden) return;
      stageHint.textContent = text;
    }, delay));
  }
}

function clearTimeLimitNotices(): void {
  for (const timer of timeLimitTimers) window.clearTimeout(timer);
  timeLimitTimers = [];
}

function cleanup(reason: string, notify: boolean, notifyMessage: "call.hangup" | "call.cancel" = "call.hangup"): void {
  ended = true;
  clearTimeLimitNotices();
  clearIceRestartTimer();
  iceRestarting = false;
  cancelSignalingReconnect();
  telemetry("call_ended", { reason });
  recordDiagnostics.checked = false;
  recordDiagnostics.disabled = true;
  setSettingsOpen(false);
  if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer);
  heartbeatTimer = null;
  if (statsTimer !== null) window.clearInterval(statsTimer);
  statsTimer = null;
  stopConnecting();
  stopOrbStateLoop();
  orb.stop();
  if (notify) send(notifyMessage, { reason });
  socket?.close(1000, "Call ended.");
  peer?.close();
  for (const track of microphone?.getTracks() ?? []) track.stop();
  if (remoteAudio) {
    remoteAudio.pause();
    remoteAudio.srcObject = null;
    remoteAudio.remove();
  }
  localAnalyser = null;
  remoteAnalyser = null;
  void audioContext?.close();
  audioContext = null;
  document.getElementById("voice-preview")?.remove();
}

/* ------------------------------------------------------------------ */
/* Signaling plumbing                                                  */
/* ------------------------------------------------------------------ */

function sendLocalCandidate(candidate: RTCIceCandidateInit | null): void {
  if (candidate) send("webrtc.ice", { candidate });
  else send("webrtc.ice_complete");
}

function send(type: SignalType, payload: Record<string, unknown> = {}): void {
  if (socket?.readyState !== WebSocket.OPEN) return;
  socket.send(
    JSON.stringify({
      v: 1,
      id: crypto.randomUUID(),
      type,
      payload,
    } satisfies SignalEnvelope),
  );
}

function telemetry(stage: string, fields: Record<string, unknown> = {}): void {
  // Lifecycle observations also drive call-start receipts and recovery.
  // They still reach the host; its recording gate decides what is persisted.
  const payload = {
    stage,
    browserElapsedMs: elapsedSince(pageStartedAt),
    ...fields,
  };
  if (socket?.readyState === WebSocket.OPEN) {
    send("call.telemetry", payload);
    return;
  }
  if (pendingTelemetry.length >= 32) pendingTelemetry.shift();
  pendingTelemetry.push(payload);
}

function flushTelemetry(): void {
  for (const payload of pendingTelemetry.splice(0)) send("call.telemetry", payload);
}

async function sendTransportStats(): Promise<void> {
  if (!recordDiagnostics.checked) return;
  if (!peer || peer.connectionState !== "connected") return;
  try {
    telemetry("transport_stats", { ...(await collectRtcStats(peer)) });
  } catch (error) {
    telemetry("transport_stats_error", { error: errorName(error) });
  }
}

function elapsedSince(startedAt: number): number {
  return Math.max(Math.round(performance.now() - startedAt), 0);
}

function parseEnvelope(raw: string): SignalEnvelope | null {
  try {
    const value = JSON.parse(raw) as Partial<SignalEnvelope>;
    if (value.v !== 1 || typeof value.id !== "string" || typeof value.type !== "string") {
      return null;
    }
    return { v: 1, id: value.id, type: value.type as SignalType, payload: value.payload ?? {} };
  } catch {
    return null;
  }
}

function developmentSignalUrl(roomId: string): string {
  const signalUrl = new URL(`/api/dev/rooms/${roomId}/signal`, location.origin);
  signalUrl.protocol = signalUrl.protocol === "https:" ? "wss:" : "ws:";
  signalUrl.searchParams.set("role", "browser");
  return signalUrl.toString();
}

async function redeemCallTicket(routingId: string, token: string): Promise<void> {
  setPage("checking ticket");
  setDiag("diag-ticket", "checking");
  connectStep.textContent = "Checking your call link…";
  try {
    const response = await fetch(`/api/tickets/${routingId}/redeem`, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        ticket: token,
        relayRequired: browserPolicy.iceTransportPolicy === "relay",
      }),
      signal: AbortSignal.timeout(10_000),
    });
    const value = (await response.json()) as Partial<TicketRedemption> & {
      error?: { code?: string };
    };
    if (!response.ok) {
      const code = String(value.error?.code ?? "ticket_failed");
      setDiag("diag-ticket", code);
      if (code === "ticket_expired") {
        fail(
          "This call link has expired.",
          "Return to the conversation and ask for a new link.",
          code,
        );
      } else if (code === "ticket_used") {
        fail(
          "This call link has already been used.",
          "Return to the conversation and ask for a new link.",
          code,
        );
      } else {
        fail(
          "This call link isn't available.",
          "Return to the conversation and ask for a new link.",
          code,
        );
      }
      return;
    }
    activeSignalUrl = validBrowserSignalUrl(value.browserSignalUrl);
    activeAccessKey =
      typeof value.browserAccessKey === "string" && value.browserAccessKey
        ? value.browserAccessKey
        : null;
    if (!activeSignalUrl || !activeAccessKey) {
      fail(
        "This call link couldn't be opened.",
        "Return to the conversation and ask for a new link.",
        "invalid_signal_url",
      );
      return;
    }
    activeIceServers = parseIceServers(value.iceServers);
    if (
      browserPolicy.iceTransportPolicy === "relay" &&
      !activeIceServers.some((server) =>
        (Array.isArray(server.urls) ? server.urls : [server.urls]).some((url) =>
          url.toLowerCase().startsWith("turn"),
        ),
      )
    ) {
      fail(
        "This in-app browser needs the audio relay.",
        "Return to the conversation and ask for a new link.",
        "turn_unavailable",
      );
      return;
    }
    scheduleTimeLimitNotices(value.sessionExpiresAt, value.sessionCapSeconds);
    telemetry("ticket_redeemed");
    setDiag("diag-ticket", "redeemed");
    showReady("Tap to allow your microphone and join the live call.");
  } catch (error) {
    setDiag("diag-ticket", "unreachable");
    fail(
      "The call service couldn't be reached.",
      "Check your connection, then ask for a new call link.",
      errorName(error),
    );
  }
}

function showReady(copy: string): void {
  setPage("ready");
  showView("connecting");
  void sleepVideo.play().catch(() => undefined);
  connectTitle.textContent = "Join voice room";
  connectStep.textContent = copy;
  talkButton.hidden = false;
  talkButton.disabled = false;
  controls.hidden = true;
  orb.setState("idle");
}

function preferOpus(connection: RTCPeerConnection): void {
  const codecs = RTCRtpSender.getCapabilities?.("audio")?.codecs ?? [];
  const opus = codecs.filter((codec) => codec.mimeType.toLowerCase() === "audio/opus");
  if (!opus.length) return;
  for (const transceiver of connection.getTransceivers()) {
    if (transceiver.receiver.track.kind === "audio") transceiver.setCodecPreferences(opus);
  }
}

function setPage(value: string): void {
  setDiag("diag-page", value);
}

function setDiag(id: string, value: string): void {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function errorName(error: unknown): string {
  return error instanceof Error ? error.name : "unknown_error";
}

function required<T extends Element>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing #${id}`);
  return element as unknown as T;
}

function bootFromRoute(): void {
  if (isLocalPreview()) {
    showPreviewFrame();
    return;
  }
  if (!callRoute) {
    fail("This call link isn't valid.", "Ask for a new link and try again.", "invalid_link");
    return;
  }
  if (!window.isSecureContext && location.hostname !== "localhost") {
    fail(
      "This call needs a secure browser window.",
      "Open the link in Safari or Chrome and try again.",
      "insecure_context",
    );
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) {
    fail(
      "This browser can't start the call.",
      "Open the link in the latest Safari or Chrome and try again.",
      "unsupported_browser",
    );
    return;
  }
  if (callRoute.kind === "dev") {
    activeSignalUrl = developmentSignalUrl(callRoute.roomId);
    setDiag("diag-ticket", "development room");
    showReady("Tap to allow your microphone for the echo test.");
    return;
  }
  activeRoutingId = callRoute.routingId;
  void redeemCallTicket(callRoute.routingId, callRoute.token);
}

window.addEventListener("hashchange", () => {
  if (isLocalPreview()) showPreviewFrame();
});
requestAnimationFrame(() => bootFromRoute());
