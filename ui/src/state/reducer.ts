/**
 * The page's state, as a pure reducer over what the local server sends.
 *
 * The rules that matter:
 *
 * - One turn is on screen at a time, and a new `turn_id` replaces it. Nothing
 *   from the earlier turn survives into the new one.
 * - A turn that never reached completed / interrupted / failed before it was
 *   replaced or reset is remembered as "No outcome observed". That is UI-only:
 *   the telemetry contract has no such event.
 * - Late events for an earlier turn, repeated events, and events after a
 *   terminal outcome are ignored rather than merged into what is on screen.
 */

import type { Hello, ServerMessage } from "../contract/events";
import { outcomeOf, type Turn } from "../model/turn";

export type Connection = "connecting" | "open" | "reconnecting";
export type Playback = "idle" | "playing" | "finished";
export type Runtime = "idle" | "listening" | "stopped" | "error";

export interface ViewState {
  connection: Connection;
  hello: Hello | null;
  turn: Turn | null;
  /** Turn IDs already shown, so a late event cannot resurrect an old turn. */
  seenTurnIds: readonly string[];
  /** The turn shown before this one never reached a terminal outcome. */
  previousWithoutOutcome: boolean;
  playback: Playback;
  scenario: string | null;
  runtime: Runtime;
  /** The live microphone is delivering only silence, so nothing will be heard. */
  inputSilent: boolean;
  /** A fixed, safe sentence. Never server-provided free text. */
  notice: string | null;
}

export type Action =
  | { type: "connection"; status: Connection }
  | { type: "message"; message: ServerMessage }
  | { type: "notice"; text: string | null };

export const initialState: ViewState = {
  connection: "connecting",
  hello: null,
  turn: null,
  seenTurnIds: [],
  previousWithoutOutcome: false,
  playback: "idle",
  scenario: null,
  runtime: "idle",
  inputSilent: false,
  notice: null,
};

const MAX_SEEN_TURNS = 32;

const unfinished = (turn: Turn | null) => turn !== null && outcomeOf(turn) === null;

export function reducer(state: ViewState, action: Action): ViewState {
  switch (action.type) {
    case "connection":
      // A dropped connection means whatever was running is no longer observed:
      // never keep claiming the pipeline is listening once the server is gone.
      return {
        ...state,
        connection: action.status,
        runtime: action.status === "reconnecting" && state.runtime === "listening" ? "stopped" : state.runtime,
      };
    case "notice":
      return { ...state, notice: action.text };
    case "message":
      return onMessage(state, action.message);
  }
}

function onMessage(state: ViewState, message: ServerMessage): ViewState {
  switch (message.kind) {
    case "hello":
      // A (re)connection: show what the server says now, not what we last saw.
      return {
        ...initialState,
        connection: "open",
        hello: message,
        previousWithoutOutcome: unfinished(state.turn),
        runtime: message.running ? "listening" : "idle",
      };
    case "configuration":
      // Settings were saved on the server: show the new snapshot and values.
      return state.hello === null
        ? state
        : { ...state, hello: { ...state.hello, configuration: message.configuration, tuning: message.tuning } };
    case "replay_reset":
      return {
        ...state,
        turn: null,
        seenTurnIds: [],
        previousWithoutOutcome: unfinished(state.turn),
        playback: "playing",
        scenario: message.scenario,
        notice: null,
      };
    case "replay_finished":
      return { ...state, playback: "finished" };
    case "ready":
      return { ...state, runtime: "listening", notice: null };
    case "input_silent":
      return { ...state, inputSilent: true };
    case "input_ok":
      return { ...state, inputSilent: false };
    case "stopped":
      return { ...state, runtime: "stopped", inputSilent: false };
    case "error":
      return {
        ...state,
        runtime: "error",
        notice: `The local pipeline reported an error (${message.classification}).`,
      };
    case "telemetry":
      return onEvent(state, message.event);
  }
}

function onEvent(state: ViewState, event: Extract<ServerMessage, { kind: "telemetry" }>["event"]): ViewState {
  const turnId = event.identities.turn_id;
  const current = state.turn;
  if (current !== null && current.turnId === turnId) {
    if (outcomeOf(current) !== null) return state;                       // nothing follows an outcome
    if (current.events.some((e) => e.event_id === event.event_id)) return state;
    return { ...state, turn: { ...current, events: [...current.events, event] } };
  }
  if (state.seenTurnIds.includes(turnId)) return state;                  // a late event from an earlier turn
  return {
    ...state,
    turn: { turnId, events: [event] },
    seenTurnIds: [...state.seenTurnIds, turnId].slice(-MAX_SEEN_TURNS),
    previousWithoutOutcome: unfinished(current),
  };
}
