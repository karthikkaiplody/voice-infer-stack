/** A live-mode `hello` with the tuning fields the server sends. */
import { parseServerMessage, type Hello, type ServerMessage } from "../../src/contract/events";
import { fixtureHello } from "./fixtures";

export const rawTuningFields = (overrides: Record<string, unknown> = {}) => [
  { key: "vad_stop_secs", label: "VAD silence window", kind: "number", about: "Silence needed before the turn ends.",
    minimum: 0.1, maximum: 2, step: 0.05, unit: "s", warn_below: 0.3,
    warning: "Below about 0.3 s a pause can split one question into two turns.", launch: 0.4, value: 0.4 },
  { key: "user_speech_timeout", label: "Speech timeout", kind: "number", about: "Extra time to resume.",
    minimum: 0, maximum: 2, step: 0.05, unit: "s", launch: 0.2, value: 0.2 },
  { key: "use_smart_turn", label: "Smart-turn model", kind: "boolean", about: "A model decides you are done.",
    warning: "The model loads on the first turn.", launch: false, value: false },
  { key: "llm_max_tokens", label: "Reply length cap", kind: "integer", about: "Caps the reply.",
    minimum: 10, maximum: 300, step: 1, unit: "tokens", warn_below: 20, warning: "Replies may be cut off.", launch: 60, value: 60 },
].map((field) => ({ ...field, ...(overrides[field.key] as object | undefined) }));

export function liveHello(over: { running?: boolean; values?: Record<string, unknown> } = {}): Hello {
  const base = fixtureHello();
  const message = parseServerMessage({
    kind: "hello", mode: "live",
    configuration: { ...base.configuration, endpointing_settings: { vad_stop_secs: 0.4, user_speech_timeout: 0.2, stt_ttfs_p99: 0.15, use_smart_turn: false, wait_for_transcript: true } },
    capabilities: { stages: { user_speech: true, endpointing: true, stt: true, retrieval: true, llm: true, tools: false, tts: true, output_transport: true },
                    outcomes: { completed: true, interrupted: false, failed: true } },
    scenarios: [], running: over.running ?? false,
    tuning: { fields: rawTuningFields(Object.fromEntries(Object.entries(over.values ?? {}).map(([k, v]) => [k, { value: v }]))) },
  });
  if (message === null || message.kind !== "hello") throw new Error("live hello rejected");
  return message;
}

export const configurationMessage = (values: Record<string, unknown>): ServerMessage => {
  const message = parseServerMessage({
    kind: "configuration",
    configuration: { snapshot_id: "config_tuned", provider_ids: {}, model_ids: {}, endpointing_settings: { vad_stop_secs: values.vad_stop_secs ?? 0.4 } },
    tuning: { fields: rawTuningFields(Object.fromEntries(Object.entries(values).map(([k, v]) => [k, { value: v }]))) },
  });
  if (message === null) throw new Error("configuration message rejected");
  return message;
};
