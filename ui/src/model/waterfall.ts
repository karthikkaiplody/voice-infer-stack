/**
 * The waterfall: one row per stage of a turn, positioned by recorded timestamps.
 *
 * Every bar comes from two timestamps that were observed. A stage that has no
 * timestamps is given a *status*, never a duration:
 *
 *   waiting             it may still happen in this turn
 *   in progress         it started and has not finished, and the turn is live
 *   measured            both ends were observed
 *   not instrumented    this source cannot report the stage at all
 *   not in this scenario the selected fixture does not contain it
 *   not observed        this live turn ended without the stage reporting
 *   started, no end     it started, the turn is over, and its end never came
 *
 * The axis is zero at "user speech ended" (the origin of the primary metric),
 * so anything to the left of zero happened while the user was still talking.
 */

import type { EventName, Mode, StageKey, TelemetryEvent } from "../contract/events";
import { firstAt, outcomeOf, type Turn } from "./turn";

export type RowStatus =
  | "waiting"
  | "in_progress"
  | "measured"
  | "not_instrumented"
  | "not_in_scenario"
  | "not_observed"
  | "started_no_end";

export interface Row {
  key: string;
  stage: StageKey;
  label: string;
  /** One plain sentence saying what this stage is. */
  about: string;
  status: RowStatus;
  /** Milliseconds relative to the axis origin. Only set when observed. */
  startMs: number | null;
  endMs: number | null;
  /** Full observed duration. `null` unless both ends were observed. */
  durationMs: number | null;
  /**
   * Time this stage added after the user stopped speaking. Equals `durationMs`
   * except for stages that begin while the user is still talking (speech-to-
   * text), where the part before the user stopped is not latency.
   */
  latencyMs: number | null;
  /** An observed instant inside or after the bar (first token, first audio). */
  markerMs: number | null;
  markerLabel: string | null;
  /** Extra facts observed or configured for this stage. */
  notes: string[];
}

export interface Axis {
  minMs: number;
  maxMs: number;
  /** Where zero is: "user speech ended", or the first event if that is unknown. */
  originLabel: string;
  ticks: number[];
}

export interface Waterfall {
  rows: Row[];
  axis: Axis;
  /** The measured stage that added the most time after speech ended. */
  slowest: { label: string; latencyMs: number } | null;
}

export interface WaterfallContext {
  mode: Mode | null;
  stages: Record<StageKey, boolean>;
  /** The source stopped, or a replay finished, so nothing more will arrive. */
  sourceEnded: boolean;
  /** Configured values shown as notes, labelled as configuration. */
  vadStopSecs: number | null;
  speechTimeoutSecs?: number | null;
  sttTimerSecs?: number | null;
}

const NS_PER_MS = 1e6;
const AXIS_PADDING = 0.04;

const at = (turn: Turn, name: EventName) => firstAt(turn, name);

/** Whole milliseconds, with honest wording for sub-millisecond and instant spans. */
export function formatDuration(ms: number): string {
  if (ms <= 0) return "instant";
  if (ms < 1) return "<1 ms";
  return `${Math.round(ms).toLocaleString("en-US")} ms`;
}

export function formatOffset(ms: number): string {
  const sign = ms < 0 ? "−" : "+";
  return `${sign}${Math.round(Math.abs(ms)).toLocaleString("en-US")} ms`;
}

// ------------------------------------------------------------------ tools ---

interface ToolAttempt {
  attemptId: string;
  name: string | null;
  attemptNumber: number | null;
  requestedNs: number;
  closedNs: number | null;
  closedAs: string | null;
  events: TelemetryEvent[];
}

const TOOL_CLOSE: Partial<Record<EventName, string>> = {
  "tool.completed": "completed",
  "tool.error": "error",
  "tool.cancelled": "cancelled",
};

function toolAttempts(turn: Turn): ToolAttempt[] {
  const attempts = new Map<string, ToolAttempt>();
  for (const event of turn.events) {
    const id = event.identities.tool_attempt_id;
    if (!event.event_name.startsWith("tool.") || id === null) continue;
    const name = event.attributes["tool.name"];
    const number = event.attributes["tool.attempt_number"];
    const attempt = attempts.get(id) ?? {
      attemptId: id,
      name: null,
      attemptNumber: null,
      requestedNs: event.timestamp_ns,
      closedNs: null,
      closedAs: null,
      events: [],
    };
    attempt.events.push(event);
    if (typeof name === "string") attempt.name = name;
    if (typeof number === "number") attempt.attemptNumber = number;
    if (event.event_name === "tool.requested") attempt.requestedNs = event.timestamp_ns;
    const closed = TOOL_CLOSE[event.event_name];
    if (closed !== undefined) {
      attempt.closedNs = event.timestamp_ns;
      attempt.closedAs = closed;
    }
    attempts.set(id, attempt);
  }
  return [...attempts.values()].sort((a, b) => a.requestedNs - b.requestedNs);
}

// -------------------------------------------------------------------- rows ---

interface Span {
  startNs: number | null;
  endNs: number | null;
}

