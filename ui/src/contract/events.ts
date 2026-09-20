/**
 * Telemetry contract v1, as the browser understands it.
 *
 * `telemetry_contract/v1.schema.json` is the source of truth; a test compares
 * `EVENT_NAMES` with its enum so the two cannot drift. Everything received from
 * the local server passes through `parseEvent` / `parseServerMessage`, which
 * fail closed: an unknown schema version, event name, or malformed field yields
 * `null`, and only the few attribute keys this UI uses are kept. Anything else
 * a server (or a bug) might send never reaches state, so it cannot be rendered.
 */

/** 1.1.0 adds the retrieval stage and nothing else; 1.0.0 traces stay valid. */
export const SCHEMA_VERSIONS = ["1.0.0", "1.1.0"] as const;
export type SchemaVersion = (typeof SCHEMA_VERSIONS)[number];

export const EVENT_NAMES = [
  "user_speech.started",
  "user_speech.ended",
  "endpointing.started",
  "endpointing.resolved",
  "stt.started",
  "stt.first",
  "stt.final",
  "llm.started",
  "llm.first_token",
  "retrieval.started",
  "retrieval.completed",
  "tool.requested",
  "tool.running",
  "tool.progress",
  "tool.completed",
  "tool.error",
  "tool.cancelled",
  "tts.started",
  "tts.first_synthesized_sample",
  "output_transport.first_audio",
  "turn.completed",
  "turn.interrupted",
  "turn.failed",
] as const;

export type EventName = (typeof EVENT_NAMES)[number];

/** The only attribute keys the UI reads. The rest are dropped on parse. */
export const UI_ATTRIBUTE_KEYS = [
  "strategy",
  "turn.outcome",
  "error.classification",
  "tool.name",
  "tool.attempt_number",
  "tool.progress_code",
  "retrieval.match_count",
  "retrieval.method",
  "retrieval.top_source",
] as const;

export type Attributes = Partial<
  Record<(typeof UI_ATTRIBUTE_KEYS)[number], string | number>
>;

export interface EventIdentities {
  turn_id: string;
  span_id: string;
  parent_span_id: string | null;
  logical_tool_call_id: string | null;
  tool_attempt_id: string | null;
}

export interface TelemetryEvent {
  schema_version: SchemaVersion;
  event_id: string;
  event_name: EventName;
  timestamp_ns: number;
  identities: EventIdentities;
  attributes: Attributes;
}

const NAMES = new Set<string>(EVENT_NAMES);
const VERSIONS = new Set<unknown>(SCHEMA_VERSIONS);
/** Introduced in 1.1.0, so an event that claims 1.0.0 cannot be one of these. */
const RETRIEVAL_NAMES = new Set<string>(["retrieval.started", "retrieval.completed"]);
const COUNT_KEYS = new Set<string>(["tool.attempt_number", "retrieval.match_count"]);
const SAFE_ID = /^[A-Za-z0-9_.:-]{1,128}$/;
const CODE = /^[a-z0-9_.-]{1,64}$/;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const safeId = (value: unknown): value is string =>
  typeof value === "string" && SAFE_ID.test(value);

const nullableId = (value: unknown): string | null | undefined =>
  value === null ? null : safeId(value) ? value : undefined;

function parseAttributes(value: unknown): Attributes | null {
  if (!isRecord(value)) return null;
  const kept: Attributes = {};
  for (const key of UI_ATTRIBUTE_KEYS) {
    const item = value[key];
    if (item === undefined) continue;
    if (COUNT_KEYS.has(key)) {
      // An attempt is numbered from 1; a match count can be 0 (nothing matched).
      const least = key === "tool.attempt_number" ? 1 : 0;
      if (typeof item === "number" && Number.isInteger(item) && item >= least) {
        kept[key] = item;
      }
    } else if (typeof item === "string" && CODE.test(item)) {
      kept[key] = item;
    }
  }
  return kept;
}

