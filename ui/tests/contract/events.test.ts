import { describe, expect, it } from "vitest";
import { FIXTURE_NAMES, fixtureEvents, loadJson, rawFixtureEvents } from "../helpers/fixtures";
import { rawTuningFields } from "../helpers/tuning";
import { EVENT_NAMES, parseEvent, parseServerMessage } from "../../src/contract/events";

const valid = () => structuredClone(rawFixtureEvents("normal-completed")[0]) as Record<string, any>;

describe("contract parity", () => {
  it("lists exactly the event names in telemetry_contract/v1.schema.json", () => {
    const schema = loadJson("v1.schema.json") as any;
    expect([...EVENT_NAMES]).toEqual(schema.properties.event_name.enum);
  });

  it.each(FIXTURE_NAMES)("accepts every event in the %s fixture", (name) => {
    expect(fixtureEvents(name)).toHaveLength(rawFixtureEvents(name).length);
  });
});

describe("parseEvent fails closed", () => {
  it.each([
    ["an unknown schema version", (e: any) => { e.schema_version = "2.0.0"; }],
    ["an unknown event name", (e: any) => { e.event_name = "user.said_something"; }],
    ["a negative timestamp", (e: any) => { e.timestamp_ns = -1; }],
    ["a fractional timestamp", (e: any) => { e.timestamp_ns = 1.5; }],
    ["a string timestamp", (e: any) => { e.timestamp_ns = "1000"; }],
    ["a missing turn id", (e: any) => { delete e.identities.turn_id; }],
    ["an unsafe event id", (e: any) => { e.event_id = "../../etc/passwd"; }],
    ["an unsafe turn id", (e: any) => { e.identities.turn_id = "turn one"; }],
    ["missing identities", (e: any) => { delete e.identities; }],
    ["non-object attributes", (e: any) => { e.attributes = "x"; }],
  ])("rejects %s", (_label, mutate) => {
    const event = valid();
    mutate(event);
    expect(parseEvent(event)).toBeNull();
  });

  it.each([null, undefined, 5, "text", [], [1]])("rejects the non-event %j", (value) => {
    expect(parseEvent(value)).toBeNull();
  });

  it("keeps only the attribute keys the UI uses, and only controlled values", () => {
    const event = valid();
    event.attributes = {
      "tool.name": "synthetic_lookup",
      "tool.attempt_number": 2,
      "error.classification": "tool_unavailable",
      "turn.outcome": "/Users/canary/secret",     // not a controlled code
      transcript: "CANARY spoken words",
      prompt: "CANARY system prompt",
      "gen_ai.request.model": "CANARY-model",
    };
    expect(parseEvent(event)?.attributes).toEqual({
      "tool.name": "synthetic_lookup",
      "tool.attempt_number": 2,
      "error.classification": "tool_unavailable",
    });
  });
});

describe("retrieval events (contract 1.1.0)", () => {
  const retrieval = (version: string, attributes: Record<string, unknown> = {}) => ({
    ...valid(), schema_version: version, event_name: "retrieval.completed", attributes,
  });

  it("are accepted under 1.1.0, and 1.0.0 events still parse", () => {
    expect(parseEvent(retrieval("1.1.0"))?.event_name).toBe("retrieval.completed");
    expect(parseEvent({ ...valid(), schema_version: "1.0.0" })).not.toBeNull();
  });

  it("are refused under 1.0.0, which never had them", () => {
    expect(parseEvent(retrieval("1.0.0"))).toBeNull();
  });

  it("keep the count, method and source, and only controlled values", () => {
    const kept = parseEvent(retrieval("1.1.0", {
      "retrieval.match_count": 0,
      "retrieval.method": "bm25",
      "retrieval.top_source": "library.opening-hours",
    }))?.attributes;
    expect(kept).toEqual({
      "retrieval.match_count": 0, "retrieval.method": "bm25", "retrieval.top_source": "library.opening-hours",
    });
    const bad = parseEvent(retrieval("1.1.0", {
      "retrieval.match_count": -1, "retrieval.method": "Best Match", "retrieval.top_source": "/Users/x/notes.md",
    }))?.attributes;
    expect(bad).toEqual({});
    expect(parseEvent(retrieval("1.1.0", { "retrieval.match_count": 1.5 }))?.attributes).toEqual({});
  });
});