function status(
  span: Span,
  capable: boolean,
  ended: boolean,
  mode: Mode | null,
): RowStatus {
  if (!capable) return "not_instrumented";
  if (span.startNs !== null && span.endNs !== null) return "measured";
  if (span.startNs !== null) return ended ? "started_no_end" : "in_progress";
  if (!ended) return "waiting";
  return mode === "fixture" ? "not_in_scenario" : "not_observed";
}

const STAGE_TEXT: Record<Exclude<StageKey, "tools">, { label: string; about: string }> = {
  user_speech: {
    label: "Voice activity (VAD)",
    about: "The voice-activity detector hears speech: from when you started talking to when the sound stopped.",
  },
  endpointing: {
    label: "Endpointing",
    about: "Deciding you have finished your turn: it waits for enough silence, then confirms the turn is over.",
  },
  stt: {
    label: "Speech to text",
    about: "Transcription, from the start of your speech to the final transcript. It overlaps your speaking.",
  },
  retrieval: {
    label: "Knowledge lookup",
    about: "Finding the notes that answer your question in the agent's knowledge, before the model is asked. Only agents with knowledge have this stage.",
  },
  llm: {
    label: "Language model",
    about: "From the request being sent to the model to its first generated token.",
  },
  tts: {
    label: "Text to speech",
    about: "From synthesis starting to the first synthesized audio sample existing.",
  },
  output_transport: {
    label: "Output transport",
    about: "From the first synthesized sample to the output transport accepting the first audio. This is not confirmed audible playback.",
  },
};