/** A validated event, or `null` if it is not a well-formed contract-v1 event. */
export function parseEvent(value: unknown): TelemetryEvent | null {
  if (!isRecord(value) || !VERSIONS.has(value.schema_version)) return null;
  const version = value.schema_version as SchemaVersion;
  const name = value.event_name;
  if (typeof name !== "string" || !NAMES.has(name)) return null;
  if (version === "1.0.0" && RETRIEVAL_NAMES.has(name)) return null;
  const timestamp = value.timestamp_ns;
  // Live timestamps are epoch nanoseconds (~1.8e18), beyond Number.MAX_SAFE_INTEGER
  // (~9e15), so `isSafeInteger` would reject every real event. A double at that
  // size still resolves about 256 ns, and the UI only ever subtracts two
  // timestamps and shows milliseconds, so integer-valued and finite is enough.
  if (typeof timestamp !== "number" || !Number.isFinite(timestamp) ||
      !Number.isInteger(timestamp) || timestamp < 0) {
    return null;
  }
  if (!safeId(value.event_id) || !isRecord(value.identities)) return null;
  const ids = value.identities;
  const parent = nullableId(ids.parent_span_id);
  const logical = nullableId(ids.logical_tool_call_id);
  const attempt = nullableId(ids.tool_attempt_id);
  if (!safeId(ids.turn_id) || !safeId(ids.span_id)) return null;
  if (parent === undefined || logical === undefined || attempt === undefined) return null;
  const attributes = parseAttributes(value.attributes);
  if (attributes === null) return null;
  return {
    schema_version: version,
    event_id: value.event_id,
    event_name: name as EventName,
    timestamp_ns: timestamp,
    identities: {
      turn_id: ids.turn_id,
      span_id: ids.span_id,
      parent_span_id: parent,
      logical_tool_call_id: logical,
      tool_attempt_id: attempt,
    },
    attributes,
  };
}

// ---------------------------------------------------------------- server ---

export type Mode = "fixture" | "live";

export const STAGE_KEYS = [
  "user_speech",
  "endpointing",
  "stt",
  "retrieval",
  "llm",
  "tools",
  "tts",
  "output_transport",
] as const;
export type StageKey = (typeof STAGE_KEYS)[number];

export const OUTCOME_KEYS = ["completed", "interrupted", "failed"] as const;
export type Outcome = (typeof OUTCOME_KEYS)[number];

/** Safe scalars only; `null` means "not available", never an empty string. */
export interface ConfigurationSummary {
  snapshot_id: string | null;
  source_revision: string | null;
  provider_ids: Record<string, string>;
  model_ids: Record<string, string>;
  endpointing_settings: Record<string, string | number | boolean>;
  hardware_class: string | null;
  cpu_architecture: string | null;
  os_version: string | null;
  execution_engine_id: string | null;
  privacy_mode: "metadata-only";
}

export interface Capabilities {
  stages: Record<StageKey, boolean>;
  outcomes: Record<Outcome, boolean>;
}

/** One setting the page may change. Limits come from the server, never from here. */
export interface TuningField {
  key: string;
  label: string;
  kind: "number" | "integer" | "boolean";
  about: string;
  launch: number | boolean;
  value: number | boolean;
  minimum?: number;
  maximum?: number;
  step?: number;
  unit?: string;
  warnBelow?: number;
  warnAbove?: number;
  warning?: string;
}

export interface Tuning {
  fields: TuningField[];
}

/** Written in the repository (`agents/<id>/agent.toml`), and shown as text. */
export interface AgentSummary {
  id: string;
  title: string;
}

export interface Hello {
  kind: "hello";
  mode: Mode;
  configuration: ConfigurationSummary;
  capabilities: Capabilities;
  /** Live mode only: which agent is answering. `null` for fixtures and the original prompt. */
  agent: AgentSummary | null;
  scenarios: string[];
  running: boolean;
  replay: { pacing: "accelerated"; max_gap_ms: number } | null;
  /** Live mode only: the latency thresholds that can be changed. */
  tuning: Tuning | null;
}