describe("parseServerMessage", () => {
  it("sanitizes a hello: unknown keys and unsafe values never survive", () => {
    const message = parseServerMessage({
      kind: "hello",
      mode: "live",
      configuration: {
        snapshot_id: "config_abc",
        source_revision: "/Users/canary/repo",
        provider_ids: { stt: "mlx", llm: "ollama" },
        model_ids: { llm: "llama3.2:1b", tts: "C:\\Users\\canary\\voice.onnx", stt: "org/model" },
        endpointing_settings: { vad_stop_secs: 0.4, use_smart_turn: false, bad: "two words" },
        audio_device: "CANARY Microphone",
        hostname: "canary.local",
        privacy_mode: "everything",
      },
      capabilities: { stages: { stt: true, tools: false, tts: "yes" }, outcomes: { failed: true } },
      scenarios: ["normal-completed", "../etc/passwd"],
      running: true,
    });
    expect(message?.kind).toBe("hello");
    const text = JSON.stringify(message);
    for (const canary of ["CANARY", "canary", "Users", "audio_device", "hostname", "two words"]) {
      expect(text).not.toContain(canary);
    }
    if (message?.kind !== "hello") throw new Error("unreachable");
    expect(message.configuration.source_revision).toBeNull();
    expect(message.configuration.model_ids).toEqual({ llm: "llama3.2:1b", stt: "org/model" });
    expect(message.configuration.privacy_mode).toBe("metadata-only");
    expect(message.scenarios).toEqual(["normal-completed"]);
  });

  it("treats every stage and outcome the server did not affirm as not instrumented", () => {
    const message = parseServerMessage({
      kind: "hello",
      mode: "live",
      configuration: {},
      capabilities: { stages: { stt: true, tts: "yes" } },
    });
    if (message?.kind !== "hello") throw new Error("unreachable");
    expect(message.capabilities.stages).toEqual({
      user_speech: false, endpointing: false, stt: true, retrieval: false, llm: false, tools: false,
      tts: false, output_transport: false,
    });
    expect(Object.values(message.capabilities.outcomes)).toEqual([false, false, false]);
  });

  it("rejects an unknown mode, an unknown kind, and a telemetry message with a bad event", () => {
    expect(parseServerMessage({ kind: "hello", mode: "cloud" })).toBeNull();
    expect(parseServerMessage({ kind: "span_start", span: {} })).toBeNull();
    expect(parseServerMessage({ kind: "telemetry", event: { event_name: "nope" } })).toBeNull();
    expect(parseServerMessage({ kind: "replay_reset", scenario: "a b" })).toBeNull();
  });

  it("accepts the two microphone messages and nothing extra on them", () => {
    expect(parseServerMessage({ kind: "input_silent", device: "CANARY Mic" })).toEqual({ kind: "input_silent" });
    expect(parseServerMessage({ kind: "input_ok" })).toEqual({ kind: "input_ok" });
  });

  it("reduces an error message to a controlled code", () => {
    expect(parseServerMessage({ kind: "error", classification: "transport_error" }))
      .toEqual({ kind: "error", classification: "transport_error" });
    expect(parseServerMessage({ kind: "error", classification: "Traceback (most recent call)" }))
      .toEqual({ kind: "error", classification: "pipeline_error" });
  });
});

describe("real live timestamps (epoch nanoseconds)", () => {
  it("are beyond the safe-integer range, which is why this matters", () => {
    expect(Number.isSafeInteger(1_789_855_104_000_000_000)).toBe(false);
  });

  it("are accepted, as they arrive in JSON text", () => {
    const text = JSON.stringify(valid()).replace(/"timestamp_ns":\d+/, '"timestamp_ns":1789855104269000000');
    const event = parseEvent(JSON.parse(text));
    expect(event).not.toBeNull();
    expect(event?.timestamp_ns).toBe(1789855104269000000);
  });

  it.each([1.5, -1, Number.NaN, Number.POSITIVE_INFINITY, "1789855104269000000"])("still rejects %j", (bad) => {
    const event = valid();
    event.timestamp_ns = bad;
    expect(parseEvent(event)).toBeNull();
  });
});

