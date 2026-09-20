/**
 * Pure functions over one turn's contract-v1 events. No React, no clock, no
 * network: the same events always give the same state, metric and outcome, which
 * is what makes fixture replay deterministic.
 *
 * Nothing here estimates. A value the events do not contain is `null` or an
 * explicit "unavailable" result, never a number.
 */

import type { EventName, Outcome, TelemetryEvent } from "../contract/events";

export type TurnState =
  | "Waiting"
  | "Listening"
  | "Endpointing"
  | "Transcribing"
  | "Thinking"
  | "Calling tool"
  | "Speaking"
  | "Completed"
  | "Interrupted"
  | "Failed";

export interface Turn {
  turnId: string;
  /** In arrival order. Use `byTime` for display order. */
  events: readonly TelemetryEvent[];
}

const TERMINALS: Record<string, Outcome> = {
  "turn.completed": "completed",
  "turn.interrupted": "interrupted",
  "turn.failed": "failed",
};

const TOOL_CLOSED = new Set<EventName>(["tool.completed", "tool.error", "tool.cancelled"]);

/** Earliest timestamp for a boundary in this turn, or `null` if never observed. */
export function firstAt(turn: Turn, name: EventName): number | null {
  let found: number | null = null;
  for (const event of turn.events) {
    if (event.event_name === name && (found === null || event.timestamp_ns < found)) {
      found = event.timestamp_ns;
    }
  }
  return found;
}

const has = (turn: Turn, name: EventName) => firstAt(turn, name) !== null;

export function outcomeOf(turn: Turn | null): Outcome | null {
  if (turn === null) return null;
  for (const event of turn.events) {
    const outcome = TERMINALS[event.event_name];
    if (outcome !== undefined) return outcome;
  }
  return null;
}

/** True while at least one tool attempt has been requested and not yet closed. */
function toolInFlight(turn: Turn): boolean {
  const closed = new Map<string, boolean>();
  for (const event of turn.events) {
    const attempt = event.identities.tool_attempt_id;
    if (!event.event_name.startsWith("tool.") || attempt === null) continue;
    closed.set(attempt, (closed.get(attempt) ?? false) || TOOL_CLOSED.has(event.event_name));
  }
  return [...closed.values()].some((isClosed) => !isClosed);
}

/**
 * The stage the turn is in, from what has been observed.
 *
 * "Transcribing" and "Endpointing" only apply after speech has ended: a live
 * STT span can open while the user is still talking, and that is still
 * "Listening".
 */
export function deriveState(turn: Turn | null): TurnState {
  if (turn === null || turn.events.length === 0) return "Waiting";
  switch (outcomeOf(turn)) {
    case "completed":
      return "Completed";
    case "interrupted":
      return "Interrupted";
    case "failed":
      return "Failed";
    default:
  }
  if (toolInFlight(turn)) return "Calling tool";
  if (has(turn, "tts.started") || has(turn, "tts.first_synthesized_sample") ||
      has(turn, "output_transport.first_audio")) {
    return "Speaking";
  }
  if (has(turn, "llm.started")) return "Thinking";
  if (has(turn, "user_speech.ended") || has(turn, "endpointing.started")) {
    if (has(turn, "stt.started") && !has(turn, "stt.final")) return "Transcribing";
    if (has(turn, "endpointing.started") && !has(turn, "endpointing.resolved")) {
      return "Endpointing";
    }
    if (has(turn, "stt.final") || has(turn, "endpointing.resolved")) return "Thinking";
    return "Endpointing";
  }
  return has(turn, "user_speech.started") ? "Listening" : "Waiting";
}

// ---------------------------------------------------------------- metric ---

export type MetricResult =
  | { status: "measured"; ms: number }
  | { status: "waiting" }
  | { status: "unavailable"; reason: string };

/**
 * User speech ended → output transport accepted first audio.
 *
 * `output_transport.first_audio` means the transport accepted a frame. It does
 * not mean the user heard it, and nothing here says so.
 */
export function primaryMetric(turn: Turn | null): MetricResult {
  if (turn === null) return { status: "waiting" };
  const start = firstAt(turn, "user_speech.ended");
  const end = firstAt(turn, "output_transport.first_audio");
  if (start !== null && end !== null) {
    return end >= start
      ? { status: "measured", ms: (end - start) / 1e6 }
      : { status: "unavailable", reason: "Timestamps were out of order." };
  }
  if (outcomeOf(turn) === null) return { status: "waiting" };
  if (end === null) {
    return { status: "unavailable", reason: "The output transport accepted no audio in this turn." };
  }
  return { status: "unavailable", reason: "The end of user speech was not observed." };
}

export function formatMs(ms: number): string {
  return `${Math.round(ms).toLocaleString("en-US")} ms`;
}

// ------------------------------------------------------------------ log ---

export interface LoggedEvent {
  id: string;
  name: EventName;
  /** Milliseconds after the turn's earliest event. `null` for that first event. */
  offsetMs: number | null;
}

/** Events in time order with offsets from the turn's first recorded event. */
export function eventLog(turn: Turn | null): LoggedEvent[] {
  if (turn === null || turn.events.length === 0) return [];
  const ordered = turn.events
    .map((event, index) => ({ event, index }))
    .sort((a, b) => a.event.timestamp_ns - b.event.timestamp_ns || a.index - b.index);
  const origin = ordered[0]!.event.timestamp_ns;
  return ordered.map(({ event }, position) => ({
    id: event.event_id,
    name: event.event_name,
    offsetMs: position === 0 ? null : (event.timestamp_ns - origin) / 1e6,
  }));
}