export type ServerMessage =
  | Hello
  | { kind: "configuration"; configuration: ConfigurationSummary; tuning: Tuning | null }
  | { kind: "telemetry"; event: TelemetryEvent }
  | { kind: "replay_reset"; scenario: string }
  | { kind: "replay_finished"; scenario: string }
  | { kind: "ready" }
  | { kind: "stopped" }
  /** The microphone has started, or stopped, delivering only digital silence. */
  | { kind: "input_silent" }
  | { kind: "input_ok" }
  | { kind: "error"; classification: string };

/**
 * Mirrors the server's "does this look like a hostname" check
 * (`telemetry._unsafe_text`). The server already filters; this is the browser
 * refusing to display one even if a filter upstream ever failed.
 */
const HOSTNAME = /\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|local|internal|test)\b/i;

const text = (value: unknown): string | null =>
  typeof value === "string" && SAFE_ID.test(value) && !HOSTNAME.test(value) ? value : null;

/** Model and voice IDs may contain one `/`; otherwise the same safe grammar. */
const modelText = (value: unknown): string | null =>
  typeof value === "string" &&
  /^[A-Za-z0-9][A-Za-z0-9_.:+-]*(\/[A-Za-z0-9][A-Za-z0-9_.:+-]*)?$/.test(value) &&
  value.length <= 96 &&
  !HOSTNAME.test(value)
    ? value
    : null;

function stringMap(value: unknown, read: (v: unknown) => string | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!isRecord(value)) return out;
  for (const [key, item] of Object.entries(value)) {
    const clean = read(item);
    if (SAFE_ID.test(key) && clean !== null) out[key] = clean;
  }
  return out;
}

function scalarMap(value: unknown): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  if (!isRecord(value)) return out;
  for (const [key, item] of Object.entries(value)) {
    if (!SAFE_ID.test(key)) continue;
    if (typeof item === "boolean" || (typeof item === "number" && Number.isFinite(item))) {
      out[key] = item;
    } else if (text(item) !== null) {
      out[key] = item as string;
    }
  }
  return out;
}

function parseConfiguration(value: unknown): ConfigurationSummary {
  const raw = isRecord(value) ? value : {};
  return {
    snapshot_id: text(raw.snapshot_id),
    source_revision: text(raw.source_revision),
    provider_ids: stringMap(raw.provider_ids, text),
    model_ids: stringMap(raw.model_ids, modelText),
    endpointing_settings: scalarMap(raw.endpointing_settings),
    hardware_class: text(raw.hardware_class),
    cpu_architecture: text(raw.cpu_architecture),
    os_version: text(raw.os_version),
    execution_engine_id: text(raw.execution_engine_id),
    privacy_mode: "metadata-only",
  };
}

/** Missing or non-boolean means "not instrumented": never assume a stage works. */
function flags<K extends string>(keys: readonly K[], value: unknown): Record<K, boolean> {
  const raw = isRecord(value) ? value : {};
  return Object.fromEntries(keys.map((key) => [key, raw[key] === true])) as Record<K, boolean>;
}

/** Prose written by this repository's server. Bounded, printable, and rendered as text. */
const prose = (value: unknown, max = 300): string | undefined =>
  typeof value === "string" && value.length > 0 && value.length <= max && !/[\u0000-\u001f<>]/.test(value)
    ? value
    : undefined;

const finite = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

