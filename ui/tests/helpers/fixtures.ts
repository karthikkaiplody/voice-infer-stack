/**
 * Test-only access to the repository's real Phase 1 files. The UI is tested
 * against the same fixtures the server replays, so there are no copies to drift.
 */
import { parseEvent, parseServerMessage, type Hello, type ServerMessage, type TelemetryEvent } from "../../src/contract/events";
import type { Turn } from "../../src/model/turn";
import { initialState, reducer, type ViewState } from "../../src/state/reducer";

const raw = (files: Record<string, unknown>) => files as Record<string, string>;

const jsonl = raw(
  import.meta.glob("../../../fixtures/telemetry/*.jsonl", { query: "?raw", import: "default", eager: true }),
);
const json = raw(
  import.meta.glob(["../../../fixtures/telemetry/*.json", "../../../telemetry_contract/*.json"], {
    query: "?raw",
    import: "default",
    eager: true,
  }),
);

export const FIXTURE_NAMES = ["normal-completed", "slow-blocking-tool", "interrupted", "false-endpoint", "failed-tool", "grounded-answer"] as const;
export type FixtureName = (typeof FIXTURE_NAMES)[number];

const find = (files: Record<string, string>, name: string): string => {
  const key = Object.keys(files).find((path) => path.endsWith(`/${name}`));
  if (key === undefined) throw new Error(`missing repository file: ${name}`);
  return files[key]!;
};

export const loadJson = (name: string): Record<string, unknown> => JSON.parse(find(json, name));

export function rawFixtureEvents(name: FixtureName): unknown[] {
  return find(jsonl, `${name}.jsonl`)
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line));
}

export function fixtureEvents(name: FixtureName): TelemetryEvent[] {
  return rawFixtureEvents(name).map((value) => {
    const event = parseEvent(value);
    if (event === null) throw new Error(`fixture ${name} contains an event the UI rejects`);
    return event;
  });
}

export const turnOf = (events: TelemetryEvent[]): Turn => ({
  turnId: events[0]!.identities.turn_id,
  events,
});

/** The server's `hello` for fixture mode, built from the real fixture configuration. */
export function fixtureHello(): Hello {
  const message = parseServerMessage({
    kind: "hello",
    mode: "fixture",
    configuration: loadJson("configuration.json"),
    capabilities: {
      stages: Object.fromEntries(
        ["user_speech", "endpointing", "stt", "retrieval", "llm", "tools", "tts", "output_transport"].map((k) => [k, true]),
      ),
      outcomes: { completed: true, interrupted: true, failed: true },
    },
    scenarios: [...FIXTURE_NAMES],
    running: false,
    replay: { pacing: "accelerated", max_gap_ms: 350 },
  });
  if (message === null || message.kind !== "hello") throw new Error("fixture hello rejected");
  return message;
}

export const fold = (messages: ServerMessage[], from: ViewState = initialState): ViewState =>
  messages.reduce((state, message) => reducer(state, { type: "message", message }), from);

export const telemetry = (event: TelemetryEvent): ServerMessage => ({ kind: "telemetry", event });

/** State after the server said hello, announced a replay, and sent `count` events. */
export function replayState(name: FixtureName, count?: number): ViewState {
  const events = fixtureEvents(name);
  return fold([
    fixtureHello(),
    { kind: "replay_reset", scenario: name },
    ...events.slice(0, count ?? events.length).map(telemetry),
  ]);
}

/** Live timestamps are epoch nanoseconds. Fixtures use tiny ones, which hid a bug. */
export const EPOCH_BASE_NS = 1_789_855_104_000_000_000;

export function epochFixtureEvents(name: FixtureName): TelemetryEvent[] {
  return rawFixtureEvents(name).map((value) => {
    const raw = value as { timestamp_ns: number };
    const shifted = JSON.parse(JSON.stringify({ ...raw, timestamp_ns: raw.timestamp_ns + EPOCH_BASE_NS }));
    // Round-trip through JSON text, exactly as the event stream delivers it.
    const event = parseEvent(JSON.parse(JSON.stringify(shifted).replace(/"timestamp_ns":\d+/,
      `"timestamp_ns":${BigInt(raw.timestamp_ns) + BigInt(EPOCH_BASE_NS)}`)));
    if (event === null) throw new Error(`epoch-scale ${name} event rejected`);
    return event;
  });
}