/** Build every row and the shared axis for one turn. Pure and deterministic. */
export function buildWaterfall(turn: Turn | null, context: WaterfallContext): Waterfall {
  const events = turn?.events ?? [];
  const live = turn ?? { turnId: "", events: [] };
  const ended = turn !== null && (outcomeOf(turn) !== null || context.sourceEnded);
  const originNs = at(live, "user_speech.ended");
  const allNs = events.map((e) => e.timestamp_ns);
  const zeroNs = originNs ?? (allNs.length ? Math.min(...allNs) : 0);
  // Microsecond resolution. Epoch-nanosecond doubles carry ~0.25 µs of rounding
  // noise, which is far below anything measured but would otherwise decide ties.
  const rel = (ns: number | null) =>
    ns === null ? null : Math.round(((ns - zeroNs) / NS_PER_MS) * 1000) / 1000;

  const spans: Record<Exclude<StageKey, "tools">, Span> = {
    user_speech: { startNs: at(live, "user_speech.started"), endNs: originNs },
    endpointing: { startNs: at(live, "endpointing.started"), endNs: at(live, "endpointing.resolved") },
    stt: { startNs: at(live, "stt.started"), endNs: at(live, "stt.final") },
    retrieval: { startNs: at(live, "retrieval.started"), endNs: at(live, "retrieval.completed") },
    llm: { startNs: at(live, "llm.started"), endNs: at(live, "llm.first_token") },
    tts: { startNs: at(live, "tts.started"), endNs: at(live, "tts.first_synthesized_sample") },
    output_transport: {
      startNs: at(live, "tts.first_synthesized_sample"),
      endNs: at(live, "output_transport.first_audio"),
    },
  };
  const markers: Partial<Record<StageKey, { ns: number | null; label: string }>> = {
    stt: { ns: at(live, "stt.first"), label: "first transcript" },
    llm: { ns: at(live, "llm.first_token"), label: "first token" },
    tts: { ns: at(live, "tts.first_synthesized_sample"), label: "first sample" },
    output_transport: { ns: at(live, "output_transport.first_audio"), label: "first audio accepted" },
  };

  const rows: Row[] = [];
  const order: Exclude<StageKey, "tools">[] = ["user_speech", "endpointing", "stt", "retrieval", "llm"];
  const afterTools: Exclude<StageKey, "tools">[] = ["tts", "output_transport"];

  const make = (stage: Exclude<StageKey, "tools">): Row => {
    const span = spans[stage];
    const st = status(span, context.stages[stage], ended, context.mode);
    const startMs = st === "not_instrumented" ? null : rel(span.startNs);
    const endMs = st === "not_instrumented" ? null : rel(span.endNs);
    const durationMs = startMs !== null && endMs !== null ? Math.max(0, endMs - startMs) : null;
    // Only stages that start before the user stopped talking are clipped.
    const latencyMs =
      durationMs === null || stage === "user_speech" || endMs === null || startMs === null
        ? null
        : Math.max(0, endMs - Math.max(startMs, originNs === null ? startMs : 0));
    const marker = markers[stage];
    const notes: string[] = [];
    if (stage === "endpointing") {
      const strategy = events.find((e) => e.event_name === "endpointing.started")?.attributes.strategy;
      if (typeof strategy === "string") notes.push(`policy: ${strategy}`);
      if (context.vadStopSecs !== null) {
        notes.push(`configured VAD silence: ${Math.round(context.vadStopSecs * 1000)} ms`);
      }
      if (context.speechTimeoutSecs != null) {
        notes.push(`configured speech timeout: ${Math.round(context.speechTimeoutSecs * 1000)} ms`);
      }
      if (context.sttTimerSecs != null) {
        notes.push(`configured STT safety timer: ${Math.round(context.sttTimerSecs * 1000)} ms`);
      }
    }
    if (stage === "retrieval") {
      const done = events.find((e) => e.event_name === "retrieval.completed")?.attributes;
      const count = done?.["retrieval.match_count"];
      if (typeof count === "number") notes.push(count === 0 ? "no notes matched" : `${count} ${count === 1 ? "note" : "notes"} matched`);
      const source = done?.["retrieval.top_source"];
      if (typeof source === "string") notes.push(`best match: ${source}`);
    }
    if (stage === "stt" && startMs !== null && startMs < 0 && latencyMs !== null) {
      notes.push("counts only the time after you stopped talking");
    }
    return {
      key: stage,
      stage,
      ...STAGE_TEXT[stage],
      status: st,
      startMs,
      endMs,
      durationMs,
      latencyMs,
      markerMs: st === "not_instrumented" || marker?.ns == null ? null : rel(marker.ns),
      markerLabel: marker?.label ?? null,
      notes,
    };
  };

  for (const stage of order) rows.push(make(stage));

  // Tools: one row per attempt; a single status row when there are none.
  const attempts = toolAttempts(live);
  if (attempts.length === 0 || !context.stages.tools) {
    const st = status({ startNs: null, endNs: null }, context.stages.tools, ended, context.mode);
    rows.push({
      key: "tools",
      stage: "tools",
      label: "Tool calls",
      about: "The model asking the application to run a function, from the request to its result.",
      status: st,
      startMs: null,
      endMs: null,
      durationMs: null,
      latencyMs: null,
      markerMs: null,
      markerLabel: null,
      notes: [],
    });
  } else {
    for (const attempt of attempts) {
      const st = status(
        { startNs: attempt.requestedNs, endNs: attempt.closedNs },
        true, ended, context.mode,
      );
      const startMs = rel(attempt.requestedNs)!;
      const endMs = rel(attempt.closedNs);
      const durationMs = endMs === null ? null : Math.max(0, endMs - startMs);
      const notes: string[] = [];
      if (attempt.closedAs !== null) notes.push(`ended: ${attempt.closedAs}`);
      const progress = attempt.events.filter((e) => e.event_name === "tool.progress").length;
      if (progress > 0) notes.push(`${progress} progress update${progress === 1 ? "" : "s"}`);
      rows.push({
        key: `tool:${attempt.attemptId}`,
        stage: "tools",
        label: `Tool: ${attempt.name ?? "unnamed"}${attempt.attemptNumber === null ? "" : ` (attempt ${attempt.attemptNumber})`}`,
        about: "The model asking the application to run a function, from the request to its result.",
        status: st,
        startMs,
        endMs,
        durationMs,
        latencyMs: durationMs,
        markerMs: null,
        markerLabel: null,
        notes,
      });
    }
  }

  for (const stage of afterTools) rows.push(make(stage));

  // Axis: every observed boundary except the terminal outcome (which can trail
  // far past the last stage and would leave the bars squeezed to one side).
  const observed = events
    .filter((e) => !e.event_name.startsWith("turn."))
    .map((e) => rel(e.timestamp_ns)!);
  const dataMin = Math.min(0, ...observed);
  const dataMax = Math.max(1, ...observed);
  // A little room on both sides so the first and last bars, and their markers,
  // are not cut off by the edge of the lane.
  const pad = (dataMax - dataMin) * AXIS_PADDING;
  const minMs = dataMin - pad;
  const maxMs = dataMax + pad;
  const axis: Axis = {
    minMs,
    maxMs,
    originLabel: originNs === null ? "first event" : "user speech ended",
    ticks: niceTicks(minMs, maxMs).filter((t) => t >= dataMin - 1e-9 && t <= dataMax + 1e-9),
  };

  const measured = rows.filter((r) => r.status === "measured" && r.latencyMs !== null && r.latencyMs > 0);
  const top = measured.reduce<Row | null>(
    (best, r) => (best === null || r.latencyMs! > best.latencyMs! ? r : best), null);
  return {
    rows,
    axis,
    slowest: top === null ? null : { label: top.label, latencyMs: top.latencyMs! },
  };
}

/** Round tick marks (multiples of 1, 2, or 5 × 10ⁿ ms) covering the range. */
export function niceTicks(minMs: number, maxMs: number, target = 7): number[] {
  const span = Math.max(maxMs - minMs, 1);
  const raw = span / target;
  const base = 10 ** Math.floor(Math.log10(raw));
  // Whole milliseconds only: a fractional step would round into duplicate ticks.
  const step = Math.max(1, [1, 2, 5, 10].map((m) => m * base).find((s) => s >= raw) ?? base * 10);
  const ticks: number[] = [];
  for (let t = Math.ceil(minMs / step) * step; t <= maxMs + 1e-9; t += step) {
    ticks.push(Math.round(t));
  }
  return ticks;
}

/** Position in [0, 100] percent along the axis. */
export function percent(axis: Axis, ms: number): number {
  return ((ms - axis.minMs) / Math.max(axis.maxMs - axis.minMs, 1)) * 100;
}