function parseTuningField(raw: unknown): TuningField | null {
  if (!isRecord(raw) || !safeId(raw.key)) return null;
  const kind = raw.kind;
  if (kind !== "number" && kind !== "integer" && kind !== "boolean") return null;
  const label = prose(raw.label, 60);
  const about = prose(raw.about);
  if (label === undefined || about === undefined) return null;
  const base = { key: raw.key, label, kind, about } as const;
  const warning = prose(raw.warning);
  if (kind === "boolean") {
    if (typeof raw.launch !== "boolean" || typeof raw.value !== "boolean") return null;
    return { ...base, launch: raw.launch, value: raw.value, ...(warning ? { warning } : {}) };
  }
  const [launch, value] = [finite(raw.launch), finite(raw.value)];
  const [minimum, maximum, step] = [finite(raw.minimum), finite(raw.maximum), finite(raw.step)];
  if (launch === undefined || value === undefined) return null;
  if (minimum === undefined || maximum === undefined || step === undefined) return null;
  if (!(minimum < maximum) || !(step > 0)) return null;
  const unit = prose(raw.unit, 16);
  const warnBelow = finite(raw.warn_below);
  const warnAbove = finite(raw.warn_above);
  return {
    ...base, launch, value, minimum, maximum, step,
    ...(unit ? { unit } : {}),
    ...(warnBelow !== undefined ? { warnBelow } : {}),
    ...(warnAbove !== undefined ? { warnAbove } : {}),
    ...(warning ? { warning } : {}),
  };
}

function parseTuning(raw: unknown): Tuning | null {
  if (!isRecord(raw) || !Array.isArray(raw.fields)) return null;
  const fields = raw.fields.slice(0, 20).map(parseTuningField).filter((f): f is TuningField => f !== null);
  return fields.length > 0 ? { fields } : null;
}

/** An agent's id and title, or `null` if either is missing or not plain text. */
function parseAgent(value: unknown): AgentSummary | null {
  if (!isRecord(value) || !safeId(value.id)) return null;
  const title = prose(value.title, 80);
  return title === undefined ? null : { id: value.id, title };
}

function parseHello(raw: Record<string, unknown>): Hello | null {
  if (raw.mode !== "fixture" && raw.mode !== "live") return null;
  const caps = isRecord(raw.capabilities) ? raw.capabilities : {};
  const replay = raw.replay;
  return {
    kind: "hello",
    mode: raw.mode,
    configuration: parseConfiguration(raw.configuration),
    capabilities: {
      stages: flags(STAGE_KEYS, caps.stages),
      outcomes: flags(OUTCOME_KEYS, caps.outcomes),
    },
    scenarios: Array.isArray(raw.scenarios)
      ? raw.scenarios.filter((s): s is string => text(s) !== null)
      : [],
    agent: raw.mode === "live" ? parseAgent(raw.agent) : null,
    running: raw.running === true,
    tuning: raw.mode === "live" ? parseTuning(raw.tuning) : null,
    replay:
      isRecord(replay) &&
      replay.pacing === "accelerated" &&
      typeof replay.max_gap_ms === "number" &&
      Number.isFinite(replay.max_gap_ms)
        ? { pacing: "accelerated", max_gap_ms: replay.max_gap_ms }
        : null,
  };
}

/** One SSE `data:` payload, or `null` if it is not a message this UI accepts. */
export function parseServerMessage(value: unknown): ServerMessage | null {
  if (!isRecord(value)) return null;
  switch (value.kind) {
    case "hello":
      return parseHello(value);
    case "configuration":
      return { kind: "configuration", configuration: parseConfiguration(value.configuration),
               tuning: parseTuning(value.tuning) };
    case "telemetry": {
      const event = parseEvent(value.event);
      return event === null ? null : { kind: "telemetry", event };
    }
    case "replay_reset":
    case "replay_finished": {
      const scenario = text(value.scenario);
      return scenario === null ? null : { kind: value.kind, scenario };
    }
    case "input_silent":
    case "input_ok":
    case "ready":
    case "stopped":
      return { kind: value.kind };
    case "error": {
      const classification = typeof value.classification === "string" && CODE.test(value.classification)
        ? value.classification
        : "pipeline_error";
      return { kind: "error", classification };
    }
    default:
      return null;
  }
}