describe("tuning contract", () => {
  const good = () => structuredClone(rawTuningFields());
  const parse = (fieldsValue: unknown) => {
    const message = parseServerMessage({ kind: "hello", mode: "live", configuration: {}, capabilities: {}, scenarios: [], tuning: { fields: fieldsValue } });
    return message?.kind === "hello" ? message.tuning : undefined;
  };

  it("is read from a live hello, with the server's limits", () => {
    const tuning = parse(good())!;
    expect(tuning.fields.map((f) => f.key)).toEqual(["vad_stop_secs", "user_speech_timeout", "use_smart_turn", "llm_max_tokens"]);
    expect(tuning.fields[0]).toMatchObject({ kind: "number", minimum: 0.1, maximum: 2, step: 0.05, unit: "s", warnBelow: 0.3, launch: 0.4, value: 0.4 });
  });

  it("is never read from a fixture hello", () => {
    const message = parseServerMessage({ kind: "hello", mode: "fixture", configuration: {}, tuning: { fields: good() } });
    expect(message?.kind === "hello" && message.tuning).toBeNull();
  });

  it("drops fields that are malformed, and keeps the good ones", () => {
    const fields = good() as any[];
    fields.push({ key: "bad kind", label: "x", kind: "number", about: "x" });
    fields.push({ key: "no_limits", label: "x", kind: "number", about: "x", launch: 1, value: 1 });
    fields.push({ key: "bad_range", label: "x", kind: "number", about: "x", launch: 1, value: 1, minimum: 5, maximum: 1, step: 1 });
    fields.push({ key: "nan_value", label: "x", kind: "number", about: "x", launch: 1, value: null, minimum: 0, maximum: 2, step: 1 });
    fields.push({ key: "bool_mismatch", label: "x", kind: "boolean", about: "x", launch: 1, value: 0 });
    expect(parse(fields)!.fields).toHaveLength(4);
  });

  it("refuses prose containing markup or control characters", () => {
    const fields = good() as any[];
    fields[0].about = "<img src=x onerror=alert(1)>";
    fields[1].label = "Speech" + String.fromCharCode(7) + "timeout";
    fields[2].about = "x".repeat(400);
    expect(parse(fields)!.fields.map((f) => f.key)).toEqual(["llm_max_tokens"]);
  });

  it("is null when there is nothing usable", () => {
    expect(parse([])).toBeNull();
    expect(parse("not a list")).toBeNull();
    expect(parse(undefined)).toBeNull();
  });

  it("a configuration message carries the new snapshot and values", () => {
    const message = parseServerMessage({ kind: "configuration", configuration: { snapshot_id: "config_new" }, tuning: { fields: good() } });
    expect(message?.kind).toBe("configuration");
    if (message?.kind === "configuration") {
      expect(message.configuration.snapshot_id).toBe("config_new");
      expect(message.tuning?.fields).toHaveLength(4);
    }
  });
});

describe("hello.agent", () => {
  const hello = (agent: unknown, mode = "live") => {
    const message = parseServerMessage({ kind: "hello", mode, configuration: {}, capabilities: {}, scenarios: [], agent });
    return message?.kind === "hello" ? message.agent : undefined;
  };

  it("is the id and title of the live agent", () => {
    expect(hello({ id: "library", title: "Riverside Public Library information line" }))
      .toEqual({ id: "library", title: "Riverside Public Library information line" });
  });

  it("is never taken from a fixture hello", () => {
    expect(hello({ id: "library", title: "Library" }, "fixture")).toBeNull();
  });

  it("is null when absent, and when the id or title is not plain text", () => {
    expect(hello(undefined)).toBeNull();
    expect(hello(null)).toBeNull();
    expect(hello({ id: "../etc", title: "x" })).toBeNull();
    expect(hello({ id: "library", title: "<img src=x onerror=alert(1)>" })).toBeNull();
    expect(hello({ id: "library", title: "x".repeat(200) })).toBeNull();
    expect(hello({ id: "library" })).toBeNull();
  });
});
